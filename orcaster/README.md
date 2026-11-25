# orcaster — the Orcaster CLI

`orcaster` is a single Go binary that manages a workspace's tasks from the
terminal, authenticating with a **Personal Access Token** (PAT) against the
public JSON API `/api/v1`. It is to the orchestrator what `gh` is to GitHub: you
paste a token once and operate your tasks without going through the web
dashboard.

> Design and contract: Personal Access Tokens over the public JSON API v1
> (Bearer authentication). A PAT is scoped to **one** workspace (1 token = 1 workspace).

---

## Installation

### Release binaries (recommended)

Each release publishes binaries for Linux, macOS and Windows (amd64 and arm64)
and FreeBSD (amd64), plus an `orcaster-<version>-checksums.txt` (SHA-256). Download the one for your platform from the
releases page, verify the checksum and put the binary on your `PATH`:

```sh
# Linux amd64 example
tar xzf orcaster-cli-<version>-linux-amd64.tar.gz
sudo install orcaster /usr/local/bin/orcaster
```
```sh
orcaster version
```

### With `go install`

If you have Go installed (at least the version in `go.mod`):

```sh
go install github.com/AInvirion/orcaster/orcaster@latest
```

### Local build from the repo

```sh
make build orcaster    # builds orcaster/orcaster with the version derived from git
./orcaster/orcaster version
```

---

## Quickstart

```sh
# 1. Log in by pasting your PAT (the workspace is inferred from the token).
#    The token is prompted hidden; you can also pass it via --token or ORCASTER_TOKEN.
orcaster auth login --host https://orcaster.example.com

# 2. Confirm your identity and the active profile.
orcaster auth status

# 3. Create a task.
orcaster task create --payload '{"requirements": [], "commands": ["echo hello"]}' --priority 5

# 4. List tasks and view the detail.
orcaster task list
orcaster task get 123
orcaster task children 123              # lists the task's direct forks/children

# 5. Operate on existing tasks.
orcaster task cancel 123 --reason "no longer needed"
orcaster task retry 123                 # to any eligible worker (default)
orcaster task retry 123 --force-same-worker   # pinned to the last attempt's worker
orcaster task fork 123 --priority 9     # inherits the rest (deep-merge); runs now, no idempotency key

# 6. Delete terminal tasks (hard-delete, irreversible: asks for confirmation).
orcaster task delete 123                          # one id → DELETE /tasks/{id}
orcaster task delete 123 456 789 --bulk --yes     # several ids → POST /tasks/bulk/delete, no prompt

# 7. Diagnose failures: aggregate the errors and download a run's log.
orcaster insights failures --since 2026-06-01     # failed tasks grouped by exit code / worker / signature
orcaster task output 123                           # log of task 123's latest execution
orcaster task output --execution <uuid> -o run.log # log of one specific execution (drill-down from a signature)

# 8. Manage the workspace's task secrets (the value only comes in through stdin).
orcaster secret set API_TOKEN                      # in a terminal it prompts without echo
pass show api/token | orcaster secret set API_TOKEN  # from a pipe
orcaster secret set GCP_CREDENTIALS < sa.json      # a multiline value (PEM, JSON)
orcaster secret list                               # names and fingerprints, never values
orcaster secret delete API_TOKEN                   # asks for confirmation (--yes to skip it)
pass show gh/worker-a | orcaster secret set GITHUB_TOKENS --worker worker-a  # only that worker
```

Every command accepts `--json` to emit the raw API body (useful for
scripting / `jq`). Errors go to `stderr` and the exit code is `1` on failure.

### `task delete` flags

`task delete` only deletes tasks in a **terminal** state (`completed`, `cancelled`
or `failed`); the server rejects the rest (`409`). The deletion is an
**irreversible hard-delete** (CASCADE of executions + artifacts), so it asks for a
`[y/N]` confirmation listing the ids before proceeding.

- `--bulk` — uses `POST /tasks/bulk/delete` (1–500 ids) instead of one `DELETE`
  per id. Passing more than one id **without** `--bulk` is a usage error. Bulk is
  best-effort: the output groups the ids under `Deleted:` / `Skipped:` / `Not found:`
  (omitting empty sections).
