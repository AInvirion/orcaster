// Package config manages orcaster's profiles in ~/.config/orcaster/.
//
// Since 1 PAT == 1 workspace, each PROFILE binds host + workspace + its token.
// A user with N workspaces has N profiles. The non-secret part lives in
// config.toml; the token lives apart in `credentials` with 0600 permissions (AWS
// CLI style), so an accidental dump of the config does not leak credentials.
//
// Python->Go glossary: this is ~ a module with dataclasses + (de)serialization
// functions. `map[string]X` values are dicts; the `toml:"..."` tags map names the
// way a Pydantic-settings model would.
package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/pelletier/go-toml/v2"
)

// File names inside the config directory.
const (
	configFile      = "config.toml"
	credentialsFile = "credentials" // no extension, AWS style; TOML content
	credentialsMode = 0o600         // rw for the owner only
	dirMode         = 0o700
)

// Profile is the non-secret part of a profile: which server and workspace it
// points to.
type Profile struct {
	Host      string `toml:"host"`
	Workspace string `toml:"workspace"`
}

// Config is the content of config.toml.
type Config struct {
	Active   string             `toml:"active"`
	Profiles map[string]Profile `toml:"profiles"`
}

// credEntry is an entry of `credentials` ([<profile>] token = "...").
type credEntry struct {
	Token string `toml:"token"`
}

// Store is the loaded view of config + credentials, ready to resolve/mutate.
type Store struct {
	dir   string
	cfg   Config
	creds map[string]credEntry
}

// Resolved is the effective profile after applying env overrides.
type Resolved struct {
	Profile   string
	Host      string
	Workspace string
	Token     string
	// HostFromEnv / TokenFromEnv report whether ORCASTER_HOST / ORCASTER_TOKEN
	// overrode what was stored. `auth status` uses
	// them to say exactly which part of the displayed identity comes from the
	// environment and not from the profile on disk.
	HostFromEnv  bool
	TokenFromEnv bool
}

// Config directory name and environment variable prefix.
const (
	appName   = "orcaster"
	envPrefix = "ORCASTER_"
)

// Getenv reads the ORCASTER_<suffix> variable.
func Getenv(suffix string) string {
	return os.Getenv(envPrefix + suffix)
}

// baseDir returns the parent config directory: XDG_CONFIG_HOME or ~/.config.
func baseDir() (string, error) {
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		return xdg, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolving user home: %w", err)
	}
	return filepath.Join(home, ".config"), nil
}

// Dir returns the config directory that is WRITTEN to, honoring
// XDG_CONFIG_HOME (default ~/.config/orcaster). It is the equivalent of
// click.get_app_dir / appdirs in Python.
func Dir() (string, error) {
	base, err := baseDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, appName), nil
}

// Load reads config + credentials from disk. If they do not exist yet, it
// returns an empty Store (not an error: it is the pre-login state).
func Load() (*Store, error) {
	dir, err := Dir()
	if err != nil {
		return nil, err
	}
	s := &Store{
		dir:   dir,
		cfg:   Config{Profiles: map[string]Profile{}},
		creds: map[string]credEntry{},
	}
	if err := readTOML(filepath.Join(dir, configFile), &s.cfg); err != nil {
		return nil, err
	}
	if s.cfg.Profiles == nil {
		s.cfg.Profiles = map[string]Profile{}
	}
	if err := readTOML(filepath.Join(dir, credentialsFile), &s.creds); err != nil {
		return nil, err
	}
	return s, nil
}

// readTOML deserializes a TOML file into out. A missing file == no-op (out stays
// as it was). In Go, "file does not exist" is detected with os.IsNotExist.
func readTOML(path string, out any) error {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	if err := toml.Unmarshal(data, out); err != nil {
		return fmt.Errorf("parsing %s: %w", path, err)
	}
	return nil
}

