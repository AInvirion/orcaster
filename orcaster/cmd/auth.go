package cmd

import (
	"bufio"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
	"github.com/AInvirion/orcaster/orcaster/internal/config"
	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// nearExpiryWindow is how far ahead `auth status`/`login` warn about a PAT that
// is about to expire. An orcaster PAT expires: a warning 7 days ahead leaves room
// to rotate it before everything starts returning 401.
const nearExpiryWindow = 7 * 24 * time.Hour

var (
	loginHost  string
	loginToken string
)

var authCmd = &cobra.Command{
	Use:   "auth",
	Short: "Authenticate orcaster with a Personal Access Token",
}

var authLoginCmd = &cobra.Command{
	Use:   "login",
	Short: "Log in by pasting a Personal Access Token",
	Long: "Validates the token against the server, then stores a profile.\n" +
		"The workspace is derived from the token (1 token = 1 workspace).\n" +
		"The profile name defaults to the workspace slug; override it with --profile.",
	RunE: runAuthLogin,
}

var authStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show the identity of the active profile",
	RunE:  runAuthStatus,
}

var authLogoutCmd = &cobra.Command{
	Use:   "logout",
	Short: "Remove the stored credentials for a profile",
	Long: "Deletes the active profile (or the one given with --profile) from disk,\n" +
		"including its token. Does not contact the server.",
	RunE: runAuthLogout,
}

func init() {
	authLoginCmd.Flags().StringVar(&loginHost, "host", "", "server base URL (e.g. https://orca.example.com)")
	authLoginCmd.Flags().StringVar(&loginToken, "token", "", "token (omit to be prompted; also via ORCASTER_TOKEN)")
	authCmd.AddCommand(authLoginCmd, authStatusCmd, authLogoutCmd)
	rootCmd.AddCommand(authCmd)
}

func runAuthLogin(cmd *cobra.Command, _ []string) error {
	if loginHost == "" {
		return fmt.Errorf("--host is required")
	}

	token := loginToken
	if token == "" {
		token = config.Getenv("TOKEN")
	}
	if token == "" {
		var err error
		token, err = readSecret("Paste your token: ")
		if err != nil {
			return fmt.Errorf("reading token: %w", err)
		}
	}
	if token == "" {
		return fmt.Errorf("no token provided")
	}

	// Validate the token against the server before persisting anything.
	client := api.New(loginHost, token)
	who, err := client.Whoami(cmd.Context())
	if err != nil {
		return fmt.Errorf("token validation failed: %w", err)
	}

	// The profile name: --profile if given, otherwise the workspace slug.
	profileName := profileFlag
	if profileName == "" {
		profileName = who.Workspace.Slug
	}

	store, err := config.Load()
	if err != nil {
		return err
	}
	if err := store.SaveProfile(profileName, loginHost, who.Workspace.Slug, token); err != nil {
		return fmt.Errorf("saving profile: %w", err)
	}

	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "Logged in to %s as %s (workspace %s, role %s)\n",
		loginHost, who.User.DisplayName, who.Workspace.Slug, who.Role)
	fmt.Fprintf(out, "Active profile: %s\n", profileName)
	// Warn already at login if the token is about to expire, so the operator
	// rotates it before it starts returning 401. Only if there is data and it is near.
	if note := nearExpiryNote(who.Token.ExpiresAt, time.Now()); note != "" {
		fmt.Fprintf(out, "Warning: token%s\n", note)
	}
	return nil
}

