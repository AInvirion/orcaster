package cmd

// `orcaster secret` manages the workspace's task secrets.
// Values are write-only: `set` reads them ONLY from stdin (never from argv,
// flags or the environment, which end up in the shell history or in `ps`) and no
// CLI output prints them.
//
// `--worker` sets or deletes a worker's secret (id or name) instead of the
// workspace-wide one. Before using it the CLI checks that the server advertises
// `supports_worker_scope`: an older one ignores the query param and would silently
// touch the workspace secret.

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// maxSecretValueBytes bounds the value read from stdin. A real token is tens or
// hundreds of bytes; 64 KiB leaves room for a PEM or a credentials JSON and cuts
// off a wrong pipe in time (e.g. a binary file).
const maxSecretValueBytes = 64 * 1024

// secretNameRe is the shape of an environment variable name, the same shape the
// server requires of `payload.task_env` and of secret names.
var secretNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// newSecretCmd builds the `secret` tree with its flags bound to local variables
// (not globals), so each test builds a clean tree.
func newSecretCmd() *cobra.Command {
	var setWorker, deleteWorker string
	secretCmd := &cobra.Command{
		Use:   "secret",
		Short: "Manage the workspace's task secrets",
		Long: "Task secrets are stored encrypted on the server and delivered to each task\n" +
			"that declares the name in payload.task_env. Values are write-only: no command\n" +
			"ever prints them.",
	}

	setCmd := &cobra.Command{
		Use:   "set NAME",
		Short: "Create or replace a secret, reading its value from stdin",
		Long: "Reads the value from stdin only, never from arguments, flags or the\n" +
			"environment. On a terminal the value is prompted without echo and the prompt\n" +
			"reads one line only. From a pipe, all of stdin is read and exactly one\n" +
			"trailing newline is removed.\n\n" +
			"A multi-line value (a PEM key, a JSON credential) needs a pipe:\n" +
			"  orcaster secret set NAME < file\n" +
			"Pasted in the prompt, it is refused rather than stored as its first line.\n\n" +
			"With --worker (worker id or name) the secret applies only to that worker and\n" +
			"takes precedence over the workspace-wide secret of the same name.\n\n" +
			"Examples:\n" +
			"  orcaster secret set API_TOKEN\n" +
			"  pass show api/token | orcaster secret set API_TOKEN\n" +
			"  orcaster secret set GCP_CREDENTIALS < service-account.json\n" +
			"  pass show gh/worker-a | orcaster secret set GITHUB_TOKENS --worker worker-a",
		Args: func(_ *cobra.Command, args []string) error {
			// Own message for the most likely error (passing the value as a second
			// argument) and WITHOUT repeating the received arguments.
			if len(args) != 1 {
				return fmt.Errorf("secret set takes exactly one argument, NAME; pass the value on stdin")
			}
			return nil
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSecretSet(cmd, args, setWorker)
		},
	}
	setCmd.Flags().StringVar(&setWorker, "worker", "",
		"set the secret for this worker only (worker id or name)")

	var listJSON bool
	listCmd := &cobra.Command{
		Use:   "list",
		Short: "List the workspace's secrets (names and fingerprints, never values)",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSecretList(cmd, listJSON)
		},
	}
	listCmd.Flags().BoolVar(&listJSON, "json", false, "output raw JSON instead of a table")

	var deleteYes bool
	deleteCmd := &cobra.Command{
		Use:   "delete NAME",
		Short: "Delete a secret",
		Long: "Deletes a secret (irreversible). Tasks that declare the name afterwards fall\n" +
			"back to the workspace-wide secret (for --worker) or to the worker's own\n" +
			"environment. Without --worker only the workspace-wide secret is deleted.\n" +
			"Asks for confirmation unless --yes.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSecretDelete(cmd, args[0], deleteWorker, deleteYes)
		},
	}
	deleteCmd.Flags().BoolVarP(&deleteYes, "yes", "y", false, "skip the confirmation prompt")
	deleteCmd.Flags().StringVar(&deleteWorker, "worker", "",
		"delete the secret of this worker only (worker id or name)")

	secretCmd.AddCommand(setCmd, listCmd, deleteCmd)
	return secretCmd
}

