"""Shared validation of the environment variable names of a task.

`payload.task_env` and task secret names share the environment variable name
pattern and the rule that the `ORCH_` prefix is reserved for the worker control
plane. Secrets add on top a maximum length, the denylist of reserved names and
the `ORCH_` prefix case-insensitively. `task_env` errors name the index of the
entry and never repeat it: a mistyped entry may be a `NAME=value` carrying the
value of a secret, or the token itself pasted in place of the name.
"""

from __future__ import annotations

import re

RESERVED_ENV_PREFIX = "ORCH_"
SECRET_NAME_MAX_LEN = 128
# Bounds on `payload.task_env` at write time. Without them any workspace member
# or PAT owner could store a list of millions of entries or of huge names, and
# the server signs it and delivers it on every poll. The name length equals that
# of a secret (`SECRET_NAME_MAX_LEN`): a longer name cannot have a stored secret.
# They apply only at write time; rows already stored are still served.
TASK_ENV_MAX_ENTRIES = 256
TASK_ENV_NAME_MAX_LEN = SECRET_NAME_MAX_LEN
# Names that a secret cannot take. The denylist only prevents the owner of a
# workspace (who controls the stored secrets) from overriding, through the
# server's delivery, a variable that the task declares in `task_env` with a
# value that changes how the shell, the dynamic loader, interpreters and
# runtimes, build tools, installers, git, the trust store, proxy routing (set by
# the worker) or helper programs behave, or where the task writes (the cache and
# artifacts directory names, also set by the worker). It is not a sandbox: the
# task already runs arbitrary code with what it receives. The list is closed; a new name
# enters only with a concrete case. The worker applies the same denylist to the
# delivered values. The comparison is case-insensitive (`name.upper()`):
# lowercase `http_proxy` is also read by curl and others, and no variant may
# evade the rule. The lists are uppercase and literals: the parity test reads
# them with `ast` and requires them to be identical to those in
# `client/src/orcaster_worker/task_env.py` (which include `PATH` and its
# `BASE_ENV_PASSTHROUGH`).
RESERVED_SECRET_NAMES: frozenset[str] = frozenset(
    {
        # Base environment of the worker (`BASE_ENV_PASSTHROUGH`) and PATH.
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        # Shell startup and behaviour.
        "BASH_ENV",
        "ENV",
        "IFS",
        "CDPATH",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        # glibc loader. GLIBC_TUNABLES: CVE-2023-4911.
        "GCONV_PATH",
        "LOCPATH",
        "GLIBC_TUNABLES",
        # Interpreters and runtimes (NODE_* is covered as a prefix).
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "PERLLIB",
        "CLASSPATH",
        "PHPRC",
        # Compilers and build tools.
        "CC",
        "CXX",
        "CPP",
        "LDSHARED",
        "CFLAGS",
        "LDFLAGS",
        "MAKEFLAGS",
        "GOFLAGS",
        "RUSTC_WRAPPER",
        # Helper programs that other tools invoke.
        "LESSOPEN",
        "PAGER",
        "EDITOR",
        "VISUAL",
        "SHELL",
        # Configuration files of network clients and version control.
        "CURL_HOME",
        "WGETRC",
        "HGRCPATH",
        # Destination and TLS keys of a client. HF_ENDPOINT changes the host
        # to which `huggingface_hub` sends the task's HF_TOKEN, and TLS then
        # validates the new host; SSLKEYLOGFILE makes Python's `ssl` write the
        # TLS session keys to the path it names.
        "HF_ENDPOINT",
        "SSLKEYLOGFILE",
        # Proxy routing: set by the worker.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        # Task directories: the worker sets them itself. The TASKORCH_* names are
        # deprecated aliases kept for tasks written before the rename.
        "ORCASTER_CACHE_DIR",
        "ORCASTER_ARTIFACTS_DIR",
        "TASKORCH_CACHE_DIR",
        "TASKORCH_ARTIFACTS_DIR",
    }
)
RESERVED_SECRET_PREFIXES: tuple[str, ...] = (
    "LD_",
    "DYLD_",
    "PYTHON",
    "UV_",
    "PIP_",
    "SSL_CERT_",
    # GIT_ covers GIT_CONFIG_*, GIT_SSH_COMMAND and GIT_EXEC_PATH; GITHUB_TOKEN
    # does not start with GIT_ and stays allowed.
    "GIT_",
    "PERL5",
    "PERL_",
    "RUBY",
    "GEM_",
    "BUNDLE_",
    "NPM_CONFIG_",
    "JAVA_",
    "DOTNET_",
    "CORECLR_",
    "NODE_",
    "CARGO_",
    "OPENSSL_",
    "XDG_",
    # ORCH_ in any case: a secret never takes a control plane name. The `ORCH_`
    # rule of `task_env` remains separate and case-sensitive.
    "ORCH_",
)
RESERVED_SECRET_SUFFIXES: tuple[str, ...] = ("_CA_BUNDLE",)
# Explicit classes (ASCII): `\w` would accept Unicode letters.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Known token prefixes (Hugging Face, GitHub, GitLab, Slack, OpenAI and
# OpenRouter: `sk-or-` is covered by `sk-`). A `task_env` entry that STARTS with
# one is almost certainly a secret pasted in place of the name: it is rejected on
# creation. It is anchored at the start (used with `match`) and case-sensitive, so
# `HF_TOKEN`, `GITHUB_TOKEN` and `http_proxy` remain valid names. It is a prefix
# guard, not a secret detector: a token without one of these prefixes passes. The
# parity test requires it to be identical to the one in
# `client/src/orcaster_worker/task_env.py`, which uses it to avoid listing the
# entry in its logs (rows stored before this rule are still served and run).
_TOKEN_PREFIX_RE = re.compile(r"(?:hf_|gh[opsur]_|github_pat_|glpat-|xox[bp]-|sk-)")


