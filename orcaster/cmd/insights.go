package cmd

import (
	"fmt"
	"io"
	"strconv"
	"strings"
	"text/tabwriter"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
	"github.com/spf13/cobra"
)

var (
	failuresSince string
	failuresUntil string
	failuresLimit int
	failuresJSON  bool
)

var insightsCmd = &cobra.Command{
	Use:   "insights",
	Short: "Aggregate views over the workspace's tasks",
}

var insightsFailuresCmd = &cobra.Command{
	Use:   "failures",
	Short: "Aggregate failed tasks by exit code, worker and error signature",
	Long: "Aggregates the workspace's failed tasks (live, retroactive) so recurring\n" +
		"errors are easy to spot. Groups by exit code, by worker, and by a normalized\n" +
		"error signature; each signature group includes a representative task and a\n" +
		"few sample task ids. Drill into a group's full log with\n" +
		"`orcaster task output --execution <uuid>` using the example's execution.\n\n" +
		"--since/--until filter on the task's completed_at (ISO 8601; a date without a\n" +
		"timezone offset is interpreted as UTC). Each flag defaults independently when\n" +
		"omitted: --since to 00:00 UTC of the current day, --until to now; pass an\n" +
		"older --since for full history. The effective window is shown in the summary\n" +
		"line.",
	Args: cobra.NoArgs,
	RunE: runInsightsFailures,
}

func init() {
	f := insightsFailuresCmd.Flags()
	f.StringVar(&failuresSince, "since", "", "only tasks closed on/after this time (ISO 8601, UTC if no offset)")
	f.StringVar(&failuresUntil, "until", "", "only tasks closed on/before this time (ISO 8601, UTC if no offset)")
	f.IntVar(&failuresLimit, "limit", 100, "max signature groups to return (1-1000)")
	f.BoolVar(&failuresJSON, "json", false, "output raw JSON instead of a table")

	insightsCmd.AddCommand(insightsFailuresCmd)
	rootCmd.AddCommand(insightsCmd)
}

func runInsightsFailures(cmd *cobra.Command, _ []string) error {
	client, _, err := newClient()
	if err != nil {
		return err
	}
	resp, err := client.FailuresInsights(cmd.Context(), api.FailuresOptions{
		Since: failuresSince,
		Until: failuresUntil,
		Limit: failuresLimit,
	})
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if failuresJSON {
		return printJSON(out, resp)
	}
	printFailures(out, resp)
	return nil
}

// printFailures renders the failures aggregate as readable text: a header with
// the total and the filters, two tables (by exit code and by worker) and a block
// for each signature group with its example task and the sample ids.
func printFailures(out io.Writer, r *api.FailuresResponse) {
	fmt.Fprintf(out, "Failed tasks: %d  (since %s, until %s, limit %d)\n",
		r.TotalFailedTasks, fmtFilterTime(r.Filters.Since), fmtFilterTime(r.Filters.Until), r.Filters.Limit)
	if !r.GeneratedAt.IsZero() {
		fmt.Fprintf(out, "Computed at %s UTC\n", r.GeneratedAt.UTC().Format("2006-01-02 15:04:05"))
	}

	if r.TotalFailedTasks == 0 {
		fmt.Fprintln(out, "\nNo failed tasks.")
		return
	}

	if len(r.ByExitCode) > 0 {
		fmt.Fprintln(out, "\nBy exit code:")
		w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
		fmt.Fprintln(w, "  EXIT\tCOUNT")
		for _, e := range r.ByExitCode {
			fmt.Fprintf(w, "  %s\t%d\n", exitCodeLabel(e.ExitCode), e.Count)
		}
		_ = w.Flush()
	}

	if len(r.ByWorker) > 0 {
		fmt.Fprintln(out, "\nBy worker:")
		w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
		fmt.Fprintln(w, "  WORKER\tCOUNT")
		for _, wk := range r.ByWorker {
			fmt.Fprintf(w, "  %s\t%d\n", wk.WorkerID, wk.Count)
		}
		_ = w.Flush()
	}

	if len(r.BySignature) > 0 {
		fmt.Fprintf(out, "\nBy signature (%d):\n", len(r.BySignature))
		for _, g := range r.BySignature {
			printSignatureGroup(out, g)
		}
	}
}

// printSignatureGroup renders a signature group: the signature with its count,
// the exit codes present, the example task (with its execution for the
// drill-down) and the sample ids. The example's null fields fall back to "-".
func printSignatureGroup(out io.Writer, g api.FailureSignatureGroup) {
	ex := g.Example
	fmt.Fprintf(out, "\n  [%dx] %s\n", g.Count, g.Signature)
	fmt.Fprintf(out, "       exit codes: %s\n", joinExitCodes(g.ExitCodes))
	fmt.Fprintf(out, "       example: task %d  execution %s  worker %s  exit %s\n",
		ex.TaskID, strPtrOr(ex.ExecutionID, "-"), strPtrOr(ex.WorkerID, "-"), exitCodeLabel(ex.ExitCode))
	if cl := strPtrOr(ex.CommandLine, ""); cl != "" {
		fmt.Fprintf(out, "       command: %s\n", cl)
	}
	if tail := lastNonEmptyLine(ex.StderrTail); tail != "" {
		fmt.Fprintf(out, "       stderr: %s\n", tail)
	}
	if len(g.SampleTaskIDs) > 0 {
		fmt.Fprintf(out, "       samples: %s\n", joinIDs(g.SampleTaskIDs))
	}
}

// fmtFilterTime renders an optional date filter ("-" if not applied). The
// server returns the EFFECTIVE window in filters.since (the current day by
// default), so this summary shows the window actually applied; it is anchored to
// UTC and labeled so it is unambiguous. Nuance: if the client did not pass
// --until, filters.until is the instant at which the server resolved the request
// and did NOT bound the result (this keeps app<->DB clock skew from hiding a
// freshly closed task); with an explicit --until the echo and the filter match.
func fmtFilterTime(t *api.Time) string {
	if t == nil || t.IsZero() {
		return "-"
	}
	return t.UTC().Format("2006-01-02 15:04:05") + " UTC"
}

// exitCodeLabel renders an exit code that may be null (a group without a code) as "-".
func exitCodeLabel(code *int) string {
	if code == nil {
		return "-"
	}
	return strconv.Itoa(*code)
}

// joinExitCodes joins a group's exit codes (each may be null) into a readable
// list, e.g. "1, 137, -".
func joinExitCodes(codes []*int) string {
	if len(codes) == 0 {
		return "-"
	}
	parts := make([]string, len(codes))
	for i, c := range codes {
		parts[i] = exitCodeLabel(c)
	}
	return strings.Join(parts, ", ")
}

// strPtrOr returns *s, or def if s is nil.
func strPtrOr(s *string, def string) string {
	if s == nil {
		return def
	}
	return *s
}

// lastNonEmptyLine returns the last non-empty line of s (stderr_tail may carry
// several lines; for the summary we show only the most recent one with text).
func lastNonEmptyLine(s string) string {
	lines := strings.Split(s, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if line := strings.TrimSpace(lines[i]); line != "" {
			return line
		}
	}
	return ""
}
