package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Tests for the /api/v1/secrets client. Values are write-only: they travel
// in the PUT body and no response ever returns them.

func TestListSecretsParsesItems(t *testing.T) {
	var gotMethod, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"items": [
			{"name": "API_TOKEN", "fingerprint": "Ab3d/0123456789ab",
			 "created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
			 "updated_by": "Ada"},
			{"name": "GITHUB_TOKEN", "fingerprint": "zz99/ba9876543210",
			 "created_at": "2026-10-19T09:00:00Z", "updated_at": "2026-10-19T09:00:00Z",
			 "updated_by": null}
		]}`)
	}))
	defer srv.Close()

	got, err := New(srv.URL, "orcaster_pat_abc").ListSecrets(context.Background())
	if err != nil {
		t.Fatalf("ListSecrets() error inesperado: %v", err)
	}
	if gotMethod != http.MethodGet || gotPath != "/api/v1/secrets" {
		t.Errorf("request = %s %s, want GET /api/v1/secrets", gotMethod, gotPath)
	}
	if len(got.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(got.Items))
	}
	first := got.Items[0]
	if first.Name != "API_TOKEN" || first.Fingerprint != "Ab3d/0123456789ab" {
		t.Errorf("item mal parseado: %+v", first)
	}
	if first.UpdatedBy == nil || *first.UpdatedBy != "Ada" {
		t.Errorf("updated_by = %v, want Ada", first.UpdatedBy)
	}
	if first.UpdatedAt.IsZero() || first.CreatedAt.IsZero() {
		t.Errorf("timestamps not parsed: %+v", first)
	}
	if got.Items[1].UpdatedBy != nil {
		t.Errorf("updated_by null should be nil, got %q", *got.Items[1].UpdatedBy)
	}
}

func TestListSecretsRawKeepsServerBytes(t *testing.T) {
	const body = `{"items":[{"name":"API_TOKEN","fingerprint":"f","created_at":"2026-10-20T10:00:00Z","updated_at":"2026-10-20T10:00:00Z","updated_by":null,"extra":1}]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, body)
	}))
	defer srv.Close()

	raw, err := New(srv.URL, "t").ListSecretsRaw(context.Background())
	if err != nil {
		t.Fatalf("ListSecretsRaw() error inesperado: %v", err)
	}
	if string(raw) != body {
		t.Errorf("raw = %s, want the server bytes verbatim", raw)
	}
}

func TestSetSecretSendsValueInBodyAndEscapesName(t *testing.T) {
	var gotMethod, gotEscapedPath, gotContentType string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotEscapedPath = r.URL.EscapedPath()
		gotContentType = r.Header.Get("Content-Type")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"name": "API_TOKEN", "fingerprint": "Ab3d/0123456789ab",
			"created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
			"updated_by": "Ada"}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "t")
	got, err := c.SetSecret(context.Background(), "API_TOKEN", "tok_s3cr3t", "")
	if err != nil {
		t.Fatalf("SetSecret() error inesperado: %v", err)
	}
	if gotMethod != http.MethodPut {
		t.Errorf("method = %s, want PUT", gotMethod)
	}
	if gotEscapedPath != "/api/v1/secrets/API_TOKEN" {
		t.Errorf("path = %q, want /api/v1/secrets/API_TOKEN", gotEscapedPath)
	}
	if gotContentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", gotContentType)
	}
	if len(gotBody) != 1 || gotBody["value"] != "tok_s3cr3t" {
		t.Errorf("body = %v, want only {\"value\": \"tok_s3cr3t\"}", gotBody)
	}
	if got.Fingerprint != "Ab3d/0123456789ab" {
		t.Errorf("fingerprint = %q", got.Fingerprint)
	}

	// A name with URL-reserved characters must not split the path: local
	// validation prevents it, but the client escapes anyway (defense in
	// depth; the server validates again).
	_, _ = c.SetSecret(context.Background(), "a/b?c", "v", "")
	if gotEscapedPath != "/api/v1/secrets/a%2Fb%3Fc" {
		t.Errorf("escaped path = %q, want /api/v1/secrets/a%%2Fb%%3Fc", gotEscapedPath)
	}
}