def is_valid_env_name(name: str) -> bool:
    """True if the whole `name` is an ASCII environment variable name.

    It is the same rule the worker applies (`_VALID_NAME` in
    `client/src/orcaster_worker/task_env.py`; a parity test compares the two
    patterns). It validates the shape, not the content: a string of letters,
    digits and underscore is a valid name even if it is a token pasted by
    mistake. The token prefix is checked by `validate_task_env_entries` and
    `validate_secret_name`, not by this function.
    """
    return _ENV_NAME_RE.fullmatch(name) is not None


def validate_task_env_entries(entries: list, *, field: str) -> None:
    """Validate each `task_env` entry and raise `ValueError` with the index.

    Per entry and in order: it is a string, it does not start with a known token
    prefix (`_TOKEN_PREFIX_RE`; checked before the shape so that `glpat-...`
    gets the useful message), it is a valid variable name (covers `NAME=value`,
    spaces, a trailing newline and the empty string) and it does not use the
    reserved `ORCH_` prefix. Before iterating, the list cannot exceed
    `TASK_ENV_MAX_ENTRIES` entries and, per entry, a string cannot exceed
    `TASK_ENV_NAME_MAX_LEN` characters (checked before the regex). It stops at
    the first invalid entry. The messages carry only the index and the bound:
    neither the entry nor part of it, because a mistyped entry may be a
    `NAME=value` with the value of a secret or the token itself.
    """
    if len(entries) > TASK_ENV_MAX_ENTRIES:
        raise ValueError(f"{field} may have at most {TASK_ENV_MAX_ENTRIES} entries")
    for i, entry in enumerate(entries):
        if not isinstance(entry, str):
            raise ValueError(f"{field}[{i}] must be a string")
        if len(entry) > TASK_ENV_NAME_MAX_LEN:
            raise ValueError(
                f"{field}[{i}] is longer than {TASK_ENV_NAME_MAX_LEN} characters; "
                "declare the variable name only"
            )
        if _TOKEN_PREFIX_RE.match(entry) is not None:
            raise ValueError(
                f"{field}[{i}] looks like a credential (known token prefix), not a "
                "variable name; declare the variable name only"
            )
        if not is_valid_env_name(entry):
            raise ValueError(
                f"{field}[{i}] is not a valid environment variable name; declare the "
                "variable name only (letters, digits and underscores, not starting "
                "with a digit), not NAME=value"
            )
        if entry.startswith(RESERVED_ENV_PREFIX):
            raise ValueError(
                f"{field}[{i}] may not declare ORCH_ vars; those are worker/control-plane secrets"
            )


