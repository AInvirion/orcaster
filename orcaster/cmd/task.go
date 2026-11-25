package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"strconv"
	"strings"
	"text/tabwriter"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
	"github.com/spf13/cobra"
	"golang.org/x/term"
)

var (
	listStatus         []string
	listLimit          int
	listCursor         int
	listJSON           bool
	listVariant        string
	listIncludePayload bool

	getJSON bool

	childrenLimit  int
	childrenCursor int
	childrenJSON   bool

	cancelReason string
	cancelJSON   bool

	retryForceSameWorker bool
	retryJSON            bool

	deleteBulk bool
	deleteYes  bool
	deleteJSON bool

	outputAttempt   int
	outputFormat    string
	outputFile      string
	outputExecution string

	// createParams and forkParams are separate instances of the same flag set
	// (create requires defaults for target/priority; fork only sends overrides).
	createParams taskParams
	forkParams   taskParams
)

// taskParams groups the flags shared by `task create` and `task fork`.
type taskParams struct {
	target         string
	priority       int
	payload        string
	eligibility    string
	claimTimeout   int
	execTimeout    int
	runAt          string
	idempotencyKey string
	autoRetry      string
	json           bool
	taskEnv        []string
	egressAllow    []string
}

var taskCmd = &cobra.Command{
	Use:   "task",
	Short: "Create and manage tasks",
}

var taskListCmd = &cobra.Command{
	Use:   "list",
	Short: "List tasks in the active workspace",
	RunE:  runTaskList,
}

var taskGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Show the full detail of a task",
	Args:  cobra.ExactArgs(1),
	RunE:  runTaskGet,
}

var taskChildrenCmd = &cobra.Command{
	Use:   "children <id>",
	Short: "List the direct children/forks of a task",
	Args:  cobra.ExactArgs(1),
	RunE:  runTaskChildren,
}

var taskCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create a new task",
	RunE:  runTaskCreate,
}

var taskCancelCmd = &cobra.Command{
	Use:   "cancel <id>",
	Short: "Cancel a task",
	Args:  cobra.ExactArgs(1),
	RunE:  runTaskCancel,
}

var taskRetryCmd = &cobra.Command{
	Use:   "retry <id>",
	Short: "Retry a terminal task in place",
	Long: "Re-runs the same task (same id, same cache key, same attempt history);\n" +
		"only completed, failed and cancelled tasks can be retried.\n\n" +
		"By default the retry goes back to the general queue and any eligible\n" +
		"worker can take it — the same default as POST /admin/tasks/{id}/retry and\n" +
		"the dashboard checkbox. Pass --force-same-worker to pin it to the worker\n" +
		"that ran the last attempt. On an all-available task the flag is ignored:\n" +
		"the retry re-snapshots every online worker.",
	Args: cobra.ExactArgs(1),
	RunE: runTaskRetry,
}

var taskForkCmd = &cobra.Command{
	Use:   "fork <id>",
	Short: "Fork a task, overriding only the fields you pass",
	Long: "Creates a new task that inherits the parent's parameters; any flag you\n" +
		"pass overrides that field. JSON fields (payload, eligibility) are deep-merged\n" +
		"server-side: keys you don't mention are kept from the parent.\n\n" +
		"Two fields are NOT inherited by design: --run-at (a fork is meant to run\n" +
		"now, so it defaults to immediate unless you pass a new one) and\n" +
		"--idempotency-key (the key is unique per task; the fork gets none unless\n" +
		"you pass one).",
	Args: cobra.ExactArgs(1),
	RunE: runTaskFork,
}

var taskOutputCmd = &cobra.Command{
	Use:   "output [<id>]",
	Short: "Fetch the stdout/stderr log of a task's execution",
	Long: "Fetches a log. By default it targets a task by id and serves its latest\n" +
		"execution (use --attempt to pick another). Alternatively, pass\n" +
		"--execution <uuid> to fetch one specific execution directly by its UUID\n" +
		"(handy for drilling down into a signature from `orcaster insights failures`).\n" +
		"The two modes are mutually exclusive: pass a task id OR --execution, not both.",
	Args: cobra.MaximumNArgs(1),
	RunE: runTaskOutput,
}

var taskDeleteCmd = &cobra.Command{
	Use:   "delete <id> [<id>...]",
	Short: "Delete terminal tasks (irreversible hard-delete)",
	Long: "Permanently deletes tasks. This is a hard-delete: the task's executions\n" +
		"and artifacts are removed via CASCADE and cannot be recovered. Only tasks\n" +
		"in a terminal state can be deleted; the server rejects the rest.\n\n" +
		"By default the command deletes EXACTLY one task with a single REST call\n" +
		"(DELETE /tasks/{id}). To delete several at once pass --bulk with up to 500\n" +
		"ids; the server then classifies each id as deleted, skipped (not terminal)\n" +
		"or not found.",
	Args: cobra.MinimumNArgs(1),
	RunE: runTaskDelete,
}

