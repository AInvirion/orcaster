package cmd

// Tests for `orcaster secret`. Each case builds a fresh `secret` tree with
// newSecretCmd() (no shared state between tests), points the CLI at an
// httptest.Server through ORCASTER_HOST/ORCASTER_TOKEN and injects
// stdin/stdout/stderr with SetIn/SetOut/SetErr. This exercises the whole flow:
// local validation, reading stdin, the HTTP request and rendering the output.
//
// Core invariant: the secret value travels ONLY in the PUT body. It never
// appears in stdout or stderr, nor in errors.

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const secretItemJSON = `{"name": "API_TOKEN", "fingerprint": "Ab3d/0123456789ab",
	"created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
	"updated_by": "Ada"}`

// secretRun is the result of running the `secret` tree.
type secretRun struct {
	stdout, stderr string
	err            error
}

// runSecretCmd runs `secret <args>` against handler with the given stdin. It
// isolates the config in a temp dir and sets host/token through the environment
// (they take precedence over the profile).
func runSecretCmd(t *testing.T, handler http.HandlerFunc, stdin string, args ...string) secretRun {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_test")

	c := newSecretCmd()
	c.SetArgs(args)
	c.SetIn(strings.NewReader(stdin))
	var out, errOut bytes.Buffer
	c.SetOut(&out)
	c.SetErr(&errOut)
	c.SilenceUsage = true
	c.SilenceErrors = true
	err := c.Execute()
	return secretRun{stdout: out.String(), stderr: errOut.String(), err: err}
}

// noRequest fails the test if the CLI reaches the server: it proves that a
// local validation stops before the network.
func noRequest(t *testing.T) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("no request expected, got %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusTeapot)
	}
}

// assertNoLeak checks that value appears in no output nor in the error.
func assertNoLeak(t *testing.T, r secretRun, value string) {
	t.Helper()
	if strings.Contains(r.stdout, value) {
		t.Errorf("value appeared in stdout: %q", r.stdout)
	}
	if strings.Contains(r.stderr, value) {
		t.Errorf("value appeared in stderr: %q", r.stderr)
	}
	if r.err != nil && strings.Contains(r.err.Error(), value) {
		t.Errorf("value appeared in the error: %q", r.err.Error())
	}
}

// --- secret set ------------------------------------------------------------

func TestSecretSetSendsStdinValueInBody(t *testing.T) {
	const value = "tok_s3cr3t_value"
	var gotMethod, gotPath string
	var gotBody map[string]any
	handler := func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, secretItemJSON)
	}
	r := runSecretCmd(t, handler, value+"\n", "set", "API_TOKEN")
	if r.err != nil {
		t.Fatalf("set: %v", r.err)
	}
	if gotMethod != http.MethodPut || gotPath != "/api/v1/secrets/API_TOKEN" {
		t.Errorf("request = %s %s, want PUT /api/v1/secrets/API_TOKEN", gotMethod, gotPath)
	}
	// Exactly one trailing "\n" is stripped; the rest of the value travels intact.
	if gotBody["value"] != value {
		t.Errorf("value in the body = %q, want %q", gotBody["value"], value)
	}
	assertNoLeak(t, r, value)
	assertGolden(t, "secret_set", []byte(r.stdout))
}

