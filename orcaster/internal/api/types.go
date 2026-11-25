// Package api contains orcaster's HTTP client for /api/v1 and the structs that
// mirror the server's JSON contract. It shares no code with the Python server:
// the types are its own, deliberately decoupled.
//
// Python->Go glossary: a `struct` with `json:"..."` tags is the equivalent of a
// Pydantic model, but WITHOUT runtime validation: `encoding/json` only maps field
// names. Pointers (`*int`, `*string`, a wrapped `Time`) model the JSON's
// `null`/optional fields: nil == absent/null.
package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Time wraps time.Time to tolerate the two formats the server can emit: RFC3339
// with an offset (Postgres timestamptz, e.g. "2026-06-02T10:23:00Z") and the
// zone-less naive one that Pydantic sometimes produces ("2026-06-02T10:23:00").
// A bare time.Time only accepts RFC3339 when deserializing JSON, so without this
// a naive timestamp would break parsing. (In Python, datetime.fromisoformat
// absorbs this.)
type Time struct{ time.Time }

// layouts tried in order; the first one that parses wins.
var timeLayouts = []string{
	time.RFC3339Nano,
	time.RFC3339,
	"2006-01-02T15:04:05.999999", // naive with microseconds (Pydantic)
	"2006-01-02T15:04:05",        // naive without a fraction
}

// UnmarshalJSON implements the json.Unmarshaler interface (like __init__ from
// JSON in Pydantic). `null` -> zero value (IsZero() == true).
func (t *Time) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	if s == "" || s == "null" {
		t.Time = time.Time{}
		return nil
	}
	for _, layout := range timeLayouts {
		if parsed, err := time.Parse(layout, s); err == nil {
			t.Time = parsed
			return nil
		}
	}
	// Last resort: leave the zero value instead of breaking the whole response.
	t.Time = time.Time{}
	return nil
}

// --- /api/v1/auth/whoami ---------------------------------------------------

type WhoamiUser struct {
	ID          string `json:"id"`
	DisplayName string `json:"display_name"`
	Email       string `json:"email"`
}

type WhoamiWorkspace struct {
	ID   string `json:"id"`
	Slug string `json:"slug"`
	Name string `json:"name"`
}

type WhoamiToken struct {
	Label      string `json:"label"`
	LastUsedAt *Time  `json:"last_used_at"`
	// ExpiresAt is the PAT's expiry instant. A pointer like LastUsedAt:
	// nil == absent/null, either because the token does not expire or because the
	// server is old and does not project the field yet. `auth status`/`login` use
	// it to warn about an upcoming expiry.
	ExpiresAt *Time `json:"expires_at"`
}

// WhoamiResponse is the identity resolved by the PAT. Basis of `auth login`/`status`.
type WhoamiResponse struct {
	User      WhoamiUser      `json:"user"`
	Workspace WhoamiWorkspace `json:"workspace"`
	Role      string          `json:"role"`
	Token     WhoamiToken     `json:"token"`
}

// --- /api/v1/tasks ---------------------------------------------------------

// TaskSummary is a row of `GET /tasks`. done/total derive from the executions;
// target_count is the planned fan-out. parent_task_id is null on root tasks.
//
// Outcome is the nuance of the terminal close: null = ordinary close, a name
// (e.g. `no_result`) = the task closed completed with that nuance. Without it, a
// consumer cannot tell a real completed from an empty one. The three task reads
// (list, children and detail) project it, so it has no omitempty.
//
// VariantKind/VariantLabel are pointers because a task without a variant carries
// them as null; no omitempty, like ParentTaskID, because the server always sends
// them (against an old server that does not know them they stay nil and come out
// as null).
//
// Careful: this struct is NOT the source of the `--json` output of the list nor of
// the children. Those two routes print the server's raw bytes (see ListTasksRaw),
// because re-serializing here invents keys the server omitted on purpose and drops
// the ones this struct does not know yet.
//
// Payload only comes if the request asked for ?include_payload=true, so it has
// omitempty so that rows without it do not clutter the `--json` output.
//
// It is *map and not map on purpose: `omitempty` on a bare map also omits the
// EMPTY map, and the server sends `"payload": {}` for a task without a payload
// (it normalizes the NULL, see `_coerce_dict`). With the bare map those rows came
// out of `--json` without the key, indistinguishable from "I did not ask for the
// payload". With the pointer, nil == absent and a non-nil empty map is serialized
// as `{}`, faithful to the response.
//
// Embedding gotcha: TaskDetail declares its own Payload and encoding/json
// resolves the clash by depth: TaskDetail's field (depth 0) beats the one promoted
// from here (depth 1), whatever the type. That is, in a detail this field stays
// nil and the one that is filled/serialized is TaskDetail.Payload, which has no
// omitempty: the detail keeps showing `"payload": {}` as it always has.
type TaskSummary struct {
	ID           int             `json:"id"`
	Status       string          `json:"status"`
	TargetMode   string          `json:"target_mode"`
	TargetCount  int             `json:"target_count"`
	Done         int             `json:"done"`
	Total        int             `json:"total"`
	Priority     int             `json:"priority"`
	RetryCount   int             `json:"retry_count"`
	ParentTaskID *int            `json:"parent_task_id"`
	CreatedAt    Time            `json:"created_at"`
	CompletedAt  *Time           `json:"completed_at"`
	Outcome      *string         `json:"outcome"`
	VariantKind  *string         `json:"variant_kind"`
	VariantLabel *string         `json:"variant_label"`
	Payload      *map[string]any `json:"payload,omitempty"`
}

