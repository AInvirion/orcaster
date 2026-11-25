package api

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// httptest.NewServer starts a real HTTP server on an ephemeral port and gives us
// its URL. It is the Go equivalent of a pytest fixture that starts a test server:
// the client speaks real HTTP, with no mocks of the transport layer.

func TestWhoamiSendsBearerAndParses(t *testing.T) {
	var gotAuth, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"user": {"id": "u_1", "display_name": "Ada", "email": "ada@example.com"},
			"workspace": {"id": "ws_1", "slug": "acme", "name": "Acme"},
			"role": "owner",
			"token": {"label": "my-laptop", "last_used_at": "2026-06-02T10:23:00Z", "expires_at": "2026-09-01T00:00:00Z"}
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.Whoami(context.Background())
	if err != nil {
		t.Fatalf("Whoami() error inesperado: %v", err)
	}
	if gotAuth != "Bearer orcaster_pat_abc" {
		t.Errorf("Authorization = %q, want %q", gotAuth, "Bearer orcaster_pat_abc")
	}
	if gotPath != "/api/v1/auth/whoami" {
		t.Errorf("path = %q, want /api/v1/auth/whoami", gotPath)
	}
	if got.User.DisplayName != "Ada" || got.Workspace.Slug != "acme" || got.Role != "owner" {
		t.Errorf("respuesta mal parseada: %+v", got)
	}
	if got.Token.LastUsedAt == nil || got.Token.LastUsedAt.IsZero() {
		t.Errorf("last_used_at not parsed: %+v", got.Token)
	}
	if got.Token.ExpiresAt == nil || got.Token.ExpiresAt.IsZero() {
		t.Fatalf("expires_at not parsed: %+v", got.Token)
	}
	if y, m, d := got.Token.ExpiresAt.Date(); y != 2026 || m != 9 || d != 1 {
		t.Errorf("expires_at parsed wrong: %v, want 2026-09-01", got.Token.ExpiresAt)
	}
}

func TestListTasksSendsQueryAndParses(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"tasks": [
				{"id": 42, "status": "completed", "target_mode": "count", "target_count": 1,
				 "done": 1, "total": 1, "priority": 0, "retry_count": 0, "parent_task_id": null,
				 "created_at": "2026-06-02T10:00:00Z", "completed_at": "2026-06-02T10:01:00Z"}
			],
			"next_cursor": 42
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	cursor := 100
	got, err := c.ListTasks(context.Background(), ListOptions{
		Status: []string{"queued", "running"},
		Limit:  50,
		Cursor: &cursor,
	})
	if err != nil {
		t.Fatalf("ListTasks() error inesperado: %v", err)
	}
	// All three filters must travel in the query string.
	for _, want := range []string{"status=queued", "status=running", "limit=50", "cursor=100"} {
		if !contains(gotQuery, want) {
			t.Errorf("query %q does not contain %q", gotQuery, want)
		}
	}
	if len(got.Tasks) != 1 || got.Tasks[0].ID != 42 || got.Tasks[0].Done != 1 {
		t.Errorf("tasks mal parseadas: %+v", got.Tasks)
	}
	if got.NextCursor == nil || *got.NextCursor != 42 {
		t.Errorf("next_cursor = %v, want 42", got.NextCursor)
	}
}

