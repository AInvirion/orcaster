package cmd

// Golden-file tests for the output of the `task` commands. Each case starts an
// httptest.Server that answers the JSON /api/v1 would emit, runs the real client
// call (exercising HTTP/JSON parsing) and compares the rendered output against
// a file in testdata/*.golden.
//
// Python->Go glossary: golden files are like pytest snapshots. `go test -update`
// regenerates the .golden files (like --snapshot-update); without the flag, the
// run compares byte by byte. httptest.Server ~ starting a test server with
// fixed responses.
//
// Fixture timestamps are in UTC ("Z") on purpose: the api.Time type parses them
// as UTC and Format() keeps that zone, so the golden is deterministic on any
// machine (it does not depend on the runner's local TZ).

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"flag"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
)

// update regenerates the .golden files when `go test -update` is passed.
var update = flag.Bool("update", false, "update golden files")

// mockClient starts an httptest.Server with the given handler and returns a
// client pointed at it. The server is closed when the test ends (t.Cleanup).
func mockClient(t *testing.T, handler http.HandlerFunc) *api.Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return api.New(srv.URL, "orcaster_pat_test")
}

// jsonResponder answers the given JSON and checks that the request hit the expected path.
func jsonResponder(t *testing.T, wantPath, body string) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != wantPath {
			t.Errorf("request path = %q, want %q", r.URL.Path, wantPath)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
}

// gzipResponder answers a gzip-compressed body with Content-Encoding: gzip and
// checks the path. Go's Transport transparently decompresses on the client
// (as long as Accept-Encoding was NOT set by hand), so TaskOutput receives the
// body already in plain text.
func gzipResponder(t *testing.T, wantPath, contentType, body string) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != wantPath {
			t.Errorf("request path = %q, want %q", r.URL.Path, wantPath)
		}
		w.Header().Set("Content-Type", contentType)
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, body)
		_ = gz.Close()
	}
}

func TestGoldenTaskOutputText(t *testing.T) {
	client := mockClient(t, gzipResponder(t,
		"/api/v1/tasks/195/output", "text/plain; charset=utf-8", "one\ntwo\nthree\n"))
	rc, err := client.TaskOutput(context.Background(), 195, nil, "text")
	if err != nil {
		t.Fatalf("TaskOutput: %v", err)
	}
	defer rc.Close() //nolint:errcheck // best-effort close of the stream in the test
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, rc); err != nil {
		t.Fatalf("copy: %v", err)
	}
	assertGolden(t, "task_output_text", buf.Bytes())
}

func TestGoldenTaskOutputJSONL(t *testing.T) {
	const body = `{"seq":1,"ts":"2026-06-11T10:00:00Z","stream":"stdout","text":"one\n"}
{"seq":2,"ts":"2026-06-11T10:00:01Z","stream":"stderr","text":"two\n"}
`
	client := mockClient(t, gzipResponder(t,
		"/api/v1/tasks/195/output", "application/x-ndjson", body))
	rc, err := client.TaskOutput(context.Background(), 195, nil, "jsonl")
	if err != nil {
		t.Fatalf("TaskOutput: %v", err)
	}
	defer rc.Close() //nolint:errcheck // best-effort close of the stream in the test
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, rc); err != nil {
		t.Fatalf("copy: %v", err)
	}
	assertGolden(t, "task_output_jsonl", buf.Bytes())
}

func TestTaskOutputAttemptQuery(t *testing.T) {
	wantAttempt := 3
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/tasks/195/output" {
			t.Errorf("request path = %q, want %q", r.URL.Path, "/api/v1/tasks/195/output")
		}
		if got := r.URL.Query().Get("attempt"); got != "3" {
			t.Errorf("attempt query = %q, want \"3\"", got)
		}
		if got := r.URL.Query().Get("format"); got != "text" {
			t.Errorf("format query = %q, want \"text\"", got)
		}
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, "ok\n")
		_ = gz.Close()
	}))
	t.Cleanup(srv.Close)
	client := api.New(srv.URL, "orcaster_pat_test")
	rc, err := client.TaskOutput(context.Background(), 195, &wantAttempt, "text")
	if err != nil {
		t.Fatalf("TaskOutput: %v", err)
	}
	_ = rc.Close()
}