func TestSecretSetStripsExactlyOneTrailingNewline(t *testing.T) {
	cases := []struct {
		name, stdin, want string
	}{
		{"LF", "abc\n", "abc"},
		{"CRLF", "abc\r\n", "abc"},
		{"no trailing newline", "abc", "abc"},
		{"only one is stripped", "abc\n\n", "abc\n"},
		{"multiline", "line1\nline2\n", "line1\nline2"},
		{"spaces are kept", "  abc  \n", "  abc  "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var got any
			handler := func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				raw, _ := io.ReadAll(r.Body)
				_ = json.Unmarshal(raw, &body)
				got = body["value"]
				_, _ = io.WriteString(w, secretItemJSON)
			}
			r := runSecretCmd(t, handler, tc.stdin, "set", "API_TOKEN")
			if r.err != nil {
				t.Fatalf("set: %v", r.err)
			}
			if got != tc.want {
				t.Errorf("value = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestSecretSetRejectsEmptyValue(t *testing.T) {
	for _, stdin := range []string{"", "\n", "\r\n"} {
		r := runSecretCmd(t, noRequest(t), stdin, "set", "API_TOKEN")
		if r.err == nil || !strings.Contains(r.err.Error(), "empty secret value") {
			t.Errorf("stdin %q: error = %v, want 'empty secret value'", stdin, r.err)
		}
	}
}

func TestSecretSetRejectsOversizedValue(t *testing.T) {
	big := strings.Repeat("x", maxSecretValueBytes+1)
	r := runSecretCmd(t, noRequest(t), big, "set", "API_TOKEN")
	if r.err == nil || !strings.Contains(r.err.Error(), "exceeds") {
		t.Fatalf("error = %v, want rejection for size", r.err)
	}
	assertNoLeak(t, r, big)

	// A newline right at the read cutoff must not let a truncated value pass:
	// what follows the limit also counts.
	tricky := strings.Repeat("z", maxSecretValueBytes) + "\r\n" + "rest"
	r = runSecretCmd(t, noRequest(t), tricky, "set", "API_TOKEN")
	if r.err == nil || !strings.Contains(r.err.Error(), "exceeds") {
		t.Fatalf("truncated value accepted: error = %v", r.err)
	}

	// Exactly at the limit (plus the "\n" that is stripped) it passes.
	atLimit := strings.Repeat("y", maxSecretValueBytes)
	var gotLen int
	handler := func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		gotLen = len(body["value"])
		_, _ = io.WriteString(w, secretItemJSON)
	}
	r = runSecretCmd(t, handler, atLimit+"\n", "set", "API_TOKEN")
	if r.err != nil {
		t.Fatalf("value at the limit rejected: %v", r.err)
	}
	if gotLen != maxSecretValueBytes {
		t.Errorf("len(value) = %d, want %d", gotLen, maxSecretValueBytes)
	}
}

func TestSecretSetRejectsValueAsArgument(t *testing.T) {
	// The value is NEVER accepted through argv (it would stay in the shell
	// history and in `ps`). A second argument is a usage error and the message
	// does not repeat it.
	const value = "tok_from_argv"
	r := runSecretCmd(t, noRequest(t), "", "set", "API_TOKEN", value)
	if r.err == nil || !strings.Contains(r.err.Error(), "stdin") {
		t.Fatalf("error = %v, want a usage error that points to stdin", r.err)
	}
	assertNoLeak(t, r, value)
}

func TestSecretSetHasNoValueFlag(t *testing.T) {
	c := newSecretCmd()
	set, _, err := c.Find([]string{"set"})
	if err != nil {
		t.Fatalf("Find set: %v", err)
	}
	for _, name := range []string{"value", "from-env", "env"} {
		if set.Flags().Lookup(name) != nil {
			t.Errorf("`secret set` must not have --%s: the value comes only from stdin", name)
		}
	}
}

func TestSecretSetValidatesNameLocally(t *testing.T) {
	for _, name := range []string{"", "1ABC", "API-TOKEN", "a/b", "ORCH_SECRET", "API TOKEN", "ÑANDU"} {
		r := runSecretCmd(t, noRequest(t), "v\n", "set", name)
		if r.err == nil {
			t.Errorf("name %q accepted, want local rejection", name)
		}
	}
}

func TestValidateSecretName(t *testing.T) {
	for _, ok := range []string{"API_TOKEN", "_X", "a1", "GITHUB_TOKEN", "MY_ORCH_TOKEN"} {
		if err := validateSecretName(ok); err != nil {
			t.Errorf("validateSecretName(%q) = %v, want nil", ok, err)
		}
	}
	err := validateSecretName("ORCH_TOKEN")
	if err == nil || !strings.Contains(err.Error(), "ORCH_") {
		t.Errorf("ORCH_TOKEN: error = %v, want it to mention ORCH_", err)
	}
}

// A name with a known token prefix is rejected before the network and the error
// does not repeat it. Common variable names still pass.
func TestSecretSetRejectsTokenPrefixedNameWithoutEcho(t *testing.T) {
	const mark = "SECRETMARK123"
	for _, prefix := range []string{"hf_", "ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_", "glpat-", "sk-"} {
		name := prefix + mark
		r := runSecretCmd(t, noRequest(t), "v\n", "set", name)
		if r.err == nil {
			t.Errorf("%s: accepted, want local rejection", prefix)
			continue
		}
		if !strings.Contains(r.err.Error(), "known token prefix") {
			t.Errorf("%s: error = %v, want it to mention the token prefix", prefix, r.err)
		}
		for _, out := range []string{r.err.Error(), r.stdout, r.stderr} {
			if strings.Contains(out, mark) {
				t.Errorf("%s: the output repeats the name: %q", prefix, out)
			}
		}
	}
	for _, ok := range []string{"HF_TOKEN", "GITHUB_TOKEN", "http_proxy"} {
		if err := validateSecretName(ok); err != nil {
			t.Errorf("validateSecretName(%q) = %v, want nil", ok, err)
		}
	}
}

// The ORCH_ prefix is rejected case-insensitively, as on the server.
func TestValidateSecretNameOrchAnyCase(t *testing.T) {
	for _, name := range []string{"orch_token", "Orch_X", "oRCH_api_key"} {
		err := validateSecretName(name)
		if err == nil || !strings.Contains(err.Error(), "reserved ORCH_ prefix") {
			t.Errorf("%s: error = %v, want a rejection that mentions ORCH_", name, err)
			continue
		}
		// The error gives the reason without repeating the name.
		if strings.Contains(err.Error(), name) {
			t.Errorf("%s: the error repeats the name: %v", name, err)
		}
	}
}

func TestSecretSet403IsFriendly(t *testing.T) {
	const value = "tok_s3cr3t"
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, `{"error": "forbidden", "message": "owner role required"}`)
	}
	r := runSecretCmd(t, handler, value, "set", "API_TOKEN")
	if r.err == nil || !strings.Contains(r.err.Error(), "owner") {
		t.Fatalf("error = %v, want it to mention the owner role", r.err)
	}
	assertNoLeak(t, r, value)
}