// TaskListResponse: a page of tasks + cursor. NextCursor is nil when exhausted.
type TaskListResponse struct {
	Tasks      []TaskSummary `json:"tasks"`
	NextCursor *int          `json:"next_cursor"`
}

// TaskExecutionSummary is an execution attempt of a task (a worker that claimed
// or received it). stdout/stderr are NOT inline in v1 (they can be large).
//
// Outcome is the ATTEMPT's, distinct from the task's: `_SQL_TASK_EXECUTIONS`
// projects it and the detail is the only read that exposes it. Without it,
// `task get --json` showed a `completed` attempt without saying whether it
// produced a result. It has no omitempty, like TaskSummary's: the detail route has
// no opt-in fields.
type TaskExecutionSummary struct {
	WorkerID     string  `json:"worker_id"`
	Status       string  `json:"status"`
	AttemptIndex int     `json:"attempt_index"`
	AssignedAt   *Time   `json:"assigned_at"`
	StartedAt    *Time   `json:"started_at"`
	FinishedAt   *Time   `json:"finished_at"`
	ExitCode     *int    `json:"exit_code"`
	Outcome      *string `json:"outcome"`
}

// TaskDetail is `GET /tasks/{id}`: the summary + the heavy fields (payload,
// eligibility, timeouts, retry policy, last error) + the executions.
//
// Python->Go glossary: the anonymous `TaskSummary` field is composition by
// embedding — encoding/json "flattens" its fields to the top level of the JSON, so
// a detail carries id/status/done/total/... exactly like the summary, without
// repeating them. map[string]any is Python's dict for free-form JSON (payload,
// eligibility).
type TaskDetail struct {
	TaskSummary
	ChildCount              int                    `json:"child_count"`
	Payload                 map[string]any         `json:"payload"`
	Eligibility             map[string]any         `json:"eligibility"`
	ClaimTimeoutSeconds     *int                   `json:"claim_timeout_seconds"`
	ExecutionTimeoutSeconds *int                   `json:"execution_timeout_seconds"`
	RunAt                   *Time                  `json:"run_at"`
	AutoRetryPolicy         map[string]any         `json:"auto_retry_policy"`
	LastError               *string                `json:"last_error"`
	CancelledReason         *string                `json:"cancelled_reason"`
	Executions              []TaskExecutionSummary `json:"executions"`
}

// CreateTaskResponse is the response of POST /tasks and POST /tasks/{id}/fork.
// On fork, ParentTaskID carries the parent's id; on a plain create it is nil.
type CreateTaskResponse struct {
	TaskID       int    `json:"task_id"`
	Status       string `json:"status"`
	Target       string `json:"target"`
	TargetMode   string `json:"target_mode"`
	TargetCount  int    `json:"target_count"`
	ParentTaskID *int   `json:"parent_task_id"`
}

// RetryTaskResponse is the response of POST /tasks/{id}/retry.
type RetryTaskResponse struct {
	TaskID         int     `json:"task_id"`
	RetryCount     int     `json:"retry_count"`
	Status         string  `json:"status"`
	AttemptNumber  int     `json:"attempt_number"`
	TargetWorkerID *string `json:"target_worker_id"`
	DeliveryMode   string  `json:"delivery_mode"`
}

// CancelTaskResponse is the response of POST /tasks/{id}/cancel.
type CancelTaskResponse struct {
	TaskID int    `json:"task_id"`
	Status string `json:"status"`
}

// BulkDeleteRequest is the body of POST /tasks/bulk/delete: the list of ids to
// delete (1-500, positive, no duplicates; the server validates with 422).
type BulkDeleteRequest struct {
	TaskIDs []int `json:"task_ids"`
}