// registerTaskParamFlags binds the create/fork flag set to p.
func registerTaskParamFlags(c *cobra.Command, p *taskParams) {
	f := c.Flags()
	f.StringVar(&p.target, "target", "1", `target spec: "N", "all-available", "worker:<uuid>", "workers:<u1>,<u2>"`)
	f.IntVar(&p.priority, "priority", 0, "scheduling priority (-32768..32767)")
	f.StringVar(&p.payload, "payload", "", "task payload as JSON (literal, @file, or - for stdin)")
	f.StringVar(&p.eligibility, "eligibility", "", "worker eligibility as JSON (literal, @file, or -)")
	f.IntVar(&p.claimTimeout, "claim-timeout", 0, "seconds before an unclaimed task times out")
	f.IntVar(&p.execTimeout, "exec-timeout", 0, "seconds before a running task times out")
	f.StringVar(&p.runAt, "run-at", "", "schedule for a future time (RFC3339, e.g. 2026-06-02T15:00:00Z)")
	f.StringVar(&p.idempotencyKey, "idempotency-key", "", "UUID (e.g. a UUIDv4) to dedupe duplicate submissions")
	f.StringVar(&p.autoRetry, "auto-retry", "", "auto-retry policy as JSON (literal, @file, or -)")
	f.BoolVar(&p.json, "json", false, "output raw JSON instead of a table")
	f.StringArrayVar(&p.taskEnv, "env", nil, "name of a worker env var forwarded to the task, e.g. --env API_TOKEN (repeatable); a name only, never NAME=value: the value comes from the worker env or a stored secret; passing this flag enables strict env isolation in the worker")
	f.StringArrayVar(&p.egressAllow, "egress-allow", nil, "domain allowed for task egress (repeatable); passing this flag enables strict egress isolation in the worker")
}

func init() {
	f := taskListCmd.Flags()
	f.StringArrayVar(&listStatus, "status", nil, "filter by status (repeatable)")
	f.IntVar(&listLimit, "limit", 50, "max rows (1-200)")
	f.IntVar(&listCursor, "cursor", 0, "pagination cursor: a task id (use the previous page's next_cursor), min 1; returns rows with id < cursor")
	f.BoolVar(&listJSON, "json", false, "output raw JSON instead of a table")
	f.StringVar(&listVariant, "variant", "",
		`filter by task variant: "all", "other", "<kind>" or "<kind>~<label>" (default: server-side "all")`)
	f.BoolVar(&listIncludePayload, "include-payload", false,
		"include each task's payload in the response (shown with --json)")

	taskGetCmd.Flags().BoolVar(&getJSON, "json", false, "output raw JSON instead of a table")

	chf := taskChildrenCmd.Flags()
	chf.IntVar(&childrenLimit, "limit", 50, "max rows (1-200)")
	chf.IntVar(&childrenCursor, "cursor", 0, "pagination cursor: a task id (use the previous page's next_cursor), min 1; returns rows with id < cursor")
	chf.BoolVar(&childrenJSON, "json", false, "output raw JSON instead of a table")

	registerTaskParamFlags(taskCreateCmd, &createParams)
	registerTaskParamFlags(taskForkCmd, &forkParams)

	cf := taskCancelCmd.Flags()
	cf.StringVar(&cancelReason, "reason", "", "optional cancellation reason")
	cf.BoolVar(&cancelJSON, "json", false, "output raw JSON instead of a table")

	rf := taskRetryCmd.Flags()
	// Default false, same as `AdminRetryTaskRequest.force_same_worker` on the
	// server and the dashboard checkbox: the same operation cannot mean something
	// different depending on where it comes in from.
	rf.BoolVar(&retryForceSameWorker, "force-same-worker", false,
		"pin the retry to the worker that ran the last attempt (reuses its local cache; waits if that worker is down, busy or retired). Default: any eligible worker")
	rf.BoolVar(&retryJSON, "json", false, "output raw JSON instead of a table")

	of := taskOutputCmd.Flags()
	of.IntVar(&outputAttempt, "attempt", 0, "execution attempt to fetch (default: latest); not valid with --execution")
	of.StringVar(&outputFormat, "format", "text", "output format: text|jsonl")
	of.StringVarP(&outputFile, "output", "o", "", "write to file instead of stdout")
	of.StringVar(&outputExecution, "execution", "", "fetch one execution by its UUID instead of a task id")

	df := taskDeleteCmd.Flags()
	df.BoolVar(&deleteBulk, "bulk", false, "delete multiple tasks (1-500 ids) via the bulk endpoint")
	df.BoolVarP(&deleteYes, "yes", "y", false, "skip the confirmation prompt")
	df.BoolVar(&deleteJSON, "json", false, "output raw JSON instead of a human summary")

	taskCmd.AddCommand(taskListCmd, taskGetCmd, taskChildrenCmd, taskCreateCmd, taskCancelCmd, taskRetryCmd, taskForkCmd, taskOutputCmd, taskDeleteCmd)
	rootCmd.AddCommand(taskCmd)
}

