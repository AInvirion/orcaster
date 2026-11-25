package cmd

// Tests for multiline values: a multiline value pasted into the one-line prompt
// is not stored truncated, the help explains how to set one, and `secret delete`
// against a server without the secrets API does not say "not found".

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestReadSecretValueTTYRejectsMultilineCutToOneLine(t *testing.T) {
	// The prompt returns only the first line of what was pasted.
	for _, first := range []string{
		"-----BEGIN PRIVATE KEY-----\n",
		"-----BEGIN OPENSSH PRIVATE KEY-----\r\n",
		"  -----BEGIN RSA PRIVATE KEY-----MIIEvQIB-----END RSA PRIVATE KEY-----",
		"{\n",
		"[\n",
		" { \n",
	} {
		_, err := readSecretValue(failOnReadReader{t}, true, func() ([]byte, error) {
			return []byte(first), nil
		})
		if !errors.Is(err, errPromptMultiline) {
			t.Errorf("TTY %q: error = %v, want errPromptMultiline", first, err)
		}
	}
}

func TestReadSecretValueTTYAcceptsOneLineValues(t *testing.T) {
	for _, v := range []string{"tok_abc123", `{"token": "x"}`, "[1, 2]", "-BEGIN"} {
		got, err := readSecretValue(failOnReadReader{t}, true, func() ([]byte, error) {
			return []byte(v + "\n"), nil
		})
		if err != nil || got != v {
			t.Errorf("TTY %q = %q, %v; want the value as is", v, got, err)
		}
	}
}

func TestReadSecretValuePipeKeepsMultilinePEM(t *testing.T) {
	pem := "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----"
	got, err := readSecretValue(strings.NewReader(pem+"\n"), false, nil)
	if err != nil || got != pem {
		t.Errorf("pipe = %q, %v; want the whole PEM", got, err)
	}
}

func TestPromptMultilineErrorPointsToPipe(t *testing.T) {
	err := promptMultilineError("SSH_KEY", nil)
	msg := err.Error()
	for _, want := range []string{"one line", "orcaster secret set SSH_KEY < file", "nothing was changed"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q does not contain %q", msg, want)
		}
	}
	if strings.Contains(msg, "terminal input") {
		t.Errorf("with a successful discard it must not warn about the rest: %q", msg)
	}
	// If the rest of the pasted text could not be discarded, the shell will read it.
	msg = promptMultilineError("SSH_KEY", errors.New("ioctl")).Error()
	if !strings.Contains(msg, "terminal input") {
		t.Errorf("without a discard it must warn that the rest stayed in the terminal: %q", msg)
	}
}

func TestSecretSetHelpExplainsMultilineValues(t *testing.T) {
	c := newSecretCmd()
	set, _, err := c.Find([]string{"set"})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"one line", "orcaster secret set NAME < file"} {
		if !strings.Contains(set.Long, want) {
			t.Errorf("set help does not contain %q:\n%s", want, set.Long)
		}
	}
}

// oldServer404 answers 404 to everything, like a server that predates the
// secrets API (FastAPI without the route, /api/v1 error model).
func oldServer404(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNotFound)
	_, _ = io.WriteString(w, `{"error": "not_found", "message": "Not Found"}`)
}

func TestSecretDeleteAgainstServerWithoutSecretsAPI(t *testing.T) {
	r := runSecretCmd(t, oldServer404, "", "delete", "API_TOKEN", "-y")
	if r.err == nil {
		t.Fatal("want an error")
	}
	if strings.Contains(r.err.Error(), "not found") {
		t.Errorf("error = %q; must not say 'not found' about the secret", r.err)
	}
	if !strings.Contains(r.err.Error(), "does not support task secrets") {
		t.Errorf("error = %q; want 'does not support task secrets'", r.err)
	}
}

func TestSecretDeleteProbeFailureKeepsServerError(t *testing.T) {
	// The DELETE returns 404 and the list probe fails for another reason: it is
	// unknown whether the API exists, so neither "not found" nor "not supported"
	// is invented.
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			w.WriteHeader(http.StatusBadGateway)
			_, _ = io.WriteString(w, `{"error": "bad_gateway", "message": "upstream down"}`)
			return
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error": "not_found", "message": "secret not found"}`)
	}
	r := runSecretCmd(t, handler, "", "delete", "API_TOKEN", "-y")
	if r.err == nil {
		t.Fatal("want an error")
	}
	msg := r.err.Error()
	if strings.Contains(msg, "API_TOKEN not found") || strings.Contains(msg, "does not support") {
		t.Errorf("error = %q; with a failed probe it must conclude nothing", msg)
	}
	if !strings.Contains(msg, "secret not found") {
		t.Errorf("error = %q; want the original server error", msg)
	}
}

func TestSecretSetAndListAgainstServerWithoutSecretsAPI(t *testing.T) {
	for _, args := range [][]string{{"set", "API_TOKEN"}, {"list"}} {
		r := runSecretCmd(t, oldServer404, "value\n", args...)
		if r.err == nil || !strings.Contains(r.err.Error(), "does not support task secrets") {
			t.Errorf("%v: error = %v; want 'does not support task secrets'", args, r.err)
		}
	}
}