func TestDeleteSecretSendsDeleteAndTreats204AsSuccess(t *testing.T) {
	var gotMethod, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	if err := New(srv.URL, "t").DeleteSecret(context.Background(), "API_TOKEN", ""); err != nil {
		t.Fatalf("DeleteSecret() error inesperado: %v", err)
	}
	if gotMethod != http.MethodDelete || gotPath != "/api/v1/secrets/API_TOKEN" {
		t.Errorf("request = %s %s, want DELETE /api/v1/secrets/API_TOKEN", gotMethod, gotPath)
	}
}

func TestSecretsPropagateAPIErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"error": "task_secrets_unconfigured", "message": "no keys"}`)
	}))
	defer srv.Close()

	_, err := New(srv.URL, "t").ListSecrets(context.Background())
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("error = %v, want *APIError", err)
	}
	if apiErr.Status != http.StatusServiceUnavailable || apiErr.Code != "task_secrets_unconfigured" {
		t.Errorf("APIError = %+v", apiErr)
	}
}

// --- Per-worker scope ---------------------------------------------------------

func TestSetAndDeleteSecretSendWorkerQuery(t *testing.T) {
	var gotQuery []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = append(gotQuery, r.Method+" "+r.URL.Path+"?"+r.URL.RawQuery)
		if r.Method == http.MethodDelete {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"name": "GH", "fingerprint": "f",
			"created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-21T11:30:00Z",
			"updated_by": null, "worker_id": "0b6f1c1e-0000-4000-8000-000000000001",
			"worker_name": "worker-a"}`)
	}))
	defer srv.Close()

	c := New(srv.URL, "t")
	got, err := c.SetSecret(context.Background(), "GH", "v", "worker a")
	if err != nil {
		t.Fatalf("SetSecret() error inesperado: %v", err)
	}
	if got.WorkerID == nil || *got.WorkerID != "0b6f1c1e-0000-4000-8000-000000000001" {
		t.Errorf("worker_id = %v", got.WorkerID)
	}
	if got.WorkerName == nil || *got.WorkerName != "worker-a" {
		t.Errorf("worker_name = %v", got.WorkerName)
	}
	if _, err := c.SetSecret(context.Background(), "GH", "v", ""); err != nil {
		t.Fatalf("SetSecret() without worker: %v", err)
	}
	if err := c.DeleteSecret(context.Background(), "GH", "worker-a"); err != nil {
		t.Fatalf("DeleteSecret() error inesperado: %v", err)
	}
	if err := c.DeleteSecret(context.Background(), "GH", ""); err != nil {
		t.Fatalf("DeleteSecret() without worker: %v", err)
	}
	want := []string{
		"PUT /api/v1/secrets/GH?worker=worker+a",
		"PUT /api/v1/secrets/GH?",
		"DELETE /api/v1/secrets/GH?worker=worker-a",
		"DELETE /api/v1/secrets/GH?",
	}
	if len(gotQuery) != len(want) {
		t.Fatalf("requests = %v, want %v", gotQuery, want)
	}
	for i := range want {
		if gotQuery[i] != want[i] {
			t.Errorf("request %d = %q, want %q", i, gotQuery[i], want[i])
		}
	}
}

func TestListSecretsParsesWorkerScope(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"items": [
			{"name": "GH", "fingerprint": "f", "created_at": "2026-10-20T10:00:00Z",
			 "updated_at": "2026-10-20T10:00:00Z", "updated_by": null,
			 "worker_id": null, "worker_name": null},
			{"name": "GH", "fingerprint": "g", "created_at": "2026-10-20T10:00:00Z",
			 "updated_at": "2026-10-20T10:00:00Z", "updated_by": null,
			 "worker_id": "id-1", "worker_name": "worker-a"}
		], "keys_configured": true, "supports_worker_scope": true}`)
	}))
	defer srv.Close()

	got, err := New(srv.URL, "t").ListSecrets(context.Background())
	if err != nil {
		t.Fatalf("ListSecrets() error inesperado: %v", err)
	}
	if !got.WorkerScopeSupported() {
		t.Errorf("supports_worker_scope true was not read")
	}
	if got.Items[0].WorkerID != nil || got.Items[1].WorkerName == nil || *got.Items[1].WorkerName != "worker-a" {
		t.Errorf("items mal parseados: %+v", got.Items)
	}
	old := SecretListResponse{}
	if old.WorkerScopeSupported() {
		t.Errorf("a server without the field does not support per-worker scope")
	}
}