func TestTaskOutput404(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"not_found","message":"task not found"}`)
	}))
	t.Cleanup(srv.Close)
	client := api.New(srv.URL, "orcaster_pat_test")
	_, err := client.TaskOutput(context.Background(), 195, nil, "text")
	if err == nil {
		t.Fatal("TaskOutput should return an error on 404")
	}
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 404 {
		t.Errorf("error = %v, want *APIError with Status 404", err)
	}
}

// assertGolden compares got against testdata/<name>.golden (or rewrites it with -update).
func assertGolden(t *testing.T, name string, got []byte) {
	t.Helper()
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatalf("creating testdata dir: %v", err)
		}
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatalf("writing golden %s: %v", path, err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading golden %s: %v (run `go test -update` to create it)", path, err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("output mismatch for %s\n--- got ---\n%s\n--- want ---\n%s", name, got, want)
	}
}

func TestGoldenTaskList(t *testing.T) {
	const body = `{
		"tasks": [
			{"id": 196, "status": "queued", "target_mode": "count", "target_count": 2,
			 "done": 0, "total": 0, "priority": 9, "retry_count": 0,
			 "parent_task_id": 195, "created_at": "2026-06-02T16:40:00Z", "completed_at": null},
			{"id": 194, "status": "succeeded", "target_mode": "count", "target_count": 1,
			 "done": 1, "total": 1, "priority": 5, "retry_count": 0,
			 "parent_task_id": null, "created_at": "2026-06-02T16:20:00Z",
			 "completed_at": "2026-06-02T16:21:30Z"}
		],
		"next_cursor": 192
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks", body))
	resp, err := client.ListTasks(context.Background(), api.ListOptions{Limit: 50})
	if err != nil {
		t.Fatalf("ListTasks: %v", err)
	}
	var buf bytes.Buffer
	printTaskTable(&buf, resp)
	assertGolden(t, "task_list", buf.Bytes())
}

