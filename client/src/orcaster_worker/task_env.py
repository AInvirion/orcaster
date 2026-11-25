"""Build the environment of a task subprocess (env isolation).

Tri-state semantics:
- ``declared is None``  -> LEGACY: full copy of the worker environment
  (compatible with tasks without ``task_env``; all secrets pass through as before).
- ``declared`` is a list  -> STRICT: minimal environment built from scratch
  (allowlist of non-secret vars) PLUS the declared names that exist,
  EXCEPT those with a denylisted prefix (ORCH_*), which are never injected.

Server secrets: in strict mode each declared name takes
the value from ``secrets`` first and, if absent, from the worker environment,
except reserved names (``is_reserved_env_name``), where the server value is
always discarded and the worker value applies. In legacy mode ``secrets`` is
ignored (the server does not send them without ``task_env``).
``describe_env_sources`` reports the origin of each name, names only, for
the per-task log. That precedence lives in ``_value_origin``: ``missing_declared_names``
and ``warn_missing_declared`` use it too, so a name the server supplies is not
reported as missing. Which entry is listed and which is only counted is decided by
``_listable_name``, also shared: a declared entry that is not a valid variable name
(``API_TOKEN=hf_abc123``, or containing newlines) or that starts with a known token
prefix (``hf_...``) is only counted (missing warning, ``invalid_missing``,
``invalid_denied``) and its text is not printed. A name is printed when it is a valid
identifier without that prefix or when it really exists in the worker environment or
in the server secrets: then ``describe_env_sources`` lists it (``server``,
``worker_env`` or ``ignored``, with ``repr`` if it does not look like a name), because
that filter does not decide injection.

Pure unit without side effects: takes ``source`` (default os.environ) for tests.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("orcaster.worker.task_env")

# Never injectable into the task, even if declared. They are worker
# control-plane secrets.
ENV_DENYLIST_PREFIXES: tuple[str, ...] = ("ORCH_",)

# Non-secret vars that a legitimate task needs and that ALWAYS pass through
# in strict mode if present in the worker environment. PATH is handled
# separately (the venv prepends it). Deliberately EXCLUDED: PYTHONPATH (points to
# the worker /app/src), HOST_HOSTNAME, and every ORCH_*/token.
BASE_ENV_PASSTHROUGH: tuple[str, ...] = (
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
)


# Bounds of the missing-names warning. The server limits neither how many names
# `task_env` carries nor their length, so the warning cannot list them all: it lists at
# most `MISSING_NAMES_LOG_LIMIT` and counts the rest. A name longer than
# `TASK_ENV_NAME_MAX_LEN` characters is never written, only counted. 128 is the
# same limit the server applies to secret names
# (`SECRET_NAME_MAX_LEN` in `server/orcaster_server/env_names.py`); it is repeated here
# because the client does not import server code. `cap_names_for_log` applies both.
MISSING_NAMES_LOG_LIMIT = 50
TASK_ENV_NAME_MAX_LEN = 128


# Valid variable name: the ENTIRE string. `fullmatch` is used instead of `match`
# with `$`, because `$` accepts a trailing newline ("FOO\n").
_VALID_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Known token prefixes (Hugging Face, GitHub, GitLab, Slack, OpenAI/OpenRouter).
# An entry that STARTS with one is almost certainly a secret pasted in place of the
# name: it is not listed as missing or denied, only counted (`_listable_name`).
# It is anchored at the start (used with `match`) and case-sensitive, so it does not touch
# `API_TOKEN`, `GITHUB_TOKEN` or `http_proxy`. It is a prefix guard, not a secret
# detector. It only affects logs: injection does not consult it. It is the same
# pattern as `_TOKEN_PREFIX_RE` in `server/orcaster_server/env_names.py` (the server
# applies it when creating the task); a parity test compares the two.
_TOKEN_PREFIX_RE = re.compile(r"(?:hf_|gh[opsur]_|github_pat_|glpat-|xox[bp]-|sk-)")

# Names whose value is NEVER taken from the server ``secrets``, even if the task
# declares them. The denylist only prevents a workspace owner (who controls the
# stored secrets) from overriding, through the server delivery, a variable the task
# declares in ``task_env`` with a value that changes how the shell, the dynamic
# loader, interpreters and runtimes, build tools, installers, git, PATH and the
# base environment, the TLS trust store, proxy routing (owned by the worker) or
# helper programs behave. It is not a sandbox: the task already runs arbitrary code
# with what it receives. The list is closed; a new name is added only with a
# concrete case. For these names the worker environment value applies; the server
# value is discarded and reported by name (``refused_server``). The server also
# rejects them when storing them: this is defense in depth.
# The comparison is case-insensitive (``name.upper()``): lowercase ``http_proxy``
# is also read by curl and others. The lists are uppercase and literal: the parity
# test reads them with ``ast`` and requires them to be identical to those in
# ``server/orcaster_server/env_names.py``. That is why PATH and
# ``BASE_ENV_PASSTHROUGH`` are repeated here instead of derived.
RESERVED_ENV_NAMES: frozenset[str] = frozenset(
    {
        # Worker base environment (``BASE_ENV_PASSTHROUGH``) and PATH.
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
        # Shell startup and behavior.
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
        # Interpreters and runtimes (NODE_* is a prefix).
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
        # Configuration files of network and version-control clients.
        "CURL_HOME",
        "WGETRC",
        "HGRCPATH",
        # TLS destination and keys of a client. HF_ENDPOINT changes the
        # host to which `huggingface_hub` sends the task token, and TLS
        # then validates the new host; SSLKEYLOGFILE makes Python `ssl`
        # write the TLS session keys to the path it names.
        "HF_ENDPOINT",
        "SSLKEYLOGFILE",
        # Proxy routing: set by the worker.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    }
)
RESERVED_ENV_PREFIXES: tuple[str, ...] = (
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
    # ORCH_ in any case: the server value never takes a control-plane name.
    # ``ENV_DENYLIST_PREFIXES`` (declared names) stays separate and
    # case-sensitive.
    "ORCH_",
)
RESERVED_ENV_SUFFIXES: tuple[str, ...] = ("_CA_BUNDLE",)


def _denylisted(name: str) -> bool:
    return any(name.startswith(p) for p in ENV_DENYLIST_PREFIXES)


def _listable_name(entry: str) -> str | None:
    """The name that may be written to a log for ``entry``, or ``None``.

    Single rule for what is listed and what is only counted. A denylist entry is
    evaluated by the part before the first ``=``: what follows may be a value
    (``ORCH_X=secret``, written as ``podman -e``). Any other entry is evaluated whole.
    If that is not a valid variable name (``_VALID_NAME``) or starts with a known
    token prefix (``_TOKEN_PREFIX_RE``; ``hf_...`` looks like a name but is almost
    certainly a secret) it returns ``None``: the entry may be a secret or contain
    newlines, so it is only counted and the caller does not write its text as missing
    or denied. Used by ``build_scrubbed_base_env``, ``missing_declared_names`` and
    ``describe_env_sources``. Outside the denylist, the last two consult it only when
    ``_value_origin`` does not locate the name: one that exists in the worker
    environment or in the server secrets is reported by origin without going through
    here. It only decides what is logged: what is injected is decided by
    ``_value_origin``."""
    candidate = entry.split("=", 1)[0] if _denylisted(entry) else entry
    if _VALID_NAME.fullmatch(candidate) is None or _TOKEN_PREFIX_RE.match(candidate) is not None:
        return None
    return candidate


def render_names(names: Iterable[str], sep: str = ",") -> str:
    """Join names for a log so that none can forge a line.

    A valid variable name is emitted as is. Any other (for example the key of a
    server secret or of the worker environment containing a newline) is emitted
    with ``repr``, which escapes newlines and quotes it."""
    return sep.join(
        name if _VALID_NAME.fullmatch(name) is not None else repr(name) for name in names
    )


def cap_names_for_log(
    names: Sequence[str],
    *,
    limit: int = MISSING_NAMES_LOG_LIMIT,
    max_len: int = TASK_ENV_NAME_MAX_LEN,
) -> tuple[list[str], int, int]:
    """Bound a list of names for a log: ``(listed, not_listed, too_long)``.

    ``listed`` are the first ``limit`` names of at most ``max_len``
    characters, in the order received. ``too_long`` counts the names longer than
    ``max_len`` characters: they are never listed and take no slot of the limit.
    ``not_listed`` counts the short names left out because of the limit.
    Pure and linear: it neither deduplicates nor validates, whoever builds the list does."""
    listed: list[str] = []
    left_out = 0
    too_long = 0
    for name in names:
        if len(name) > max_len:
            too_long += 1
        elif len(listed) < limit:
            listed.append(name)
        else:
            left_out += 1
    return listed, left_out, too_long


def is_reserved_env_name(name: str) -> bool:
    """True if the value of ``name`` cannot come from the server ``secrets``.

    Case-insensitive: compares ``name.upper()`` against the lists."""
    upper = name.upper()
    return (
        upper in RESERVED_ENV_NAMES
        or upper.startswith(RESERVED_ENV_PREFIXES)
        or upper.endswith(RESERVED_ENV_SUFFIXES)
    )


def _value_origin(
    name: str, src: Mapping[str, str], server: Mapping[str, str]
) -> Literal["server", "worker"] | None:
    """Where the value of a declared name comes from, or ``None`` if it is not injected.

    Single precedence rule: ``server`` first, except reserved names
    (``is_reserved_env_name``), whose server value is discarded; otherwise the
    worker environment. It does not evaluate the ``ORCH_*`` denylist: the caller
    checks it. Shared by ``build_scrubbed_base_env`` (injects),
    ``missing_declared_names`` (warns) and ``describe_env_sources`` (reports), so
    they cannot drift apart. It only asks about names, never reads a value."""
    if name in server and not is_reserved_env_name(name):
        return "server"
    if name in src:
        return "worker"
    return None


def build_scrubbed_base_env(
    declared: list[str] | None,
    *,
    source: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the base environment for the task (without the venv PATH/VIRTUAL_ENV).

    ``secrets`` takes precedence over ``source`` for the declared names,
    except reserved ones (``is_reserved_env_name``), where the server value is
    discarded; the ORCH_* denylist applies to both origins."""
    src = os.environ if source is None else source
    if declared is None:
        return dict(src)
    server = secrets or {}

    env: dict[str, str] = {}
    for name in BASE_ENV_PASSTHROUGH:
        if name in src:
            env[name] = src[name]
    if "PATH" in src:
        env["PATH"] = src["PATH"]
    invalid_denylisted = 0
    for name in declared:
        if _denylisted(name):
            # Only the part before the first "=" (`_listable_name`): what follows
            # may be a value (`ORCH_X=secret`) and is never logged.
            bare = _listable_name(name)
            if bare is not None:
                log.warning("task declared denylisted env var %r; refusing to inject", bare)
            else:
                invalid_denylisted += 1
            continue
        if name in server and is_reserved_env_name(name):
            log.warning("server secret for reserved env var %r refused; worker value applies", name)
        origin = _value_origin(name, src, server)
        if origin == "server":
            env[name] = server[name]
        elif origin == "worker":
            env[name] = src[name]
        # nonexistent name -> not injected; the warning is emitted by
        # `warn_missing_declared` once per execution (this function is called
        # several times per execution and must not warn) and `describe_env_sources`
        # reports it as `missing` in the per-task log.
    if invalid_denylisted:
        # No text: an entry that is not a valid name may be a secret
        # or contain newlines. Only the count is reported.
        log.warning(
            "task declared %d denylisted env entry(ies) that are not valid variable "
            "names (text omitted); refusing to inject",
            invalid_denylisted,
        )
    return env