- `--yes`, `-y` — skips the interactive confirmation. Without a TTY and without
  `--yes` the command refuses to delete (there is no terminal to confirm on).
- `--json` — emits the raw `{deleted, skipped, not_found}` (when deleting a single
  id it is normalized to `{"deleted":[id],"skipped":[],"not_found":[]}`).

### Failure diagnosis: `insights failures` + `task output`

`insights failures` aggregates the workspace's `failed` tasks **live** and groups
them by `exit code`, by `worker` and by a **normalized error signature** (the last
stderr line, with UUIDs/paths/numbers replaced so that equal errors deduplicate).
Each signature group carries an example task —with the `execution` of its
representative run— and a few `sample task ids` for drill-down.

- `--since` / `--until` — filter by `completed_at` in **ISO 8601**. A date without
  a timezone offset is interpreted as **UTC** (same as the server). E.g.: `--since 2026-06-01`.
- `--limit` — maximum number of signature groups (1–1000, default `100`).
- `--json` — emits the raw aggregate (`generated_at`, `filters`, `total_failed_tasks`,
  `by_exit_code`, `by_worker`, `by_signature`).

`task output` downloads the log of one execution. It has **two mutually
exclusive modes**:

- **By task id** (`orcaster task output 123`): serves the task's most recent
  execution; `--attempt N` picks another attempt.
- **By execution** (`orcaster task output --execution <uuid>`): downloads one
  specific execution by its UUID, ideal for opening the log of the signature
  that `insights failures` showed. `--attempt` does not apply here (the UUID
  already identifies a single execution) and passing both —an id and
  `--execution`— is a usage error. The UUID is validated locally before calling
  the API.

Common to both modes: `--format text|jsonl` (default `text`: raw log; `jsonl`: one
JSON event per line) and `-o/--output <file>` (writes to a file instead of
stdout; without it the log is streamed to stdout).

### Task secrets: `secret set` / `list` / `delete`

Task secrets are stored encrypted on the server, per workspace, and reach each
task that declares the name in `payload.task_env`. Values are **write-only**: no
command or API response returns them.

- `secret set NAME` reads the value **only from stdin**: never from arguments,
  flags or environment variables, which end up in the shell history or in `ps`. In
  a terminal it prompts without echo (the prompt goes to `stderr`); from a pipe it
  reads all of stdin (up to 64 KiB) and strips exactly one trailing newline (`\n`
  or `\r\n`). An empty value is an error. It prints the name and the fingerprint,
  never the value. The prompt reads **a single line**: a multiline value (PEM,
  JSON) goes through a pipe or with `orcaster secret set NAME < file`. When pasted
  into the prompt, its first line is rejected without being saved and the rest of
  the pasted text is discarded (Linux, macOS and FreeBSD) so the shell does not
  execute it.
- `secret list` shows `NAME`, `SCOPE`, `FINGERPRINT`, `UPDATED` and `UPDATED BY`;
  `SCOPE` is `workspace` or `worker:<name>`. `--json` emits the server's response
  as is (including `keys_configured`, `worker_id` and `worker_name`).
- `secret delete NAME` asks for a `[y/N]` confirmation, like `task delete`;
  `--yes`/`-y` skips it and without a TTY or `--yes` the command refuses. A name
  that does not exist gives `secret NAME not found`.
- `--worker <id|name>` on `set` and `delete` sets or deletes that worker's
  secret, which for that worker wins over the workspace one with the same name.
  Without the flag, `set` and `delete` act only on the workspace secret. The name
  is the one visible in the dashboard (`name`, or the hostname if it has none); if
  it matches more than one worker, the server answers `worker_ambiguous` and you
  must pass the id. A worker that does not exist or belongs to another workspace
  gives `worker ... not found in this workspace`, and a deleted one accepts no new
  secrets (but its own can be deleted). `--worker ""` is an error, and so is a
  value with leading or trailing spaces: it is not trimmed, because worker names
  are not trimmed on registration and `"worker-a "` and `"worker-a"` are different
  workers (for such a worker, pass its id). Before writing, the CLI queries
  `GET /secrets` and refuses (`nothing was changed`) if the server does not
  advertise `supports_worker_scope`, so the parameter can never be ignored and
  touch the workspace secret instead.

