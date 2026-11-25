package cmd

import (
	"fmt"
	"sort"
	"text/tabwriter"

	"github.com/AInvirion/orcaster/orcaster/internal/config"
	"github.com/spf13/cobra"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Manage orcaster profiles",
}

var configUseCmd = &cobra.Command{
	Use:   "use <profile>",
	Short: "Set the active profile",
	Args:  cobra.ExactArgs(1),
	RunE:  runConfigUse,
}

var configListCmd = &cobra.Command{
	Use:   "list",
	Short: "List configured profiles",
	RunE:  runConfigList,
}

func init() {
	configCmd.AddCommand(configUseCmd, configListCmd)
	rootCmd.AddCommand(configCmd)
}

func runConfigUse(cmd *cobra.Command, args []string) error {
	store, err := config.Load()
	if err != nil {
		return err
	}
	if err := store.SetActive(args[0]); err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Active profile is now %s.\n", args[0])
	return nil
}

func runConfigList(cmd *cobra.Command, _ []string) error {
	store, err := config.Load()
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	profiles := store.Profiles()
	if len(profiles) == 0 {
		fmt.Fprintln(out, "No profiles. Run `orcaster auth login` to create one.")
		return nil
	}

	// Stable order by name for deterministic output.
	names := make([]string, 0, len(profiles))
	for name := range profiles {
		names = append(names, name)
	}
	sort.Strings(names)

	active := store.Active()
	w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "\tPROFILE\tHOST\tWORKSPACE")
	for _, name := range names {
		marker := " " // an asterisk marks the active one (`git branch` style).
		if name == active {
			marker = "*"
		}
		p := profiles[name]
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", marker, name, p.Host, p.Workspace)
	}
	_ = w.Flush()
	return nil
}
