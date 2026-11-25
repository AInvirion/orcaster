package cmd

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
)

// TestTokenPrefix covers the pure helper: never the full token, and it degrades
// gracefully for a short or empty token.
func TestTokenPrefix(t *testing.T) {
	cases := []struct {
		name  string
		token string
		want  string
	}{
		{"empty", "", "(none)"},
		{"short is fully masked", "abc", "(redacted)"},
		{"exactly 10 is fully masked", "0123456789", "(redacted)"},
		{"11 shows a prefix", "0123456789a", "0123456789…"},
		{"long is truncated to 10", "orcaster_pat_0123456789abcdef", "orcaster_p…"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := tokenPrefix(c.token); got != c.want {
				t.Errorf("tokenPrefix(%q) = %q, want %q", c.token, got, c.want)
			}
		})
	}
}

// mkTime builds an *api.Time from a time.Time (test helper).
func mkTime(tt time.Time) *api.Time {
	return &api.Time{Time: tt}
}

// TestNearExpiryNote covers the pure notice-decision helper, with `now` injected
// so it does not depend on the clock.
func TestNearExpiryNote(t *testing.T) {
	now := time.Date(2026, 8, 21, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name    string
		expires *api.Time
		want    string
	}{
		{"nil", nil, ""},
		{"zero", &api.Time{}, ""},
		{"far (30 days)", mkTime(now.Add(30 * 24 * time.Hour)), ""},
		{"just outside the window (8 days)", mkTime(now.Add(8 * 24 * time.Hour)), ""},
		{"window edge (7 days)", mkTime(now.Add(7 * 24 * time.Hour)), " (expires in 7 days!)"},
		{"3 days", mkTime(now.Add(3 * 24 * time.Hour)), " (expires in 3 days!)"},
		{"1 day", mkTime(now.Add(24 * time.Hour)), " (expires in 1 day!)"},
		{"less than a day", mkTime(now.Add(6 * time.Hour)), " (expires in less than a day!)"},
		{"already expired", mkTime(now.Add(-1 * time.Hour)), " (EXPIRED)"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := nearExpiryNote(c.expires, now); got != c.want {
				t.Errorf("nearExpiryNote(%v) = %q, want %q", c.expires, got, c.want)
			}
		})
	}
}

// TestRunAuthStatusInvalidTokenStillPrints pins that, with a token
// the server rejects (401), `auth status` must keep printing what it knows
// (host + token prefix) and explain why, but still return an error.
func TestRunAuthStatusInvalidTokenStillPrints(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"error":"unauthorized","message":"invalid or expired token"}`)
	}))
	defer srv.Close()

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")

	var buf bytes.Buffer
	authStatusCmd.SetOut(&buf)
	authStatusCmd.SetContext(context.Background())

	err := runAuthStatus(authStatusCmd, nil)
	if err == nil {
		t.Fatal("with an invalid token, runAuthStatus must return an error (exit != 0)")
	}
	out := buf.String()
	for _, want := range []string{"Profile:", "Host:", srv.URL, "Token:", "orcaster_p…", "rejected the token"} {
		if !strings.Contains(out, want) {
			t.Errorf("the output does not contain %q:\n%s", want, out)
		}
	}
	// The full token must NEVER appear.
	if strings.Contains(out, "orcaster_pat_0123456789abcdef") {
		t.Errorf("the output leaked the full token:\n%s", out)
	}
	// The env-origin suffix must reflect that host+token come from the environment.
	if !strings.Contains(out, "(host+token from env)") {
		t.Errorf("missing the env-origin suffix:\n%s", out)
	}
}

// TestRunAuthStatusNetworkErrorStillPrints: if validation cannot complete
// (unreachable host), the diagnostic block is still printed and the error is
// propagated. We use a closed server to force the connection failure.
func TestRunAuthStatusNetworkErrorStillPrints(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // the port is dead: the connection will fail

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", url)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")

	var buf bytes.Buffer
	authStatusCmd.SetOut(&buf)
	authStatusCmd.SetContext(context.Background())

	err := runAuthStatus(authStatusCmd, nil)
	if err == nil {
		t.Fatal("with the host down, runAuthStatus must return an error")
	}
	out := buf.String()
	for _, want := range []string{"Host:", "Token:", "could not validate token:"} {
		if !strings.Contains(out, want) {
			t.Errorf("the output does not contain %q:\n%s", want, out)
		}
	}
}

// TestRunAuthStatusSuccessShowsExpiryWarning: on success, with a token about to expire,
// the Expires line is printed with the warning annotation.
func TestRunAuthStatusSuccessShowsExpiryWarning(t *testing.T) {
	expires := time.Now().Add(3 * 24 * time.Hour).UTC().Format(time.RFC3339)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{
			"user": {"id": "u_1", "display_name": "Ada", "email": "ada@example.com"},
			"workspace": {"id": "ws_1", "slug": "acme", "name": "Acme"},
			"role": "owner",
			"token": {"label": "my-laptop", "last_used_at": "2026-06-02T10:23:00Z", "expires_at": "`+expires+`"}
		}`)
	}))
	defer srv.Close()

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")

	var buf bytes.Buffer
	authStatusCmd.SetOut(&buf)
	authStatusCmd.SetContext(context.Background())

	if err := runAuthStatus(authStatusCmd, nil); err != nil {
		t.Fatalf("runAuthStatus() unexpected error: %v", err)
	}
	out := buf.String()
	// The exact day count depends on truncation to whole days (floor) against the
	// real clock, so the warning annotation is checked without fixing the number.
	for _, want := range []string{"User:      Ada", "Expires:", "(expires in ", "days!)"} {
		if !strings.Contains(out, want) {
			t.Errorf("the output does not contain %q:\n%s", want, out)
		}
	}
}

// TestRunAuthStatusSuccessOmitsExpiryWhenAbsent: an old server that does not send
// expires_at (nil) must not produce an Expires line (nor a false "never").
func TestRunAuthStatusSuccessOmitsExpiryWhenAbsent(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{
			"user": {"id": "u_1", "display_name": "Ada", "email": "ada@example.com"},
			"workspace": {"id": "ws_1", "slug": "acme", "name": "Acme"},
			"role": "owner",
			"token": {"label": "my-laptop", "last_used_at": "2026-06-02T10:23:00Z"}
		}`)
	}))
	defer srv.Close()

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("ORCASTER_HOST", srv.URL)
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_0123456789abcdef")

	var buf bytes.Buffer
	authStatusCmd.SetOut(&buf)
	authStatusCmd.SetContext(context.Background())

	if err := runAuthStatus(authStatusCmd, nil); err != nil {
		t.Fatalf("runAuthStatus() unexpected error: %v", err)
	}
	if out := buf.String(); strings.Contains(out, "Expires:") {
		t.Errorf("without expires_at there should be no Expires line:\n%s", out)
	}
}