func runTaskList(cmd *cobra.Command, _ []string) error {
	client, _, err := newClient()
	if err != nil {
		return err
	}

	// An empty Variant and IncludePayload=false do not travel (see ListOptions):
	// without the flags the request goes out as it always has and the server
	// applies its defaults.
	opts := api.ListOptions{
		Status:         listStatus,
		Limit:          listLimit,
		Variant:        listVariant,
		IncludePayload: listIncludePayload,
	}
	if cmd.Flags().Changed("cursor") {
		opts.Cursor = &listCursor // we only paginate if the user asked for a cursor
	}

	out := cmd.OutOrStdout()
	// The branch is decided BEFORE the request: each variant makes ONE single GET,
	// with the same params. `--json` goes through the server's raw bytes (see
	// ListTasksRaw); the table needs the typed fields.
	if listJSON {
		raw, err := client.ListTasksRaw(cmd.Context(), opts)
		if err != nil {
			return err
		}
		return printRawJSON(out, raw)
	}

	resp, err := client.ListTasks(cmd.Context(), opts)
	if err != nil {
		return err
	}
	printTaskTable(out, resp)
	return nil
}

// printJSON serializes indented to the given writer. Python->Go glossary: ~ json.dumps(x, indent=2).
// It takes the io.Writer explicitly (instead of os.Stdout) so the golden-file tests
// can capture the output with a buffer; cobra provides cmd.OutOrStdout() at runtime.
func printJSON(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

// printRawJSON prints the server response's bytes re-indented, without
// deserializing them. Same two-space indent and same trailing newline as printJSON,
// so the `--json` output does not change in SHAPE; what changes is that the set of
// keys is now the server's and not the client struct's.
func printRawJSON(w io.Writer, raw json.RawMessage) error {
	var buf bytes.Buffer
	if err := json.Indent(&buf, raw, "", "  "); err != nil {
		return fmt.Errorf("indenting JSON response: %w", err)
	}
	buf.WriteByte('\n')
	_, err := w.Write(buf.Bytes())
	return err
}

// printTaskTable prints a table aligned in `gh` style. tabwriter aligns the
// tab-separated columns (~ like a manual str.ljust, but automatic).
func printTaskTable(out io.Writer, resp *api.TaskListResponse) {
	if len(resp.Tasks) == 0 {
		fmt.Fprintln(out, "No tasks.")
		return
	}
	w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tSTATUS\tPROGRESS\tPRIORITY\tRETRIES\tCREATED\tCOMPLETED")
	for _, t := range resp.Tasks {
		created := "-"
		if !t.CreatedAt.IsZero() {
			created = t.CreatedAt.Format("2006-01-02 15:04:05")
		}
		completed := "-"
		if t.CompletedAt != nil && !t.CompletedAt.IsZero() {
			completed = t.CompletedAt.Format("2006-01-02 15:04:05")
		}
		fmt.Fprintf(w, "%d\t%s\t%d/%d\t%d\t%d\t%s\t%s\n",
			t.ID, t.Status, t.Done, t.Total, t.Priority, t.RetryCount, created, completed)
	}
	_ = w.Flush()
	if resp.NextCursor != nil {
		fmt.Fprintf(out, "\nMore results: --cursor %d\n", *resp.NextCursor)
	}
}

// printTaskChildrenTable prints a task's children with the ID/STATUS/CREATED
// columns (a subset of printTaskTable: for children the link and the status
// matter, not the progress breakdown). Same tabwriter mechanics and same
// pagination notice as printTaskTable.
func printTaskChildrenTable(out io.Writer, resp *api.TaskListResponse) {
	if len(resp.Tasks) == 0 {
		fmt.Fprintln(out, "No children.")
		return
	}
	w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tSTATUS\tCREATED")
	for _, t := range resp.Tasks {
		created := "-"
		if !t.CreatedAt.IsZero() {
			created = t.CreatedAt.Format("2006-01-02 15:04:05")
		}
		fmt.Fprintf(w, "%d\t%s\t%s\n", t.ID, t.Status, created)
	}
	_ = w.Flush()
	if resp.NextCursor != nil {
		fmt.Fprintf(out, "\nMore results: --cursor %d\n", *resp.NextCursor)
	}
}

func runTaskGet(cmd *cobra.Command, args []string) error {
	id, err := parseTaskID(args[0])
	if err != nil {
		return err
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}
	detail, err := client.GetTask(cmd.Context(), id)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if getJSON {
		return printJSON(out, detail)
	}
	printTaskDetail(out, detail)
	return nil
}

func runTaskChildren(cmd *cobra.Command, args []string) error {
	id, err := parseTaskID(args[0])
	if err != nil {
		return err
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}

	var cursor *int
	if cmd.Flags().Changed("cursor") {
		cursor = &childrenCursor // we only paginate if the user asked for a cursor
	}

	out := cmd.OutOrStdout()
	if childrenJSON {
		raw, err := client.ListTaskChildrenRaw(cmd.Context(), id, childrenLimit, cursor)
		if err != nil {
			return err
		}
		return printRawJSON(out, raw)
	}

	resp, err := client.ListTaskChildren(cmd.Context(), id, childrenLimit, cursor)
	if err != nil {
		return err
	}
	printTaskChildrenTable(out, resp)
	return nil
}

func runTaskCreate(cmd *cobra.Command, _ []string) error {
	// --payload is required on create: without it the server silently applied {}
	// and empty tasks were created by accident. For a deliberately empty one the
	// user passes an explicit --payload '{}'.
	if !cmd.Flags().Changed("payload") {
		return fmt.Errorf("--payload is required (use --payload '{}' for an empty payload)")
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}
	// create always sends target+priority (they have a flag-level default).
	body, err := buildTaskBody(cmd, &createParams, true)
	if err != nil {
		return err
	}
	resp, err := client.CreateTask(cmd.Context(), body)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if createParams.json {
		return printJSON(out, resp)
	}
	printCreateResponse(out, resp)
	return nil
}

func runTaskFork(cmd *cobra.Command, args []string) error {
	id, err := parseTaskID(args[0])
	if err != nil {
		return err
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}
	// fork only sends the flags the user changed (server-side deep-merge).
	body, err := buildTaskBody(cmd, &forkParams, false)
	if err != nil {
		return err
	}
	resp, err := client.ForkTask(cmd.Context(), id, body)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if forkParams.json {
		return printJSON(out, resp)
	}
	printCreateResponse(out, resp)
	return nil
}

func runTaskOutput(cmd *cobra.Command, args []string) error {
	// Validate the format locally before calling the server (fail fast and do not
	// spend a round-trip on an error we know about beforehand).
	if outputFormat != "text" && outputFormat != "jsonl" {
		return fmt.Errorf(`invalid --format %q: must be "text" or "jsonl"`, outputFormat)
	}

	// Two mutually exclusive modes: a positional task id (serves the most recent
	// execution, or --attempt) or --execution <uuid> (one specific execution). A
	// UUID is never empty, so outputExecution != "" signals the execution mode.
	useExecution := outputExecution != ""
	// --attempt given ⇔ outputAttempt != 0: the valid value is >=1, so 0 means
	// "not given" (and an explicit --attempt 0 would already be a 422 server-side).
	// A single signal, without depending on cobra.Changed, robust when the tests
	// set the global.
	attemptGiven := outputAttempt != 0
	switch {
	case useExecution && len(args) > 0:
		return fmt.Errorf("pass either a task id or --execution <uuid>, not both")
	case !useExecution && len(args) == 0:
		return fmt.Errorf("provide a task id or --execution <uuid>")
	case useExecution && attemptGiven:
		return fmt.Errorf("--attempt does not apply with --execution: the UUID already identifies one execution")
	}

	// Resolve the task id or validate the execution UUID BEFORE touching the
	// network, to fail fast on errors we know about beforehand.
	var id int
	if useExecution {
		if !isUUID(outputExecution) {
			return fmt.Errorf(
				"invalid --execution %q: must be a UUID (e.g. 550e8400-e29b-41d4-a716-446655440000)",
				outputExecution)
		}
	} else {
		var err error
		if id, err = parseTaskID(args[0]); err != nil {
			return err
		}
	}

	// attempt is *int: "absent" (latest) is told apart from an explicit value.
	var attempt *int
	if attemptGiven {
		attempt = &outputAttempt
	}

	client, _, err := newClient()
	if err != nil {
		return err
	}
	var body io.ReadCloser
	if useExecution {
		body, err = client.ExecutionOutput(cmd.Context(), outputExecution, outputFormat)
	} else {
		body, err = client.TaskOutput(cmd.Context(), id, attempt, outputFormat)
	}
	if err != nil {
		return err
	}
	defer body.Close() //nolint:errcheck // best-effort body close

	// io.Copy streams without loading the whole log into memory.
	if outputFile != "" {
		f, err := os.Create(outputFile)
		if err != nil {
			return fmt.Errorf("creating %s: %w", outputFile, err)
		}
		defer f.Close() //nolint:errcheck // safety net on the error paths; the real close is checked below
		if _, err := io.Copy(f, body); err != nil {
			return fmt.Errorf("writing output: %w", err)
		}
		// Close explicitly and propagate the error: on a full disk the failure may
		// only show up at the flush in Close(), and a defer that discards it would
		// report success with a truncated file.
		if err := f.Close(); err != nil {
			return fmt.Errorf("closing %s: %w", outputFile, err)
		}
		return nil
	}
	if _, err := io.Copy(cmd.OutOrStdout(), body); err != nil {
		return fmt.Errorf("writing output: %w", err)
	}
	return nil
}

func runTaskCancel(cmd *cobra.Command, args []string) error {
	id, err := parseTaskID(args[0])
	if err != nil {
		return err
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}
	resp, err := client.CancelTask(cmd.Context(), id, cancelReason)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if cancelJSON {
		return printJSON(out, resp)
	}
	fmt.Fprintf(out, "Task %d cancelled.\n", resp.TaskID)
	return nil
}

func runTaskRetry(cmd *cobra.Command, args []string) error {
	id, err := parseTaskID(args[0])
	if err != nil {
		return err
	}
	client, _, err := newClient()
	if err != nil {
		return err
	}
	resp, err := client.RetryTask(cmd.Context(), id, retryForceSameWorker)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if retryJSON {
		return printJSON(out, resp)
	}
	printRetryResponse(out, resp)
	return nil
}

// runTaskDelete deletes tasks. Without --bulk it is REST-idiomatic: it requires
// EXACTLY one id and calls DELETE /tasks/{id} once (it is not a loop). With --bulk
// it sends all the ids (1-500) to POST /tasks/bulk/delete and the server classifies
// the result.
//
// The deletion is irreversible (hard-delete + CASCADE), so we confirm first
// unless --yes. The confirmation lists the ids WITHOUT a prior status fetch: in
// bulk the server marks non-terminal ones as skipped, and in single the 409
// reports it.
func runTaskDelete(cmd *cobra.Command, args []string) error {
	ids, proceed, err := preDeleteCheck(
		cmd.OutOrStdout(), os.Stdin, args, deleteBulk, deleteYes,
		term.IsTerminal(int(os.Stdin.Fd())),
	)
	if err != nil {
		return err
	}
	if !proceed {
		// Confirmation declined by the user: clean exit, nothing deleted.
		return nil
	}

	client, _, err := newClient()
	if err != nil {
		return err
	}

	out := cmd.OutOrStdout()
	if deleteBulk {
		resp, err := client.BulkDeleteTasks(cmd.Context(), ids)
		if err != nil {
			return err
		}
		if deleteJSON {
			return printJSON(out, resp)
		}
		printBulkDeleteResponse(out, resp)
		return nil
	}

	// single: one task only. The server answers 204 with no body; a 409/404 is
	// propagated as is (same as CancelTask).
	id := ids[0]
	if err := client.DeleteTask(cmd.Context(), id); err != nil {
		return err
	}
	if deleteJSON {
		// The DELETE returns no body; we make the JSON shape uniform with the
		// bulk one so the consumer gets the same structure in both modes.
		return printJSON(out, api.BulkDeleteResponse{
			Deleted:  []int{id},
			Skipped:  []api.BulkSkipped{},
			NotFound: []int{},
		})
	}
	printDeleteSingle(out, id)
	return nil
}

// preDeleteCheck validates the ids and resolves the confirmation BEFORE any
// request, with all dependencies injected (out/in/isTTY) so it is testable
// without a real TTY or a client. It returns (ids, proceed, err): proceed=false
// with err=nil means "the user aborted the confirmation" (clean exit, deletes
// nothing).
func preDeleteCheck(
	out io.Writer, in io.Reader, args []string, bulk, yes, isTTY bool,
) ([]int, bool, error) {
	ids, err := parseTaskIDs(args)
	if err != nil {
		return nil, false, err
	}

	// Default = REST-idiomatic single: >1 id without --bulk is a local usage error,
	// it never reaches the server.
	if !bulk && len(ids) > 1 {
		return nil, false, fmt.Errorf("multiple task ids require --bulk")
	}

	// --yes skips the confirmation entirely (it does not read stdin).
	if yes {
		return ids, true, nil
	}

	// Without --yes and without a TTY there is no safe way to confirm: we abort
	// before any request.
	if !isTTY {
		return nil, false, fmt.Errorf("refusing to delete without --yes (no interactive terminal)")
	}

	ok, err := confirmDestructive(out, in, ids)
	if err != nil {
		return nil, false, err
	}
	return ids, ok, nil
}

// confirmDestructive prints a notice with the ids to delete and reads [y/N] from
// in. It returns true only if the answer is "y"/"Y". out/in are injected so the
// helper can be tested in isolation (without a real TTY). The caller decides
// separately whether we are on a TTY (term.IsTerminal on stdin).
func confirmDestructive(out io.Writer, in io.Reader, ids []int) (bool, error) {
	fmt.Fprintf(out, "About to delete %d task(s) (irreversible): %s. Continue? [y/N] ",
		len(ids), joinIDs(ids))
	line, err := bufio.NewReader(in).ReadString('\n')
	if err != nil && err != io.EOF {
		return false, fmt.Errorf("reading confirmation: %w", err)
	}
	answer := strings.TrimSpace(line)
	return answer == "y" || answer == "Y", nil
}

// joinIDs formats a list of ids as "217, 229" for user notices.
func joinIDs(ids []int) string {
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = strconv.Itoa(id)
	}
	return strings.Join(parts, ", ")
}