func TestSecretSet503UnconfiguredIsFriendly(t *testing.T) {
	const value = "tok_s3cr3t"
	r := runSecretCmd(t, unconfiguredHandler, value, "set", "API_TOKEN")
	if r.err == nil || !strings.Contains(r.err.Error(), "TASK_SECRETS_KEYS_PATH") {
		t.Fatalf("error = %v, want an explanation that the server has no keys", r.err)
	}
	assertNoLeak(t, r, value)
}

// unconfiguredHandler answers the server's 503 when there is no keys file.
func unconfiguredHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusServiceUnavailable)
	_, _ = io.WriteString(w, `{"error": "task_secrets_unconfigured", "message": "task secrets are not configured"}`)
}

func TestReadSecretValueTTYUsesHiddenReader(t *testing.T) {
	// On a TTY the value comes from the no-echo reader, not from in.
	got, err := readSecretValue(failOnReadReader{t}, true, func() ([]byte, error) {
		return []byte("typed_value"), nil
	})
	if err != nil || got != "typed_value" {
		t.Errorf("readSecretValue TTY = %q, %v; want typed_value, nil", got, err)
	}

	_, err = readSecretValue(failOnReadReader{t}, true, func() ([]byte, error) {
		return nil, nil
	})
	if err == nil || !strings.Contains(err.Error(), "empty secret value") {
		t.Errorf("empty TTY: error = %v, want 'empty secret value'", err)
	}

	_, err = readSecretValue(failOnReadReader{t}, true, func() ([]byte, error) {
		return nil, errors.New("boom")
	})
	if err == nil || !strings.Contains(err.Error(), "boom") {
		t.Errorf("hidden reader error not propagated: %v", err)
	}
}

// --- secret list -----------------------------------------------------------

const secretListJSON = `{"items": [
	{"name": "GITHUB_TOKEN", "fingerprint": "zz99/ba9876543210",
	 "created_at": "2026-10-19T09:00:00Z", "updated_at": "2026-10-19T09:00:00Z",
	 "updated_by": null},
	` + secretItemJSON + `
]}`

