package config

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Unsupported legacy names: environment variables with this prefix and a config
// directory with this name. orcaster never reads them; LegacyNotice only reports
// that they are present and being ignored, so they are not dropped in silence.
const (
	legacyEnvPrefix = "TASKOR_"
	legacyDirName   = "taskor"
)

// LegacyNotice returns one warning line naming the unsupported legacy names found
// in environ (os.Environ() style KEY=VALUE pairs) and on disk, plus what to use
// instead; "" when there are none. Values are never read nor printed.
func LegacyNotice(environ []string) string {
	var legacy, current []string
	for _, kv := range environ {
		name, _, _ := strings.Cut(kv, "=")
		if suffix, ok := strings.CutPrefix(name, legacyEnvPrefix); ok && suffix != "" {
			legacy = append(legacy, name)
		}
	}
	sort.Strings(legacy)
	for _, name := range legacy {
		current = append(current, envPrefix+strings.TrimPrefix(name, legacyEnvPrefix))
	}
	if base, err := baseDir(); err == nil {
		legacyDir := filepath.Join(base, legacyDirName)
		if info, err := os.Stat(legacyDir); err == nil && info.IsDir() {
			legacy = append(legacy, legacyDir)
			current = append(current, filepath.Join(base, appName))
		}
	}
	if len(legacy) == 0 {
		return ""
	}
	return fmt.Sprintf(
		"warning: ignoring unsupported legacy names %s; use %s instead (run 'orcaster auth login' to create a profile)",
		strings.Join(legacy, ", "), strings.Join(current, ", "))
}