// printDeleteSingle prints the deletion confirmation of a single task.
func printDeleteSingle(w io.Writer, id int) {
	fmt.Fprintf(w, "Task %d deleted.\n", id)
}

// printBulkDeleteResponse prints the bulk result in human format, omitting empty
// sections.
func printBulkDeleteResponse(out io.Writer, r *api.BulkDeleteResponse) {
	if len(r.Deleted) > 0 {
		fmt.Fprintf(out, "Deleted: %d (%s)\n", len(r.Deleted), joinIDs(r.Deleted))
	}
	if len(r.Skipped) > 0 {
		fmt.Fprintf(out, "Skipped: %d\n", len(r.Skipped))
		w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
		for _, s := range r.Skipped {
			fmt.Fprintf(w, "  %d\t%s\n", s.ID, s.Reason)
		}
		_ = w.Flush()
	}
	if len(r.NotFound) > 0 {
		fmt.Fprintf(out, "Not found: %d (%s)\n", len(r.NotFound), joinIDs(r.NotFound))
	}
}

// parseTaskIDs validates each positional id (>0) reusing parseTaskID. It neither
// deduplicates nor caps at 500: the server validates that (422) to have a single
// source of truth.
func parseTaskIDs(args []string) ([]int, error) {
	ids := make([]int, len(args))
	for i, a := range args {
		id, err := parseTaskID(a)
		if err != nil {
			return nil, err
		}
		ids[i] = id
	}
	return ids, nil
}