func TestGoldenSecretList(t *testing.T) {
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", secretListJSON), "", "list")
	if r.err != nil {
		t.Fatalf("list: %v", r.err)
	}
	assertGolden(t, "secret_list", []byte(r.stdout))
}

func TestGoldenSecretListEmpty(t *testing.T) {
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", `{"items": []}`), "", "list")
	if r.err != nil {
		t.Fatalf("list: %v", r.err)
	}
	assertGolden(t, "secret_list_empty", []byte(r.stdout))
}

// secretListBody builds the GET /secrets body with the given keys_configured;
// "" omits the field, like a server that predates the flag.
func secretListBody(keysConfigured string) string {
	if keysConfigured == "" {
		return `{"items": [` + secretItemJSON + `]}`
	}
	return `{"items": [` + secretItemJSON + `], "keys_configured": ` + keysConfigured + `}`
}

// Without keys the server still lists names and fingerprints: the table goes to
// stdout (parseable) and the notice to stderr.
func TestGoldenSecretListKeysUnconfigured(t *testing.T) {
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", secretListBody("false")), "", "list")
	if r.err != nil {
		t.Fatalf("list: %v", r.err)
	}
	assertGolden(t, "secret_list_keys_unconfigured", []byte(r.stdout))
	assertGolden(t, "secret_list_keys_unconfigured_stderr", []byte(r.stderr))
}

func TestSecretListKeysUnconfiguredEmptyStillNotes(t *testing.T) {
	body := `{"items": [], "keys_configured": false}`
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", body), "", "list")
	if r.err != nil {
		t.Fatalf("list: %v", r.err)
	}
	if r.stdout != "No secrets.\n" {
		t.Errorf("stdout = %q, want the empty state", r.stdout)
	}
	if !strings.Contains(r.stderr, "TASK_SECRETS_KEYS_PATH") {
		t.Errorf("stderr = %q, want the keys-not-configured notice", r.stderr)
	}
}

// keys_configured true and a missing field (old server) count as configured:
// no notice.
func TestSecretListKeysConfiguredOrMissingHasNoNotice(t *testing.T) {
	for _, tc := range []struct{ name, flag string }{{"true", "true"}, {"missing", ""}} {
		t.Run(tc.name, func(t *testing.T) {
			r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", secretListBody(tc.flag)), "", "list")
			if r.err != nil {
				t.Fatalf("list: %v", r.err)
			}
			if r.stderr != "" {
				t.Errorf("stderr = %q, there should be no notice", r.stderr)
			}
			if !strings.Contains(r.stdout, "API_TOKEN") {
				t.Errorf("stdout = %q, want the table", r.stdout)
			}
		})
	}
}

// --json passes the body through as is, with keys_configured visible and no notice.
func TestSecretListJSONKeepsKeysConfiguredWithoutNotice(t *testing.T) {
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", secretListBody("false")), "", "list", "--json")
	if r.err != nil {
		t.Fatalf("list --json: %v", r.err)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(r.stdout), &parsed); err != nil {
		t.Fatalf("--json is not valid JSON: %v\n%s", err, r.stdout)
	}
	if parsed["keys_configured"] != false {
		t.Errorf("--json should keep keys_configured=false: %s", r.stdout)
	}
	if r.stderr != "" {
		t.Errorf("stderr = %q, --json should not warn", r.stderr)
	}
}

func TestSecretListJSONPreservesServerShape(t *testing.T) {
	const body = `{"items":[{"name":"API_TOKEN","fingerprint":"f","created_at":"2026-10-20T10:00:00Z","updated_at":"2026-10-20T10:00:00Z","updated_by":null,"future_field":true}]}`
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", body), "", "list", "--json")
	if r.err != nil {
		t.Fatalf("list --json: %v", r.err)
	}
	var parsed struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal([]byte(r.stdout), &parsed); err != nil {
		t.Fatalf("--json is not valid JSON: %v\n%s", err, r.stdout)
	}
	if len(parsed.Items) != 1 || parsed.Items[0]["future_field"] != true {
		t.Errorf("--json should keep the server keys: %s", r.stdout)
	}
	if _, ok := parsed.Items[0]["updated_by"]; !ok {
		t.Errorf("--json lost updated_by null: %s", r.stdout)
	}
}

