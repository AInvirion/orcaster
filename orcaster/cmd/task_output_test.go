package cmd

import (
	"compress/gzip"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// resetOutputFlags restores the command's global flags between tests (the
// variables are package-global and leak between cases if not cleared).
func resetOutputFlags() {
	outputAttempt = 0
	outputFormat = "text"
	outputFile = ""
	outputExecution = ""
	taskOutputCmd.SetContext(context.Background())
}

func TestTaskOutputRequiresIDOrExecution(t *testing.T) {
	resetOutputFlags()
	// No positional id nor --execution: usage error, without touching the network.
	if err := runTaskOutput(taskOutputCmd, []string{}); err == nil {
		t.Fatal("without id or --execution it should fail")
	}
}

func TestTaskOutputRejectsIDAndExecution(t *testing.T) {
	resetOutputFlags()
	outputExecution = "3f2a0000-0000-0000-0000-0000000000e9"
	// Both sources at once is ambiguous: it must fail locally.
	if err := runTaskOutput(taskOutputCmd, []string{"5"}); err == nil {
		t.Fatal("positional id + --execution should fail")
	}
}

func TestTaskOutputExecutionRejectsAttempt(t *testing.T) {
	resetOutputFlags()
	outputExecution = "3f2a0000-0000-0000-0000-0000000000e9"
	outputAttempt = 2 // --attempt does not apply when the UUID already identifies the execution
	if err := runTaskOutput(taskOutputCmd, []string{}); err == nil {
		t.Fatal("--execution + --attempt should fail")
	}
}

func TestTaskOutputExecutionInvalidUUID(t *testing.T) {
	resetOutputFlags()
	outputExecution = "not-a-uuid"
	// The UUID is validated locally before hitting the server (fails fast).
	if err := runTaskOutput(taskOutputCmd, []string{}); err == nil {
		t.Fatal("--execution with an invalid UUID should fail locally")
	}
}

func TestTaskOutputExecutionWritesToFile(t *testing.T) {
	resetOutputFlags()
	const uuid = "3f2a0000-0000-0000-0000-0000000000e9"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/executions/"+uuid+"/output" {
			t.Errorf("path = %q, want /api/v1/executions/%s/output", r.URL.Path, uuid)
		}
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, "exec log\n")
		_ = gz.Close()
	}))
	t.Cleanup(srv.Close)

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_test")

	outputExecution = uuid
	dst := filepath.Join(t.TempDir(), "exec.log")
	outputFile = dst
	if err := runTaskOutput(taskOutputCmd, []string{}); err != nil {
		t.Fatalf("runTaskOutput: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("reading %s: %v", dst, err)
	}
	if string(got) != "exec log\n" {
		t.Errorf("file = %q, want %q", got, "exec log\n")
	}
}

func TestTaskOutputInvalidFormatFailsLocally(t *testing.T) {
	resetOutputFlags()
	outputFormat = "xml"
	// No server is started: format validation happens before newClient,
	// so an invalid format fails without hitting the network.
	err := runTaskOutput(taskOutputCmd, []string{"1"})
	if err == nil {
		t.Fatal("invalid --format should fail locally")
	}
}

func TestTaskOutputWritesToFile(t *testing.T) {
	resetOutputFlags()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, "line one\nline two\n")
		_ = gz.Close()
	}))
	t.Cleanup(srv.Close)

	// Isolate the config: XDG to a tmpdir and host/token via env (config.Resolve
	// prefers ORCASTER_HOST/ORCASTER_TOKEN over what is stored).
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_test")

	dst := filepath.Join(t.TempDir(), "out.log")
	outputFile = dst
	if err := runTaskOutput(taskOutputCmd, []string{"195"}); err != nil {
		t.Fatalf("runTaskOutput: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatalf("reading %s: %v", dst, err)
	}
	if string(got) != "line one\nline two\n" {
		t.Errorf("file = %q, want %q", got, "line one\nline two\n")
	}
}