// parseTaskID converts the positional argument to a valid task id (>0).
func parseTaskID(s string) (int, error) {
	id, err := strconv.Atoi(s)
	if err != nil || id <= 0 {
		return 0, fmt.Errorf("invalid task id %q: must be a positive integer", s)
	}
	return id, nil
}

// buildTaskBody builds the create/fork body from the flags. With includeDefaults
// (create) target and priority are always sent; without it (fork) only the flags
// the user changed are sent, so the server inherits the rest from the parent.
//
// Python->Go glossary: `cmd.Flags().Changed(name)` tells "the user passed the
// flag" from "it stayed at its default" — equivalent to checking whether a key is
// in a kwargs dict. json.RawMessage passes the user's JSON as is (without re-parsing).
func buildTaskBody(cmd *cobra.Command, p *taskParams, includeDefaults bool) (map[string]any, error) {
	f := cmd.Flags()
	body := map[string]any{}

	if includeDefaults || f.Changed("target") {
		body["target"] = p.target
	}
	if includeDefaults || f.Changed("priority") {
		body["priority"] = p.priority
	}
	if f.Changed("payload") {
		raw, err := readJSONArg(p.payload)
		if err != nil {
			return nil, fmt.Errorf("--payload: %w", err)
		}
		body["payload"] = raw
	}
	if f.Changed("eligibility") {
		raw, err := readJSONArg(p.eligibility)
		if err != nil {
			return nil, fmt.Errorf("--eligibility: %w", err)
		}
		body["eligibility"] = raw
	}
	if f.Changed("auto-retry") {
		raw, err := readJSONArg(p.autoRetry)
		if err != nil {
			return nil, fmt.Errorf("--auto-retry: %w", err)
		}
		body["auto_retry"] = raw
	}
	if f.Changed("claim-timeout") {
		body["claim_timeout_seconds"] = p.claimTimeout
	}
	if f.Changed("exec-timeout") {
		body["execution_timeout_seconds"] = p.execTimeout
	}
	if f.Changed("run-at") {
		body["run_at"] = p.runAt
	}
	if f.Changed("idempotency-key") {
		// The server requires a UUID (it parses it as such and returns a cryptic
		// 422 if it is not). We validate the format locally to fail fast without
		// spending a round-trip, same as --format in `task output`.
		if !isUUID(p.idempotencyKey) {
			return nil, fmt.Errorf(
				"invalid --idempotency-key %q: must be a UUID (e.g. 550e8400-e29b-41d4-a716-446655440000)",
				p.idempotencyKey)
		}
		body["idempotency_key"] = p.idempotencyKey
	}
	// Each --env is a variable NAME, never NAME=value. Like --idempotency-key, it
	// is validated locally to fail before any request: this way a value pasted by
	// mistake does not travel to the server nor end up in the payload.
	if err := validateTaskEnvEntries(p.taskEnv); err != nil {
		return nil, err
	}
	// Merge the isolation flags into the payload.
	// A PRESENT flag (even empty) writes the key -> enables strict mode on the
	// worker (tri-state semantics). Absent -> left untouched (legacy).
	if p.taskEnv != nil || p.egressAllow != nil {
		var raw json.RawMessage
		if existing, ok := body["payload"].(json.RawMessage); ok {
			raw = existing
		}
		var pm map[string]any
		if len(raw) == 0 {
			pm = map[string]any{}
		} else if err := json.Unmarshal(raw, &pm); err != nil {
			return nil, fmt.Errorf("payload must be a JSON object to merge --env/--egress-allow: %w", err)
		} else if pm == nil {
			return nil, fmt.Errorf("payload must be a JSON object to merge --env/--egress-allow: got non-object value")
		}
		if p.taskEnv != nil {
			pm["task_env"] = p.taskEnv
		}
		if p.egressAllow != nil {
			pm["egress_allowlist"] = p.egressAllow
		}
		merged, err := json.Marshal(pm)
		if err != nil {
			return nil, fmt.Errorf("re-marshal payload: %w", err)
		}
		body["payload"] = json.RawMessage(merged)
	}
	return body, nil
}

