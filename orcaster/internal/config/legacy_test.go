package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLegacyNoticeEmptyWhenNothingLegacy(t *testing.T) {
	withTempConfig(t)
	if got := LegacyNotice([]string{"ORCASTER_HOST=https://x", "PATH=/bin"}); got != "" {
		t.Errorf("LegacyNotice() = %q, want empty", got)
	}
}

func TestLegacyNoticeNamesVariablesAndReplacementsNeverValues(t *testing.T) {
	withTempConfig(t)
	got := LegacyNotice([]string{
		"TASKOR_TOKEN=taskor_pat_dummy",
		"TASKOR_HOST=https://invalid.example",
		"PATH=/bin",
	})
	for _, want := range []string{
		"warning:", "unsupported legacy names",
		"TASKOR_HOST, TASKOR_TOKEN", "ORCASTER_HOST, ORCASTER_TOKEN",
		"orcaster auth login",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("notice lacks %q:\n%s", want, got)
		}
	}
	for _, leaked := range []string{"taskor_pat_dummy", "invalid.example"} {
		if strings.Contains(got, leaked) {
			t.Errorf("notice leaks the value %q:\n%s", leaked, got)
		}
	}
	if strings.Contains(got, "\n") {
		t.Errorf("notice must be one line:\n%s", got)
	}
}

func TestLegacyNoticeNamesTheLegacyConfigDir(t *testing.T) {
	confDir := withTempConfig(t)
	legacyDir := filepath.Join(filepath.Dir(confDir), "taskor")
	if err := os.MkdirAll(legacyDir, 0o700); err != nil {
		t.Fatal(err)
	}
	got := LegacyNotice(nil)
	if !strings.Contains(got, legacyDir) || !strings.Contains(got, confDir) {
		t.Errorf("notice must name %s and %s:\n%s", legacyDir, confDir, got)
	}
	if !strings.Contains(got, "orcaster auth login") {
		t.Errorf("notice must point to 'orcaster auth login':\n%s", got)
	}
}

func TestLegacyNoticeReportsSetButEmptyVariables(t *testing.T) {
	withTempConfig(t)
	// A set-but-empty variable is still set and still ignored: it is reported.
	if got := LegacyNotice([]string{"TASKOR_PROFILE="}); !strings.Contains(got, "TASKOR_PROFILE") {
		t.Errorf("set-but-empty legacy variable not reported:\n%s", got)
	}
}