func init() {
	rootCmd.AddCommand(newSecretCmd())
}

// validateSecretName mirrors the server's validation to fail before the network.
// First the known token prefix (envTokenPrefixRe, the same guard as --env and as
// the server's validate_secret_name): a token pasted in place of the name never
// shows up in the PUT URL or in the error message. The ORCH_ prefix is reserved
// for worker/control plane secrets and is compared case-insensitively, as on the
// server (which is the authority and also applies the denylist of reserved
// names).
func validateSecretName(name string) error {
	if envTokenPrefixRe.MatchString(name) {
		return fmt.Errorf("invalid secret name: looks like a credential (known token prefix), not a variable name; use the variable name only")
	}
	if err := validateSecretNamePattern(name); err != nil {
		return err
	}
	if strings.HasPrefix(strings.ToUpper(name), "ORCH_") {
		return fmt.Errorf("invalid secret name: uses the reserved ORCH_ prefix (in any case)")
	}
	return nil
}

// validateSecretNamePattern validates only the shape of the name. It is what
// delete requires, like the server: a row saved before its name became reserved
// must still be deletable.
func validateSecretNamePattern(name string) error {
	if !secretNameRe.MatchString(name) {
		// Without repeating the name: it may be NAME=value or a token with a dash.
		return fmt.Errorf("invalid secret name: use letters, digits and underscores, not starting with a digit")
	}
	return nil
}

// stdinIsTTY reports whether in is a terminal. A reader that is not an *os.File
// (the tests) counts as a pipe.
func stdinIsTTY(in io.Reader) bool {
	f, ok := in.(*os.File)
	return ok && term.IsTerminal(int(f.Fd()))
}

// readSecretValue obtains the secret's value. On a TTY readHidden asks for it
// (without echo); otherwise it reads all of in up to maxSecretValueBytes. In both
// cases it strips exactly one trailing newline ("\n" or "\r\n") and rejects an
// empty value. Errors never include the value.
func readSecretValue(in io.Reader, isTTY bool, readHidden func() ([]byte, error)) (string, error) {
	// The longest valid input is the maximum value followed by "\r\n". One more
	// byte is read to tell "exactly at the limit" from "over it" without loading
	// an arbitrarily large pipe into memory.
	const maxRaw = maxSecretValueBytes + 2
	var raw []byte
	var err error
	if isTTY {
		raw, err = readHidden()
	} else {
		raw, err = io.ReadAll(io.LimitReader(in, maxRaw+1))
	}
	if err != nil {
		return "", fmt.Errorf("reading secret value: %w", err)
	}
	if len(raw) > maxRaw {
		return "", fmt.Errorf("secret value exceeds %d bytes", maxSecretValueBytes)
	}

	value := string(raw)
	switch {
	case strings.HasSuffix(value, "\r\n"):
		value = value[:len(value)-2]
	case strings.HasSuffix(value, "\n"):
		value = value[:len(value)-1]
	}
	if len(value) > maxSecretValueBytes {
		return "", fmt.Errorf("secret value exceeds %d bytes", maxSecretValueBytes)
	}
	if value == "" {
		return "", fmt.Errorf("empty secret value")
	}
	if isTTY && looksLikeMultilineCutToOneLine(value) {
		return "", errPromptMultiline
	}
	return value, nil
}

// errPromptMultiline: the (one-line) prompt received the first line of a pasted
// multi-line value. Storing it would produce a broken secret that the task only
// discovers when it parses it.
var errPromptMultiline = errors.New("multi-line value in the one-line prompt")

// looksLikeMultilineCutToOneLine recognizes the first line of a PEM (or a PEM
// without line breaks) and the lone first line of a JSON. Same rule as the
// server's `validate_secret_value`, which applies it to every value.
func looksLikeMultilineCutToOneLine(value string) bool {
	trimmed := strings.TrimSpace(value)
	if strings.HasPrefix(trimmed, "-----BEGIN") && !strings.ContainsAny(value, "\r\n") {
		return true
	}
	return trimmed == "{" || trimmed == "["
}