// BulkSkipped describes an id the server did NOT delete because it was not in a
// terminal state. Reason carries the server's reason (e.g. "not terminal: queued").
type BulkSkipped struct {
	ID     int    `json:"id"`
	Reason string `json:"reason"`
}

// BulkDeleteResponse is the best-effort response of POST /tasks/bulk/delete: the
// server classifies each id as deleted / skipped (not terminal) / not found.
type BulkDeleteResponse struct {
	Deleted  []int         `json:"deleted"`
	Skipped  []BulkSkipped `json:"skipped"`
	NotFound []int         `json:"not_found"`
}

// --- /api/v1/insights/failures ---------------------------------------------

// ExitCodeCount is a count of failed tasks by the exit_code of their
// representative execution. ExitCode is *int because the server includes the
// `null` group (tasks without a terminal execution or without exit_code): nil ==
// that group.
type ExitCodeCount struct {
	ExitCode *int `json:"exit_code"`
	Count    int  `json:"count"`
}

// WorkerFailureCount is a count of failed tasks by the worker of their
// representative execution.
type WorkerFailureCount struct {
	WorkerID string `json:"worker_id"`
	Count    int    `json:"count"`
}

// FailureExample is the representative task of a signature group, with
// breadcrumbs for diagnosis. ExecutionID feeds the drill-down via ExecutionOutput.
// Almost everything is a pointer/string because the server emits them as null when
// there is no terminal execution.
type FailureExample struct {
	TaskID          int            `json:"task_id"`
	ExecutionID     *string        `json:"execution_id"`
	ExitCode        *int           `json:"exit_code"`
	WorkerID        *string        `json:"worker_id"`
	Command         *string        `json:"command"`
	CommandLine     *string        `json:"command_line"`
	Payload         map[string]any `json:"payload"`
	StartedAt       *Time          `json:"started_at"`
	FinishedAt      *Time          `json:"finished_at"`
	DurationSeconds *float64       `json:"duration_seconds"`
	StderrTail      string         `json:"stderr_tail"`
	StdoutTail      string         `json:"stdout_tail"`
}

// FailureSignatureGroup is a group of failed tasks with the same normalized error
// signature. ExitCodes mixes ints and null (a group can gather several exit
// codes), hence []*int. SampleTaskIDs carries up to 10 ids for drill-down.
type FailureSignatureGroup struct {
	Signature     string         `json:"signature"`
	Count         int            `json:"count"`
	ExitCodes     []*int         `json:"exit_codes"`
	Example       FailureExample `json:"example"`
	SampleTaskIDs []int          `json:"sample_task_ids"`
}

// FailuresFilters is the echo of the applied filters (the workspace scope is
// implicit to the PAT). Since/Until are *Time (nil when not filtered by date).
type FailuresFilters struct {
	Since *Time `json:"since"`
	Until *Time `json:"until"`
	Limit int   `json:"limit"`
}

// FailuresResponse is the response of GET /api/v1/insights/failures: the live
// aggregation of the workspace's failed tasks by exit_code, worker and error
// signature. total_failed_tasks and by_* are computed over the full filtered set;
// limit only truncates by_signature.
type FailuresResponse struct {
	GeneratedAt      Time                    `json:"generated_at"`
	Filters          FailuresFilters         `json:"filters"`
	TotalFailedTasks int                     `json:"total_failed_tasks"`
	ByExitCode       []ExitCodeCount         `json:"by_exit_code"`
	ByWorker         []WorkerFailureCount    `json:"by_worker"`
	BySignature      []FailureSignatureGroup `json:"by_signature"`
}

// --- Uniform error model ---------------------------------------------------

// ValidationDetail is an entry of the `detail` array that FastAPI attaches to 422
// validation errors: {"loc": [...], "msg": "...", "type": "..."}. `loc` can mix
// strings and ints (e.g. ["body", "items", 0, "name"]).
type ValidationDetail struct {
	Loc  []any  `json:"loc"`
	Msg  string `json:"msg"`
	Type string `json:"type"`
}

// LocString joins the Loc segments with '.', converting each one to a string
// robustly (it accepts ints, strings and any other scalar).
func (d ValidationDetail) LocString() string {
	parts := make([]string, len(d.Loc))
	for i, seg := range d.Loc {
		parts[i] = fmt.Sprintf("%v", seg)
	}
	return strings.Join(parts, ".")
}