func TestListTasksSendsVariantAndIncludePayload(t *testing.T) {
	var gotVariant, gotIncludePayload string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// We read via Query().Get() and not by substring of RawQuery: the "~" in the
		// kind~label pair travels escaped and a textual comparison would lie.
		gotVariant = r.URL.Query().Get("variant")
		gotIncludePayload = r.URL.Query().Get("include_payload")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"tasks": [
				{"id": 42, "status": "completed", "target_mode": "count", "target_count": 1,
				 "done": 1, "total": 1, "priority": 0, "retry_count": 0, "parent_task_id": null,
				 "created_at": "2026-06-02T10:00:00Z", "completed_at": "2026-06-02T10:01:00Z",
				 "variant_kind": "example-tool", "variant_label": "0.7.19",
				 "payload": {"repo_id": "org/model"}}
			],
			"next_cursor": null
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.ListTasks(context.Background(), ListOptions{
		Limit:          50,
		Variant:        "example-tool~0.7.19",
		IncludePayload: true,
	})
	if err != nil {
		t.Fatalf("ListTasks() error inesperado: %v", err)
	}
	if gotVariant != "example-tool~0.7.19" {
		t.Errorf("variant = %q, want %q", gotVariant, "example-tool~0.7.19")
	}
	if gotIncludePayload != "true" {
		t.Errorf("include_payload = %q, want \"true\"", gotIncludePayload)
	}
	if len(got.Tasks) != 1 {
		t.Fatalf("tasks = %+v, want 1 row", got.Tasks)
	}
	row := got.Tasks[0]
	if row.VariantKind == nil || *row.VariantKind != "example-tool" {
		t.Errorf("variant_kind = %v, want \"example-tool\"", row.VariantKind)
	}
	if row.VariantLabel == nil || *row.VariantLabel != "0.7.19" {
		t.Errorf("variant_label = %v, want \"0.7.19\"", row.VariantLabel)
	}
	if row.Payload == nil || (*row.Payload)["repo_id"] != "org/model" {
		t.Errorf("payload mal parseado: %v", row.Payload)
	}
}

// An EMPTY payload must arrive as `{}` and not as absent: the server sends
// `"payload": {}` for a task without payload, and with a bare map the field's
// `omitempty` dropped it on re-serialization, making it indistinguishable from
// "I did not ask for the payload". The list's `--json` output no longer
// re-serializes this struct (it prints the server bytes, see ListTasksRaw), but
// the invariant still matters: any consumer of the struct must be able to tell
// empty from absent.
func TestListTasksEmptyPayloadRoundTripsAsObject(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"tasks": [
				{"id": 42, "status": "completed", "target_mode": "count", "target_count": 1,
				 "done": 1, "total": 1, "priority": 0, "retry_count": 0, "parent_task_id": null,
				 "created_at": "2026-06-02T10:00:00Z", "completed_at": null,
				 "variant_kind": null, "variant_label": null, "payload": {}}
			],
			"next_cursor": null
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.ListTasks(context.Background(), ListOptions{Limit: 50, IncludePayload: true})
	if err != nil {
		t.Fatalf("ListTasks() error inesperado: %v", err)
	}
	if got.Tasks[0].Payload == nil {
		t.Fatalf("empty payload arrived as absent, want a non-nil map")
	}
	out, err := json.Marshal(got.Tasks[0])
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if !contains(string(out), `"payload":{}`) {
		t.Errorf("serialization = %s, want it to keep \"payload\":{}", out)
	}
}

