package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Version is injected at build time (ldflags) and goes in the User-Agent. Default dev.
var Version = "dev"

// Client talks to /api/v1. host is the origin without a path (e.g. https://orca.example.com);
// token is the plain PAT that goes in every request as a Bearer.
//
// Python->Go glossary: this is the equivalent of a class with __init__(self, host, token)
// that holds an httpx.Client. `*http.Client` with a timeout replaces requests/httpx.
type Client struct {
	host  string
	token string
	http  *http.Client
}

// New builds a Client. It normalizes the host by trimming the trailing "/" so
// path joining is predictable.
func New(host, token string) *Client {
	return &Client{
		host:  strings.TrimRight(host, "/"),
		token: token,
		http:  &http.Client{Timeout: 30 * time.Second},
	}
}

// ListOptions are the filters of GET /tasks. Cursor is *int because "no cursor"
// (first page) is different from "cursor 0": nil == absent.
//
// Variant filters by task variant ("all", "other", "<kind>" or "<kind>~<label>");
// empty string = absent, and without a filter the parameter is NOT sent: the
// default ("all") is set by the server. It is worth not sending it even though
// today absent and empty resolve the same (both to "all", the default_mode that
// each /api/v1 endpoint declares at its own call site): the reason is simply not
// to send a parameter the user did not set. IncludePayload=false does not travel
// either: it is an opt-in projection.
type ListOptions struct {
	Status         []string
	Limit          int
	Cursor         *int
	Variant        string
	IncludePayload bool
}

// FailuresOptions are the filters of GET /insights/failures. Since/Until are
// ISO 8601 strings (empty string = absent); a date without a timezone offset is
// interpreted by the server as UTC. Limit bounds by_signature (range 1..1000,
// validated server-side).
type FailuresOptions struct {
	Since string
	Until string
	Limit int
}

// do executes a request, injects auth/headers and centralizes error handling:
// status >= 400 -> *APIError parsed from the JSON body; 2xx -> decodes into out.
// body is serialized to JSON if != nil (nil = request without a body, e.g. GET).
// out may be nil for responses whose body is of no interest.
func (c *Client) do(ctx context.Context, method, path string, query url.Values, body, out any) error {
	u := c.host + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}

	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("serializing body: %w", err)
		}
		reader = bytes.NewReader(raw)
	}

	req, err := http.NewRequestWithContext(ctx, method, u, reader)
	if err != nil {
		return fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "orcaster/"+Version)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("connecting to %s: %w", c.host, err)
	}
	defer resp.Body.Close() //nolint:errcheck // best-effort body close

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("reading respuesta: %w", err)
	}

	if resp.StatusCode >= 400 {
		apiErr := &APIError{Status: resp.StatusCode}
		// The body should be the JSON error model; if it does not parse, we leave
		// a generic message with the status so no information is lost.
		if jsonErr := json.Unmarshal(respBody, apiErr); jsonErr != nil || apiErr.Code == "" {
			apiErr.Code = http.StatusText(resp.StatusCode)
			if apiErr.Message == "" {
				apiErr.Message = fmt.Sprintf("HTTP %d", resp.StatusCode)
			}
		}
		return apiErr
	}

	if out != nil {
		if err := json.Unmarshal(respBody, out); err != nil {
			return fmt.Errorf("parsing JSON response: %w", err)
		}
	}
	return nil
}

