package cmd

// Tests for `orcaster secret ... --worker`. With --worker the CLI first queries
// GET /secrets: an older server does not advertise supports_worker_scope and
// would ignore the query param, so it would silently set or delete the
// workspace secret. In that case the CLI refuses without writing anything.

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

const workerSecretItemJSON = `{"name": "GITHUB_TOKENS", "fingerprint": "Ab3d/0123456789ab",
	"created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
	"updated_by": "Ada", "worker_id": "0b6f1c1e-0000-4000-8000-000000000001",
	"worker_name": "worker-a"}`

const listSupportsWorkerScope = `{"items": [], "keys_configured": true, "supports_worker_scope": true}`

// workerScopeServer answers the GET /secrets preflight with listBody and
// delegates the rest to next, recording each request as "METHOD path?query".
func workerScopeServer(t *testing.T, listBody string, next http.HandlerFunc, seen *[]string) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		*seen = append(*seen, r.Method+" "+r.URL.Path+"?"+r.URL.RawQuery)
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/secrets" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, listBody)
			return
		}
		next(w, r)
	}
}

func TestSecretSetWithWorkerSendsQueryAndReportsScope(t *testing.T) {
	const value = "ghp_worker-a_value"
	var seen []string
	var gotBody map[string]any
	handler := workerScopeServer(t, listSupportsWorkerScope, func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, workerSecretItemJSON)
	}, &seen)
	r := runSecretCmd(t, handler, value+"\n", "set", "GITHUB_TOKENS", "--worker", "worker-a")
	if r.err != nil {
		t.Fatalf("set: %v", r.err)
	}
	want := []string{"GET /api/v1/secrets?", "PUT /api/v1/secrets/GITHUB_TOKENS?worker=worker-a"}
	if strings.Join(seen, "|") != strings.Join(want, "|") {
		t.Errorf("requests = %v, want %v", seen, want)
	}
	if gotBody["value"] != value {
		t.Errorf("value in the body = %q", gotBody["value"])
	}
	assertNoLeak(t, r, value)
	assertGolden(t, "secret_set_worker", []byte(r.stdout))
}

func TestSecretSetWithWorkerRefusesOnOldServer(t *testing.T) {
	var seen []string
	handler := workerScopeServer(t, `{"items": [], "keys_configured": true}`,
		func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("must not write to an old server: %s %s", r.Method, r.URL.Path)
		}, &seen)
	r := runSecretCmd(t, handler, "v\n", "set", "GITHUB_TOKENS", "--worker", "worker-a")
	if r.err == nil || !strings.Contains(r.err.Error(), "does not support worker-scoped secrets") {
		t.Fatalf("error = %v, want a refusal because of the old server", r.err)
	}
	if !strings.Contains(r.err.Error(), "nothing was changed") {
		t.Errorf("error = %v, want it to state that nothing changed", r.err)
	}
}

func TestSecretSetEmptyWorkerIsRejectedLocally(t *testing.T) {
	r := runSecretCmd(t, noRequest(t), "v\n", "set", "GH", "--worker", "")
	if r.err == nil || !strings.Contains(r.err.Error(), "--worker") {
		t.Fatalf("error = %v, want an empty --worker to be rejected", r.err)
	}
}

// TestSecretWorkerWithSurroundingWhitespaceIsRejectedLocally: worker names are
// not trimmed on registration, so "worker-a " and "worker-a" are different
// workers. A --worker with surrounding whitespace is rejected before any
// request; it is never trimmed.
func TestSecretWorkerWithSurroundingWhitespaceIsRejectedLocally(t *testing.T) {
	for _, ref := range []string{"worker-a ", " worker-a", "   ", "worker-a\t"} {
		r := runSecretCmd(t, noRequest(t), "v\n", "set", "GH", "--worker", ref)
		if r.err == nil || !strings.Contains(r.err.Error(), "--worker") {
			t.Fatalf("set --worker %q: error = %v, want local rejection", ref, r.err)
		}
		r = runSecretCmd(t, noRequest(t), "", "delete", "GH", "--worker", ref, "--yes")
		if r.err == nil || !strings.Contains(r.err.Error(), "--worker") {
			t.Fatalf("delete --worker %q: error = %v, want local rejection", ref, r.err)
		}
	}
}