def missing_declared_names(
    declared: list[str] | None,
    *,
    source: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> tuple[list[str], int]:
    """Return ``(valid_missing, n_invalid)`` for the declared names.

    Pure: it only checks presence, never reads values (``secrets`` is only
    consulted by name). A name is present exactly when
    ``build_scrubbed_base_env`` injects it (``_value_origin``: server secret
    above the worker environment, except reserved names). ``None``
    (legacy) discards nothing. Evaluated in this order:

    - denylisted names are excluded first (they already have their own warning);
    - the empty string is skipped without being counted;
    - a name that is injected (from the server or the worker) is not reported,
      whether or not it is a valid variable name;
    - what is not a valid variable name (the ENTIRE ``[A-Za-z_][A-Za-z0-9_]*``,
      for example ``API_TOKEN=hf_abc123`` written as ``podman -e``) or starts with
      a known token prefix (``hf_abc123``) is only counted: its text may be a
      secret and is never returned;
    - a valid name that is not injected is listed, without duplicates (first
      occurrence) and in declared order. The list is complete: bounding it for a
      log is the job of ``cap_names_for_log``.

    Linear in the length of ``declared``: duplicates are detected with a
    ``set`` next to the list, not by searching the list.
    """
    if declared is None:
        return [], 0
    src = os.environ if source is None else source
    server = secrets or {}
    missing: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for name in declared:
        if _denylisted(name) or name == "":
            continue
        if _value_origin(name, src, server) is not None:
            continue
        if _listable_name(name) is None:
            invalid += 1
            continue
        if name in seen:
            continue
        seen.add(name)
        missing.append(name)
    return missing, invalid


def warn_missing_declared(
    declared: list[str] | None,
    *,
    task_id: str,
    source: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> tuple[list[str], int]:
    """Emit ONE warning for the missing names and invalid entries; return the pair.

    Returns the same as ``missing_declared_names``. It is called once per
    execution (not from ``build_scrubbed_base_env``, which is invoked several times)
    with the same ``secrets`` as ``build_scrubbed_base_env``: a name the server
    supplies is not missing. ``task_id`` is the task id (the one shown by
    ``orcaster task``): the worker receives no other per-execution id. Names
    only, never values (``secrets`` is only consulted by name). Each valid
    name is escaped with ``%r`` (like the denylist warning). For entries that are
    not valid names only the count is reported: their text may contain a secret
    or newlines and is never logged.

    The record is bounded (``cap_names_for_log``): it lists at most
    ``MISSING_NAMES_LOG_LIMIT`` names and says how many it left out; a name longer
    than ``TASK_ENV_NAME_MAX_LEN`` characters is only counted. ``declares %d
    name(s)`` is always the total of missing names. The returned value is not bounded.
    """
    missing, invalid = missing_declared_names(declared, source=source, secrets=secrets)
    if missing:
        listed, left_out, too_long = cap_names_for_log(missing)
        message = (
            "task_id=%s task_env declares %d name(s) absent from the worker "
            "environment and not provided by the task's stored secrets; not injected"
        )
        args: tuple[object, ...] = (task_id, len(missing))
        if listed:
            message += ": %s"
            args += (", ".join(repr(name) for name in listed),)
        if left_out:
            message += "; %d more name(s) not listed"
            args += (left_out,)
        if too_long:
            message += "; %d name(s) longer than %d characters (text omitted)"
            args += (too_long, TASK_ENV_NAME_MAX_LEN)
        if invalid:
            message += "; %d entry(ies) are not valid variable names (text omitted)"
            args += (invalid,)
        log.warning(message, *args)
    elif invalid:
        log.warning(
            "task_id=%s task_env has %d entry(ies) that are not valid variable "
            "names (text omitted, not injected)",
            task_id,
            invalid,
        )
    return missing, invalid


@dataclass(frozen=True)
class EnvSources:
    """Origin of each ``task_env`` name for a task. Names only.

    ``ignored``: server-supplied secrets that are not injected (not
    declared, or the task is in legacy mode).
    ``refused_server``: server secrets with a reserved name
    (``is_reserved_env_name``), discarded even if declared.
    ``invalid_missing`` / ``invalid_denied``: declared entries that are not a valid
    variable name or start with a known token prefix
    (``_listable_name``) and that would have gone to ``missing`` / ``denied``. They are
    only counted: their text may be a secret or contain newlines and is not stored.
    An empty string is not counted.
    ``server_worker_scoped``: subset of ``server`` whose value came from a
    row of the worker itself on the server. It is only shown when it is not
    empty, as a separate token at the end of the line.

    ``render`` bounds only ``missing`` with ``cap_names_for_log``, the same
    rules as the missing-names warning: it lists at most ``MISSING_NAMES_LOG_LIMIT``
    names and adds ``missing_not_listed=N``; a name longer than
    ``TASK_ENV_NAME_MAX_LEN`` characters is not written and is counted in
    ``missing_too_long=N``. The two tokens only appear when above zero,
    so the line stays byte for byte identical up to that limit. The dataclass keeps
    the full tuple; the rest of the line (``server=[...]`` included) is not bounded."""

    legacy: bool
    server: tuple[str, ...] = ()
    worker_env: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    refused_server: tuple[str, ...] = ()
    invalid_missing: int = 0
    invalid_denied: int = 0
    server_worker_scoped: tuple[str, ...] = ()

    def render(self) -> str:
        def _names(names: tuple[str, ...]) -> str:
            return "[" + render_names(names) + "]"

        def _count(label: str, n: int) -> str:
            # Only appears when there is something to count: without invalid entries the
            # line stays as it always was.
            return f" {label}={n}" if n else ""

        if self.legacy:
            return f"legacy (full worker env) ignored_server_secrets={_names(self.ignored)}"
        listed, not_listed, too_long = cap_names_for_log(self.missing)
        return (
            f"server={_names(self.server)} worker_env={_names(self.worker_env)} "
            f"missing={_names(tuple(listed))}{_count('missing_not_listed', not_listed)}"
            f"{_count('missing_too_long', too_long)}"
            f"{_count('invalid_missing', self.invalid_missing)} "
            f"denied={_names(self.denied)}{_count('invalid_denied', self.invalid_denied)} "
            f"ignored_server_secrets={_names(self.ignored)} "
            f"refused_server={_names(self.refused_server)}"
            # Separate token at the end, only if there is any. The line
            # stays byte for byte identical without worker secrets, and the token does
            # not contain `server=[`, so existing greps keep matching.
            + (
                f" server_worker_scoped={_names(self.server_worker_scoped)}"
                if self.server_worker_scoped
                else ""
            )
        )


def describe_env_sources(
    declared: list[str] | None,
    *,
    source: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    worker_scoped: Iterable[str] = (),
) -> EnvSources:
    """Classify the declared names by where their value comes from, with the
    same precedence as ``build_scrubbed_base_env``. Never reads values beyond
    checking presence.

    ``worker_scoped``: names the server supplied from a row of the worker
    itself. Only those that actually come from the server (``server``) are
    reported, in the same order.

    ``missing`` and ``denied`` follow the ``_listable_name`` rule (the same as
    ``missing_declared_names``): only valid names are listed, without duplicates and
    in order; the remaining entries are only counted (``invalid_missing``,
    ``invalid_denied``), once per occurrence, and the empty string is skipped."""
    src = os.environ if source is None else source
    server = secrets or {}
    if declared is None:
        return EnvSources(legacy=True, ignored=tuple(sorted(server)))
    # dict as an ordered set: no duplicates, in declared order.
    from_server: dict[str, None] = {}
    from_worker: dict[str, None] = {}
    missing: dict[str, None] = {}
    denied: dict[str, None] = {}
    invalid_missing = 0
    invalid_denied = 0
    for name in declared:
        if name == "":
            continue
        if _denylisted(name):
            bare = _listable_name(name)
            if bare is None:
                invalid_denied += 1
            else:
                denied[bare] = None
            continue
        origin = _value_origin(name, src, server)
        if origin == "server":
            from_server[name] = None
        elif origin == "worker":
            from_worker[name] = None
        elif _listable_name(name) is None:
            invalid_missing += 1
        else:
            missing[name] = None
    declared_set = set(declared)
    refused = sorted(n for n in server if is_reserved_env_name(n) and not _denylisted(n))
    refused_set = set(refused)
    ignored = sorted(
        n for n in server if n not in refused_set and (n not in declared_set or _denylisted(n))
    )
    scoped_set = set(worker_scoped)
    return EnvSources(
        legacy=False,
        server=tuple(from_server),
        server_worker_scoped=tuple(n for n in from_server if n in scoped_set),
        worker_env=tuple(from_worker),
        missing=tuple(missing),
        denied=tuple(denied),
        ignored=tuple(ignored),
        refused_server=tuple(refused),
        invalid_missing=invalid_missing,
        invalid_denied=invalid_denied,
    )