func runAuthStatus(cmd *cobra.Command, _ []string) error {
	client, resolved, err := newClient()
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()

	// If ORCASTER_HOST/ORCASTER_TOKEN overrode the profile, say exactly which part
	// of the identity below comes from the environment and not from disk. It is
	// computed before calling the server because the failure diagnostics block
	// also uses it.
	profileLine := resolved.Profile
	switch {
	case resolved.HostFromEnv && resolved.TokenFromEnv:
		profileLine += " (host+token from env)"
	case resolved.HostFromEnv:
		profileLine += " (host from env)"
	case resolved.TokenFromEnv:
		profileLine += " (token from env)"
	}

	who, err := client.Whoami(cmd.Context())
	if err != nil {
		// The server did not validate the token: instead of printing nothing, show
		// what is known from disk/environment and why validation failed. It is
		// still a failure: we return the error at the end for a non-zero exit code
		// (scripts detect it; the root.go handler also prints "error: <msg>" on top).
		fmt.Fprintf(out, "Profile:   %s\n", profileLine)
		fmt.Fprintf(out, "Host:      %s\n", resolved.Host)
		fmt.Fprintf(out, "Token:     %s\n", tokenPrefix(resolved.Token))
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.Status == http.StatusUnauthorized {
			// The server does not reveal whether the token is invalid, expired or
			// revoked: a 401 covers them all.
			fmt.Fprintln(out, "Status:    server rejected the token (invalid, expired, or revoked)")
		} else {
			// Network down, DNS, mistyped host, 5xx: validation could not be completed.
			fmt.Fprintf(out, "Status:    could not validate token: %v\n", err)
		}
		return err
	}

	fmt.Fprintf(out, "Profile:   %s\n", profileLine)
	fmt.Fprintf(out, "Host:      %s\n", resolved.Host)
	fmt.Fprintf(out, "User:      %s (%s)\n", who.User.DisplayName, who.User.ID)
	fmt.Fprintf(out, "Workspace: %s (%s)\n", who.Workspace.Name, who.Workspace.Slug)
	fmt.Fprintf(out, "Role:      %s\n", who.Role)
	if who.Token.Label != "" {
		fmt.Fprintf(out, "Token:     %s\n", who.Token.Label)
	}
	// Expires only if the server projects expires_at (nil = old server or a token
	// without expiry): the line is omitted, just as the empty label is omitted
	// above. With data, the date is shown and, if it is near or already past, the
	// warning note.
	if who.Token.ExpiresAt != nil && !who.Token.ExpiresAt.IsZero() {
		fmt.Fprintf(out, "Expires:   %s%s\n",
			who.Token.ExpiresAt.Format("2006-01-02"),
			nearExpiryNote(who.Token.ExpiresAt, time.Now()))
	}
	return nil
}

// tokenPrefix returns a safe prefix of the token for diagnostics: NEVER the full
// token. It shows the first 10 characters followed by "…". A real PAT is long
// (orcaster_pat_...), so 10 characters do not compromise it. For a token of 10
// characters or fewer, showing the prefix would reveal the whole value (no hidden
// remainder is left), so it is masked entirely.
func tokenPrefix(token string) string {
	if token == "" {
		return "(none)"
	}
	const n = 10
	if len(token) <= n {
		return "(redacted)"
	}
	return token[:n] + "…"
}

// nearExpiryNote returns a parenthesized note for a token whose expiry is near
// (within nearExpiryWindow) or already past, or "" if it is far off or there is
// no data. now is injected so it can be tested without depending on the clock.
func nearExpiryNote(expires *api.Time, now time.Time) string {
	if expires == nil || expires.IsZero() {
		return ""
	}
	remaining := expires.Sub(now)
	switch {
	case remaining <= 0:
		return " (EXPIRED)"
	case remaining <= nearExpiryWindow:
		days := int(remaining / (24 * time.Hour))
		switch {
		case days <= 0:
			return " (expires in less than a day!)"
		case days == 1:
			return " (expires in 1 day!)"
		default:
			return fmt.Sprintf(" (expires in %d days!)", days)
		}
	default:
		return ""
	}
}

func runAuthLogout(cmd *cobra.Command, _ []string) error {
	store, err := config.Load()
	if err != nil {
		return err
	}

	// The profile to delete: --profile if given, otherwise the active one.
	name := profileFlag
	if name == "" {
		name = store.Active()
	}
	if name == "" {
		return fmt.Errorf("no active profile to log out from")
	}
	if _, ok := store.Profiles()[name]; !ok {
		return fmt.Errorf("profile %q does not exist", name)
	}

	if err := store.RemoveProfile(name); err != nil {
		return fmt.Errorf("removing profile: %w", err)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Logged out of profile %s.\n", name)
	return nil
}

// readSecret asks for a secret without echo if stdin is a TTY; if it comes
// through a pipe (non-TTY), it reads a normal line. The prompt goes to stderr so
// it does not pollute a stdout that might be redirected.
//
// Python->Go glossary: term.ReadPassword ~ getpass.getpass(); term.IsTerminal ~
// sys.stdin.isatty().
func readSecret(prompt string) (string, error) {
	fd := int(os.Stdin.Fd())
	if term.IsTerminal(fd) {
		fmt.Fprint(os.Stderr, prompt)
		b, err := term.ReadPassword(fd)
		fmt.Fprintln(os.Stderr) // newline after the hidden input
		return strings.TrimSpace(string(b)), err
	}
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	return strings.TrimSpace(line), err
}
