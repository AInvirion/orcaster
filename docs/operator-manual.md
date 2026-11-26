# Orcaster — Operator Manual

This manual is for an **operator**: someone who enqueues tasks on a running
Orcaster server and follows them to completion, from a terminal or a script.
It assumes the server and its workers are already running.

- Doing the same work from the web dashboard is covered in the
  [Operator manual (Dashboard)](operator-manual-ui.md).
- Installing or running the server and the workers, and the full reference of
  the dashboard and workspace administration, are in the
  [User manual](user-manual.md).
- The complete flag reference of the CLI is in the
  [CLI guide](../orcaster/README.md).

Every example uses the `orcaster` CLI or the public JSON API under `/api/v1`,
authenticated with a Personal Access Token (PAT).

## Contents

1. [Set up the CLI](#1-set-up-the-cli)
2. [Workspaces](#2-workspaces)
3. [Enqueue tasks](#3-enqueue-tasks)
4. [Follow tasks](#4-follow-tasks)
5. [Workers, from the operator's side](#5-workers-from-the-operators-side)
6. [Troubleshooting](#6-troubleshooting)
7. [Plugins](#7-plugins)

---

## 1. Set up the CLI

### Install

Download the release archive for your platform, verify it against
`orcaster-<version>-checksums.txt` and put the binary on your `PATH`:

```sh
tar xzf orcaster-cli-<version>-linux-amd64.tar.gz
sudo install orcaster /usr/local/bin/orcaster
orcaster version
```

Alternatives: `go install github.com/AInvirion/orcaster/orcaster@latest`, or
`make build orcaster` from a checkout (see the [CLI guide](../orcaster/README.md#installation)).

### Get a Personal Access Token

PATs are issued from the dashboard only: **Personal access tokens** page
(`/admin/pats`), see
[Issuing a personal access token](user-manual.md#issuing-a-personal-access-token).

- A token starts with `orcaster_pat_` and is shown **once**; the server keeps
  only its hash.
- One token belongs to one user and **one workspace**.
- Expiry: 30, 60 or 90 days, or none. Any workspace member can issue one.
- There is no "reissue": to rotate, issue a new token, log in with it, then
  revoke the old one from the same page.

### Log in

```sh
orcaster auth login --host https://orcaster.example.com
# Paste your token: (hidden prompt)
orcaster auth status
```

`auth login` validates the token against the server before saving anything.
The profile is named after the workspace slug unless you pass `--profile`.
The token can also come from `--token` or `ORCASTER_TOKEN` instead of the
prompt. `auth status` prints the profile, host, user, workspace, role and the
token's expiry, and warns when the token expires within 7 days.

`orcaster auth logout` deletes the stored profile (it does not revoke the token).

### Configuration file and environment variables

| Where | What |
|-------|------|
| `~/.config/orcaster/config.toml` (or `$XDG_CONFIG_HOME/orcaster/`) | Active profile and, per profile, `host` and `workspace` |
| `~/.config/orcaster/credentials` (mode `0600`) | The token of each profile |
| `ORCASTER_HOST` | Server URL; overrides the profile |
| `ORCASTER_TOKEN` | PAT; overrides the profile |

For CI and scripts, set both variables and skip `auth login`:

```sh
export ORCASTER_HOST=https://orcaster.example.com
export ORCASTER_TOKEN=orcaster_pat_...   # from your secret store, never hard-coded
orcaster auth status                      # shows "(host+token from env)"
```

Either variable alone overrides only that half of the active profile; if the
other half is not in a profile either, the CLI stops with `incomplete env override`.

---

## 2. Workspaces

Every task, secret and worker belongs to one workspace. The workspace of a
request is **the workspace of the token**: there is no workspace flag or
field. To work in several workspaces, keep one profile per workspace:

```sh
orcaster auth login --host https://orcaster.example.com --profile team-a
orcaster auth login --host https://orcaster.example.com --profile team-b
orcaster config list                    # * marks the active profile
orcaster config use team-b              # switch the active profile
orcaster --profile team-a task list     # one-off override
```

Roles are `owner` and `member`. Both can create, cancel, retry, fork and
delete tasks and list secrets; only an `owner` can set or delete secrets.

---

## 3. Enqueue tasks

### The smallest task

```sh
orcaster task create --payload '{"requirements": [], "commands": ["echo hello"]}'
# Task 42 created (status queued, target 1, mode count, count 1).
```

`--payload` is required; pass `--payload '{}'` only if you really want an
empty payload.

### The payload

A worker reads these payload keys:

| Key | Required | Meaning |
|-----|----------|---------|
| `requirements` | yes | List of Python packages (PEP 508). The worker creates a fresh virtual environment with `uv` and installs them; `[]` for none. |
| `commands` | yes | Non-empty list of shell commands. Each runs with `/bin/sh -c`, in order, in a fresh temporary directory; the batch stops at the first non-zero exit. The task's exit code is the one of the last command that ran. |
| `timeout_seconds` | no | Time limit **per command**. When it is absent, the worker's own default applies (60 seconds unless the worker host changed it). A command that hits it is killed, the batch continues with the next command, and the run ends `failed`. |
| `task_env` | no | Names of environment variables the task receives (see [Secrets and environment](#secrets-and-environment)). |
| `egress_allowlist` | no | Hosts the task may reach (see [Network egress](#network-egress)). |
| `artifacts` | no | `{"include": [...], "exclude": [...], "required": bool}`: collect the files the task writes (see [Artifacts](#artifacts)). |
| `outcomes` | no | Names for exit codes, read by the server; see [Outcomes](user-manual.md#analyzing-completions-and-failures-outcomes). |

Any other key is stored with the task but ignored by the worker. A payload
without `requirements` or `commands` is accepted by the server and fails on the
worker with exit code `2`.

A complete payload, saved as `task.json`:

```json
{
  "requirements": ["requests==2.32.3"],
  "commands": [
    "python -c 'import requests; print(requests.__version__)'",
    "python -c 'import json, os; json.dump({\"ok\": True}, open(os.environ[\"ORCASTER_ARTIFACTS_DIR\"] + \"/result.json\", \"w\"))'"
  ],
  "timeout_seconds": 600,
  "task_env": ["API_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"],
  "egress_allowlist": ["pypi.org", "files.pythonhosted.org", "api.example.com"],
  "artifacts": {"include": ["**/*.json"], "required": true}
}
```

```sh
orcaster task create --payload @task.json --priority 5 --exec-timeout 3600
```

JSON flags (`--payload`, `--eligibility`, `--auto-retry`) accept a literal,
`@file`, or `-` for stdin.

### Task options

| Flag | API field | Default | Meaning |
|------|-----------|---------|---------|
| `--target` | `target` | `"1"` | `N`: N successful runs on any eligible workers. `all-available`: one run on every online eligible worker at creation time. `worker:<uuid>`: one run on that worker only. `workers:<u1>,<u2>`: one run per listed worker, restricted to that set. |
| `--priority` | `priority` | `0` | -32768..32767; higher runs first. |
| `--eligibility` | `eligibility` | `{}` | `{"capabilities": ["gpu", "ram_gb>=32"]}`: only workers that declare every capability. Operators `>=`, `<=`, `==`, `>`, `<`. See [Declaring the worker's capabilities](user-manual.md#declaring-the-workers-capabilities). |
| `--exec-timeout` | `execution_timeout_seconds` | none | Total time budget for the run; when reached, the run is cancelled with reason `outer_timeout`. |
| `--claim-timeout` | `claim_timeout_seconds` | server default (3600) | How long a pre-assigned run waits to be claimed before it expires. |
| `--run-at` | `run_at` | now | RFC 3339 time before which the task is not handed out (count mode). |
| `--idempotency-key` | `idempotency_key` | none | A UUID. Re-sending the same key with the same payload returns the existing task instead of creating a new one; the same key with a different payload is rejected (`422`). Not allowed with `all-available`. |
| `--auto-retry` | `auto_retry` | off | Automatic retry policy, see below. |
| `--env` | `payload.task_env` | — | Repeatable; **replaces** `task_env` of `--payload`. |
| `--egress-allow` | `payload.egress_allowlist` | — | Repeatable; **replaces** `egress_allowlist` of `--payload`. |
| `--json` | — | — | Print the server response as JSON. |

Automatic retries (`auto_retry`), all fields optional:

```json
{"max_attempts": 3, "retryable_exit_codes": [75], "backoff_seconds": 60, "sticky": true}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `max_attempts` | `0` (off) | Retry budget, 0..20. |
| `retryable_exit_codes` | `[75]` | Exit codes that trigger a retry. `75` (`EX_TEMPFAIL`) is the convention for "temporary failure". |
| `retryable_cancelled_reasons` | `[]` | Cancel reasons that trigger a retry, for example `["outer_timeout"]`. |
| `backoff_seconds` | `60` | Minimum delay between attempts (0..3600). |
| `sticky` | `true` | Send the retry back to the same worker so it reuses `$ORCASTER_CACHE_DIR`. |
| `sticky_offline_timeout_seconds` | `1800` | If that worker stays offline longer, any eligible worker may take the retry. |

### Secrets and environment

A task starts with a clean environment when its payload has `task_env`: it
receives the variables named there, plus a fixed base set from the worker
(`PATH`, `HOME`, locale, CA bundles, `$ORCASTER_CACHE_DIR`,
`$ORCASTER_ARTIFACTS_DIR` and their deprecated `TASKORCH_*` aliases, ...). For
each name declared in `task_env` the value comes from, in order:

1. a secret stored for the worker that runs the task;
2. a workspace secret with that name;
3. the worker's own environment.

A name with no value anywhere is skipped with a warning; the task still runs.
The proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `ALL_PROXY`) are
reserved: they always take the worker's value and cannot be stored as secrets.
The task directory variables `ORCASTER_CACHE_DIR`, `ORCASTER_ARTIFACTS_DIR`,
`TASKORCH_CACHE_DIR` and `TASKORCH_ARTIFACTS_DIR` are reserved too: the worker
always sets them itself, so neither `--env` nor a secret can override them. A
secret stored under one of them before this rule is still listed and deletable;
a worker of this version or later never passes it to a task, an older one may,
so remove it with `orcaster secret delete NAME`.
The full list of reserved names is in
[Setting a secret](user-manual.md#setting-a-secret-owner-only).
Names are names only: `--env NAME=value`, names starting with `ORCH_` and
strings that look like tokens (`hf_`, `ghp_`, `sk-`, ...) are rejected before
the task is created.

Store the value once (owner only). It is read from stdin and never printed:

```sh
orcaster secret set API_TOKEN                       # hidden prompt
pass show api/token | orcaster secret set API_TOKEN  # from a password manager
orcaster secret set GCP_CREDENTIALS < service-account.json
orcaster secret set API_TOKEN --worker worker-a      # only for that worker
orcaster secret list                                 # names and fingerprints only
```

Then reference it by name:

```sh
orcaster task create --payload '{"requirements": [], "commands": ["./call-api.sh"]}' \
  --env API_TOKEN
```

Details: [Task secrets](user-manual.md#task-secrets).

### Network egress

On workers with network isolation, a task with `egress_allowlist` reaches only
the listed hosts, on ports 80 and 443:

- `api.example.com` matches that host exactly;
- `*.example.com` or `.example.com` matches the domain and its subdomains;
- `[]` allows nothing beyond the proxy's base list.

Traffic goes through the worker's egress proxy. Under a strict `task_env`,
include `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` in `task_env`, or the task
(including the `requirements` install) cannot resolve any host. Package
installs need their index hosts in the allowlist too (`pypi.org`,
`files.pythonhosted.org`).

```sh
orcaster task create --payload '{"requirements": ["requests"], "commands": ["python fetch.py"]}' \
  --env HTTPS_PROXY --env HTTP_PROXY --env NO_PROXY \
  --egress-allow pypi.org --egress-allow files.pythonhosted.org --egress-allow api.example.com
```

`--env` and `--egress-allow` replace the whole list of the payload, so list
every value in the flags or keep the lists in the payload file only.

Details: [Isolating a worker's network](user-manual.md#isolating-a-workers-network-egress).

### Enqueue a batch from a file

One task per line of `inputs.txt`. A deterministic idempotency key (a UUIDv5
of the input) makes the script safe to re-run: inputs already enqueued return
the existing task instead of a duplicate. `@sh` quotes each input for the
shell, so spaces or quotes in a line cannot break the command.

```sh
#!/bin/bash
set -euo pipefail
while IFS= read -r item; do
  [ -n "$item" ] || continue
  key=$(python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, "my-batch/" + sys.argv[1]))' "$item")
  jq -n --arg item "$item" \
    '{requirements: [], commands: ["./process.sh " + ($item | @sh)], timeout_seconds: 900}' \
  | orcaster task create --payload - --idempotency-key "$key" --priority 1 --json \
  | jq -r --arg item "$item" '"\(.task_id)\t\(.status)\t\($item)"'
done < inputs.txt
```

### Enqueue with the HTTP API

`POST /api/v1/tasks` with the token as a Bearer header. The body takes the API
fields of the table above; unknown fields are rejected with `422`.

```sh
curl -fsS -X POST "$ORCASTER_HOST/api/v1/tasks" \
  -H "Authorization: Bearer $ORCASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "payload": {"requirements": [], "commands": ["echo hello"]},
        "target": "1",
        "priority": 5,
        "execution_timeout_seconds": 600,
        "idempotency_key": "6f1c2b9e-3c0a-4f57-9a43-2f6b8f0d9a11"
      }'
```

The response (`201`) contains `task_id`, `status`, `target`, `target_mode`,
`target_count`, `parent_task_id` and, for `all-available` targets with no
eligible worker, `no_eligible_reason`.

Other routes, all under `/api/v1` with the same header:

| Method and path | Purpose |
|-----------------|---------|
| `GET /auth/whoami` | Identity of the token |
| `GET /tasks?status=failed&limit=50&cursor=<id>` | List tasks |
| `GET /tasks/{id}` | Task detail with its executions |
| `GET /tasks/{id}/children` | Forks of a task |
| `GET /tasks/{id}/output?format=text\|jsonl&attempt=N` | Log of a run (gzip; use `curl --compressed`) |
| `GET /executions/{uuid}/output` | Log of one execution |
| `POST /tasks/{id}/cancel` | Body `{"reason": "..."}` |
| `POST /tasks/{id}/retry` | Body `{"force_same_worker": false}` |
| `POST /tasks/{id}/fork` | Same body as create; only the fields you send are overridden |
| `DELETE /tasks/{id}` | Delete a terminal task |
| `POST /tasks/bulk/delete` | Body `{"task_ids": [...]}`, up to 500 |
| `GET /insights/failures`, `GET /insights/outcomes` | Aggregated failures and outcomes |
| `GET /secrets`, `PUT /secrets/{name}`, `DELETE /secrets/{name}` | Task secrets (writes: owner only) |

---

## 4. Follow tasks

### List and inspect

```sh
orcaster task list                                  # newest first, 50 rows
orcaster task list --status queued --status dispatching
orcaster task list --status failed --limit 200 --json
orcaster task list --cursor 1200                    # next page (printed at the bottom)
orcaster task get 42                                # detail and every execution
orcaster task get 42 --json
orcaster task children 42                           # forks of task 42
```

`--variant` filters by the variant a plugin assigned to the task (see
[Plugins](#7-plugins)); without plugins every task is in the `other` variant.

### Status lifecycle

A **task** is one request; each attempt to run it on a worker is an
**execution**.

| Task status | Meaning |
|-------------|---------|
| `queued` | Waiting for a worker. |
| `dispatching` | At least one execution is claimed or running (count mode), or the per-worker runs of an `all-available` task are in progress. |
| `paused` | Paused from the dashboard; no new runs are handed out. |
| `completed` | The target number of runs completed (`all-available`: every run completed). |
| `failed` | The attempt budget is used up, or a run failed in `all-available`. |
| `cancelled` | Cancelled by an operator, by the execution timeout, or by a worker shutdown. |

| Execution status | Meaning |
|------------------|---------|
| `assigned` | Pre-assigned to a worker (`all-available`), not claimed yet. |
| `claimed` | A worker took it and holds a lease. |
| `running` | The worker is running the commands. |
| `completed` / `failed` / `cancelled` | Finished, with an exit code when there is one. |
| `expired` | The worker lost its lease or never claimed the run; it does not use up the retry budget and the task becomes claimable again. |

Reference: [Task states](user-manual.md#task-states) and
[Run states](user-manual.md#run-states).

### Logs

```sh
orcaster task output 42                     # log of the latest execution
orcaster task output 42 --attempt 1         # a specific attempt
orcaster task output 42 --format jsonl -o run-42.jsonl
orcaster task output --execution 550e8400-e29b-41d4-a716-446655440000
```

### Duration and exit code

`orcaster task get 42` lists every execution with its worker, status, exit
code and finish time; `--json` adds `started_at` and `finished_at`, from which
the duration follows. Orcaster does not record memory usage (peak RSS) itself:
if your workload needs it, have the commands write it to the log or to an
artifact.

To see failures grouped by exit code, worker and error signature (without
`--since`, only the current UTC day):

```sh
orcaster insights failures --since 2026-06-01
```

### Artifacts

Files the task writes to `$ORCASTER_ARTIFACTS_DIR` are uploaded when the run
ends **only if the payload has an `artifacts` block** (`{}` is enough to
collect everything).

- `include` / `exclude` are POSIX globs relative to that directory (`**`
  crosses folders, `*` does not); `exclude` wins.
- With `"required": true`, a failed upload turns a successful run into
  `failed`; otherwise the failure is only logged.
- Default limits per run (server settings): 50 MiB per file, 200 MiB in
  total, 500 files. A workspace-wide quota applies on top.
- Download from the task's page in the dashboard (single files or a zip);
  there is no CLI or `/api/v1` route for artifacts. See
  [Downloading the artifacts](user-manual.md#downloading-the-artifacts).

`$ORCASTER_CACHE_DIR` is a per-job cache directory (keyed by the idempotency
key, or by the task id). The worker keeps it after a failed run, so a retry on
the same worker can reuse downloads, and by default deletes it after a
successful run.

The worker also exports `$TASKORCH_CACHE_DIR` and `$TASKORCH_ARTIFACTS_DIR` with
the same values as `$ORCASTER_CACHE_DIR` and `$ORCASTER_ARTIFACTS_DIR`. They are
deprecated aliases kept for tasks written before the rename; use the
`ORCASTER_*` names in new tasks. The worker sets all four itself and they cannot
be overridden (see [Secrets and environment](#secrets-and-environment)). If the
worker cannot create the cache directory, the task runs without any of them.

### Cancel, retry, fork, delete

```sh
orcaster task cancel 42 --reason "wrong input"
orcaster task retry 42                       # same task, any eligible worker
orcaster task retry 42 --force-same-worker   # same task, the last worker (reuses its cache)
orcaster task fork 42 --priority 9 --exec-timeout 7200   # new task, inherits the rest
orcaster task delete 42                      # terminal tasks only; irreversible
orcaster task delete 40 41 42 --bulk --yes
```

- `retry` works on `completed`, `failed` and `cancelled` tasks and keeps the
  same task id and attempt history.
- `fork` creates a new task linked to the original; JSON fields are
  deep-merged. A fork runs now and has no idempotency key unless you pass
  `--run-at` or `--idempotency-key`.

To re-queue the 200 most recent failed tasks (page with `--cursor` for more):

```sh
orcaster task list --status failed --limit 200 --json \
| jq -r '.tasks[].id' \
| while read -r id; do orcaster task retry "$id"; done
```

---

## 5. Workers, from the operator's side

Workers are managed from the dashboard; the CLI and `/api/v1` have no worker
commands. What matters when you enqueue:

| Worker status | Takes new tasks |
|---------------|-----------------|
| `online` | Yes. While online, it also reports itself `idle` or `busy`; one task at a time per worker. |
| `paused` | No. It keeps polling the server but receives no tasks. |
| `offline` | No. No contact for longer than the heartbeat timeout (120 seconds by default). |

- **Pause and resume** are done from the **Workers** panel of the dashboard,
  with a signed-in session; a PAT cannot pause a worker. Pausing is a soft
  drain: a run in progress continues to the end, and no new run is handed out.
  See [Pausing and resuming a worker](user-manual.md#pausing-and-resuming-a-worker).
- **Queued tasks while a worker is paused** stay `queued`. Tasks of the general
  queue go to other online workers; a task targeted at that worker only
  (`worker:<uuid>`) waits until the worker is resumed.
- **Redeploy or restart of a worker**: the worker stops cooperatively. A run in
  progress is cancelled and its task closes as `cancelled`; it is not
  re-queued. Retry it with `orcaster task retry <id>`. A paused worker stays
  paused after a restart. See
  [Cooperative shutdown](user-manual.md#cooperative-shutdown).

---

## 6. Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| `401` / `authentication required` | Token does not start with `orcaster_pat_`, was mistyped, is expired or revoked. The server does not say which. | `orcaster auth status` shows which profile, host and token (first characters) were used; on success it also shows the expiry. Check the token's state on the **Personal access tokens** page, issue a new one and `auth login` again. |
| `403` / `not a workspace member` | The token's user was removed from the workspace. | Ask a workspace owner. |
| `403` on `secret set` / `secret delete` | Only an `owner` can write secrets. | Ask a workspace owner. |
| `incomplete env override` | One of `ORCASTER_HOST` / `ORCASTER_TOKEN` is set and the other is in neither the environment nor the profile. | Set both, or unset both and use a profile. |
| `422` on create | Invalid field: unknown key, `task_env` entry that is not a name, idempotency key reused with a different payload, unknown worker id in `--target`. | Read the error message; it names the field. |
| Task stays `queued` | No online worker, none with the required capabilities, the only target worker is paused or offline, `run_at` is in the future, or the server cannot decrypt a referenced secret. | `orcaster task get <id>`; check the **Workers** panel; see [A task waiting for an eligible worker](user-manual.md#task-waiting-for-an-eligible-worker). |
| `failed`, exit code `2` | Payload lacks `requirements` or `commands`, or the worker could not run the task. | Fix the payload; read `orcaster task output <id>`. |
| `failed`, exit code `1`, `venv setup failed` | A requirement could not be installed: wrong name, or the package index is not reachable under egress isolation. | Add the index hosts to `egress_allowlist` and the proxy variables to `task_env`. |
| `failed` with `[batch] timeout during command #N` in the log (exit code often negative or `-1`) | A command hit the per-command `timeout_seconds` (60 seconds by default) and was killed. | Set `timeout_seconds` in the payload. |
| `cancelled`, reason `outer_timeout` | The run exceeded `--exec-timeout`. | Raise it, or retry with `auto_retry.retryable_cancelled_reasons`. |
| Other exit codes | The code of the last command that ran. | See [Exit codes](user-manual.md#exit-codes). |
| Network call fails with `403` or a name-resolution error | The host is not in `egress_allowlist`, or the proxy variables are missing from `task_env`. | Add both; see [Proxy DENY](user-manual.md#proxy-deny-403-when-downloading-something). |
| No artifacts on the task page | No `artifacts` block in the payload; files written outside `$ORCASTER_ARTIFACTS_DIR`; upload rejected by a size limit or the workspace quota. | Add `"artifacts": {}`; write into `$ORCASTER_ARTIFACTS_DIR`; set `"required": true` to make upload failures visible as `failed`. |
| Artifact shows **Collected**, download returns `410` | Its bytes were moved to external storage by a retention process. | Ask the server administrator. |

---

## 7. Plugins

A server plugin is a Python package installed next to the server and
registered under the `orcaster.plugins` entry-point group. The server loads
every installed plugin; `ORCASTER_PLUGINS` marks some as required and sets
their precedence. A plugin does **not** add task types or queues: every
task uses the same payload described above. What a plugin adds, for the
operator:

- **Variants**: a plugin can tag each task with a variant (`<kind>~<label>`,
  for example the tool and version a payload runs), derived from the payload
  at creation. Filter with `orcaster task list --variant <kind>` or
  `--variant '<kind>~<label>'`. Without plugins every task is `other`.
- **Default outcomes**: names for exit codes of that kind of task, shown in
  insights and in the dashboard.
- **Legacy status aliases**: mappings of old status labels onto current
  outcomes.

Writing and installing a plugin: [Plugins](../README.md#plugins) in the README.