The name follows the `payload.task_env` rule: it does not start with a known
token prefix (`hf_`, `ghp_`, `github_pat_`, `glpat-`, `sk-`..., case-sensitive, so
`HF_TOKEN` passes), has the form `^[A-Za-z_][A-Za-z0-9_]*$` and cannot start with
`ORCH_` in any capitalization; `orcaster` validates that locally before calling
the API, and the error does not repeat the name (the server's name errors do not
either). The server also rejects with `422` names longer than 128 characters and
reserved ones, regardless of case (`PATH`, the fixed base of strict `task_env`,
the variables that control the shell, the loader, the runtimes, the build,
helper programs and the proxy, and prefixes such as `LD_`, `PYTHON`, `UV_`,
`PIP_`, `GIT_`, `NODE_`, `CARGO_` or `NPM_CONFIG_`; the server applies the full
list when validating the name). `GITHUB_TOKEN` is allowed.

The name travels in the URL (`PUT` and `DELETE /api/v1/secrets/{name}`), so a
proxy that logs paths sees it before the server validates it. `orcaster secret
set` stops a name with a token prefix before sending anything; `secret delete`
validates only the form,
and a direct API client (curl, a script) has no guard at all: validate the name
before sending it. A proxy in front of the server must not log the paths under
`/api/v1/secrets/`.
Creating, replacing and deleting is reserved to the workspace `owner` (a `403`
says so). If the server has no keys file (`TASK_SECRETS_KEYS_PATH`), `secret set`
fails (503) with a message that explains it; `secret delete` does work. `secret
list` still shows the stored names and fingerprints, but warns on `stderr` (the
table on `stdout` stays parseable) that they cannot be stored or delivered to
tasks.

---

## Profiles and configuration

Since **1 token = 1 workspace**, each **profile** binds a host + workspace + its
PAT. If you work with several workspaces, you have one profile for each.

```sh
orcaster config list                 # shows the profiles (* marks the active one)
orcaster config use staging          # switches the active profile
orcaster --profile staging task list # per-invocation override
orcaster auth logout                 # deletes the active profile (or --profile)
```

Files (XDG-aware, under `~/.config/orcaster/` or `$XDG_CONFIG_HOME/orcaster/`):

| File               | Contents                                              | Permissions |
|--------------------|-------------------------------------------------------|-------------|
| `config.toml`      | active profile + `[profiles.<name>]` host, workspace  | normal      |
| `credentials`      | the tokens, kept apart from the rest (AWS CLI style)  | `0600`      |

Environment overrides (for CI/scripting, they win over the profile):

```sh
export ORCASTER_HOST=https://orcaster.example.com
export ORCASTER_TOKEN=orcaster_pat_xxxxxxxx
orcaster task list
```

---

## Commands

| Command                       | What it does                                                    |
|-------------------------------|-----------------------------------------------------------------|
| `auth login`                  | Validates a PAT against the server and saves a profile          |
| `auth status`                 | Shows the active profile's identity (whoami)                    |
| `auth logout`                 | Deletes the profile (does not contact the server)               |
| `config use <profile>`        | Switches the active profile                                     |
| `config list`                 | Lists the configured profiles                                   |
| `task create`                 | Creates a task (see flags below)                                |
| `task list`                   | Lists tasks (`--status` repeatable, `--limit`, `--cursor`, `--variant`, `--include-payload`) |
| `task get <id>`               | Full detail of a task (includes `Children: N`)                  |
| `task children <id>`          | Lists a task's direct children/forks (`--limit`, `--cursor`)    |
| `task cancel <id>`            | Cancels a task (`--reason`)                                     |
| `task retry <id>`             | Retries a terminal task in place; goes to any eligible worker unless `--force-same-worker` |
| `task fork <id>`              | Creates a task inheriting the parent's params; you only override what you pass |
| `task delete <id> [<id>...]`  | Deletes (hard-delete, irreversible) terminal tasks (`--bulk`, `--yes`) |
| `task output [<id>]`          | Downloads an execution's log: by task id (`--attempt`) or by `--execution <uuid>` (`--format`, `-o`) |
| `insights failures`           | Aggregates failed tasks by exit code, worker and error signature (`--since`, `--until`, `--limit`) |
| `secret set <NAME>`           | Creates or replaces a task secret; the value only comes in through stdin (`--worker`) |
| `secret list`                 | Lists the secrets (name, fingerprint, dates; never the value)   |
| `secret delete <NAME>`        | Deletes a secret (`--yes`, `--worker`)                          |
| `version`                     | Prints the binary's version                                     |

