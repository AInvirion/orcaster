package cmd

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestMain isolates the package from the developer's environment: the config
// directory points to a temporary one and legacy variables are cleared, so the
// legacy-name warning only appears in tests that set it up on purpose.
func TestMain(m *testing.M) {
	tmp, err := os.MkdirTemp("", "orcaster-cmd-test-")
	if err != nil {
		panic(err)
	}
	_ = os.Setenv("XDG_CONFIG_HOME", filepath.Join(tmp, "config"))
	for _, kv := range os.Environ() {
		if name, _, _ := strings.Cut(kv, "="); strings.HasPrefix(name, "TASKOR_") {
			_ = os.Unsetenv(name)
		}
	}
	code := m.Run()
	_ = os.RemoveAll(tmp)
	os.Exit(code)
}

// runAs runs the CLI as the binary prog and returns exit code, stdout and stderr.
// It restores the global state (args, outputs, name) on exit.
func runAs(t *testing.T, prog string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	var out, errOut bytes.Buffer
	rootCmd.SetOut(&out)
	authStatusCmd.SetOut(nil) // other tests leave it with its own buffer set
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
		rootCmd.Use = progName
	})
	code = Run(prog, args, &errOut)
	return code, out.String(), errOut.String()
}

func TestOrcasterDoesNotWarn(t *testing.T) {
	for _, prog := range []string{"orcaster", "/usr/bin/orcaster", "orcaster.exe", "cmd.test"} {
		code, stdout, stderr := runAs(t, prog, "--help")
		if code != 0 || stderr != "" {
			t.Errorf("%s: exit=%d stderr=%q, want 0 and empty", prog, code, stderr)
		}
		if !strings.Contains(stdout, "orcaster [command]") {
			t.Errorf("%s: the usage must show orcaster:\n%s", prog, stdout)
		}
	}
}

func TestRunReturnsOneAndPrintsErrorOnFailure(t *testing.T) {
	code, _, stderr := runAs(t, "orcaster", "no-such-command")
	if code != 1 || !strings.HasPrefix(stderr, "error: ") {
		t.Errorf("exit=%d stderr=%q, want 1 and prefix 'error: '", code, stderr)
	}
}

func TestInvokedAs(t *testing.T) {
	cases := map[string]string{
		"orcaster": "orcaster", "/usr/bin/orcaster": "orcaster", "": "orcaster",
	}
	for in, want := range cases {
		if got := invokedAs(in); got != want {
			t.Errorf("invokedAs(%q) = %q, want %q", in, got, want)
		}
	}
}

// statusServer rejects the token with 401: enough to exercise the env
// host/token resolution without depending on the whoami contract.
func statusServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"error":"unauthorized","message":"nope"}`)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestOrcasterEnvTakesPrecedenceAndIsSilent(t *testing.T) {
	srv := statusServer(t)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")

	_, stdout, stderr := runAs(t, "orcaster", "auth", "status")
	if !strings.Contains(stdout, srv.URL) {
		t.Errorf("ORCASTER_HOST did not win:\n%s", stdout)
	}
	if strings.Contains(stderr, "deprecated") {
		t.Errorf("with ORCASTER_* set there must be no notices:\n%s", stderr)
	}
}

// TestLegacyNamesWarnOnceOnStderr reproduces the reported case: legacy
// variables were silently ignored. Now one warning line names them.
func TestLegacyNamesWarnOnceOnStderr(t *testing.T) {
	srv := statusServer(t)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")
	t.Setenv("TASKOR_HOST", "https://invalid.example")
	t.Setenv("TASKOR_TOKEN", "taskor_pat_dummy")

	_, stdout, stderr := runAs(t, "orcaster", "auth", "status")
	if !strings.Contains(stdout, srv.URL) {
		t.Errorf("the legacy host must not be used:\n%s", stdout)
	}
	var warnings []string
	for _, line := range strings.Split(stderr, "\n") {
		if strings.HasPrefix(line, "warning:") {
			warnings = append(warnings, line)
		}
	}
	if len(warnings) != 1 {
		t.Fatalf("want exactly one warning line, got %d:\n%s", len(warnings), stderr)
	}
	for _, want := range []string{"TASKOR_HOST", "TASKOR_TOKEN", "ORCASTER_HOST", "orcaster auth login"} {
		if !strings.Contains(warnings[0], want) {
			t.Errorf("warning lacks %q: %s", want, warnings[0])
		}
	}
	if strings.Contains(stderr, "taskor_pat_dummy") || strings.Contains(stderr, "invalid.example") {
		t.Errorf("warning must not print legacy values:\n%s", stderr)
	}
}