// Resolve computes the effective profile. Precedence (CI-friendly):
// ORCASTER_TOKEN / ORCASTER_HOST from the environment win over what is stored. The profile is chosen
// by: explicit override > config.active > "default".
func (s *Store) Resolve(profileOverride string) (Resolved, error) {
	name := profileOverride
	if name == "" {
		name = s.cfg.Active
	}
	if name == "" {
		name = "default"
	}

	r := Resolved{Profile: name}
	p, profileExists := s.cfg.Profiles[name]
	if profileExists {
		r.Host = p.Host
		r.Workspace = p.Workspace
	}
	if c, ok := s.creds[name]; ok {
		r.Token = c.Token
	}

	// Env overrides (scripting / CI).
	if h := Getenv("HOST"); h != "" {
		r.Host = h
		r.HostFromEnv = true
	}
	if t := Getenv("TOKEN"); t != "" {
		r.Token = t
		r.TokenFromEnv = true
	}

	if r.Host == "" || r.Token == "" {
		// An env override supplied part of the identity but the other half is
		// missing: the problem is not the profile (the identity comes from the
		// environment, not from disk) but the half-set override. Saying "the
		// profile does not exist" would mislead.
		if r.HostFromEnv || r.TokenFromEnv {
			missing := envPrefix + "TOKEN"
			if r.Host == "" {
				missing = envPrefix + "HOST"
			}
			return r, fmt.Errorf(
				"incomplete env override: %s is not set (set it, or run `orcaster auth login`)",
				missing)
		}
		// The named profile (via --profile or the stored active one) is not in
		// the config: the precise error is "does not exist", same as `config use`.
		// Only the implicit "default" of a freshly installed CLI (no override) is
		// an expected pre-login state, not a mistyped profile.
		if !profileExists {
			if profileOverride == "" && name == "default" {
				return r, fmt.Errorf(
					"no active profile: run `orcaster auth login` or set ORCASTER_HOST/ORCASTER_TOKEN")
			}
			return r, fmt.Errorf("profile %q does not exist", name)
		}
		return r, fmt.Errorf(
			"profile %q has no host or token: run `orcaster auth login` or set ORCASTER_HOST/ORCASTER_TOKEN",
			name,
		)
	}
	return r, nil
}

// SaveProfile creates/updates a profile (host+workspace in config.toml, token in
// credentials 0600) and makes it the active one. It persists both files
// atomically per file (write-rename).
func (s *Store) SaveProfile(name, host, workspace, token string) error {
	s.cfg.Profiles[name] = Profile{Host: host, Workspace: workspace}
	s.cfg.Active = name
	s.creds[name] = credEntry{Token: token}
	return s.flush()
}

// SetActive changes the active profile (for `config use`). It fails if the
// profile does not exist.
func (s *Store) SetActive(name string) error {
	if _, ok := s.cfg.Profiles[name]; !ok {
		return fmt.Errorf("profile %q does not exist", name)
	}
	s.cfg.Active = name
	return s.flush()
}

// RemoveProfile deletes a profile and its token (for `auth logout`). If it was
// the active one, it leaves active empty (recomputed as "default" on the next
// Resolve).
func (s *Store) RemoveProfile(name string) error {
	delete(s.cfg.Profiles, name)
	delete(s.creds, name)
	if s.cfg.Active == name {
		s.cfg.Active = ""
	}
	return s.flush()
}

// Active returns the active profile name and Profiles the list of profiles (for
// `config list`).
func (s *Store) Active() string               { return s.cfg.Active }
func (s *Store) Profiles() map[string]Profile { return s.cfg.Profiles }

// flush writes both files. credentials gets 0600; the directory 0700.
func (s *Store) flush() error {
	if err := os.MkdirAll(s.dir, dirMode); err != nil {
		return fmt.Errorf("creating %s: %w", s.dir, err)
	}
	if err := writeTOML(filepath.Join(s.dir, configFile), s.cfg, 0o644); err != nil {
		return err
	}
	return writeTOML(filepath.Join(s.dir, credentialsFile), s.creds, credentialsMode)
}

// writeTOML serializes v and writes it with write-then-rename (atomic: the final
// file is never left half-written if the process dies).
func writeTOML(path string, v any, mode os.FileMode) error {
	data, err := toml.Marshal(v)
	if err != nil {
		return fmt.Errorf("serializing %s: %w", path, err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, mode); err != nil {
		return fmt.Errorf("writing %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("renaming %s: %w", path, err)
	}
	// WriteFile honors the umask; we force the exact mode (key for the 0600).
	return os.Chmod(path, mode)
}