### `task list` flags

`--status` (repeatable), `--limit` (1-200), `--cursor`, `--json`, plus two sweep
filters:

- `--variant`: narrows by task variant, that is, by the tool and version that
  produced it. It accepts `all` (the server default: no filtering), `other` (tasks
  with no recognized tool), `<kind>` (the whole tool) or `<kind>~<label>` (an
  exact pair). Without the flag the parameter is not sent. `current` is NOT
  accepted here: the server answers 422. A `<kind>` the server does not know also
  answers 422 and lists the valid ones, instead of returning an empty page: that
  way a typo (`example_tool` with an underscore) is not mistaken for "there are no
  tasks in that variant". The `<label>` is free-form and a non-existent label
  gives an empty page.
- `--include-payload`: adds each task's `payload` to the response. The table does
  not change (a payload does not fit in a tabular row); it is visible with `--json`.

```sh
orcaster task list --variant 'example-tool~0.7.19' --include-payload --limit 200 --json
```

> **`--json` prints the server's response as is** (re-indented), in `task
> list` and in `task children`. It does not go through the client structs, so it
> does not lose fields the binary does not know yet nor invent keys the server
> omitted on purpose: `task children --json` does not show `variant_kind`, because
> that route does not project the variant, and emitting it as `null` would make a
> fork with a variant read as if it had none. The table does use the structs, and
> therefore shows only the columns the binary declares.

### `task create` / `task fork` flags

`--target` (default `"1"`), `--priority`, `--payload`, `--eligibility`,
`--claim-timeout`, `--exec-timeout`, `--run-at`, `--idempotency-key`,
`--auto-retry`, `--env`, `--egress-allow`. The JSON flags (`--payload`,
`--eligibility`, `--auto-retry`) accept a literal, `@file`, or `-` to read
from stdin:

```sh
orcaster task create --payload @task.json
echo '{"requirements": [], "commands": ["ls"]}' | orcaster task create --payload -
```

`--env` (repeatable) and `--egress-allow` (repeatable) stamp `task_env` and
`egress_allowlist` into the payload, merging over the JSON you passed with
`--payload` (or creating the object if you passed none):

```sh
orcaster task create --payload '{"requirements": [], "commands": ["ls"]}' \
  --env GITHUB_TOKEN --env API_TOKEN \
  --egress-allow files.example.com --egress-allow github.com
```

Passing either flag writes the corresponding key into the payload (the server's
tri-state semantics: key present = strict mode, key absent = legacy, unrestricted).
If you want to stamp an explicitly empty list (block all env/egress) do it via
`--payload` with the key set to `[]`, since `--env`/`--egress-allow` require at
least one value per invocation.

Each `--env` carries only the variable name: an ASCII name of letters, digits and
underscore that does not start with a digit (`[A-Za-z_][A-Za-z0-9_]*`), does not
use the `ORCH_` prefix and does not start with a known token prefix (`hf_`,
`ghp_`, `glpat-`, `sk-`...). `--env NAME=value` is not valid, unlike `podman -e`,
and neither is pasting a token in place of the name. A value is delivered by
saving it as a secret (`orcaster secret set NAME`). `orcaster` checks each `--env`
with those same rules before any request and, if one does not comply, exits with
the error `invalid --env #N: ...`, where `N` is the flag's position counted from 1
(`#1` is the first `--env`), without repeating the value. It does not apply the
`task_env` bounds (256 entries, 128 characters per name): the server applies them
on every write path and answers `422`; bringing them into the CLI is pending.
The shape and token-prefix errors end with the hint `use --env NAME to forward a
variable from the worker, not NAME=value`; the `ORCH_` one does not carry it. A
`task_env` written inside `--payload` does not go through that check: the server
validates it on create and on fork, and answers `422` with
`payload.task_env[N] ...`, where `N` is the entry's index counted from 0 (`[0]` is
the first): the CLI counts from 1 and the server from 0. The server does not
repeat the entry, neither in the message nor in the rest of the error body. The
rule is a prefix guard plus a shape check, not a secret detector: a credential
without one of those prefixes (an AWS key such as `AKIA...`, a random
alphanumeric string) passes. `task fork` of an earlier task whose stored
`task_env` has an invalid or token-prefixed entry answers `422` until the fork
passes a corrected `task_env`.