// Whoami resolves the identity of the active PAT (GET /auth/whoami).
func (c *Client) Whoami(ctx context.Context) (*WhoamiResponse, error) {
	var out WhoamiResponse
	if err := c.do(ctx, http.MethodGet, "/api/v1/auth/whoami", nil, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// listTasksQuery translates the options into query params. Extracted so the typed
// and the raw variant build EXACTLY the same request (if they diverge, `--json`
// and the table would stop showing the same page).
func listTasksQuery(opts ListOptions) url.Values {
	q := url.Values{}
	for _, s := range opts.Status {
		q.Add("status", s) // repeatable: ?status=a&status=b
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != nil {
		q.Set("cursor", strconv.Itoa(*opts.Cursor))
	}
	if opts.Variant != "" {
		q.Set("variant", opts.Variant)
	}
	if opts.IncludePayload {
		q.Set("include_payload", "true")
	}
	return q
}

// ListTasks returns a page of tasks of the PAT's workspace (GET /tasks).
// It feeds the TABLE. For `--json` use ListTasksRaw: this struct loses the fields
// it does not declare yet and fills with null the ones the server omitted.
func (c *Client) ListTasks(ctx context.Context, opts ListOptions) (*TaskListResponse, error) {
	var out TaskListResponse
	if err := c.do(ctx, http.MethodGet, "/api/v1/tasks", listTasksQuery(opts), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListTasksRaw returns the SAME page as ListTasks, but as the bytes exactly as
// the server sent them, for the `--json` output.
//
// Why raw and not the struct: the v1 response is opt-in per field. `/tasks`
// emits `payload` only with `include_payload=true`, and `/tasks/{id}/children`
// omits `variant_kind`/`variant_label` because its query does not project them.
// Deserializing and re-serializing destroys that distinction in both directions:
// absent fields come back out as `null` (a fork with a variant would read as if
// it had none) and any field the server adds that this struct does not declare is
// silently lost. An automated consumer has no way to notice either.
//
// Python->Go glossary: json.RawMessage is []byte with its own marshalers, so
// `do` checks that it is well-formed JSON and copies the bytes without
// interpreting them (~ reading response.content instead of response.json()).
func (c *Client) ListTasksRaw(ctx context.Context, opts ListOptions) (json.RawMessage, error) {
	var out json.RawMessage
	if err := c.do(ctx, http.MethodGet, "/api/v1/tasks", listTasksQuery(opts), nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// childrenQuery: pagination of the children. Shared by the typed and the raw
// variant, for the same reason as listTasksQuery.
func childrenQuery(limit int, cursor *int) url.Values {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	if cursor != nil {
		q.Set("cursor", strconv.Itoa(*cursor))
	}
	return q
}

// ListTaskChildren returns a page of a task's direct children/forks
// (GET /tasks/{id}/children). Same paginated shape as ListTasks (TaskListResponse),
// but without the status filter: only limit/cursor. 404 if the parent does not
// exist or is cross-tenant. Cursor is *int for the same reason as ListOptions:
// nil == first page. It feeds the TABLE; for `--json` use ListTaskChildrenRaw.
func (c *Client) ListTaskChildren(ctx context.Context, id int, limit int, cursor *int) (*TaskListResponse, error) {
	var out TaskListResponse
	if err := c.do(ctx, http.MethodGet, taskPath(id, "/children"), childrenQuery(limit, cursor), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListTaskChildrenRaw: the raw bytes of the children page, for `--json`.
// Here raw matters more than in the list: this route does NOT project the variant,
// so re-serializing the struct would add `variant_kind: null` to forks that do
// have a variant. See the comment on ListTasksRaw.
func (c *Client) ListTaskChildrenRaw(ctx context.Context, id int, limit int, cursor *int) (json.RawMessage, error) {
	var out json.RawMessage
	if err := c.do(ctx, http.MethodGet, taskPath(id, "/children"), childrenQuery(limit, cursor), nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// taskPath builds "/api/v1/tasks/{id}" (+ an optional suffix such as "/cancel").
func taskPath(id int, suffix string) string {
	return "/api/v1/tasks/" + strconv.Itoa(id) + suffix
}

// GetTask returns the full detail of a task (GET /tasks/{id}). 404 if cross-tenant.
func (c *Client) GetTask(ctx context.Context, id int) (*TaskDetail, error) {
	var out TaskDetail
	if err := c.do(ctx, http.MethodGet, taskPath(id, ""), nil, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// TaskOutput downloads a task's output (log) as a stream (GET
// /tasks/{id}/output). It does not use do(): do() sets Accept: application/json
// and deserializes the body, whereas here the 200 is a binary stream that the
// caller copies. attempt nil = latest attempt; format ∈ {text, jsonl}.
// The body is streamed without the client's global Timeout (see streamGet);
// cancellation goes through the context.
func (c *Client) TaskOutput(
	ctx context.Context, id int, attempt *int, format string,
) (io.ReadCloser, error) {
	q := url.Values{}
	q.Set("format", format)
	if attempt != nil {
		q.Set("attempt", strconv.Itoa(*attempt))
	}
	return c.streamGet(ctx, c.host+taskPath(id, "/output")+"?"+q.Encode())
}

// ExecutionOutput downloads the full log (stdout+stderr+command) of an
// execution as a stream (GET /api/v1/executions/{id}/output), to drill down
// on a signature from the failures endpoint or any known execution.
// executionID is the execution's UUID; format ∈ {text, jsonl}.
// 404 if the execution does not exist, the UUID is invalid or it belongs to
// another workspace. Same stream mechanics as TaskOutput (see streamGet).
func (c *Client) ExecutionOutput(
	ctx context.Context, executionID, format string,
) (io.ReadCloser, error) {
	q := url.Values{}
	q.Set("format", format)
	u := c.host + "/api/v1/executions/" + url.PathEscape(executionID) + "/output?" + q.Encode()
	return c.streamGet(ctx, u)
}

// streamGet executes a GET whose 200 is a binary stream (a log), not JSON:
// unlike do() it does not set Accept: application/json nor deserialize the body;
// the caller does io.Copy and Close on the returned ReadCloser. Errors (>=400) are
// parsed as *APIError, same as do().
//
// The log stream can be large: the *http.Client's global Timeout (30s) covers the
// whole body read and would abort long downloads halfway. We use a derived client
// without a Timeout for this request; cancellation is still available through the
// context. The rest of the Transport config (proxy, TLS, etc.) is inherited
// because it is the same base http.Client.
//
// Go/gzip gotcha: we do NOT set Accept-Encoding. The http.Transport adds
// `Accept-Encoding: gzip` by default and decompresses transparently; if we set it
// by hand, Go disables that decompression and we would have to gunzip manually.
// The server always answers gzip, so we let Go handle it.
func (c *Client) streamGet(ctx context.Context, fullURL string) (io.ReadCloser, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fullURL, nil)
	if err != nil {
		return nil, fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("User-Agent", "orcaster/"+Version)

	streamClient := *c.http
	streamClient.Timeout = 0
	resp, err := streamClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("connecting to %s: %w", c.host, err)
	}
	if resp.StatusCode >= 400 {
		defer resp.Body.Close() //nolint:errcheck // best-effort body close
		respBody, _ := io.ReadAll(resp.Body)
		apiErr := &APIError{Status: resp.StatusCode}
		if jsonErr := json.Unmarshal(respBody, apiErr); jsonErr != nil || apiErr.Code == "" {
			apiErr.Code = http.StatusText(resp.StatusCode)
			if apiErr.Message == "" {
				apiErr.Message = fmt.Sprintf("HTTP %d", resp.StatusCode)
			}
		}
		return nil, apiErr
	}
	// 200: the caller does io.Copy and Close on the body.
	return resp.Body, nil
}

// CreateTask creates a task (POST /tasks). body is a map with the fields of the
// public contract (a subset of AdminCreateTaskRequest); the server validates
// with extra="forbid", so an unrecognized field returns 422.
func (c *Client) CreateTask(ctx context.Context, body map[string]any) (*CreateTaskResponse, error) {
	var out CreateTaskResponse
	if err := c.do(ctx, http.MethodPost, "/api/v1/tasks", nil, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ForkTask forks a task with a server-side deep-merge (POST /tasks/{id}/fork).
// body carries ONLY the overrides; whatever is not sent is inherited from the
// parent. 404 if the parent does not exist or is cross-tenant.
func (c *Client) ForkTask(ctx context.Context, id int, body map[string]any) (*CreateTaskResponse, error) {
	var out CreateTaskResponse
	if err := c.do(ctx, http.MethodPost, taskPath(id, "/fork"), nil, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CancelTask cancels a task (POST /tasks/{id}/cancel). Empty reason = no reason.
// 409 if the task is already in a terminal state, 404 if cross-tenant/nonexistent.
func (c *Client) CancelTask(ctx context.Context, id int, reason string) (*CancelTaskResponse, error) {
	body := map[string]any{}
	if reason != "" {
		body["reason"] = reason
	}
	var out CancelTaskResponse
	if err := c.do(ctx, http.MethodPost, taskPath(id, "/cancel"), nil, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// DeleteTask deletes a terminal task (DELETE /tasks/{id}). The server answers
// 204 No Content with no body if it deleted (out nil = do() ignores the body);
// 409 if the task is not terminal and 404 if it does not exist / is cross-tenant,
// both propagated as *APIError. The deletion is a hard-delete with CASCADE of
// executions and artifacts.
func (c *Client) DeleteTask(ctx context.Context, id int) error {
	return c.do(ctx, http.MethodDelete, taskPath(id, ""), nil, nil, nil)
}

// BulkDeleteTasks deletes several tasks in one request (POST /tasks/bulk/delete).
// It is best-effort: the server classifies each id as deleted / skipped (not
// terminal) / not_found and returns 200. Out-of-range ids (1-500), non-positive
// ones or duplicates produce a 422 propagated as *APIError.
func (c *Client) BulkDeleteTasks(ctx context.Context, ids []int) (*BulkDeleteResponse, error) {
	var out BulkDeleteResponse
	body := BulkDeleteRequest{TaskIDs: ids}
	if err := c.do(ctx, http.MethodPost, "/api/v1/tasks/bulk/delete", nil, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// FailuresInsights aggregates the errors of the failed tasks of the PAT's
// workspace (GET /insights/failures). Optional filters by completed_at
// (since/until) and a limit on signature groups. A 422 (invalid date or limit)
// is propagated as *APIError with the server's detail.
func (c *Client) FailuresInsights(ctx context.Context, opts FailuresOptions) (*FailuresResponse, error) {
	q := url.Values{}
	if opts.Since != "" {
		q.Set("since", opts.Since)
	}
	if opts.Until != "" {
		q.Set("until", opts.Until)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	var out FailuresResponse
	if err := c.do(ctx, http.MethodGet, "/api/v1/insights/failures", q, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// RetryTask retries a terminal task in place (POST /tasks/{id}/retry).
// forceSameWorker (default false, same on the server and in the CLI) tries the
// same worker as the last attempt; with false the task goes back to the general
// queue. 409 if the task is not retryable, 404 if cross-tenant/nonexistent.
func (c *Client) RetryTask(ctx context.Context, id int, forceSameWorker bool) (*RetryTaskResponse, error) {
	body := map[string]any{"force_same_worker": forceSameWorker}
	var out RetryTaskResponse
	if err := c.do(ctx, http.MethodPost, taskPath(id, "/retry"), nil, body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// secretPath builds /api/v1/secrets/{name} escaping the name. The CLI already
// validates the name locally, but escaping still keeps an odd name from breaking
// the path.
func secretPath(name string) string {
	return "/api/v1/secrets/" + url.PathEscape(name)
}

// ListSecrets lists the workspace's task secrets (GET /secrets): name,
// fingerprint and dates, never the value.
func (c *Client) ListSecrets(ctx context.Context) (*SecretListResponse, error) {
	var out SecretListResponse
	if err := c.do(ctx, http.MethodGet, "/api/v1/secrets", nil, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListSecretsRaw makes the same GET as ListSecrets but returns the server's bytes
// without going through the structs, for `--json` (same criterion as
// ListTasksRaw: do not lose keys the binary does not know yet).
func (c *Client) ListSecretsRaw(ctx context.Context) (json.RawMessage, error) {
	var out json.RawMessage
	if err := c.do(ctx, http.MethodGet, "/api/v1/secrets", nil, nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// secretWorkerQuery builds `?worker=`; nil without a worker, so the request is
// the same as it was before the per-worker scope.
func secretWorkerQuery(worker string) url.Values {
	if worker == "" {
		return nil
	}
	return url.Values{"worker": {worker}}
}

// SetSecret creates or replaces a secret (PUT /secrets/{name}). The value travels
// only in the JSON body; the response carries the fingerprint, not the value. 403
// if the PAT is not the owner's; 503 task_secrets_unconfigured if the server has
// no keys. worker (id or name) sets that worker's secret; "" the workspace one.
func (c *Client) SetSecret(ctx context.Context, name, value, worker string) (*Secret, error) {
	body := map[string]string{"value": value}
	var out Secret
	if err := c.do(ctx, http.MethodPut, secretPath(name), secretWorkerQuery(worker), body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// DeleteSecret deletes a secret (DELETE /secrets/{name}). 204 with no body if it
// deleted; 404 if it does not exist, propagated as *APIError. worker (id or name)
// deletes that worker's secret; "" only the workspace one.
func (c *Client) DeleteSecret(ctx context.Context, name, worker string) error {
	return c.do(ctx, http.MethodDelete, secretPath(name), secretWorkerQuery(worker), nil, nil)
}