// envHint is the hint that accompanies every shape rejection of --env.
const envHint = "use --env NAME to forward a variable from the worker, not NAME=value"

// envNameRe and envTokenPrefixRe mirror `_ENV_NAME_RE` and `_TOKEN_PREFIX_RE` of
// server/app/env_names.py (validate_task_env_entries). Explicit ASCII classes:
// a name with Unicode letters is not a valid environment variable. The token
// prefix is anchored and case-sensitive, so HF_TOKEN, GITHUB_TOKEN and
// http_proxy are still valid names.
var (
	envNameRe        = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	envTokenPrefixRe = regexp.MustCompile(`^(?:hf_|gh[opsur]_|github_pat_|glpat-|xox[bp]-|sk-)`)
)

// reservedEnvPrefix is the worker control plane prefix; the server rejects it in
// task_env (case-sensitive, like RESERVED_ENV_PREFIX).
const reservedEnvPrefix = "ORCH_"

// validateTaskEnvEntries applies to the --env values the same rules and the same
// order as the server's validate_task_env_entries: known token prefix, name shape
// (covers NAME=value, spaces, newline and the empty string) and the ORCH_ prefix.
// It stops at the first invalid entry.
//
// The error names only the flag's position, counted from 1 in the order in which
// it appears on the command line (the server counts from 0 in
// payload.task_env[N]; here we count the way the user sees it). It never repeats
// the entry or any part of it: it may be a secret's value or the token itself.
func validateTaskEnvEntries(entries []string) error {
	for i, entry := range entries {
		pos := i + 1
		switch {
		case envTokenPrefixRe.MatchString(entry):
			return fmt.Errorf(
				"invalid --env #%d: looks like a credential (known token prefix), not a variable name; %s",
				pos, envHint)
		case !envNameRe.MatchString(entry):
			return fmt.Errorf(
				"invalid --env #%d: not a valid environment variable name (letters, digits and underscores, not starting with a digit); %s",
				pos, envHint)
		case strings.HasPrefix(entry, reservedEnvPrefix):
			return fmt.Errorf(
				"invalid --env #%d: ORCH_ names are reserved for the worker control plane and cannot be forwarded",
				pos)
		}
	}
	return nil
}

