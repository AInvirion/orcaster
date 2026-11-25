# ops/release

Tarball-based worker deploy tools. They close a one-way flow: local repo,
tarball with manifest, `scp`, host. The host never reaches out to GitHub and
needs no git credential.

- `make-release.sh`: builds the tarball from a clean ref of the repo (runs on
  the operator's machine, touches neither the network nor any host).
- `install-release.sh`: installs the package on the host (runs on the host, as
  root, invoked by `push-release.sh` via `sudo bash`). Besides the worker code
  tree it leaves a root-owned copy of the release (see "Root copy of the
  release: `SOURCE_DIR`").
- `push-release.sh`: builds the package with `make-release.sh` and pushes and
  installs it on each host of the fleet via `scp` + `ssh`.

## Release on GitHub (server, worker and CLI)

Different from the tarball deploy: one GitHub release per tag `vX.Y.Z`
publishes every component, each asset named after its component
(`orcaster-server-*`, `orcaster-worker-*`, `orcaster_worker-*.whl`,
`orcaster-cli-*`, `orcaster-<version>-checksums.txt`). It is run by
`.github/workflows/release.yml` only in `AInvirion/orcaster`; in any other
repository with the same tags the job is skipped. The full procedure is in
[docs/releasing.md](../../docs/releasing.md).

- `tag-release.sh vX.Y.Z [--ref REF] [--remote NAME] [--push]`: creates, on a
  single commit, the release tag `vX.Y.Z` (triggers the workflow, names the
  release) and the Go module tag `orcaster/vX.Y.Z` (the one that resolves
  `go install github.com/AInvirion/orcaster/orcaster@vX.Y.Z`; the Go proxy only
  versions a module in a subdirectory with tags prefixed by that directory).
  It keeps a tag that is already on that commit, never moves one, and with
  `--push` pushes both in a single `git push --atomic`.
- `check-tags.sh vX.Y.Z COMMIT [REMOTE]`: fails if `orcaster/vX.Y.Z` is missing
  on the remote or points to another commit. First step of the workflow.
- `check-licenses.sh [--notice PATH]`: `go-licenses` for each release target;
  fails on a license other than Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause,
  ISC, or on a linked module whose version has no entry in `THIRD_PARTY`. Also
  `make licenses-orcaster`.
- `build-components.sh vX.Y.Z OUT_DIR [--ref REF]`: writes the server and
  worker assets to `OUT_DIR`: `orcaster-server-X.Y.Z.tar.gz` and
  `orcaster-worker-X.Y.Z.tar.gz` (the same bytes, one `make-release.sh` run)
  and `orcaster_worker-X.Y.Z-py3-none-any.whl`. Fails unless the tag equals
  `version` in `server/pyproject.toml` and `client/pyproject.toml`, and when
  `OUT_DIR` is not empty. Needs `uv` and git >= 2.37.
- `release-body.sh vX.Y.Z [CHANGELOG.md]`: prints the release notes:
  `release-body.md` (Server, Worker and CLI sections, with `@VERSION@`
  replaced) followed by the `## [X.Y.Z]` section of `CHANGELOG.md`.
- `release-notes.sh X.Y.Z [CHANGELOG.md]`: prints the `## [X.Y.Z]` section of
  `CHANGELOG.md`; fails if it is missing or empty. `release-body.sh` uses it.
- `check-archives.sh DIST VERSION COMPONENTS`: opens the CLI archives in `DIST`
  and fails if any lacks `LICENSE`, `NOTICE`, `THIRD_PARTY`, `README.md` and the
  binary or is not named `orcaster-cli-<version>-<os>-<arch>`, if `COMPONENTS`
  lacks one of the three component files or holds any other file, or if
  `orcaster-<version>-checksums.txt` does not list every one of them with its
  SHA-256.
- `dry-run.sh [vX.Y.Z] [--out DIR]`: runs `build-components.sh`,
  `release-body.sh`, `goreleaser release --snapshot` and `check-archives.sh`
  locally, without a tag and without publishing. Also `make release-dry-run`.

Procedure: set the version in `server/pyproject.toml` and
`client/pyproject.toml`, make sure the `## [X.Y.Z]` section of `CHANGELOG.md`
exists, run the dry run, and then `ops/release/tag-release.sh vX.Y.Z --push` on
the release commit.

## Fleet inventory: `deploy.local.env`

`push-release.sh` takes the list of hosts and the number of workers of each one
from `deploy.local.env`, at the root of the repo. That file is **not
versioned** (it is in `.gitignore`): it is the operator's personal copy, with
the real hosts of the fleet. `push-release.sh` reads three keys from it,
leaving alone any others the file may have.

```bash
# deploy.local.env
WORKER_HOSTS="worker-1 worker-2"
WORKER_COUNT_worker_1=1
WORKER_COUNT_worker_2=1
# Optional, only for a host whose config does NOT live in /etc/orcaster:
CONFIG_DIR_worker_2=/srv/orcaster
```

- `WORKER_HOSTS`: space-separated list of hosts, used when `push-release.sh`
  runs without `--hosts`.
- `WORKER_COUNT_<host>`: number of workers to pass to `install-release.sh
  --workers` for that host. The variable name converts the hyphens **and the
  dots** of the host to underscores (`worker-1` -> `WORKER_COUNT_worker_1`,
  `worker.example.com` -> `WORKER_COUNT_worker_example_com`), because shell
  variable names admit neither of the two.
- `CONFIG_DIR_<host>`: config directory of that host (`setup.env`, the fleet's
  `env.N` files and the server's pubkey), which `push-release.sh` propagates as
  `install-release.sh --config-dir`. Same conversion as `WORKER_COUNT_<host>`.
  It is **optional**: without the key the host uses the default
  `/etc/orcaster`. Declaring it **empty** is not the same as not declaring it:
  it is an error, because it is almost always a half-written key and silently
  falling back to the default is exactly what this key exists to avoid.

The host name must remain a valid shell identifier after that conversion. A
`user@host` or a bracketed IPv6 address does not qualify, and `push-release.sh`
aborts naming the two keys that would become unreadable: without that, the
inventory of that host would always read empty and its overrides would be lost
without warning. The remedy is to give the host an alias in `~/.ssh/config`
(letters, digits, hyphens or dots) and use that alias in `WORKER_HOSTS` and in
the keys. A bare IP (`10.0.0.5`) does work: the dots are translated.

`--workers` is **optional**: the host is the source of truth. Each host
resolves its own number of workers from the `WORKER_COUNT` in its own
`/etc/orcaster/setup.env`, and it is `install-release.sh` that reads it there
when `--workers` is not passed. `WORKER_COUNT_<host>` in `deploy.local.env` is
an operator **override**, not the canonical value: if the key is defined and
non-empty for a host, `push-release.sh` passes it `--workers <value>`; if it is
missing, it omits the argument entirely and lets the host decide. Defaulting to
1 when the key is missing would silently recreate only worker 1 on a host with
3 workers, leaving the other two on the bind mount of the old code, which is
why the script never does it. The output of the script (including that of
`--dry-run`) always says where the number comes from: from the inventory, or
"resolved by the host".

`push-release.sh --hosts "h1 h2"` replaces the **list of hosts** (ignores
`WORKER_HOSTS` from `deploy.local.env`) to push to a subset without editing the
file. It does not replace the whole inventory: the `WORKER_COUNT_<host>` keys
are still read from `deploy.local.env` when the file exists, so a host with
several workers receives its real count this way too. Without the file,
`--hosts` works the same and each host resolves its number of workers on its
own.

Env file and pubkey in the deploy's `make up client`: `install-release.sh`
passes `ETC_SETUP_ENV_FILE` and `CONFIG_DIR`, not `WORKER_ENV` or
`WORKER_PUBKEY`. The Makefile resolves `$CONFIG_DIR/env[.N]` and
`$CONFIG_DIR/server-ed25519.pub` on its own, so the deploy and a manual
`make up client` on the host read the same files from a single definition.
The installer still passes the two overrides when the installed Makefile is
from an older release without that resolution (a `--ref` rollback), because
that Makefile looks for them in `client/`. An operator can still pass
`WORKER_ENV=` / `WORKER_PUBKEY=` by hand; an explicit override always wins.

`CONFIG_DIR_<host>` exists because the environment is **not** a channel to the
host: `push-release.sh` invokes `sudo bash <staging>/install-release.sh` (and
`sudo` with `env_reset`, the default on Debian, does not propagate variables),
through `ssh` (which does not accept `CONFIG_DIR` either: it is not in
`AcceptEnv`). Without the key, a host whose config lives outside
`/etc/orcaster` could not be deployed this way: the pre-swap gate of
`install-release.sh` would look for `/etc/orcaster/setup.env`.
`cloud-init-worker.sh` allows templating `CONFIG_DIR` and exports it to its
children, so the provisioning suite can indeed leave a host in that state. The
value is validated at the SENDER, before touching the fleet, with the same
guards that `install-release.sh` applies on the host: absolute path, only
letters, digits and `/ . _ -`, and rejection of the root `/` and of `..` as a
component. It ends up interpolated in the string that runs on the host, and a
value with spaces or metacharacters would stop being one argument and become
loose words on the remote side; a bad inventory value aborts in the pre-flight
and not in the middle of a deploy. (The character set matches that of
`WORKER_COUNT_<host>` only in form; that one is an integer, `*[!0-9]*`, and
admits nothing analogous to `/` or `..`.)

`deploy.local.env` is read by sourcing it in a separate shell, never in the
script's own: whatever its content, it never overrides the command line's
`--ref`, `--out`, `--hosts` or `--dry-run`. Only the keys that `push-release.sh`
declares above (`WORKER_HOSTS`, `WORKER_COUNT_<host>` and `CONFIG_DIR_<host>`)
take effect.
An inventory that is present but has invalid syntax (for example an unclosed
quote) is a fatal error: the script aborts naming the file, before building any
tarball, instead of silently treating it as an empty file.

## Root copy of the release: `SOURCE_DIR`

`install-release.sh` runs as root, but `INSTALL_DIR`
(`/opt/orcaster`) belongs to the worker user: it is the tree that the
container mounts and that the swap replaces. If root executed `lib.sh` or a
provisioning suite script from there, a compromised process of the worker user
could edit them and the next run would execute them as root.

That is why `install-release.sh` keeps, besides the `INSTALL_DIR` tree (the
worker's), a **root copy** of the same release in `SOURCE_DIR`, by default
`/root/orcaster` (owned by root, without group or other write), and
**loads `lib.sh` from that copy**, not from `INSTALL_DIR`. It is where the
worker suite is run from afterwards (`run-all-worker.sh`, `run-fleet.sh`,
`06-autostart.sh` as root and `cloud-init-worker.sh`):

```bash
cd /root/orcaster/setup-scripts/debian13-worker && sudo bash run-all-worker.sh
```

**Origin guard.** The worker suite's `lib.sh`, when it runs as root, aborts
before any other effect if its directory or any ancestor up to `/` is not
owned by root or has group or other write, if any `*.sh` or the `Makefile` is
not a regular file owned by root without group or other write, if it was
invoked through a path with symbolic links, or if the suite tree and
`INSTALL_DIR` are the same directory or are nested. Running from
`/opt/orcaster` aborts. The `setup.env` it checks before sourcing is
`$CONFIG_DIR/setup.env`, and every ancestor of `CONFIG_DIR` must also be owned
by root without group or other write: `0640 root:orcaster` inside
`0750 root:orcaster` passes; a `CONFIG_DIR` such as `/srv/orcaster` requires
root-owned ancestors. The guard does not act when the process is not root, so
the `Makefile`'s autostart hook (`06-autostart.sh` as `orcaster`, from
`/opt/orcaster`) keeps working.

**How `SOURCE_DIR` is set**, from highest to lowest precedence:

1. `--source-dir DIR` on the `install-release.sh` command line.
2. The `SOURCE_DIR` environment variable. Like `CONFIG_DIR`, it is not a usable
   channel in production: `sudo` with `env_reset` strips it and `ssh` does not
   propagate it, so `push-release.sh` cannot use it.
3. `SOURCE_DIR` in the host's `setup.env` (`$CONFIG_DIR/setup.env`). It is only
   read with `RESTART=1` (that is, without `--no-restart`), and it warns if it
   does not match an explicit value.
4. The default, `/root/orcaster`.

Consequently, with `push-release.sh` a `SOURCE_DIR` other than the default can
only be achieved through the host's `setup.env`; and in a first delivery with
`--no-restart` (the `setup.env` is not read) the default always applies.

**`SOURCE_DIR` restrictions.** The parent directory must already exist.
`install-release.sh` refuses to use a `SOURCE_DIR` that is a symbolic link,
that contains `.git` (it never replaces a git checkout, for example the
server's on the same host) or that overlaps with `INSTALL_DIR`. Before the
swap it builds `SOURCE_DIR.new` (`cp -a`, `chown -R root:root`, `chmod -R go-w`)
and puts it in place of `SOURCE_DIR` right after the swap of `INSTALL_DIR`; the
cost is one more copy of the tree on disk. Each time it sources the host's
`setup.env`, `install-release.sh` first checks that it is owned by root, without
group or other write, and that its ancestors are owned by root, and it sources
that physical path. The parent directory of `INSTALL_DIR` (where the release is
extracted and the `.orcaster.old` backup stays) and its ancestors must also be
owned by root without group or other write: otherwise it aborts before
extracting.

Manual rollback: `rm -rf INSTALL_DIR && mv .orcaster.old INSTALL_DIR` reverts
only `INSTALL_DIR`. The previous root copy is not kept, so `SOURCE_DIR` stays
with the new release; to revert both, reinstall the previous tarball.

## Pre-swap gate: protected variables without strict isolation

With `RESTART=1`, `install-release.sh` validates the host state before
replacing the code, and any failure aborts with "The installed code was not
touched". Among those validations is the mirror of the worker's startup guard:
a worker that has in its environment a variable from the **list of variables
that require isolation** (non-empty value) and does not have a true
`ORCH_ENFORCE_ISOLATION` refuses to start; the gate stops the deploy before
it recreates such a worker into a crash loop.

The list comes from the `ORCH_ISOLATION_REQUIRED_ENV=<a,b>` line of each
`env.N`, which `run-fleet.sh` stamps from `ISOLATION_REQUIRED_ENV` in
`setup.env` (names separated by commas or spaces, each one
`[A-Za-z_][A-Za-z0-9_]*`). The built-in list is empty: the core names no
variable, and with an empty list the gate does not fire. It can only be added
to; nothing replaces it. A malformed name (a pattern, a `NAME=value`, quotes)
aborts the deploy: it fails closed, like the worker. Matches are by exact name,
never by pattern or by value.

The gate measures, by name and never printing the values, the two channels
that reach the worker's environment on a fleet host: each `env.N` (what the
deploy's `make up client` passes to podman via `--env-file`) and the
`EXTRA_ENV` of `setup.env` (what the next `run-fleet.sh` re-stamps into each
`env.N`; there the list is `ISOLATION_REQUIRED_ENV` of `setup.env`).
`EXTRA_ENV` is measured on its own: an `ORCH_ENFORCE_ISOLATION=true` added by
hand to the `env.N` does not survive the re-stamping. The pydantic values (`1`,
`true`, `yes`, `on`, `t`, `y`, case-insensitive) written as is count as true:
`podman --env-file` does not strip quotes or spaces and pydantic rejects them,
so `"on"` or ` yes ` do not count (only the trailing `\r` of a CRLF file is
discounted). With a repeated key the last occurrence wins, as in the
`--env-file`. If the gate cannot evaluate a file (unreadable, malformed list,
or awk failing), it aborts anyway: it never takes it as clean. `client/.env`
does not enter: the tarball deploy does not use it. The gate has no override
and also runs on a rollback to an earlier version.

The remedy is the worker's `ERROR` one: move the variable to the workspace's
central secret (the tasks that need it declare it in `task_env`), or drain the
queue of tasks without `task_env`, add `ORCH_ENFORCE_ISOLATION=true` to the
`EXTRA_ENV` of `setup.env` and run `run-fleet.sh`. Then retry the deploy. The
message names the variable found, never its value.

## Pre-swap gate: the worker user's `systemd --user` manager

With `RESTART=1`, `install-release.sh` probes the worker user's `systemd --user`
manager before replacing the code, but only if that user's
`~/.config/systemd/user/` has any `container-orcaster-worker*.service` or
`container-orcaster-proxy*.service` file. The probe is
`systemctl --user show-environment` as the worker user, with the same
environment (`HOME`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`) that the
script uses later for the worker. A host without those files has not yet handed
its containers over to systemd and does not need a user manager: it is not
required of it.

If the manager does not respond, the deploy aborts before the swap, with the
installed code intact and nothing stopped. The message names the units, what
`systemctl` answered and the cause with its remedy: without `/run/user/<uid>`
linger is missing (`sudo loginctl enable-linger <user>`); with the directory
present and the manager not responding, `sudo systemctl status
user@<uid>.service` and `sudo systemctl start user@<uid>.service`. The
gate has no override. Before a release, confirm linger on each worker host with
`loginctl show-user orcaster -p Linger` (it must print `Linger=yes`).

## Transport to the host: private staging directory

`push-release.sh` never leaves the installer or the tarball under a fixed
name in `/tmp`. Per host: (1) it creates over ssh a directory with `umask 077;
mktemp -d "${TMPDIR:-/tmp}/push-release.XXXXXXXXXX"` (random name, mode 0700,
owned by the ssh user); (2) it validates the path that comes back (absolute,
safe characters, no `..`, with exactly that name) and checks on the host that
it is a real directory, not a symlink, owned by the ssh user and 0700; (3) it
copies there by `scp` the tarball, its `.sha256` and `install-release.sh`; (4)
in ONE ssh session it runs `sha256sum -c` from the directory and, only if it
matches, `sudo bash <staging>/install-release.sh --tarball
<staging>/<tarball> ...`. That session carries a `trap` that deletes the
directory on exit (success, failure or disconnection); in addition the client
does a backup `ssh rm -rf` on the validated path if the script aborts earlier
(failed scp, failed install, Ctrl-C). If that backup deletion fails, it warns
with the manual command.

`install-release.sh` checks the sha256 again with the `<tarball>.sha256` that
travels alongside, and fails closed: if the file is missing or does not match,
it aborts without touching the installed code.

Operational consequence: by default a failed deploy does not leave the tarball
on the host. To diagnose, `--keep-staging` keeps the staging ONLY if the deploy
fails (on success it is deleted anyway): the remote trap and the backup
deletion do not run, and the script prints host, path and the exact command to
delete it later, `ssh <host> "rm -rf -- '<dir>'"`. Without the flag the
behavior does not change. `--dry-run` prints the real staging pattern and the
exact `sudo bash` (with `--keep-staging` it indicates it).

The first manual delivery to a new worker (without `push-release.sh`) uses the
same scheme: one `mktemp -d` per run on the host, `sha256sum -c` inside before
`sudo bash` and deletion at the end; see the
[worker setup guide](../../setup-scripts/debian13-worker/README.md#bootstrap-first-start).

Decision: the staging goes under the ssh user's `${TMPDIR:-/tmp}` with
`mktemp -d`, not in their home nor in `/run/user/<uid>`. An own 0700 directory
closes the exposure of `/tmp` without depending on the host having `/run/user`
(it requires a logind session) or on space in the home.

## Tarball name

`make-release.sh` builds a tarball with the `orcaster/` prefix at its root, and
`install-release.sh` accepts only that prefix: anything else aborts without
touching the installed code. What is inside (the `orcaster` CLI, the Python
distributions `orcaster-server` and `orcaster-worker`) travels as is in the
tree: the script neither installs nor links binaries.

## Tests

```bash
bats ops/release/tests/
```

The `push-release.sh` tests never touch real `scp`/`ssh`: they mock them via
`PATH`, the same idiom that `setup-scripts/debian13-worker/tests/` uses for
`make`/`sudo`/`systemctl`.