func TestSecretSetWorkerErrorsAreExplained(t *testing.T) {
	cases := []struct {
		status     int
		code, want string
	}{
		{http.StatusNotFound, "worker_not_found", "worker worker-z not found in this workspace"},
		{http.StatusConflict, "worker_ambiguous", "pass the worker id"},
		{http.StatusConflict, "worker_deleted", "worker worker-z is deleted"},
	}
	for _, tc := range cases {
		t.Run(tc.code, func(t *testing.T) {
			var seen []string
			handler := workerScopeServer(t, listSupportsWorkerScope, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, `{"error": "`+tc.code+`", "message": "x"}`)
			}, &seen)
			r := runSecretCmd(t, handler, "v\n", "set", "GH", "--worker", "worker-z")
			if r.err == nil || !strings.Contains(r.err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", r.err, tc.want)
			}
		})
	}
}

func TestSecretDeleteWithWorker(t *testing.T) {
	var seen []string
	handler := workerScopeServer(t, listSupportsWorkerScope, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}, &seen)
	r := runSecretCmd(t, handler, "", "delete", "GITHUB_TOKENS", "--worker", "worker-a", "--yes")
	if r.err != nil {
		t.Fatalf("delete: %v", r.err)
	}
	want := []string{"GET /api/v1/secrets?", "DELETE /api/v1/secrets/GITHUB_TOKENS?worker=worker-a"}
	if strings.Join(seen, "|") != strings.Join(want, "|") {
		t.Errorf("requests = %v, want %v", seen, want)
	}
	assertGolden(t, "secret_delete_worker", []byte(r.stdout))
}

func TestSecretDeleteWithWorkerRefusesOnOldServer(t *testing.T) {
	var seen []string
	handler := workerScopeServer(t, `{"items": []}`, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("must not delete on an old server: %s %s", r.Method, r.URL.Path)
	}, &seen)
	r := runSecretCmd(t, handler, "", "delete", "GH", "--worker", "worker-a", "--yes")
	if r.err == nil || !strings.Contains(r.err.Error(), "does not support worker-scoped secrets") {
		t.Fatalf("error = %v, want a refusal because of the old server", r.err)
	}
}

func TestSecretDeleteWithWorker404NamesTheWorker(t *testing.T) {
	var seen []string
	handler := workerScopeServer(t, listSupportsWorkerScope, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"error": "not_found", "message": "secret not found"}`)
	}, &seen)
	r := runSecretCmd(t, handler, "", "delete", "GH", "--worker", "worker-a", "-y")
	if r.err == nil || r.err.Error() != "secret GH not found for worker worker-a" {
		t.Fatalf("error = %v, want 'secret GH not found for worker worker-a'", r.err)
	}
}

func TestSecretDeleteWithoutWorkerSkipsPreflight(t *testing.T) {
	var seen []string
	handler := workerScopeServer(t, listSupportsWorkerScope, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}, &seen)
	r := runSecretCmd(t, handler, "", "delete", "GH", "--yes")
	if r.err != nil {
		t.Fatalf("delete: %v", r.err)
	}
	if strings.Join(seen, "|") != "DELETE /api/v1/secrets/GH?" {
		t.Errorf("requests = %v, want only the DELETE", seen)
	}
}

const secretListWorkerJSON = `{"items": [
	{"name": "API_TOKEN", "fingerprint": "Ab3d/0123456789ab",
	 "created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
	 "updated_by": "Ada", "worker_id": null, "worker_name": null},
	{"name": "GITHUB_TOKENS", "fingerprint": "zz99/ba9876543210",
	 "created_at": "2026-10-19T09:00:00Z", "updated_at": "2026-10-19T09:00:00Z",
	 "updated_by": null, "worker_id": "id-1", "worker_name": "worker-a"},
	{"name": "GITHUB_TOKENS", "fingerprint": "yy88/ba9876543210",
	 "created_at": "2026-10-19T09:00:00Z", "updated_at": "2026-10-19T09:00:00Z",
	 "updated_by": null, "worker_id": "id-2", "worker_name": null}
], "keys_configured": true, "supports_worker_scope": true}`

func TestGoldenSecretListWorkerScope(t *testing.T) {
	r := runSecretCmd(t, jsonResponder(t, "/api/v1/secrets", secretListWorkerJSON), "", "list")
	if r.err != nil {
		t.Fatalf("list: %v", r.err)
	}
	assertGolden(t, "secret_list_worker_scope", []byte(r.stdout))
}
