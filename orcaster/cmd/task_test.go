package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/spf13/cobra"
)

func TestParseTaskID(t *testing.T) {
	if id, err := parseTaskID("42"); err != nil || id != 42 {
		t.Errorf(`parseTaskID("42") = %d, %v; want 42, nil`, id, err)
	}
	for _, bad := range []string{"0", "-3", "abc", ""} {
		if _, err := parseTaskID(bad); err == nil {
			t.Errorf("parseTaskID(%q) should fail", bad)
		}
	}
}

func TestReadJSONArg(t *testing.T) {
	// Valid literal JSON.
	if _, err := readJSONArg(`{"cmd":"echo"}`); err != nil {
		t.Errorf("valid literal JSON failed: %v", err)
	}
	// Invalid JSON is rejected.
	if _, err := readJSONArg(`{not json}`); err == nil {
		t.Error("invalid JSON should fail")
	}
	// @file reads and validates the file contents.
	path := filepath.Join(t.TempDir(), "p.json")
	if err := os.WriteFile(path, []byte(`{"a":1}`), 0o644); err != nil {
		t.Fatalf("writing fixture: %v", err)
	}
	raw, err := readJSONArg("@" + path)
	if err != nil || string(raw) != `{"a":1}` {
		t.Errorf("@file = %q, %v; want {\"a\":1}, nil", raw, err)
	}
}

// newParamsCmd builds an ephemeral command with the create/fork flags already parsed,
// to exercise buildTaskBody without bringing up the whole cobra tree.
func newParamsCmd(t *testing.T, args []string) (*cobra.Command, *taskParams) {
	t.Helper()
	var p taskParams
	c := &cobra.Command{Use: "x", RunE: func(*cobra.Command, []string) error { return nil }}
	registerTaskParamFlags(c, &p)
	c.SetArgs(args)
	if err := c.Execute(); err != nil {
		t.Fatalf("Execute with args %v: %v", args, err)
	}
	return c, &p
}

func TestBuildTaskBodyCreateAlwaysSendsDefaults(t *testing.T) {
	// buildTaskBody itself does not require a payload (fork shares it): it always
	// sends the target/priority defaults. The --payload requirement lives in
	// runTaskCreate (see TestRunTaskCreate*).
	c, p := newParamsCmd(t, nil) // no flags
	body, err := buildTaskBody(c, p, true)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	if body["target"] != "1" || body["priority"] != 0 {
		t.Errorf("create without flags should send target/priority defaults: %v", body)
	}
}

// newCreateCmd builds a command with the create flags already parsed, bound to
// runTaskCreate, to exercise the --payload guard without bringing up the whole tree.
func newCreateCmd(t *testing.T, args []string) *cobra.Command {
	t.Helper()
	// Local variable (same as newParamsCmd) so the global createParams is not
	// polluted between tests. The --payload guard and the config resolution
	// fail before reading these flags, so the isolation does not change the
	// behaviour exercised but does avoid state leaks between tests.
	var localParams taskParams
	c := &cobra.Command{Use: "create", RunE: runTaskCreate}
	registerTaskParamFlags(c, &localParams)
	c.SetArgs(args)
	c.SilenceUsage = true
	c.SilenceErrors = true
	return c
}

func TestRunTaskCreateRequiresPayload(t *testing.T) {
	// Without --payload, create must fail locally BEFORE hitting the server.
	c := newCreateCmd(t, nil)
	err := c.Execute()
	if err == nil {
		t.Fatal("create without --payload should fail")
	}
	if !strings.Contains(err.Error(), "--payload is required") {
		t.Errorf("error = %q, want it to mention '--payload is required'", err.Error())
	}
}

func TestRunTaskCreateEmptyPayloadIsExplicitOk(t *testing.T) {
	// An explicit --payload '{}' passes the guard: the error must come later (from
	// the config/profile resolution), not from the payload guard. We isolate the
	// config in an empty temp dir so it fails there and touches neither the
	// network nor the user's config.
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	c := newCreateCmd(t, []string{"--payload", "{}"})
	err := c.Execute()
	if err != nil && strings.Contains(err.Error(), "--payload is required") {
		t.Errorf("--payload '{}' should not trigger the payload guard: %v", err)
	}
}