def validate_payload_task_env(payload: dict) -> None:
    """Validate `payload.task_env` if present; raises `ValueError`.

    It is the single rule of the routes that store a payload that the worker
    runs: creating and forking a task (`AdminCreateTaskRequest`), `POST
    /admin/workers/{id}/command` and `POST /admin/workers/{id}/control-op`.
    Absent (or `None`) is not validated: it is the legacy mode. If present it
    must be a list and each entry passes `validate_task_env_entries`. Only at
    write time: rows already stored are still served and run.
    """
    env = payload.get("task_env")
    if env is None:
        return
    if not isinstance(env, list):
        raise ValueError("payload.task_env must be a list of strings")
    validate_task_env_entries(env, field="payload.task_env")


def validate_payload_egress_allowlist(payload: dict) -> None:
    """Validate the SHAPE of `payload.egress_allowlist` if present; `ValueError`.

    It is the single rule of the same three routes as `validate_payload_task_env`:
    creating and forking a task (`AdminCreateTaskRequest`), `POST
    /admin/workers/{id}/command` and `POST /admin/workers/{id}/control-op`.
    Without it, a bare `"example.com"` without the brackets would be stored and
    signed by the other two, and the worker turns a value that is not a list of
    strings into `None`: no egress restriction.

    Absent (or `None`) is not validated: it is the legacy mode. If present (even
    `[]`) it must be a list of strings; `[]` is strict mode, not absence. Shape
    only: the semantics of each entry (exact host or subdomain wildcard) is
    applied by the worker. Only at write time: rows already stored are still
    served and run.
    """
    egress = payload.get("egress_allowlist")
    if egress is None:
        return
    if not isinstance(egress, list) or not all(isinstance(x, str) for x in egress):
        raise ValueError("payload.egress_allowlist must be a list of strings")


def looks_like_token(text: str) -> bool:
    """True if `text` starts with a known token prefix (`_TOKEN_PREFIX_RE`).

    `validate_secret_name` uses it to reject a token pasted in place of the name.
    """
    return isinstance(text, str) and _TOKEN_PREFIX_RE.match(text) is not None


def validate_secret_name(name: str) -> str:
    """Validate the name of a task secret and return it unchanged.

    Known token prefix (`_TOKEN_PREFIX_RE`, the same guard as `task_env`; checked
    first so that `glpat-...` gets the useful message), `ORCH_` prefix (here
    case-insensitive; `task_env` compares it case-sensitively), environment
    variable name pattern `^[A-Za-z_][A-Za-z0-9_]*$`, maximum length of 128 and
    the denylist of reserved names (`RESERVED_SECRET_*`, case-insensitive).
    `task_env` does not apply the denylist. Only when setting: deletion uses
    `validate_secret_name_pattern`, so a row stored before this rule can be
    deleted.
    """
    if looks_like_token(name):
        # The message does not repeat the name: it is the token itself.
        raise ValueError(
            "secret name looks like a credential (known token prefix), not a "
            "variable name; use the variable name only"
        )
    validate_secret_name_pattern(name)
    # Case-insensitive, unlike `task_env`: doing so there would reject `orch_x`,
    # which production accepts today.
    if name.upper().startswith(RESERVED_ENV_PREFIX):
        raise ValueError(
            "secret name uses the reserved ORCH_ prefix (in any case); "
            "those are worker/control-plane secrets"
        )
    if is_reserved_secret_name(name):
        raise ValueError(
            "secret name is a reserved name: it controls the shell, loader, "
            "interpreter or runtime, build tools, installer, git, trust store, proxy "
            "routing, helper programs or directories of the task"
        )
    return name


def validate_secret_name_pattern(name: str) -> str:
    """Validate only the shape of the name (non-empty, maximum length, pattern).

    It is what deletion requires: a name that became reserved after being stored
    must be deletable.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("secret name must be a non-empty string")
    if len(name) > SECRET_NAME_MAX_LEN:
        raise ValueError(f"secret name must be at most {SECRET_NAME_MAX_LEN} characters")
    if not is_valid_env_name(name):
        # Without repeating the name: a malformed name may be a `NAME=value` or
        # a token with a hyphen (`glpat-...` if it arrives through deletion).
        raise ValueError(
            "invalid secret name: use letters, digits and underscores, not starting with a digit"
        )
    return name


def is_reserved_secret_name(name: str) -> bool:
    """True if `name` falls in the denylist, case-insensitively."""
    upper = name.upper()
    return (
        upper in RESERVED_SECRET_NAMES
        or upper.startswith(RESERVED_SECRET_PREFIXES)
        or upper.endswith(RESERVED_SECRET_SUFFIXES)
    )