// isUUID validates the canonical format of a UUID (8-4-4-4-12 hex digits
// separated by dashes), without tying itself to a specific version. It avoids
// adding a dependency just for this: the server does the strict validation, here
// we only discard what clearly is not a UUID.
func isUUID(s string) bool {
	if len(s) != 36 { // 32 hex digits + 4 dashes
		return false
	}
	for i, r := range s {
		switch i {
		case 8, 13, 18, 23: // dash positions in 8-4-4-4-12
			if r != '-' {
				return false
			}
		default:
			isHex := (r >= '0' && r <= '9') || (r >= 'a' && r <= 'f') || (r >= 'A' && r <= 'F')
			if !isHex {
				return false
			}
		}
	}
	return true
}

// readJSONArg resolves a JSON argument that may be a literal, @file, or "-"
// (stdin), and validates that it is well-formed JSON before sending it.
func readJSONArg(s string) (json.RawMessage, error) {
	var data []byte
	switch {
	case s == "-":
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, fmt.Errorf("reading stdin: %w", err)
		}
		data = b
	case strings.HasPrefix(s, "@"):
		b, err := os.ReadFile(s[1:])
		if err != nil {
			return nil, fmt.Errorf("reading file: %w", err)
		}
		data = b
	default:
		data = []byte(s)
	}
	if !json.Valid(data) {
		return nil, fmt.Errorf("not valid JSON")
	}
	return json.RawMessage(data), nil
}