func TestListTasksOmitsVariantAndIncludePayloadWhenUnset(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		// Response from a server that does not send the new fields yet: the
		// rows must still parse, with the pointers nil.
		_, _ = w.Write([]byte(`{
			"tasks": [
				{"id": 42, "status": "completed", "target_mode": "count", "target_count": 1,
				 "done": 1, "total": 1, "priority": 0, "retry_count": 0, "parent_task_id": null,
				 "created_at": "2026-06-02T10:00:00Z", "completed_at": "2026-06-02T10:01:00Z"}
			],
			"next_cursor": null
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.ListTasks(context.Background(), ListOptions{Limit: 50})
	if err != nil {
		t.Fatalf("ListTasks() error inesperado: %v", err)
	}
	// Absent != empty: without the flags no trace of the parameters is sent,
	// so the server applies its own defaults ("all" / false).
	if contains(gotQuery, "variant") {
		t.Errorf("query %q should not mention variant without the flag", gotQuery)
	}
	if contains(gotQuery, "include_payload") {
		t.Errorf("query %q should not mention include_payload without the flag", gotQuery)
	}
	if len(got.Tasks) != 1 {
		t.Fatalf("tasks = %+v, want 1 row", got.Tasks)
	}
	row := got.Tasks[0]
	if row.VariantKind != nil || row.VariantLabel != nil || row.Payload != nil {
		t.Errorf("new fields should be nil: %+v", row)
	}
}

func TestListTaskChildrenSendsQueryAndParses(t *testing.T) {
	var gotPath, gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"tasks": [
				{"id": 50, "status": "queued", "target_mode": "count", "target_count": 1,
				 "done": 0, "total": 0, "priority": 0, "retry_count": 0, "parent_task_id": 7,
				 "created_at": "2026-06-02T11:00:00Z", "completed_at": null}
			],
			"next_cursor": 50
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	cursor := 100
	got, err := c.ListTaskChildren(context.Background(), 7, 25, &cursor)
	if err != nil {
		t.Fatalf("ListTaskChildren() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/tasks/7/children" {
		t.Errorf("path = %q, want /api/v1/tasks/7/children", gotPath)
	}
	for _, want := range []string{"limit=25", "cursor=100"} {
		if !contains(gotQuery, want) {
			t.Errorf("query %q does not contain %q", gotQuery, want)
		}
	}
	if len(got.Tasks) != 1 || got.Tasks[0].ID != 50 || got.Tasks[0].ParentTaskID == nil || *got.Tasks[0].ParentTaskID != 7 {
		t.Errorf("children mal parseados: %+v", got.Tasks)
	}
	if got.NextCursor == nil || *got.NextCursor != 50 {
		t.Errorf("next_cursor = %v, want 50", got.NextCursor)
	}
}

func TestListTaskChildrenOmitsCursorWhenNil(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"tasks": [], "next_cursor": null}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	if _, err := c.ListTaskChildren(context.Background(), 7, 50, nil); err != nil {
		t.Fatalf("ListTaskChildren() error inesperado: %v", err)
	}
	// Without a cursor (first page) the query must not carry it; limit it does.
	if contains(gotQuery, "cursor=") {
		t.Errorf("query %q should not carry a cursor when it is nil", gotQuery)
	}
	if !contains(gotQuery, "limit=50") {
		t.Errorf("query %q does not contain limit=50", gotQuery)
	}
}

func TestErrorBodyBecomesAPIError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error": "unauthorized", "message": "invalid or expired token"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_bad")
	_, err := c.Whoami(context.Background())
	if err == nil {
		t.Fatal("expected an error on 401, got none")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error is not *APIError: %T (%v)", err, err)
	}
	if apiErr.Status != http.StatusUnauthorized {
		t.Errorf("Status = %d, want 401", apiErr.Status)
	}
	if apiErr.Code != "unauthorized" {
		t.Errorf("Code = %q, want unauthorized", apiErr.Code)
	}
}

func TestValidationDetailBecomesAPIError(t *testing.T) {
	// FastAPI 422: detail is an ARRAY of {loc,msg,type}, with loc mixing
	// strings and ints. It used to be mapped to a map and the unmarshal failed: the
	// detail was lost.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{
			"error": "validation_error",
			"message": "request validation failed",
			"detail": [
				{"loc": ["query", "status"], "msg": "input should be 'queued'", "type": "enum"},
				{"loc": ["body", "items", 0, "name"], "msg": "field required", "type": "missing"}
			]
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	_, err := c.Whoami(context.Background())
	if err == nil {
		t.Fatal("expected an error on 422, got none")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error is not *APIError: %T (%v)", err, err)
	}
	if len(apiErr.Detail) != 2 {
		t.Fatalf("Detail = %+v, want 2 entries", apiErr.Detail)
	}
	if apiErr.Detail[0].LocString() != "query.status" {
		t.Errorf("LocString = %q, want query.status", apiErr.Detail[0].LocString())
	}
	if apiErr.Detail[1].LocString() != "body.items.0.name" {
		t.Errorf("LocString = %q, want body.items.0.name", apiErr.Detail[1].LocString())
	}
	// Error() must render the main message + one line per entry.
	want := "request validation failed" +
		"\n  - query.status: input should be 'queued'" +
		"\n  - body.items.0.name: field required"
	if got := apiErr.Error(); got != want {
		t.Errorf("Error() =\n%q\nwant\n%q", got, want)
	}
}

func TestDetailAsStringDoesNotLoseError(t *testing.T) {
	// Some errors (400/403/404, or raw FastAPI) send `detail` as a STRING.
	// The unmarshal used to fail with a type mismatch (Detail is []ValidationDetail)
	// and the WHOLE error was lost, falling back to "HTTP 4xx". Now the non-array
	// detail is ignored but error/message are kept.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error": "bad_request", "message": "something went wrong", "detail": "field required"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	_, err := c.Whoami(context.Background())
	if err == nil {
		t.Fatal("expected an error on 400, got none")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error is not *APIError: %T (%v)", err, err)
	}
	if apiErr.Code != "bad_request" {
		t.Errorf("Code = %q, want bad_request (must not fall back)", apiErr.Code)
	}
	if apiErr.Message != "something went wrong" {
		t.Errorf("Message = %q, want 'something went wrong'", apiErr.Message)
	}
	if apiErr.Detail != nil {
		t.Errorf("Detail = %+v, want nil (string detail is ignored)", apiErr.Detail)
	}
	// Without Detail, Error() is a single line (no line breaks).
	if got := apiErr.Error(); got != "something went wrong" {
		t.Errorf("Error() = %q, want a single line 'something went wrong'", got)
	}
}

func TestDetailAsObjectDoesNotLoseError(t *testing.T) {
	// Same as the string case but `detail` is an OBJECT: it must not break the
	// unmarshal ni perder error/message; Detail queda nil.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"error": "forbidden", "message": "no permission", "detail": {"reason": "rbac"}}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	_, err := c.Whoami(context.Background())
	if err == nil {
		t.Fatal("expected an error on 403, got none")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error is not *APIError: %T (%v)", err, err)
	}
	if apiErr.Code != "forbidden" {
		t.Errorf("Code = %q, want forbidden (must not fall back)", apiErr.Code)
	}
	if apiErr.Message != "no permission" {
		t.Errorf("Message = %q, want 'no permission'", apiErr.Message)
	}
	if apiErr.Detail != nil {
		t.Errorf("Detail = %+v, want nil (object detail is ignored)", apiErr.Detail)
	}
	if got := apiErr.Error(); got != "no permission" {
		t.Errorf("Error() = %q, want a single line 'no permission'", got)
	}
}

func TestDetailAsArrayStillMultiline(t *testing.T) {
	// Reconfirms that the "good" case (422 array) still produces the multiline
	// output after making the unmarshal defensive.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{
			"error": "validation_error",
			"message": "request validation failed",
			"detail": [
				{"loc": ["body", "target"], "msg": "field required", "type": "missing"}
			]
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	_, err := c.Whoami(context.Background())
	if err == nil {
		t.Fatal("expected an error on 422, got none")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error is not *APIError: %T (%v)", err, err)
	}
	if len(apiErr.Detail) != 1 {
		t.Fatalf("Detail = %+v, want 1 entry", apiErr.Detail)
	}
	want := "request validation failed\n  - body.target: field required"
	if got := apiErr.Error(); got != want {
		t.Errorf("Error() =\n%q\nwant\n%q", got, want)
	}
}

func TestErrorWithoutDetailIsSingleLine(t *testing.T) {
	// Without detail (non-422 errors) the behavior is unchanged: a single line.
	e := &APIError{Status: http.StatusUnauthorized, Code: "unauthorized", Message: "invalid token"}
	if got := e.Error(); got != "invalid token" {
		t.Errorf("Error() = %q, want 'invalid token'", got)
	}
}

func TestGetTaskParsesDetail(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		// Carries summary fields (embedding) + payload + one execution.
		_, _ = w.Write([]byte(`{
			"id": 7, "status": "completed", "target_mode": "count", "target_count": 1,
			"done": 1, "total": 1, "priority": 5, "retry_count": 0, "parent_task_id": 3,
			"child_count": 4,
			"created_at": "2026-06-02T10:00:00Z", "completed_at": "2026-06-02T10:05:00Z",
			"payload": {"cmd": "echo hi"}, "eligibility": {}, "claim_timeout_seconds": 30,
			"execution_timeout_seconds": null, "run_at": null, "auto_retry_policy": null,
			"last_error": null, "cancelled_reason": null,
			"variant_kind": "example-tool", "variant_label": "0.7.19",
			"executions": [
				{"worker_id": "wk_1", "status": "completed", "attempt_index": 0,
				 "assigned_at": "2026-06-02T10:00:01Z", "started_at": "2026-06-02T10:00:02Z",
				 "finished_at": "2026-06-02T10:05:00Z", "exit_code": 0}
			]
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.GetTask(context.Background(), 7)
	if err != nil {
		t.Fatalf("GetTask() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/tasks/7" {
		t.Errorf("path = %q, want /api/v1/tasks/7", gotPath)
	}
	// Fields promoted from the embedded summary.
	if got.ID != 7 || got.Status != "completed" || got.Priority != 5 {
		t.Errorf("summary embebido mal parseado: %+v", got.TaskSummary)
	}
	if got.ParentTaskID == nil || *got.ParentTaskID != 3 {
		t.Errorf("parent_task_id = %v, want 3", got.ParentTaskID)
	}
	if got.ChildCount != 4 {
		t.Errorf("child_count = %d, want 4", got.ChildCount)
	}
	// Shadowing guard: TaskDetail.Payload (depth 0) wins over the Payload promoted
	// from TaskSummary (depth 1), so the detail keeps filling the usual field and
	// the embedded summary's one stays untouched.
	if got.Payload["cmd"] != "echo hi" {
		t.Errorf("payload mal parseado: %v", got.Payload)
	}
	if got.TaskSummary.Payload != nil {
		t.Errorf("embedded summary payload = %v, want nil", got.TaskSummary.Payload)
	}
	// The detail now also carries variant_kind/variant_label via the summary.
	if got.VariantKind == nil || *got.VariantKind != "example-tool" {
		t.Errorf("variant_kind = %v, want \"example-tool\"", got.VariantKind)
	}
	if got.VariantLabel == nil || *got.VariantLabel != "0.7.19" {
		t.Errorf("variant_label = %v, want \"0.7.19\"", got.VariantLabel)
	}
	if got.ClaimTimeoutSeconds == nil || *got.ClaimTimeoutSeconds != 30 {
		t.Errorf("claim_timeout = %v, want 30", got.ClaimTimeoutSeconds)
	}
	if len(got.Executions) != 1 || got.Executions[0].WorkerID != "wk_1" || got.Executions[0].ExitCode == nil {
		t.Errorf("executions mal parseadas: %+v", got.Executions)
	}
}

func TestCreateTaskSendsBodyAndParses(t *testing.T) {
	var gotMethod, gotPath, gotCT string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath, gotCT = r.Method, r.URL.Path, r.Header.Get("Content-Type")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{
			"task_id": 99, "status": "queued", "target": "1",
			"target_mode": "count", "target_count": 1, "parent_task_id": null
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	body := map[string]any{
		"target":   "1",
		"priority": 0,
		"payload":  json.RawMessage(`{"cmd":"echo hi"}`),
	}
	got, err := c.CreateTask(context.Background(), body)
	if err != nil {
		t.Fatalf("CreateTask() error inesperado: %v", err)
	}
	if gotMethod != http.MethodPost || gotPath != "/api/v1/tasks" {
		t.Errorf("method/path = %s %s, want POST /api/v1/tasks", gotMethod, gotPath)
	}
	if gotCT != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", gotCT)
	}
	// The payload (json.RawMessage) must travel as a nested JSON object, not a string.
	payload, ok := gotBody["payload"].(map[string]any)
	if !ok || payload["cmd"] != "echo hi" {
		t.Errorf("payload badly serialized in the body: %v", gotBody["payload"])
	}
	if got.TaskID != 99 || got.Status != "queued" {
		t.Errorf("respuesta mal parseada: %+v", got)
	}
}

func TestForkTaskSendsOnlyOverrides(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{
			"task_id": 100, "status": "queued", "target": "2",
			"target_mode": "count", "target_count": 2, "parent_task_id": 7
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	// Only priority is sent: the rest is inherited from the parent (server-side deep-merge).
	got, err := c.ForkTask(context.Background(), 7, map[string]any{"priority": 9})
	if err != nil {
		t.Fatalf("ForkTask() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/tasks/7/fork" {
		t.Errorf("path = %q, want /api/v1/tasks/7/fork", gotPath)
	}
	if _, sentTarget := gotBody["target"]; sentTarget {
		t.Errorf("fork should not send target unless overridden: %v", gotBody)
	}
	if gotBody["priority"] != float64(9) { // JSON deserializes numbers to float64
		t.Errorf("priority = %v, want 9", gotBody["priority"])
	}
	if got.ParentTaskID == nil || *got.ParentTaskID != 7 {
		t.Errorf("parent_task_id = %v, want 7", got.ParentTaskID)
	}
}

func TestCancelTaskSendsReason(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"task_id": 7, "status": "cancelled"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.CancelTask(context.Background(), 7, "no longer needed")
	if err != nil {
		t.Fatalf("CancelTask() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/tasks/7/cancel" {
		t.Errorf("path = %q, want /api/v1/tasks/7/cancel", gotPath)
	}
	if gotBody["reason"] != "no longer needed" {
		t.Errorf("reason = %v, want 'no longer needed'", gotBody["reason"])
	}
	if got.TaskID != 7 || got.Status != "cancelled" {
		t.Errorf("respuesta mal parseada: %+v", got)
	}
}

func TestRetryTaskSendsForceSameWorker(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"task_id": 7, "retry_count": 1, "status": "queued",
			"attempt_number": 2, "target_worker_id": null, "delivery_mode": "claim"
		}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.RetryTask(context.Background(), 7, false)
	if err != nil {
		t.Fatalf("RetryTask() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/tasks/7/retry" {
		t.Errorf("path = %q, want /api/v1/tasks/7/retry", gotPath)
	}
	if gotBody["force_same_worker"] != false {
		t.Errorf("force_same_worker = %v, want false", gotBody["force_same_worker"])
	}
	if got.TaskID != 7 || got.AttemptNumber != 2 || got.DeliveryMode != "claim" {
		t.Errorf("respuesta mal parseada: %+v", got)
	}
}

func TestDeleteTaskSendsDeleteAndTreats204AsSuccess(t *testing.T) {
	var gotMethod, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusNoContent) // 204 without body
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	if err := c.DeleteTask(context.Background(), 217); err != nil {
		t.Fatalf("DeleteTask() error inesperado: %v", err)
	}
	if gotMethod != http.MethodDelete {
		t.Errorf("method = %q, want DELETE", gotMethod)
	}
	if gotPath != "/api/v1/tasks/217" {
		t.Errorf("path = %q, want /api/v1/tasks/217", gotPath)
	}
}

func TestDeleteTaskPropagates409(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = io.WriteString(w, `{"error":"conflict","message":"task is not terminal"}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	err := c.DeleteTask(context.Background(), 217)
	if err == nil {
		t.Fatal("DeleteTask() should return an error on 409")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 409 {
		t.Errorf("error = %v, want *APIError with Status 409", err)
	}
}

func TestBulkDeleteTasksSendsIDsAndParses(t *testing.T) {
	var gotMethod, gotPath string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{
			"deleted": [217, 229],
			"skipped": [{"id": 230, "reason": "not terminal: queued"}],
			"not_found": [999]
		}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.BulkDeleteTasks(context.Background(), []int{217, 229, 230, 999})
	if err != nil {
		t.Fatalf("BulkDeleteTasks() error inesperado: %v", err)
	}
	if gotMethod != http.MethodPost {
		t.Errorf("method = %q, want POST", gotMethod)
	}
	if gotPath != "/api/v1/tasks/bulk/delete" {
		t.Errorf("path = %q, want /api/v1/tasks/bulk/delete", gotPath)
	}
	rawIDs, ok := gotBody["task_ids"].([]any)
	if !ok || len(rawIDs) != 4 {
		t.Fatalf("task_ids = %v, want 4 ids", gotBody["task_ids"])
	}
	if rawIDs[0] != float64(217) || rawIDs[3] != float64(999) {
		t.Errorf("task_ids mal serializados: %v", rawIDs)
	}
	if len(got.Deleted) != 2 || got.Deleted[0] != 217 || got.Deleted[1] != 229 {
		t.Errorf("deleted = %v, want [217 229]", got.Deleted)
	}
	if len(got.Skipped) != 1 || got.Skipped[0].ID != 230 || got.Skipped[0].Reason != "not terminal: queued" {
		t.Errorf("skipped mal parseado: %+v", got.Skipped)
	}
	if len(got.NotFound) != 1 || got.NotFound[0] != 999 {
		t.Errorf("not_found = %v, want [999]", got.NotFound)
	}
}

func TestFailuresInsightsSendsQueryAndParses(t *testing.T) {
	var gotPath, gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{
			"generated_at": "2026-06-26T12:00:00Z",
			"filters": {"since": "2026-06-01T00:00:00Z", "until": null, "limit": 50},
			"total_failed_tasks": 7,
			"by_exit_code": [{"exit_code": 1, "count": 5}, {"exit_code": null, "count": 2}],
			"by_worker": [{"worker_id": "wk_1", "count": 4}],
			"by_signature": [
				{"signature": "boom <n>", "count": 5, "exit_codes": [1, null],
				 "example": {"task_id": 42, "execution_id": "3f2a0000-0000-0000-0000-0000000000e9",
				   "exit_code": 1, "worker_id": "wk_1", "command": "run", "command_line": "run --x",
				   "payload": {"repo": "r"}, "started_at": "2026-06-01T10:00:00Z",
				   "finished_at": "2026-06-01T10:01:00Z", "duration_seconds": 60.0,
				   "stderr_tail": "boom 9", "stdout_tail": "ok"},
				 "sample_task_ids": [42, 43, 44]}
			]
		}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	got, err := c.FailuresInsights(context.Background(), FailuresOptions{Since: "2026-06-01", Limit: 50})
	if err != nil {
		t.Fatalf("FailuresInsights() error inesperado: %v", err)
	}
	if gotPath != "/api/v1/insights/failures" {
		t.Errorf("path = %q, want /api/v1/insights/failures", gotPath)
	}
	for _, want := range []string{"since=2026-06-01", "limit=50"} {
		if !contains(gotQuery, want) {
			t.Errorf("query %q does not contain %q", gotQuery, want)
		}
	}
	if contains(gotQuery, "until=") {
		t.Errorf("query %q should not carry until when empty", gotQuery)
	}
	if got.TotalFailedTasks != 7 {
		t.Errorf("total_failed_tasks = %d, want 7", got.TotalFailedTasks)
	}
	if got.Filters.Limit != 50 {
		t.Errorf("filters.limit = %d, want 50", got.Filters.Limit)
	}
	if len(got.ByExitCode) != 2 || got.ByExitCode[0].ExitCode == nil || *got.ByExitCode[0].ExitCode != 1 {
		t.Errorf("by_exit_code mal parseado: %+v", got.ByExitCode)
	}
	if got.ByExitCode[1].ExitCode != nil {
		t.Errorf("by_exit_code[1].exit_code should be nil (null), got %v", got.ByExitCode[1].ExitCode)
	}
	if len(got.ByWorker) != 1 || got.ByWorker[0].WorkerID != "wk_1" || got.ByWorker[0].Count != 4 {
		t.Errorf("by_worker mal parseado: %+v", got.ByWorker)
	}
	if len(got.BySignature) != 1 {
		t.Fatalf("by_signature = %+v, want 1 group", got.BySignature)
	}
	g := got.BySignature[0]
	if g.Signature != "boom <n>" || g.Count != 5 {
		t.Errorf("signature group parsed wrong: %+v", g)
	}
	if g.Example.TaskID != 42 || g.Example.ExecutionID == nil || *g.Example.ExecutionID != "3f2a0000-0000-0000-0000-0000000000e9" {
		t.Errorf("example mal parseado: %+v", g.Example)
	}
	if g.Example.Payload["repo"] != "r" {
		t.Errorf("example.payload mal parseado: %v", g.Example.Payload)
	}
	if len(g.SampleTaskIDs) != 3 || g.SampleTaskIDs[0] != 42 {
		t.Errorf("sample_task_ids mal parseado: %v", g.SampleTaskIDs)
	}
}

func TestFailuresInsightsOmitsEmptyFilters(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{
			"generated_at": "2026-06-26T12:00:00Z",
			"filters": {"since": null, "until": null, "limit": 100},
			"total_failed_tasks": 0,
			"by_exit_code": [], "by_worker": [], "by_signature": []
		}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	if _, err := c.FailuresInsights(context.Background(), FailuresOptions{Limit: 100}); err != nil {
		t.Fatalf("FailuresInsights() error inesperado: %v", err)
	}
	if contains(gotQuery, "since=") || contains(gotQuery, "until=") {
		t.Errorf("query %q should not carry empty since/until", gotQuery)
	}
	if !contains(gotQuery, "limit=100") {
		t.Errorf("query %q does not contain limit=100", gotQuery)
	}
}

func TestExecutionOutputSendsPathAndStreams(t *testing.T) {
	var gotPath, gotQuery, gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, "log line\n")
		_ = gz.Close()
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	rc, err := c.ExecutionOutput(context.Background(), "3f2a0000-0000-0000-0000-0000000000e9", "text")
	if err != nil {
		t.Fatalf("ExecutionOutput() error inesperado: %v", err)
	}
	defer rc.Close() //nolint:errcheck // best-effort stream close in test
	body, err := io.ReadAll(rc)
	if err != nil {
		t.Fatalf("leyendo stream: %v", err)
	}
	if gotPath != "/api/v1/executions/3f2a0000-0000-0000-0000-0000000000e9/output" {
		t.Errorf("path = %q, want /api/v1/executions/<uuid>/output", gotPath)
	}
	if !contains(gotQuery, "format=text") {
		t.Errorf("query %q does not contain format=text", gotQuery)
	}
	if gotAuth != "Bearer orcaster_pat_abc" {
		t.Errorf("Authorization = %q, want Bearer orcaster_pat_abc", gotAuth)
	}
	if string(body) != "log line\n" {
		t.Errorf("body = %q, want %q", body, "log line\n")
	}
}

func TestExecutionOutput404(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error":"not_found","message":"execution not found"}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "orcaster_pat_abc")
	_, err := c.ExecutionOutput(context.Background(), "3f2a0000-0000-0000-0000-0000000000e9", "jsonl")
	if err == nil {
		t.Fatal("ExecutionOutput() should return an error on 404")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 404 {
		t.Errorf("error = %v, want *APIError with Status 404", err)
	}
}

// contains: trivial helper to avoid importing strings just for the test.
func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