// pasteSettle is how long to wait before discarding the rest of the pasted text:
// a large paste reaches the terminal in several chunks.
const pasteSettle = 200 * time.Millisecond

// discardPastedRest discards the pasted lines the prompt did not read. Without
// it, the shell reads them when orcaster exits and runs each line of the secret
// as a command (and stores it in its history).
func discardPastedRest(fd int) error {
	time.Sleep(pasteSettle)
	return flushTerminalInput(fd)
}

// promptMultilineError is the error for a multi-line value pasted into the
// prompt. discardErr is the result of discarding the rest of the pasted text.
func promptMultilineError(name string, discardErr error) error {
	msg := fmt.Sprintf("the prompt reads one line only and this looks like the first line "+
		"of a multi-line value (PEM key or JSON); nothing was changed. "+
		"Pipe the value instead: orcaster secret set %s < file", name)
	if discardErr != nil {
		msg += fmt.Sprintf(". The rest of the pasted value could not be discarded (%v) and may "+
			"still be in the terminal input: clear the shell line without running it", discardErr)
	}
	return errors.New(msg)
}

// validateWorkerFlag rejects `--worker ""`: an explicit empty value must not
// silently fall back to the workspace secret. It also rejects a value with
// leading or trailing spaces, without trimming it: worker names are not trimmed
// on registration, so "worker-a " and "worker-a" are different workers and the
// server answers 422 invalid_worker to that reference.
func validateWorkerFlag(cmd *cobra.Command, worker string) error {
	if !cmd.Flags().Changed("worker") {
		return nil
	}
	if strings.TrimSpace(worker) == "" {
		return fmt.Errorf("--worker needs a worker id or name; omit it for the workspace-wide secret")
	}
	if worker != strings.TrimSpace(worker) {
		return fmt.Errorf("--worker %q has leading or trailing whitespace; "+
			"pass the worker id for a worker whose name has it", worker)
	}
	return nil
}

// ensureWorkerScope checks that the server supports the per-worker scope before
// writing. An older server does not advertise supports_worker_scope and would
// ignore `?worker=`: it would set or delete the workspace secret.
func ensureWorkerScope(cmd *cobra.Command, client *api.Client) error {
	resp, err := client.ListSecrets(cmd.Context())
	if err != nil {
		return explainSecretError(err, "", "")
	}
	if !resp.WorkerScopeSupported() {
		return fmt.Errorf("this server does not support worker-scoped secrets " +
			"(--worker needs a newer server); nothing was changed")
	}
	return nil
}

// secretScope is the SCOPE column of `secret list`: `workspace` or
// `worker:<name>` (the id if the server carries no name).
func secretScope(s api.Secret) string {
	if s.WorkerID == nil {
		return "workspace"
	}
	return "worker:" + workerLabel(s)
}

// workerLabel is the visible name of the worker of a worker-scoped secret, or
// its id if the server carries no name.
func workerLabel(s api.Secret) string {
	if s.WorkerName != nil && *s.WorkerName != "" {
		return *s.WorkerName
	}
	return *s.WorkerID
}

// forWorker is the " for worker X" suffix of messages; "" without a worker.
func forWorker(worker string) string {
	if worker == "" {
		return ""
	}
	return " for worker " + worker
}