// printTaskDetail prints a task's detail in key: value format + tables.
func printTaskDetail(out io.Writer, d *api.TaskDetail) {
	fmt.Fprintf(out, "ID:         %d\n", d.ID)
	fmt.Fprintf(out, "Status:     %s\n", d.Status)
	fmt.Fprintf(out, "Target:     %s (count %d)\n", d.TargetMode, d.TargetCount)
	fmt.Fprintf(out, "Progress:   %d/%d\n", d.Done, d.Total)
	fmt.Fprintf(out, "Priority:   %d\n", d.Priority)
	fmt.Fprintf(out, "Retries:    %d\n", d.RetryCount)
	fmt.Fprintf(out, "Children:   %d\n", d.ChildCount)
	if d.ParentTaskID != nil {
		// "Forked from:" is 12 chars, the same as the other labels + their padding
		// (e.g. "Children:   "), so the value stays aligned without extra space.
		fmt.Fprintf(out, "Forked from:%d\n", *d.ParentTaskID)
	}
	if !d.CreatedAt.IsZero() {
		fmt.Fprintf(out, "Created:    %s\n", d.CreatedAt.Format("2006-01-02 15:04:05"))
	}
	if d.CompletedAt != nil && !d.CompletedAt.IsZero() {
		fmt.Fprintf(out, "Completed:  %s\n", d.CompletedAt.Format("2006-01-02 15:04:05"))
	}
	if d.RunAt != nil && !d.RunAt.IsZero() {
		fmt.Fprintf(out, "Run at:     %s\n", d.RunAt.Format("2006-01-02 15:04:05"))
	}
	if d.ClaimTimeoutSeconds != nil {
		fmt.Fprintf(out, "Claim timeout: %ds\n", *d.ClaimTimeoutSeconds)
	}
	if d.ExecutionTimeoutSeconds != nil {
		fmt.Fprintf(out, "Exec timeout:  %ds\n", *d.ExecutionTimeoutSeconds)
	}
	if d.CancelledReason != nil && *d.CancelledReason != "" {
		fmt.Fprintf(out, "Cancelled:  %s\n", *d.CancelledReason)
	}
	if d.LastError != nil && *d.LastError != "" {
		fmt.Fprintf(out, "Last error: %s\n", *d.LastError)
	}

	if len(d.Payload) > 0 {
		fmt.Fprintln(out, "\nPayload:")
		_ = printJSON(out, d.Payload)
	}

	if len(d.Executions) == 0 {
		fmt.Fprintln(out, "\nNo executions yet.")
		return
	}
	fmt.Fprintln(out, "\nExecutions:")
	w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "ATTEMPT\tWORKER\tSTATUS\tEXIT\tFINISHED")
	for _, e := range d.Executions {
		exit := "-"
		if e.ExitCode != nil {
			exit = strconv.Itoa(*e.ExitCode)
		}
		finished := "-"
		if e.FinishedAt != nil && !e.FinishedAt.IsZero() {
			finished = e.FinishedAt.Format("2006-01-02 15:04:05")
		}
		fmt.Fprintf(w, "%d\t%s\t%s\t%s\t%s\n", e.AttemptIndex, e.WorkerID, e.Status, exit, finished)
	}
	_ = w.Flush()
}

// printCreateResponse prints the result of create/fork.
func printCreateResponse(out io.Writer, r *api.CreateTaskResponse) {
	fmt.Fprintf(out, "Task %d created (status %s, target %s, mode %s, count %d).\n",
		r.TaskID, r.Status, r.Target, r.TargetMode, r.TargetCount)
	if r.ParentTaskID != nil {
		fmt.Fprintf(out, "Forked from task %d.\n", *r.ParentTaskID)
	}
}

// printRetryResponse prints the result of a retry.
func printRetryResponse(out io.Writer, r *api.RetryTaskResponse) {
	fmt.Fprintf(out, "Task %d retried (status %s, attempt %d, retries %d, delivery %s).\n",
		r.TaskID, r.Status, r.AttemptNumber, r.RetryCount, r.DeliveryMode)
	if r.TargetWorkerID != nil {
		fmt.Fprintf(out, "Targeted worker: %s\n", *r.TargetWorkerID)
	}
}