func TestBuildTaskBodyForkSendsOnlyChanged(t *testing.T) {
	c, p := newParamsCmd(t, []string{"--priority", "9"})
	body, err := buildTaskBody(c, p, false)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	if _, hasTarget := body["target"]; hasTarget {
		t.Errorf("fork should not send target without override: %v", body)
	}
	if body["priority"] != 9 {
		t.Errorf("priority = %v, want 9", body["priority"])
	}
}

func TestBuildTaskBodyEmbedsJSONPayload(t *testing.T) {
	c, p := newParamsCmd(t, []string{"--payload", `{"cmd":"echo hi"}`})
	body, err := buildTaskBody(c, p, true)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	// The payload is stored as json.RawMessage; when re-serialized it must be an object.
	if _, ok := body["payload"]; !ok {
		t.Fatalf("payload missing from the body: %v", body)
	}
}

func TestBuildTaskBodyRejectsBadJSON(t *testing.T) {
	c, p := newParamsCmd(t, []string{"--payload", `{nope}`})
	if _, err := buildTaskBody(c, p, true); err == nil {
		t.Error("payload with invalid JSON should fail")
	}
}

func TestBuildTaskBodyValidatesIdempotencyKey(t *testing.T) {
	// A value that is not a UUID is rejected locally, without hitting the API.
	c, p := newParamsCmd(t, []string{"--idempotency-key", "i-am-not-a-uuid"})
	if _, err := buildTaskBody(c, p, true); err == nil {
		t.Error("--idempotency-key that is not a UUID should fail")
	}
	// A valid UUID passes and goes in the body.
	c, p = newParamsCmd(t, []string{"--idempotency-key", "550e8400-e29b-41d4-a716-446655440000"})
	body, err := buildTaskBody(c, p, true)
	if err != nil {
		t.Fatalf("valid UUID should not fail: %v", err)
	}
	if body["idempotency_key"] != "550e8400-e29b-41d4-a716-446655440000" {
		t.Errorf("idempotency_key = %v, want the UUID", body["idempotency_key"])
	}
}

