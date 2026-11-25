// Package cmd defines orcaster's command tree with cobra.
//
// Python->Go glossary: cobra is ~ typer/click. Each *cobra.Command is a command
// or subcommand; RunE is the handler (the E = it returns an error, which cobra
// prints and turns into a non-zero exit code). Flags are bound to variables as
// in argparse.
package cmd

import (
	"fmt"
	"io"
	"os"

	"github.com/AInvirion/orcaster/orcaster/internal/api"
	"github.com/AInvirion/orcaster/orcaster/internal/config"
	"github.com/spf13/cobra"
)

// profileFlag is bound to the persistent --profile flag (a per-invocation
// override of the active profile). Empty = use the active one from config.
var profileFlag string

var rootCmd = &cobra.Command{
	Use:   "orcaster",
	Short: "Manage orchestrator tasks from your terminal",
	Long: "orcaster is the CLI for creating and managing tasks in a workspace using a\n" +
		"Personal Access Token, without going through the web dashboard.",
	SilenceUsage:  true, // do not dump the full usage on a runtime error
	SilenceErrors: true, // we print the error ourselves in Execute(), formatted
}

// Binary name.
const progName = "orcaster"

func init() {
	rootCmd.PersistentFlags().StringVar(&profileFlag, "profile", "",
		"profile to use (default: the active one)")
}

// invokedAs normalizes argv[0] to a program name: always orcaster.
func invokedAs(argv0 string) string {
	return progName
}

// Run runs the command tree as if the binary were called prog and returns the
// exit code. Separate from Execute so it can be tested without launching a
// process.
func Run(prog string, args []string, stderr io.Writer) int {
	name := invokedAs(prog)
	rootCmd.Use = name
	if args == nil {
		args = []string{} // nil would make cobra read os.Args
	}
	rootCmd.SetArgs(args)
	rootCmd.SetErr(stderr)
	// Unsupported legacy names are ignored; say so instead of failing in silence.
	if notice := config.LegacyNotice(os.Environ()); notice != "" {
		fmt.Fprintln(stderr, notice)
	}
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(stderr, "error: "+err.Error())
		return 1
	}
	return 0
}

// Execute runs the command tree with os.Args and exits with its exit code. It is
// the only entry point main() calls; the name comes from argv[0].
func Execute() {
	os.Exit(Run(os.Args[0], os.Args[1:], os.Stderr))
}

// newClient resolves the effective profile and returns a ready API client. It is
// the helper shared by every command that calls the API.
func newClient() (*api.Client, config.Resolved, error) {
	store, err := config.Load()
	if err != nil {
		return nil, config.Resolved{}, err
	}
	r, err := store.Resolve(profileFlag)
	if err != nil {
		return nil, config.Resolved{}, err
	}
	return api.New(r.Host, r.Token), r, nil
}

var versionCmd = &cobra.Command{
	Use:   "version",
	Short: "Print the orcaster version",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(progName + " " + api.Version)
	},
}

func init() {
	rootCmd.AddCommand(versionCmd)
}