// explainSecretError turns API errors with a known cause into an actionable
// message. The rest pass through as they are. worker is the value of --worker.
func explainSecretError(err error, name, worker string) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return err
	}
	switch {
	case apiErr.Code == "worker_not_found":
		return fmt.Errorf("worker %s not found in this workspace", worker)
	case apiErr.Code == "worker_ambiguous":
		return fmt.Errorf("worker name %s matches more than one worker; pass the worker id instead", worker)
	case apiErr.Code == "worker_deleted":
		return fmt.Errorf("worker %s is deleted; it cannot receive new secrets", worker)
	case apiErr.Code == "invalid_worker":
		return fmt.Errorf("--worker needs a worker id or name")
	case apiErr.Status == http.StatusServiceUnavailable && apiErr.Code == "task_secrets_unconfigured":
		return fmt.Errorf("task secrets are not configured on this server " +
			"(no keys file at TASK_SECRETS_KEYS_PATH); ask the server operator to set it up")
	case apiErr.Status == http.StatusForbidden:
		return fmt.Errorf("permission denied: setting or deleting secrets requires the workspace owner role (%s)",
			apiErr.Error())
	case apiErr.Status == http.StatusNotFound && name != "":
		return fmt.Errorf("secret %s not found%s", name, forWorker(worker))
	case apiErr.Status == http.StatusNotFound:
		// Without a name (list, set) a 404 that is not a worker one is the missing
		// route: the server predates the secrets API.
		return errNoSecretsAPI
	}
	return err
}

// errNoSecretsAPI: the server does not have the /api/v1/secrets routes.
var errNoSecretsAPI = errors.New("this server does not support task secrets " +
	"(it predates the secrets API; upgrade the server); nothing was changed")

// explainDeleteError translates the error of a DELETE. A 404 arrives the same
// whether the secret does not exist or the server lacks the route, so before
// saying "not found" the list is queried: only a server with the API has it.
func explainDeleteError(cmd *cobra.Command, client *api.Client, err error, name, worker string) error {
	var apiErr *api.APIError
	// With --worker the preflight already listed: the API exists.
	if worker != "" || !errors.As(err, &apiErr) || apiErr.Status != http.StatusNotFound {
		return explainSecretError(err, name, worker)
	}
	_, listErr := client.ListSecrets(cmd.Context())
	var listAPIErr *api.APIError
	switch {
	case listErr == nil:
		return explainSecretError(err, name, worker)
	case errors.As(listErr, &listAPIErr) && listAPIErr.Status == http.StatusNotFound:
		return errNoSecretsAPI
	}
	// Whether the API exists could not be determined: return what the server said,
	// without turning it into "not found" or "not supported".
	return fmt.Errorf("deleting secret %s: %w (could not check whether this server "+
		"supports task secrets: %v)", name, err, listErr)
}