`--idempotency-key` **must be a UUID** (e.g. a UUIDv4 such as
`550e8400-e29b-41d4-a716-446655440000`): the server parses it as such and
`orcaster` validates the format locally before sending, so a non-UUID value fails
immediately without calling the API.

**Create vs fork difference**: `create` always sends `target`+`priority` (with
their defaults). `fork` only sends the flags you changed; the rest (including the
payload) is inherited from the parent via a **server-side deep-merge** (keys you
do not mention are kept; the ones you do, win).

**Two fields that `fork` does NOT inherit** (by server design): `--run-at` and
`--idempotency-key`. A fork is meant to run now, so it starts with
`run_at = now` unless you pass a new one; and since the idempotency key is unique
per task, the fork is born without a key unless you give it an explicit one.

---

## Python → Go glossary

This CLI is written in Go. If you come from Python, this table maps the concepts
you will meet when reading the code (`orcaster/`):

| Go                                         | Python equivalent                               |
|--------------------------------------------|-------------------------------------------------|
| `cobra` (command library)                  | `typer` / `click` (subcommands, flags, `--help`)|
| `package main` + `func main()`             | `if __name__ == "__main__":`                    |
| module (`go.mod`)                          | package with `pyproject.toml`                   |
| `net/http` (stdlib)                        | `httpx` / `requests`                            |
| `httptest.Server`                          | spinning up a test server in pytest             |
| `struct` with `json:"..."` tags            | Pydantic model (but **without** runtime validation)|
| pointer `*int` / `*string` (`nil`)         | `Optional` field / `None` value                 |
| `if err != nil { return err }`             | `try/except`, but the error is explicit         |
| `error` as a return value                  | exception, but returned instead of raised       |
| `go test` + golden files                   | `pytest` + snapshots                            |
| `gofmt` / `golangci-lint`                  | `ruff format` / `ruff check`                    |
| `goreleaser`                               | `build` + publishing wheels/binaries            |
| `io.Writer` (e.g. `cmd.OutOrStdout()`)     | a file-like you write to (`sys.stdout`, a buffer in tests) |

### Code map

```
orcaster/
  main.go              # entrypoint: package main → cmd.Execute()
  cmd/                 # one file per command group (cobra)
    root.go            #   root command + shared helpers (newClient)
    auth.go            #   auth login/status/logout
    task.go            #   task create/list/get/cancel/retry/fork + output rendering
    secret.go          #   secret set/list/delete (values only via stdin)
    config.go          #   config use/list
    *_test.go          #   unit tests + golden-file (testdata/*.golden)
  internal/
    api/               # HTTP client against /api/v1 + structs of the JSON contract
    config/            # profiles, credentials (0600) and env override resolution
  .goreleaser.yaml     # multi-platform release config
  .golangci.yml        # linter config
```

---

## Development

From the repo root (the targets mirror those of `server`/`client`):

```sh
make test orcaster       # go test ./...
make lint orcaster       # gofmt + go vet (+ golangci-lint if installed)
make fmt orcaster        # gofmt -w
make build orcaster      # local binary
make snapshot-orcaster   # cross-compiles all platforms without publishing (goreleaser)
```

The **golden tests** (`cmd/testdata/*.golden`) pin the exact output of the
commands against an `httptest` mock. If you change an output format on purpose,
regenerate the goldens with:

```sh
cd orcaster && go test ./cmd/ -run TestGolden -update
```

### Release

CI triggers the release when a `v*` tag is pushed (see
`.github/workflows/release.yml`): goreleaser cross-compiles, generates
`orcaster-<version>-checksums.txt` and publishes the binaries on the GitHub
release, next to the server and worker bundles (see
[docs/releasing.md](../docs/releasing.md)).

> Each archive, its SBOM and the checksums file carry a GitHub build-provenance
> attestation: verify with `gh attestation verify <file> -R AInvirion/orcaster`,
> or check the SHA-256 against `orcaster-<version>-checksums.txt`.