func TestBuildTaskBodyMergesEnvAndEgress(t *testing.T) {
	c, p := newParamsCmd(t, []string{
		"--payload", `{"commands":["echo hi"]}`,
		"--env", "GITHUB_TOKEN",
		"--env", "API_TOKEN",
		"--egress-allow", "files.example.com",
		"--egress-allow", "github.com",
	})
	body, err := buildTaskBody(c, p, true)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	raw, ok := body["payload"].(json.RawMessage)
	if !ok {
		t.Fatalf("payload is not json.RawMessage: %T (%v)", body["payload"], body["payload"])
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	env, _ := payload["task_env"].([]any)
	if len(env) != 2 || env[0] != "GITHUB_TOKEN" {
		t.Fatalf("task_env not merged: %v", payload["task_env"])
	}
	eg, _ := payload["egress_allowlist"].([]any)
	if len(eg) != 2 || eg[1] != "github.com" {
		t.Fatalf("egress_allowlist not merged: %v", payload["egress_allowlist"])
	}
	// original commands preserved
	cmds, _ := payload["commands"].([]any)
	if len(cmds) != 1 {
		t.Fatalf("commands lost: %v", payload["commands"])
	}
}

func TestBuildTaskBodyEnvOnlyWithoutPayloadFlag(t *testing.T) {
	// fork can send --env without --payload: the merge must create the payload
	// object from scratch (tri-state semantics: the key being present enables
	// strict mode).
	c, p := newParamsCmd(t, []string{"--env", "GITHUB_TOKEN"})
	body, err := buildTaskBody(c, p, false)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	raw, ok := body["payload"].(json.RawMessage)
	if !ok {
		t.Fatalf("payload missing/wrong type: %T", body["payload"])
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	env, _ := payload["task_env"].([]any)
	if len(env) != 1 || env[0] != "GITHUB_TOKEN" {
		t.Fatalf("task_env not set: %v", payload["task_env"])
	}
}

func TestBuildTaskBodyNullPayloadWithEnv(t *testing.T) {
	// --payload null is valid JSON but not an object: with --env it must fail
	// with the same "payload must be a JSON object..." error, not panic.
	c, p := newParamsCmd(t, []string{
		"--payload", "null",
		"--env", "GITHUB_TOKEN",
	})
	_, err := buildTaskBody(c, p, true)
	if err == nil {
		t.Fatal("--payload null with --env should fail")
	}
	if !strings.Contains(err.Error(), "payload must be a JSON object to merge --env/--egress-allow") {
		t.Errorf("error = %q, want it to mention 'payload must be a JSON object to merge'", err.Error())
	}
}

func TestBuildTaskBodyNonObjectPayloadWithEnv(t *testing.T) {
	// --payload [1,2,3] is valid JSON but not an object: with --env it must fail
	// with the same "payload must be a JSON object..." error, not panic or decode error.
	c, p := newParamsCmd(t, []string{
		"--payload", "[1,2,3]",
		"--egress-allow", "github.com",
	})
	_, err := buildTaskBody(c, p, true)
	if err == nil {
		t.Fatal("--payload [1,2,3] with --egress-allow should fail")
	}
	if !strings.Contains(err.Error(), "payload must be a JSON object to merge --env/--egress-allow") {
		t.Errorf("error = %q, want it to mention 'payload must be a JSON object to merge'", err.Error())
	}
}

func TestBuildTaskBodyNoIsolationFlagsLeavesPayloadUntouched(t *testing.T) {
	// Without --env or --egress-allow the payload must not be touched (legacy: no new keys).
	c, p := newParamsCmd(t, []string{"--payload", `{"commands":["echo hi"]}`})
	body, err := buildTaskBody(c, p, true)
	if err != nil {
		t.Fatalf("buildTaskBody: %v", err)
	}
	raw, ok := body["payload"].(json.RawMessage)
	if !ok {
		t.Fatalf("payload missing/wrong type: %T", body["payload"])
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	if _, has := payload["task_env"]; has {
		t.Errorf("task_env should not appear without --env: %v", payload)
	}
	if _, has := payload["egress_allowlist"]; has {
		t.Errorf("egress_allowlist should not appear without --egress-allow: %v", payload)
	}
}

func TestIsUUID(t *testing.T) {
	valid := []string{
		"550e8400-e29b-41d4-a716-446655440000",
		"550E8400-E29B-41D4-A716-446655440000", // uppercase
		"00000000-0000-0000-0000-000000000000",
	}
	for _, s := range valid {
		if !isUUID(s) {
			t.Errorf("isUUID(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"not-a-uuid",
		"550e8400e29b41d4a716446655440000",      // no hyphens
		"550e8400-e29b-41d4-a716-44665544000",   // one digit too few
		"550e8400-e29b-41d4-a716-4466554400000", // one digit too many
		"550e8400-e29b-41d4-a716-44665544000g",  // 'g' is not hex
	}
	for _, s := range invalid {
		if isUUID(s) {
			t.Errorf("isUUID(%q) = true, want false", s)
		}
	}
}

// --- task delete: usage validation, confirmation and TTY guards ------------
//
// The decision to proceed (parsing, single vs --bulk, TTY, confirmation) lives in
// preDeleteCheck with all dependencies injected (out, in, isTTY), so it is tested
// in isolation WITHOUT touching a real TTY or an HTTP client. A strings.Reader
// acts as simulated stdin; a bytes.Buffer captures the prompt.

// failOnReadReader is an io.Reader that fails the test if it is read: it proves
// that certain branches (--yes, usage error, no-TTY) do NOT consult stdin.
type failOnReadReader struct{ t *testing.T }

func (r failOnReadReader) Read(_ []byte) (int, error) {
	r.t.Fatal("stdin should not be read in this branch")
	return 0, nil
}

func TestPreDeleteCheckMultipleIDsRequireBulk(t *testing.T) {
	var out bytes.Buffer
	// >1 id WITHOUT --bulk: local usage error, without reading stdin (never reaches the server).
	ids, proceed, err := preDeleteCheck(&out, failOnReadReader{t}, []string{"217", "229"},
		false /*bulk*/, false /*yes*/, true /*isTTY*/)
	if err == nil {
		t.Fatal("expected an error for multiple ids without --bulk")
	}
	if err.Error() != "multiple task ids require --bulk" {
		t.Errorf("error = %q, want 'multiple task ids require --bulk'", err.Error())
	}
	if proceed {
		t.Error("proceed = true, want false")
	}
	if ids != nil {
		t.Errorf("ids = %v, want nil", ids)
	}
}

func TestPreDeleteCheckAbortsOnNo(t *testing.T) {
	var out bytes.Buffer
	// Confirmation declined: proceed=false, no error (clean exit, nothing deleted).
	ids, proceed, err := preDeleteCheck(&out, strings.NewReader("n\n"), []string{"217"},
		false, false, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if proceed {
		t.Error("proceed = true after answering 'n', want false")
	}
	if len(ids) != 1 || ids[0] != 217 {
		t.Errorf("ids = %v, want [217]", ids)
	}
	if !strings.Contains(out.String(), "About to delete 1 task(s)") {
		t.Errorf("confirmation notice missing: %q", out.String())
	}
}

func TestPreDeleteCheckProceedsOnYesInput(t *testing.T) {
	var out bytes.Buffer
	_, proceed, err := preDeleteCheck(&out, strings.NewReader("y\n"), []string{"217"},
		false, false, true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !proceed {
		t.Error("proceed = false after answering 'y', want true")
	}
}

func TestPreDeleteCheckYesFlagSkipsPrompt(t *testing.T) {
	var out bytes.Buffer
	// --yes: must NOT read stdin (failOnReadReader would fail the test if it does).
	ids, proceed, err := preDeleteCheck(&out, failOnReadReader{t}, []string{"217", "229"},
		true /*bulk*/, true /*yes*/, false /*isTTY: irrelevant with --yes*/)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !proceed {
		t.Error("proceed = false with --yes, want true")
	}
	if len(ids) != 2 {
		t.Errorf("ids = %v, want 2 ids", ids)
	}
	if out.Len() != 0 {
		t.Errorf("--yes should not print a prompt, wrote: %q", out.String())
	}
}

func TestPreDeleteCheckNoTTYWithoutYesErrors(t *testing.T) {
	var out bytes.Buffer
	// No TTY and no --yes: local error BEFORE any request; does not read stdin.
	_, proceed, err := preDeleteCheck(&out, failOnReadReader{t}, []string{"217"},
		false, false /*yes*/, false /*isTTY*/)
	if err == nil {
		t.Fatal("expected an error without TTY and without --yes")
	}
	if err.Error() != "refusing to delete without --yes (no interactive terminal)" {
		t.Errorf("error = %q, want 'refusing to delete without --yes (no interactive terminal)'", err.Error())
	}
	if proceed {
		t.Error("proceed = true, want false")
	}
}

func TestPreDeleteCheckEdgeCases(t *testing.T) {
	cases := []struct {
		name  string
		input string
	}{
		// Immediate EOF (empty pipe): equivalent to "does not confirm" — clean exit.
		{"EOF", ""},
		// Uppercase 'N': same treatment as 'n'.
		{"uppercase N", "N\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var out bytes.Buffer
			_, proceed, err := preDeleteCheck(&out, strings.NewReader(tc.input), []string{"217"},
				false, false, true)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if proceed {
				t.Error("proceed = true, want false")
			}
		})
	}
}

// --- Golden: human output, single and bulk ---------------------------------

func TestGoldenTaskDeleteSingle(t *testing.T) {
	// After a successful DELETE (204) runTaskDelete emits this single line. We
	// pin it in a golden file so any wording change is caught.
	var buf bytes.Buffer
	printDeleteSingle(&buf, 217)
	assertGolden(t, "task_delete_single", buf.Bytes())
}

func TestGoldenTaskDeleteBulk(t *testing.T) {
	const body = `{
		"deleted": [217, 229],
		"skipped": [{"id": 230, "reason": "not terminal: queued"}],
		"not_found": [999]
	}`
	client := mockClient(t, jsonResponder(t, "/api/v1/tasks/bulk/delete", body))
	resp, err := client.BulkDeleteTasks(context.Background(), []int{217, 229, 230, 999})
	if err != nil {
		t.Fatalf("BulkDeleteTasks: %v", err)
	}
	var buf bytes.Buffer
	printBulkDeleteResponse(&buf, resp)
	assertGolden(t, "task_delete_bulk", buf.Bytes())
}

// invalidEnvCases are --env entries that the CLI rejects before hitting the
// server. The values carry a marker to check that they never appear
// in the error.
var invalidEnvCases = []struct {
	name  string
	entry string
}{
	{"with equals", "API_TOKEN=SECRETVALUE123"},
	{"with space", "API TOKEN"},
	{"trailing newline", "API_TOKEN\n"},
	{"empty string", ""},
	{"starts with digit", "1API_TOKEN"},
	{"token prefix", "hf_SECRETVALUE123"},
	{"prefix ghp_", "ghp_SECRETVALUE123"},
	{"prefix gho_", "gho_SECRETVALUE123"},
	{"prefix ghs_", "ghs_SECRETVALUE123"},
	{"prefix ghu_", "ghu_SECRETVALUE123"},
	{"prefix ghr_", "ghr_SECRETVALUE123"},
	{"prefix github_pat_", "github_pat_SECRETVALUE123"},
	{"prefix glpat-", "glpat-SECRETVALUE123"},
	{"prefix xoxb-", "xoxb-SECRETVALUE123"},
	{"prefix xoxp-", "xoxp-SECRETVALUE123"},
	{"prefix sk-", "sk-SECRETVALUE123"},
	{"prefix sk-or-", "sk-or-SECRETVALUE123"},
	{"prefix ORCH_", "ORCH_SECRETVALUE123"},
}

func TestValidateTaskEnvEntriesRejectsInvalid(t *testing.T) {
	for _, tc := range invalidEnvCases {
		t.Run(tc.name, func(t *testing.T) {
			// The invalid entry goes second: the error position counts from 1.
			err := validateTaskEnvEntries([]string{"API_TOKEN", tc.entry})
			if err == nil {
				t.Fatalf("entrada %q should fail", tc.entry)
			}
			msg := err.Error()
			if !strings.Contains(msg, "--env #2") {
				t.Errorf("error = %q, want position --env #2", msg)
			}
			if tc.entry != "" && strings.Contains(msg, tc.entry) {
				t.Errorf("error = %q repeats the entry", msg)
			}
			if strings.Contains(msg, "SECRETVALUE123") {
				t.Errorf("error = %q leaks the value", msg)
			}
			if !strings.HasPrefix(tc.entry, "ORCH_") && !strings.Contains(msg, envHint) {
				t.Errorf("error = %q, want hint %q", msg, envHint)
			}
		})
	}
}

func TestValidateTaskEnvEntriesTokenPrefixBeforeShape(t *testing.T) {
	// The rule order is the server's: the token prefix goes before the shape.
	// These entries fail both rules (the hyphen or the equals sign are not part of
	// a name) and must get the credential message, not the shape one.
	for _, entry := range []string{"sk-SECRETVALUE123", "glpat-SECRETVALUE123", "hf_x=SECRETVALUE123"} {
		err := validateTaskEnvEntries([]string{entry})
		if err == nil {
			t.Fatalf("entrada %q should fail", entry)
		}
		msg := err.Error()
		if !strings.Contains(msg, "looks like a credential") {
			t.Errorf("error = %q, want the credential message", msg)
		}
		if strings.Contains(msg, "not a valid environment variable name") {
			t.Errorf("error = %q uses the shape message; the prefix goes first", msg)
		}
		if strings.Contains(msg, "SECRETVALUE123") {
			t.Errorf("error = %q leaks the value", msg)
		}
	}
	// Conversely: HF_TOKEN=hf_x does not start with a token prefix and gets the shape one.
	err := validateTaskEnvEntries([]string{"HF_TOKEN=hf_SECRETVALUE123"})
	if err == nil || !strings.Contains(err.Error(), "not a valid environment variable name") {
		t.Errorf("error = %v, want the shape message", err)
	}
}

func TestValidateTaskEnvEntriesAcceptsNames(t *testing.T) {
	// Valid names, including those that look like a token prefix but are not
	// (the prefix is case-sensitive, as on the server).
	ok := []string{"HF_TOKEN", "GITHUB_TOKEN", "http_proxy", "_PRIVATE", "A1", "x", "ORCHX", "orch_lower"}
	if err := validateTaskEnvEntries(ok); err != nil {
		t.Errorf("valid names rejected: %v", err)
	}
	if err := validateTaskEnvEntries(nil); err != nil {
		t.Errorf("without --env it should not fail: %v", err)
	}
}

func TestBuildTaskBodyRejectsInvalidEnv(t *testing.T) {
	// create (includeDefaults) and fork (only changed) go through the same guard.
	for _, includeDefaults := range []bool{true, false} {
		c, p := newParamsCmd(t, []string{"--env", "API_TOKEN=SECRETVALUE123"})
		if _, err := buildTaskBody(c, p, includeDefaults); err == nil {
			t.Errorf("includeDefaults=%v: --env NAME=value should fail", includeDefaults)
		}
	}
}

// runEnvCommand runs create or fork with the flags bound to the global params
// used by runTaskCreate/runTaskFork, against an httptest server that counts
// the requests. It returns the combined output, how many requests arrived and the error.
func runEnvCommand(t *testing.T, fork bool, args []string) (string, int32, error) {
	t.Helper()
	out, hits, _, err := runEnvCommandBody(t, fork, args)
	return out, hits, err
}

// runEnvCommandBody is runEnvCommand and also returns the body of the last
// request received, to check which task_env was sent.
func runEnvCommandBody(t *testing.T, fork bool, args []string) (string, int32, []byte, error) {
	t.Helper()
	var hits atomic.Int32
	var mu sync.Mutex
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		lastBody = b
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id": 1, "status": "queued"}`))
	}))
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_test")

	params, run, use := &createParams, runTaskCreate, "create"
	if fork {
		params, run, use = &forkParams, runTaskFork, "fork"
	}
	saved := *params
	*params = taskParams{}
	t.Cleanup(func() { *params = saved })

	c := &cobra.Command{Use: use, RunE: run}
	registerTaskParamFlags(c, params)
	var out bytes.Buffer
	c.SetOut(&out)
	c.SetErr(&out)
	c.SetArgs(args)
	c.SilenceUsage = true
	c.SilenceErrors = true
	err := c.Execute()
	mu.Lock()
	defer mu.Unlock()
	return out.String(), hits.Load(), lastBody, err
}

func TestRunTaskCreateAndForkRejectInvalidEnvWithoutRequest(t *testing.T) {
	for _, fork := range []bool{false, true} {
		for _, tc := range invalidEnvCases {
			t.Run(fmt.Sprintf("fork=%v/%s", fork, tc.name), func(t *testing.T) {
				args := []string{"--payload", "{}", "--env", "API_TOKEN", "--env", tc.entry}
				if fork {
					args = append([]string{"7"}, args...)
				}
				out, hits, err := runEnvCommand(t, fork, args)
				if err == nil {
					t.Fatal("should fail")
				}
				if hits != 0 {
					t.Errorf("%d requests reached the server; want 0", hits)
				}
				all := err.Error() + out
				if strings.Contains(all, "SECRETVALUE123") || (tc.entry != "" && strings.Contains(all, tc.entry)) {
					t.Errorf("the output repeats the entry: %q", all)
				}
				if !strings.Contains(err.Error(), "--env #2") {
					t.Errorf("error = %q, want position --env #2", err.Error())
				}
			})
		}
	}
}

func TestRunTaskCreateAndForkSendValidEnv(t *testing.T) {
	// Positive control: with valid names the request goes out and carries task_env.
	for _, fork := range []bool{false, true} {
		args := []string{"--payload", "{}", "--env", "API_TOKEN", "--env", "http_proxy", "--json"}
		if fork {
			args = append([]string{"7"}, args...)
		}
		_, hits, body, err := runEnvCommandBody(t, fork, args)
		if err != nil {
			t.Fatalf("fork=%v: %v", fork, err)
		}
		if hits != 1 {
			t.Errorf("fork=%v: %d requests arrived; want 1", fork, hits)
		}
		var sent struct {
			Payload struct {
				TaskEnv []string `json:"task_env"`
			} `json:"payload"`
		}
		if err := json.Unmarshal(body, &sent); err != nil {
			t.Fatalf("fork=%v: body is not JSON: %v (%q)", fork, err, body)
		}
		if want := []string{"API_TOKEN", "http_proxy"}; !slices.Equal(sent.Payload.TaskEnv, want) {
			t.Errorf("fork=%v: task_env = %v; want %v", fork, sent.Payload.TaskEnv, want)
		}
	}
}

// runRetryCommand runs the REAL `task retry` command (taskRetryCmd, with the
// flags that init() registers) against an httptest.Server and returns the body
// that was sent. It uses the real command on purpose: an ephemeral command built
// in the test would have the test's default, not the CLI's, and would prove nothing.
func runRetryCommand(t *testing.T, args []string) ([]byte, int32, error) {
	t.Helper()
	var hits atomic.Int32
	var mu sync.Mutex
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		lastBody = b
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"task_id": 7, "retry_count": 1, "status": "queued",
			"attempt_number": 2, "target_worker_id": null, "delivery_mode": "claim"
		}`))
	}))
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_test")

	// The taskRetryCmd flags write to package vars and the command is shared:
	// we save and restore the state so it does not leak into other tests.
	savedForce, savedJSON := retryForceSameWorker, retryJSON
	t.Cleanup(func() {
		retryForceSameWorker, retryJSON = savedForce, savedJSON
		_ = taskRetryCmd.Flags().Set("force-same-worker",
			taskRetryCmd.Flags().Lookup("force-same-worker").DefValue)
		taskRetryCmd.Flags().Lookup("force-same-worker").Changed = false
	})
	// Start from the default declared by init(), not from what another test left.
	retryForceSameWorker = taskRetryCmd.Flags().Lookup("force-same-worker").DefValue == "true"

	// It is invoked from the root (`orcaster task retry ...`), which is how the
	// binary runs: cobra resolves the subcommand and parses its flags.
	var out bytes.Buffer
	rootCmd.SetOut(&out)
	rootCmd.SetErr(&out)
	rootCmd.SetArgs(append([]string{"task", "retry"}, args...))
	t.Cleanup(func() { rootCmd.SetArgs(nil); rootCmd.SetOut(nil); rootCmd.SetErr(nil) })
	err := rootCmd.Execute()
	mu.Lock()
	defer mu.Unlock()
	return lastBody, hits.Load(), err
}

// TestTaskRetryDefaultsToAnyEligibleWorker pins the CLI default: without
// --force-same-worker the retry goes back to the general pool, like the default of
// `AdminRetryTaskRequest.force_same_worker` (False) on the server. The CLI used
// to send true, so the same operation behaved differently from the CLI than from
// the API or the dashboard.
func TestTaskRetryDefaultsToAnyEligibleWorker(t *testing.T) {
	if got := taskRetryCmd.Flags().Lookup("force-same-worker").DefValue; got != "false" {
		t.Errorf("default of --force-same-worker = %q, want \"false\"", got)
	}

	body, hits, err := runRetryCommand(t, []string{"7"})
	if err != nil {
		t.Fatalf("task retry: %v", err)
	}
	if hits != 1 {
		t.Fatalf("%d requests arrived; want 1", hits)
	}
	var sent map[string]any
	if err := json.Unmarshal(body, &sent); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, body)
	}
	if sent["force_same_worker"] != false {
		t.Errorf("force_same_worker = %v, want false", sent["force_same_worker"])
	}
}

// TestTaskRetryForceSameWorkerIsOptIn is the positive control for the previous test:
// the explicit flag does travel as true, so a false does not mean the body
// never carries the flag's value.
func TestTaskRetryForceSameWorkerIsOptIn(t *testing.T) {
	body, hits, err := runRetryCommand(t, []string{"7", "--force-same-worker"})
	if err != nil {
		t.Fatalf("task retry --force-same-worker: %v", err)
	}
	if hits != 1 {
		t.Fatalf("%d requests arrived; want 1", hits)
	}
	var sent map[string]any
	if err := json.Unmarshal(body, &sent); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, body)
	}
	if sent["force_same_worker"] != true {
		t.Errorf("force_same_worker = %v, want true", sent["force_same_worker"])
	}
}