func runSecretSet(cmd *cobra.Command, args []string, worker string) error {
	name := args[0]
	if err := validateSecretName(name); err != nil {
		return err
	}
	if err := validateWorkerFlag(cmd, worker); err != nil {
		return err
	}
	// The client is resolved before asking for the value: with a broken config
	// there is no point in making the user type the secret.
	client, _, err := newClient()
	if err != nil {
		return err
	}
	if worker != "" {
		// Before asking for the value: with an old server there is no point in typing it.
		if err := ensureWorkerScope(cmd, client); err != nil {
			return err
		}
	}

	in := cmd.InOrStdin()
	errOut := cmd.ErrOrStderr()
	value, err := readSecretValue(in, stdinIsTTY(in), func() ([]byte, error) {
		// The prompt goes to stderr so it does not pollute a redirected stdout.
		fmt.Fprintf(errOut, "Value for %s: ", name)
		b, err := term.ReadPassword(int(in.(*os.File).Fd()))
		fmt.Fprintln(errOut) // newline after the hidden input
		return b, err
	})
	if errors.Is(err, errPromptMultiline) {
		return promptMultilineError(name, discardPastedRest(int(in.(*os.File).Fd())))
	}
	if err != nil {
		return err
	}

	s, err := client.SetSecret(cmd.Context(), name, value, worker)
	if err != nil {
		return explainSecretError(err, "", worker)
	}
	if worker != "" && s.WorkerID == nil {
		// Defensive: the preflight said yes, but the response carries no scope.
		return fmt.Errorf("the server stored secret %s without a worker scope; "+
			"check `orcaster secret list` and delete the workspace-wide row if unintended", s.Name)
	}
	scope := ""
	if s.WorkerID != nil {
		scope = forWorker(workerLabel(*s))
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Secret %s set%s (fingerprint %s).\n", s.Name, scope, s.Fingerprint)
	return nil
}

func runSecretList(cmd *cobra.Command, asJSON bool) error {
	client, _, err := newClient()
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if asJSON {
		raw, err := client.ListSecretsRaw(cmd.Context())
		if err != nil {
			return explainSecretError(err, "", "")
		}
		return printRawJSON(out, raw)
	}
	resp, err := client.ListSecrets(cmd.Context())
	if err != nil {
		return explainSecretError(err, "", "")
	}
	// The notice goes to stderr so the table on stdout stays parseable.
	if resp.KeysUnconfigured() {
		fmt.Fprintln(cmd.ErrOrStderr(), secretsKeysUnconfiguredNote)
	}
	printSecretTable(out, resp)
	return nil
}

// secretsKeysUnconfiguredNote is the `secret list` notice when the server
// answers keys_configured=false.
const secretsKeysUnconfiguredNote = "Note: task secrets are disabled on this server " +
	"(TASK_SECRETS_KEYS_PATH is not configured). Stored secrets are listed but " +
	"cannot be set or delivered to tasks."

// printSecretTable prints the secrets with the same tabwriter mechanics and the
// same date format as printTaskTable. SCOPE is `workspace` or `worker:<name>`; an
// older server carries no scope and everything is `workspace`.
func printSecretTable(out io.Writer, resp *api.SecretListResponse) {
	if len(resp.Items) == 0 {
		fmt.Fprintln(out, "No secrets.")
		return
	}
	w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "NAME\tSCOPE\tFINGERPRINT\tUPDATED\tUPDATED BY")
	for _, s := range resp.Items {
		updated := "-"
		if !s.UpdatedAt.IsZero() {
			updated = s.UpdatedAt.Format("2006-01-02 15:04:05")
		}
		by := "-"
		if s.UpdatedBy != nil && *s.UpdatedBy != "" {
			by = *s.UpdatedBy
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", s.Name, secretScope(s), s.Fingerprint, updated, by)
	}
	_ = w.Flush()
}

func runSecretDelete(cmd *cobra.Command, name, worker string, yes bool) error {
	if err := validateSecretNamePattern(name); err != nil {
		return err
	}
	if err := validateWorkerFlag(cmd, worker); err != nil {
		return err
	}
	in := cmd.InOrStdin()
	out := cmd.OutOrStdout()
	proceed, err := confirmSecretDelete(out, in, name+forWorker(worker), yes, stdinIsTTY(in))
	if err != nil {
		return err
	}
	if !proceed {
		// Confirmation declined: clean exit, nothing deleted.
		return nil
	}

	client, _, err := newClient()
	if err != nil {
		return err
	}
	if worker != "" {
		if err := ensureWorkerScope(cmd, client); err != nil {
			return err
		}
	}
	if err := client.DeleteSecret(cmd.Context(), name, worker); err != nil {
		return explainDeleteError(cmd, client, err, name, worker)
	}
	fmt.Fprintf(out, "Secret %s deleted%s.\n", name, forWorker(worker))
	return nil
}

// confirmSecretDelete follows the convention of `task delete`: --yes skips the
// confirmation without reading stdin; without --yes and without a TTY it refuses;
// on a TTY it asks [y/N] and only "y"/"Y" confirms.
func confirmSecretDelete(out io.Writer, in io.Reader, name string, yes, isTTY bool) (bool, error) {
	if yes {
		return true, nil
	}
	if !isTTY {
		return false, fmt.Errorf("refusing to delete without --yes (no interactive terminal)")
	}
	fmt.Fprintf(out, "About to delete secret %s (irreversible). Continue? [y/N] ", name)
	line, err := bufio.NewReader(in).ReadString('\n')
	if err != nil && err != io.EOF {
		return false, fmt.Errorf("reading confirmation: %w", err)
	}
	answer := strings.TrimSpace(line)
	return answer == "y" || answer == "Y", nil
}