func TestSecretList503UnconfiguredIsFriendly(t *testing.T) {
	r := runSecretCmd(t, unconfiguredHandler, "", "list")
	if r.err == nil || !strings.Contains(r.err.Error(), "TASK_SECRETS_KEYS_PATH") {
		t.Fatalf("error = %v, want an explanation that the server has no keys", r.err)
	}
}

// --- secret delete ---------------------------------------------------------

func TestSecretDeleteWithYes(t *testing.T) {
	var gotMethod, gotPath string
	handler := func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}
	r := runSecretCmd(t, handler, "", "delete", "API_TOKEN", "--yes")
	if r.err != nil {
		t.Fatalf("delete: %v", r.err)
	}
	if gotMethod != http.MethodDelete || gotPath != "/api/v1/secrets/API_TOKEN" {
		t.Errorf("request = %s %s, want DELETE /api/v1/secrets/API_TOKEN", gotMethod, gotPath)
	}
	assertGolden(t, "secret_delete", []byte(r.stdout))
}

func TestSecretDeleteWithoutYesNoTTYRefuses(t *testing.T) {
	// The test stdin is not a TTY: without --yes there is no way to confirm.
	r := runSecretCmd(t, noRequest(t), "y\n", "delete", "API_TOKEN")
	if r.err == nil || !strings.Contains(r.err.Error(), "--yes") {
		t.Fatalf("error = %v, want a refusal without --yes", r.err)
	}
}

func TestSecretDelete404IsClear(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			// The server has the secrets API: the list answers.
			_, _ = io.WriteString(w, `{"items": []}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error": "not_found", "message": "secret not found"}`)
	}
	r := runSecretCmd(t, handler, "", "delete", "API_TOKEN", "-y")
	if r.err == nil || r.err.Error() != "secret API_TOKEN not found" {
		t.Fatalf("error = %v, want 'secret API_TOKEN not found'", r.err)
	}
}

func TestSecretDelete403IsFriendly(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, `{"error": "forbidden", "message": "owner role required"}`)
	}
	r := runSecretCmd(t, handler, "", "delete", "API_TOKEN", "--yes")
	if r.err == nil || !strings.Contains(r.err.Error(), "owner") {
		t.Fatalf("error = %v, want it to mention the owner role", r.err)
	}
}

func TestSecretDeleteValidatesNameLocally(t *testing.T) {
	r := runSecretCmd(t, noRequest(t), "", "delete", "bad-name", "--yes")
	if r.err == nil {
		t.Fatal("invalid name accepted in delete")
	}
}

func TestSecretDeleteAcceptsOrchPrefixName(t *testing.T) {
	// A row stored before its name became reserved must be deletable: delete
	// only validates the pattern, as the server does.
	var gotPath string
	handler := func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}
	r := runSecretCmd(t, handler, "", "delete", "orch_old", "--yes")
	if r.err != nil {
		t.Fatalf("delete orch_old: %v", r.err)
	}
	if gotPath != "/api/v1/secrets/orch_old" {
		t.Errorf("path = %s, want /api/v1/secrets/orch_old", gotPath)
	}
}

func TestConfirmSecretDelete(t *testing.T) {
	cases := []struct {
		name, input string
		yes, isTTY  bool
		want        bool
		wantErr     string
	}{
		{"--yes skips the prompt", "", true, false, true, ""},
		{"no TTY and no --yes", "", false, false, false, "refusing to delete without --yes"},
		{"answers y", "y\n", false, true, true, ""},
		{"answers Y", "Y\n", false, true, true, ""},
		{"answers n", "n\n", false, true, false, ""},
		{"EOF", "", false, true, false, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var out bytes.Buffer
			var in io.Reader = strings.NewReader(tc.input)
			if tc.yes || !tc.isTTY {
				in = failOnReadReader{t} // these branches do not read stdin
			}
			got, err := confirmSecretDelete(&out, in, "API_TOKEN", tc.yes, tc.isTTY)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("proceed = %v, want %v", got, tc.want)
			}
			if tc.isTTY && !tc.yes && !strings.Contains(out.String(), "About to delete secret API_TOKEN") {
				t.Errorf("prompt missing: %q", out.String())
			}
		})
	}
}
