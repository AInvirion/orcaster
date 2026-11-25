package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// withTempConfig points XDG_CONFIG_HOME at a temporary dir so the user's real
// ~/.config is never touched. t.Setenv restores the env when the test ends
// (equivalent to pytest's monkeypatch.setenv with automatic teardown).
func withTempConfig(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("ORCASTER_HOST", "")
	t.Setenv("ORCASTER_TOKEN", "")
	return filepath.Join(dir, "orcaster")
}

func TestSaveResolveRoundTrip(t *testing.T) {
	confDir := withTempConfig(t)

	s, err := Load()
	if err != nil {
		t.Fatalf("Load() on empty config: %v", err)
	}
	if err := s.SaveProfile("acme", "https://orca.example.com", "acme", "orcaster_pat_xyz"); err != nil {
		t.Fatalf("SaveProfile: %v", err)
	}

	// Reload from disk: the profile persists and resolves fully.
	s2, err := Load()
	if err != nil {
		t.Fatalf("Load() tras guardar: %v", err)
	}
	r, err := s2.Resolve("")
	if err != nil {
		t.Fatalf("Resolve(): %v", err)
	}
	if r.Host != "https://orca.example.com" || r.Token != "orcaster_pat_xyz" || r.Workspace != "acme" {
		t.Errorf("resuelto inesperado: %+v", r)
	}
	if s2.Active() != "acme" {
		t.Errorf("active = %q, want acme", s2.Active())
	}

	// The credentials file MUST be 0600 (do not leak the token).
	info, err := os.Stat(filepath.Join(confDir, credentialsFile))
	if err != nil {
		t.Fatalf("stat credentials: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("credentials permissions = %o, want 600", perm)
	}
}

func TestEnvOverridesProfile(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	_ = s.SaveProfile("acme", "https://saved.example.com", "acme", "orcaster_pat_saved")

	t.Setenv("ORCASTER_HOST", "https://env.example.com")
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_env")

	r, err := s.Resolve("")
	if err != nil {
		t.Fatalf("Resolve with env: %v", err)
	}
	if r.Host != "https://env.example.com" || r.Token != "orcaster_pat_env" {
		t.Errorf("env did not win: %+v", r)
	}
}

func TestResolveMissingProfileErrors(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	if _, err := s.Resolve(""); err == nil {
		t.Fatal("expected an error without profile or env, got none")
	}
}

func TestResolveEnvOverrideFlags(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	_ = s.SaveProfile("acme", "https://saved.example.com", "acme", "orcaster_pat_saved")

	// Without env: identity comes from the profile, no override flag active.
	r, err := s.Resolve("")
	if err != nil {
		t.Fatalf("without env, Resolve(): %v", err)
	}
	if r.HostFromEnv || r.TokenFromEnv {
		t.Errorf("without env: HostFromEnv=%v TokenFromEnv=%v, want both false", r.HostFromEnv, r.TokenFromEnv)
	}

	// Con ORCASTER_TOKEN: TokenFromEnv=true, HostFromEnv=false.
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_env")
	r, err = s.Resolve("")
	if err != nil {
		t.Fatalf("Resolve with ORCASTER_TOKEN: %v", err)
	}
	if !r.TokenFromEnv {
		t.Error("TokenFromEnv = false, want true with ORCASTER_TOKEN set")
	}
	if r.HostFromEnv {
		t.Error("HostFromEnv = true, want false (host comes from the profile)")
	}

	// With ORCASTER_HOST: HostFromEnv=true, TokenFromEnv stays as set by the previous environment.
	t.Setenv("ORCASTER_HOST", "https://env.example.com")
	r, err = s.Resolve("")
	if err != nil {
		t.Fatalf("Resolve with ORCASTER_HOST: %v", err)
	}
	if !r.HostFromEnv {
		t.Error("HostFromEnv = false, want true with ORCASTER_HOST set")
	}
}

func TestResolveUnknownExplicitProfileSaysDoesNotExist(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	_ = s.SaveProfile("acme", "https://orca.example.com", "acme", "orcaster_pat_xyz")

	// --profile ghost (nonexistent) must say "does not exist", like
	// `config use`, not the generic "has no host or token".
	_, err := s.Resolve("ghost")
	if err == nil {
		t.Fatal("Resolve of a nonexistent profile should fail")
	}
	if got := err.Error(); !strings.Contains(got, `profile "ghost" does not exist`) {
		t.Errorf("error = %q, want it to contain 'profile \"ghost\" does not exist'", got)
	}
}

func TestResolvePartialEnvOverrideSaysIncomplete(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()

	// ORCASTER_HOST via env but no token or profile: the host comes from the env,
	// so the error is not "the profile does not exist" but a missing ORCASTER_TOKEN.
	t.Setenv("ORCASTER_HOST", "https://env.example.com")
	_, err := s.Resolve("ghost")
	if err == nil {
		t.Fatal("Resolve with a partial env should fail")
	}
	got := err.Error()
	if !strings.Contains(got, "incomplete env override") || !strings.Contains(got, "ORCASTER_TOKEN") {
		t.Errorf("error = %q, want 'incomplete env override' mentioning ORCASTER_TOKEN", got)
	}
	if strings.Contains(got, "does not exist") {
		t.Errorf("error = %q, should not say 'does not exist' when the host comes from the env", got)
	}
}

func TestRemoveProfile(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	_ = s.SaveProfile("acme", "https://orca.example.com", "acme", "orcaster_pat_xyz")
	if err := s.RemoveProfile("acme"); err != nil {
		t.Fatalf("RemoveProfile: %v", err)
	}
	if _, err := s.Resolve("acme"); err == nil {
		t.Fatal("a removed profile should not resolve")
	}
}

func TestSetActiveSwitchesAndPersists(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	_ = s.SaveProfile("acme", "https://acme.example.com", "acme", "orcaster_pat_a")
	_ = s.SaveProfile("beta", "https://beta.example.com", "beta", "orcaster_pat_b")
	// SaveProfile left "beta" active; switch back to "acme".
	if err := s.SetActive("acme"); err != nil {
		t.Fatalf("SetActive(acme): %v", err)
	}

	// Persists to disk: a clean reload sees "acme" active and both profiles.
	s2, _ := Load()
	if s2.Active() != "acme" {
		t.Errorf("active = %q, want acme", s2.Active())
	}
	if len(s2.Profiles()) != 2 {
		t.Errorf("Profiles() = %d profiles, want 2", len(s2.Profiles()))
	}
}

func TestSetActiveUnknownProfileErrors(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	if err := s.SetActive("ghost"); err == nil {
		t.Fatal("SetActive of a nonexistent profile should fail")
	}
}

func TestGetenvReadsOrcasterVars(t *testing.T) {
	withTempConfig(t)
	for _, name := range []string{"HOST", "TOKEN"} {
		t.Setenv("ORCASTER_"+name, "v-"+name)
		if got := Getenv(name); got != "v-"+name {
			t.Errorf("Getenv(%s) = %q, want the value of ORCASTER_%s", name, got, name)
		}
	}
}

func TestGetenvUnsetReturnsEmpty(t *testing.T) {
	withTempConfig(t)
	if got := Getenv("HOST"); got != "" {
		t.Errorf("Getenv with no env = %q, want empty", got)
	}
}

func TestIncompleteOverrideErrorNamesOrcasterVars(t *testing.T) {
	withTempConfig(t)
	s, _ := Load()
	t.Setenv("ORCASTER_TOKEN", "orcaster_pat_tok")
	_, err := s.Resolve("ghost")
	if err == nil || !strings.Contains(err.Error(), "ORCASTER_HOST") {
		t.Errorf("error = %v, want it to name ORCASTER_HOST", err)
	}
	t.Setenv("ORCASTER_TOKEN", "")
	_, err = (&Store{cfg: Config{Profiles: map[string]Profile{}}}).Resolve("")
	if err == nil || !strings.Contains(err.Error(), "ORCASTER_HOST/ORCASTER_TOKEN") {
		t.Errorf("error = %v, want it to name ORCASTER_HOST/ORCASTER_TOKEN", err)
	}
}

func TestDirHonorsXDGAndDefaultsToOrcaster(t *testing.T) {
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	if d, _ := Dir(); d != filepath.Join(xdg, "orcaster") {
		t.Errorf("Dir with XDG = %q", d)
	}
	home := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("HOME", home)
	if d, _ := Dir(); d != filepath.Join(home, ".config", "orcaster") {
		t.Errorf("Dir without XDG = %q", d)
	}
}