// The new list fields (variant_kind/variant_label/payload) go only through
// --json; the table gains no columns. The test pins this by comparing against
// the SAME golden as the list without those fields: if the table showed them,
// this case would fail.
func TestGoldenTaskListWithVariantAndPayload(t *testing.T) {
	const body = `{
		"tasks": [
			{"id": 196, "status": "queued", "target_mode": "count", "target_count": 2,
			 "done": 0, "total": 0, "priority": 9, "retry_count": 0,
			 "parent_task_id": 195, "created_at": "2026-06-02T16:40:00Z", "completed_at": null,
			 "variant_kind": "example-tool", "variant_label": "0.7.19",
			 "payload": {"repo_id": "org/repo", "cmd": "run"}},
			{"id": 194, "status": "succeeded", "target_mode": "count", "target_count": 1,
			 "done": 1, "total": 1, "priority": 5, "retry_count": 0,
			 "parent_task_id": null, "created_at": "2026-06-02T16:20:00Z",
			 "completed_at": "2026-06-02T16:21:30Z",
			 "variant_kind": null, "variant_label": null, "payload": {}}
		],
		"next_cursor": 192
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks", body))
	resp, err := client.ListTasks(context.Background(), api.ListOptions{
		Limit: 50, Variant: "all", IncludePayload: true,
	})
	if err != nil {
		t.Fatalf("ListTasks: %v", err)
	}
	var buf bytes.Buffer
	printTaskTable(&buf, resp)
	assertGolden(t, "task_list", buf.Bytes())
}

func TestGoldenTaskListEmpty(t *testing.T) {
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks", `{"tasks": [], "next_cursor": null}`))
	resp, err := client.ListTasks(context.Background(), api.ListOptions{Limit: 50})
	if err != nil {
		t.Fatalf("ListTasks: %v", err)
	}
	var buf bytes.Buffer
	printTaskTable(&buf, resp)
	assertGolden(t, "task_list_empty", buf.Bytes())
}

func TestGoldenTaskChildren(t *testing.T) {
	const body = `{
		"tasks": [
			{"id": 201, "status": "queued", "target_mode": "count", "target_count": 1,
			 "done": 0, "total": 0, "priority": 9, "retry_count": 0,
			 "parent_task_id": 195, "created_at": "2026-06-02T16:42:00Z", "completed_at": null},
			{"id": 198, "status": "succeeded", "target_mode": "count", "target_count": 1,
			 "done": 1, "total": 1, "priority": 0, "retry_count": 0,
			 "parent_task_id": 195, "created_at": "2026-06-02T16:30:00Z",
			 "completed_at": "2026-06-02T16:31:00Z"}
		],
		"next_cursor": 198
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195/children", body))
	resp, err := client.ListTaskChildren(context.Background(), 195, 50, nil)
	if err != nil {
		t.Fatalf("ListTaskChildren: %v", err)
	}
	var buf bytes.Buffer
	printTaskChildrenTable(&buf, resp)
	assertGolden(t, "task_children", buf.Bytes())
}

func TestGoldenTaskChildrenEmpty(t *testing.T) {
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195/children", `{"tasks": [], "next_cursor": null}`))
	resp, err := client.ListTaskChildren(context.Background(), 195, 50, nil)
	if err != nil {
		t.Fatalf("ListTaskChildren: %v", err)
	}
	var buf bytes.Buffer
	printTaskChildrenTable(&buf, resp)
	assertGolden(t, "task_children_empty", buf.Bytes())
}

// The `--json` output of the list is the server response re-indented, NOT a
// re-serialization of api.TaskSummary. This case pins it with a body that has
// `outcome` (which the struct does declare) next to a key it does NOT declare:
// if the output went through the struct again, the unknown key would vanish and
// the consumer would have no way to notice.
func TestTaskListJSONPreservesServerShape(t *testing.T) {
	const body = `{"tasks":[{"id":196,"status":"completed","target_mode":"count","target_count":1,` +
		`"done":1,"total":1,"priority":0,"retry_count":0,"parent_task_id":null,` +
		`"created_at":"2026-06-02T16:40:00Z","completed_at":"2026-06-02T16:41:00Z",` +
		`"outcome":"no_result","variant_kind":"example-tool","variant_label":"0.7.19",` +
		`"payload":{"repo_id":"org/repo"},"future_field":42}],"next_cursor":null}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks", body))
	raw, err := client.ListTasksRaw(context.Background(), api.ListOptions{Limit: 50, IncludePayload: true})
	if err != nil {
		t.Fatalf("ListTasksRaw: %v", err)
	}
	var buf bytes.Buffer
	if err := printRawJSON(&buf, raw); err != nil {
		t.Fatalf("printRawJSON: %v", err)
	}
	for _, want := range []string{`"outcome": "no_result"`, `"future_field": 42`} {
		if !bytes.Contains(buf.Bytes(), []byte(want)) {
			t.Errorf("the --json output lost %s:\n%s", want, buf.String())
		}
	}
	assertGolden(t, "task_list_json", buf.Bytes())
}

// `GET /tasks/{id}/children` does NOT project the variant, so it omits those
// keys. Re-serializing the struct would return them as null and a fork with a
// variant would read as if it fell into the `other` bucket. Here the ABSENCE is
// verified.
func TestTaskChildrenJSONOmitsUnsetVariant(t *testing.T) {
	const body = `{"tasks":[{"id":201,"status":"completed","target_mode":"count","target_count":1,` +
		`"done":1,"total":1,"priority":0,"retry_count":0,"parent_task_id":195,` +
		`"created_at":"2026-06-02T16:42:00Z","completed_at":"2026-06-02T16:43:00Z",` +
		`"outcome":null}],"next_cursor":null}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195/children", body))
	raw, err := client.ListTaskChildrenRaw(context.Background(), 195, 50, nil)
	if err != nil {
		t.Fatalf("ListTaskChildrenRaw: %v", err)
	}
	var buf bytes.Buffer
	if err := printRawJSON(&buf, raw); err != nil {
		t.Fatalf("printRawJSON: %v", err)
	}
	for _, absent := range []string{"variant_kind", "variant_label", "payload"} {
		if bytes.Contains(buf.Bytes(), []byte(absent)) {
			t.Errorf("the --json output of children invented the key %q:\n%s", absent, buf.String())
		}
	}
	if !bytes.Contains(buf.Bytes(), []byte(`"outcome": null`)) {
		t.Errorf("the --json output of children lost outcome:\n%s", buf.String())
	}
	assertGolden(t, "task_children_json", buf.Bytes())
}

// The two variants (typed for the table, raw for --json) must build the SAME
// request: if they diverged, `--json` and the table would show different pages.
func TestListTasksRawSameQueryAsTyped(t *testing.T) {
	const body = `{"tasks":[],"next_cursor":null}`
	opts := api.ListOptions{
		Status: []string{"completed", "failed"}, Limit: 200, Variant: "example-tool~0.7.19", IncludePayload: true,
	}
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	client := api.New(srv.URL, "orcaster_pat_test")
	if _, err := client.ListTasks(context.Background(), opts); err != nil {
		t.Fatalf("ListTasks: %v", err)
	}
	if _, err := client.ListTasksRaw(context.Background(), opts); err != nil {
		t.Fatalf("ListTasksRaw: %v", err)
	}
	if len(seen) != 2 || seen[0] != seen[1] {
		t.Errorf("the two requests differ: %q", seen)
	}
}

// `task get --json` DOES re-serialize the struct (the detail has no opt-in
// fields), so the fix there is the Outcome field in TaskSummary. Without it, a
// `completed` with outcome `no_result` was indistinguishable from an ordinary
// close.
func TestTaskGetJSONCarriesOutcome(t *testing.T) {
	const body = `{
		"id": 195, "status": "completed", "target_mode": "count", "target_count": 1,
		"done": 1, "total": 1, "priority": 0, "retry_count": 0, "child_count": 0,
		"parent_task_id": null, "created_at": "2026-06-02T16:25:00Z",
		"completed_at": "2026-06-02T16:26:00Z", "outcome": "no_result",
		"variant_kind": "example-tool", "variant_label": "0.7.19",
		"payload": {"cmd": "run"}, "eligibility": {},
		"claim_timeout_seconds": null, "execution_timeout_seconds": null,
		"run_at": null, "auto_retry_policy": null, "last_error": null,
		"cancelled_reason": null,
		"executions": [
			{"worker_id": "w-1", "status": "completed", "attempt_index": 1,
			 "assigned_at": "2026-06-02T16:25:10Z", "started_at": "2026-06-02T16:25:11Z",
			 "finished_at": "2026-06-02T16:26:00Z", "exit_code": 0,
			 "outcome": "no_result"}
		]
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195", body))
	detail, err := client.GetTask(context.Background(), 195)
	if err != nil {
		t.Fatalf("GetTask: %v", err)
	}
	if detail.Outcome == nil || *detail.Outcome != "no_result" {
		t.Fatalf("task Outcome = %v, want \"no_result\"", detail.Outcome)
	}
	// The ATTEMPT outcome is another field, in another struct, and it was also lost.
	ex := detail.Executions[0]
	if ex.Outcome == nil || *ex.Outcome != "no_result" {
		t.Fatalf("execution Outcome = %v, want \"no_result\"", ex.Outcome)
	}
	var buf bytes.Buffer
	if err := printJSON(&buf, detail); err != nil {
		t.Fatalf("printJSON: %v", err)
	}
	// Two occurrences: the one for the task and the one for its only execution.
	if n := bytes.Count(buf.Bytes(), []byte(`"outcome": "no_result"`)); n != 2 {
		t.Errorf("`task get --json` emitted %d outcome(s), want 2:\n%s", n, buf.String())
	}
}

func TestGoldenTaskGet(t *testing.T) {
	const body = `{
		"id": 195, "status": "cancelled", "target_mode": "count", "target_count": 2,
		"done": 0, "total": 1, "priority": 0, "retry_count": 1, "child_count": 2,
		"parent_task_id": null, "created_at": "2026-06-02T16:25:00Z", "completed_at": null,
		"payload": {"cmd": "echo bye"}, "eligibility": {},
		"claim_timeout_seconds": 60, "execution_timeout_seconds": 300,
		"run_at": null, "auto_retry_policy": null,
		"last_error": null, "cancelled_reason": "superseded by fork",
		"executions": [
			{"worker_id": "w-abc", "status": "failed", "attempt_index": 0,
			 "assigned_at": "2026-06-02T16:25:10Z", "started_at": "2026-06-02T16:25:11Z",
			 "finished_at": "2026-06-02T16:25:40Z", "exit_code": 1}
		]
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195", body))
	detail, err := client.GetTask(context.Background(), 195)
	if err != nil {
		t.Fatalf("GetTask: %v", err)
	}
	var buf bytes.Buffer
	printTaskDetail(&buf, detail)
	assertGolden(t, "task_get", buf.Bytes())
}

func TestGoldenTaskGetForkedChild(t *testing.T) {
	// Key case: a task that is itself a fork (parent_task_id != null) and in turn
	// has children (child_count > 0). It pins the combined output of the
	// Children: and Forked from: lines, and their alignment with the other labels.
	const body = `{
		"id": 201, "status": "succeeded", "target_mode": "count", "target_count": 1,
		"done": 1, "total": 1, "priority": 9, "retry_count": 0, "child_count": 3,
		"parent_task_id": 195, "created_at": "2026-06-02T16:42:00Z",
		"completed_at": "2026-06-02T16:43:30Z",
		"payload": {"cmd": "echo hi"}, "eligibility": {},
		"claim_timeout_seconds": null, "execution_timeout_seconds": null,
		"run_at": null, "auto_retry_policy": null,
		"last_error": null, "cancelled_reason": null, "executions": []
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/201", body))
	detail, err := client.GetTask(context.Background(), 201)
	if err != nil {
		t.Fatalf("GetTask: %v", err)
	}
	var buf bytes.Buffer
	printTaskDetail(&buf, detail)
	assertGolden(t, "task_get_forked_child", buf.Bytes())
}

func TestGoldenTaskCreate(t *testing.T) {
	const body = `{"task_id": 200, "status": "queued", "target": "1",
		"target_mode": "count", "target_count": 1, "parent_task_id": null}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks", body))
	resp, err := client.CreateTask(context.Background(), map[string]any{"target": "1", "priority": 0})
	if err != nil {
		t.Fatalf("CreateTask: %v", err)
	}
	var buf bytes.Buffer
	printCreateResponse(&buf, resp)
	assertGolden(t, "task_create", buf.Bytes())
}

func TestGoldenTaskFork(t *testing.T) {
	const body = `{"task_id": 201, "status": "queued", "target": "1",
		"target_mode": "count", "target_count": 1, "parent_task_id": 195}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195/fork", body))
	resp, err := client.ForkTask(context.Background(), 195, map[string]any{"priority": 9})
	if err != nil {
		t.Fatalf("ForkTask: %v", err)
	}
	var buf bytes.Buffer
	printCreateResponse(&buf, resp)
	assertGolden(t, "task_fork", buf.Bytes())
}

func TestGoldenTaskRetry(t *testing.T) {
	const body = `{"task_id": 195, "retry_count": 2, "status": "dispatching",
		"attempt_number": 2, "target_worker_id": "w-abc", "delivery_mode": "push"}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/195/retry", body))
	resp, err := client.RetryTask(context.Background(), 195, true)
	if err != nil {
		t.Fatalf("RetryTask: %v", err)
	}
	var buf bytes.Buffer
	printRetryResponse(&buf, resp)
	assertGolden(t, "task_retry", buf.Bytes())
}

func TestGoldenInsightsFailures(t *testing.T) {
	const body = `{
		"generated_at": "2026-06-26T12:00:00Z",
		"filters": {"since": "2026-06-01T00:00:00Z", "until": null, "limit": 50},
		"total_failed_tasks": 9,
		"by_exit_code": [
			{"exit_code": 1, "count": 6},
			{"exit_code": 137, "count": 2},
			{"exit_code": null, "count": 1}
		],
		"by_worker": [
			{"worker_id": "worker-1", "count": 5},
			{"worker_id": "worker-2", "count": 3}
		],
		"by_signature": [
			{"signature": "modulenotfounderror: no module named '<path>'", "count": 6,
			 "exit_codes": [1],
			 "example": {"task_id": 42, "execution_id": "3f2a0000-0000-0000-0000-0000000000e9",
			   "exit_code": 1, "worker_id": "worker-1", "command": "run",
			   "command_line": "python -m job --input x", "payload": {"repo": "r"},
			   "started_at": "2026-06-01T10:00:00Z", "finished_at": "2026-06-01T10:01:00Z",
			   "duration_seconds": 60.0, "stderr_tail": "Traceback...\nModuleNotFoundError: no module named 'foo'",
			   "stdout_tail": "starting"},
			 "sample_task_ids": [42, 43, 44, 45, 46, 47]},
			{"signature": "no-stderr:exit=null", "count": 1,
			 "exit_codes": [null],
			 "example": {"task_id": 70, "execution_id": null,
			   "exit_code": null, "worker_id": null, "command": null,
			   "command_line": null, "payload": {},
			   "started_at": null, "finished_at": null,
			   "duration_seconds": null, "stderr_tail": "", "stdout_tail": ""},
			 "sample_task_ids": [70]}
		]
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/insights/failures", body))
	resp, err := client.FailuresInsights(context.Background(), api.FailuresOptions{Since: "2026-06-01", Limit: 50})
	if err != nil {
		t.Fatalf("FailuresInsights: %v", err)
	}
	var buf bytes.Buffer
	printFailures(&buf, resp)
	assertGolden(t, "insights_failures", buf.Bytes())
}

func TestGoldenInsightsFailuresDefaultWindow(t *testing.T) {
	// Without --since/--until, the server bounds both edges by default
	// (since = UTC midnight of the current day, until = now) and returns the full
	// effective window in filters.since/filters.until; orcaster must show that
	// full window (labelled UTC), not "until -".
	const body = `{
		"generated_at": "2026-07-23T09:30:00Z",
		"filters": {"since": "2026-07-23T00:00:00Z", "until": "2026-07-23T09:30:00Z", "limit": 100},
		"total_failed_tasks": 1,
		"by_exit_code": [{"exit_code": 1, "count": 1}],
		"by_worker": [{"worker_id": "worker-1", "count": 1}],
		"by_signature": [
			{"signature": "boom", "count": 1, "exit_codes": [1],
			 "example": {"task_id": 7, "execution_id": null, "exit_code": 1,
			   "worker_id": "worker-1", "command": null, "command_line": null,
			   "payload": {}, "started_at": null, "finished_at": null,
			   "duration_seconds": null, "stderr_tail": "boom", "stdout_tail": ""},
			 "sample_task_ids": [7]}
		]
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/insights/failures", body))
	// The caller does NOT pass Since: the window comes from the server.
	resp, err := client.FailuresInsights(context.Background(), api.FailuresOptions{Limit: 100})
	if err != nil {
		t.Fatalf("FailuresInsights: %v", err)
	}
	var buf bytes.Buffer
	printFailures(&buf, resp)
	assertGolden(t, "insights_failures_default_window", buf.Bytes())
}

func TestGoldenInsightsFailuresEmpty(t *testing.T) {
	const body = `{
		"generated_at": "2026-06-26T12:00:00Z",
		"filters": {"since": null, "until": null, "limit": 100},
		"total_failed_tasks": 0,
		"by_exit_code": [], "by_worker": [], "by_signature": []
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/insights/failures", body))
	resp, err := client.FailuresInsights(context.Background(), api.FailuresOptions{Limit: 100})
	if err != nil {
		t.Fatalf("FailuresInsights: %v", err)
	}
	var buf bytes.Buffer
	printFailures(&buf, resp)
	assertGolden(t, "insights_failures_empty", buf.Bytes())
}