// APIError maps the error body of /api/v1: {"error","message","detail"?}.
// It implements the `error` interface (Error() method), so it propagates like any
// Go error. Status keeps the HTTP code so the caller can decide the exit code.
//
// `detail` (mostly present on 422) is an ARRAY of {loc,msg,type} objects, not an
// object: that is why Detail is a slice. A map failed to unmarshal and the detail
// was lost entirely.
type APIError struct {
	Status  int                `json:"-"`
	Code    string             `json:"error"`
	Message string             `json:"message"`
	Detail  []ValidationDetail `json:"detail,omitempty"`
}

// UnmarshalJSON unpacks the error model defensively (same style as
// Time.UnmarshalJSON above). The `detail` field is NOT always the 422 array:
// on other errors (400/403/404) the server —or FastAPI underneath— may send
// `detail` as a string ("field required") or as an object. If we let the default
// unmarshal try to put that into []ValidationDetail, it would fail with a type
// mismatch and the WHOLE error (error/message included) would be lost, falling
// back to the generic "HTTP 4xx" in client.go.
//
// Solution: capture `detail` as json.RawMessage and unpack the slice ONLY if the
// raw is a JSON array (starts with '['). For a string or an object we leave Detail
// nil but keep error/message. It degrades gracefully, without losing the main part.
func (e *APIError) UnmarshalJSON(b []byte) error {
	// the alias avoids infinite recursion: a new type without APIError's
	// UnmarshalJSON method, the usual Go pattern for custom unmarshalers.
	type apiErrorAlias struct {
		Code    string          `json:"error"`
		Message string          `json:"message"`
		Detail  json.RawMessage `json:"detail"`
	}
	var raw apiErrorAlias
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	e.Code = raw.Code
	e.Message = raw.Message
	e.Detail = nil
	// We only try the slice if `detail` is a JSON array. A string or object is
	// ignored (Detail stays nil) without breaking the rest of the error.
	trimmed := bytes.TrimSpace(raw.Detail)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		var detail []ValidationDetail
		if err := json.Unmarshal(trimmed, &detail); err == nil {
			e.Detail = detail
		}
	}
	return nil
}

// Error returns the main error message. When Detail has entries (typical of 422
// validation errors), it appends them one per line in the format
// "  - <loc>: <msg>", so the concrete reason for the rejection is not discarded.
// Without Detail it is a single line.
func (e *APIError) Error() string {
	msg := e.Message
	if msg == "" {
		msg = e.Code
	}
	if len(e.Detail) == 0 {
		return msg
	}
	var b strings.Builder
	b.WriteString(msg)
	for _, d := range e.Detail {
		b.WriteString("\n  - ")
		b.WriteString(d.LocString())
		b.WriteString(": ")
		b.WriteString(d.Msg)
	}
	return b.String()
}

// Secret is a workspace task secret as projected by /api/v1/secrets. Values are
// write-only: no response carries the value or the ciphertext, only name,
// fingerprint and dates. UpdatedBy is nil when the author no longer exists (FK
// with ON DELETE SET NULL). WorkerID/WorkerName are nil on a workspace-wide
// secret, and also in the response of a server that predates the per-worker
// scope.
type Secret struct {
	Name        string  `json:"name"`
	Fingerprint string  `json:"fingerprint"`
	CreatedAt   Time    `json:"created_at"`
	UpdatedAt   Time    `json:"updated_at"`
	UpdatedBy   *string `json:"updated_by"`
	WorkerID    *string `json:"worker_id"`
	WorkerName  *string `json:"worker_name"`
}

// SecretListResponse is the body of GET /secrets. KeysConfigured is false when
// the server has no keys file: it lists names and fingerprints but cannot store
// or deliver secrets. It is a pointer because older servers omit the field (and
// answer 503 without keys); nil counts as configured.
//
// SupportsWorkerScope is nil on a server that predates the per-worker scope: that
// server ignores `?worker=` on PUT and DELETE, so the CLI does not use it.
type SecretListResponse struct {
	Items               []Secret `json:"items"`
	KeysConfigured      *bool    `json:"keys_configured,omitempty"`
	SupportsWorkerScope *bool    `json:"supports_worker_scope,omitempty"`
}

// WorkerScopeSupported reports whether the server advertised the per-worker scope.
func (r *SecretListResponse) WorkerScopeSupported() bool {
	return r.SupportsWorkerScope != nil && *r.SupportsWorkerScope
}

// KeysUnconfigured reports whether the server explicitly declared it has no keys.
func (r *SecretListResponse) KeysUnconfigured() bool {
	return r.KeysConfigured != nil && !*r.KeysConfigured
}
