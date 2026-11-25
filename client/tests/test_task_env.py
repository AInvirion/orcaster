import logging
import time

import pytest

from orcaster_worker.task_env import (
    BASE_ENV_PASSTHROUGH,
    MISSING_NAMES_LOG_LIMIT,
    TASK_ENV_NAME_MAX_LEN,
    _listable_name,
    build_scrubbed_base_env,
    cap_names_for_log,
    describe_env_sources,
    missing_declared_names,
    render_names,
    warn_missing_declared,
)

# Logger used by the module; caplog captures it through its parent.
LOGGER = "orcaster.worker"
MISSING_MSG = "task_env"

SRC = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/orcaster",
    "LANG": "C.UTF-8",
    "GITHUB_TOKEN": "ghp_secret",
    "API_TOKEN": "hf_secret",
    "ORCH_API_KEY": "orch_secret",
    "ORCH_ENROLLMENT_TOKEN": "enroll_secret",
    "PYTHONPATH": "/app/src",
    "HOST_HOSTNAME": "worker-1",
}


def test_none_is_legacy_full_copy():
    env = build_scrubbed_base_env(None, source=SRC)
    assert env == SRC
    assert env is not SRC  # copy


def test_empty_list_gives_base_only_no_secrets():
    env = build_scrubbed_base_env([], source=SRC)
    assert "GITHUB_TOKEN" not in env
    assert "API_TOKEN" not in env
    assert "ORCH_API_KEY" not in env
    assert "PYTHONPATH" not in env  # not in the base allowlist
    assert "HOST_HOSTNAME" not in env
    assert env["PATH"] == "/usr/bin:/bin"  # PATH is not a secret, it does pass
    assert env["HOME"] == "/home/orcaster"
    assert env["LANG"] == "C.UTF-8"


def test_declared_names_are_injected():
    env = build_scrubbed_base_env(["GITHUB_TOKEN", "API_TOKEN"], source=SRC)
    assert env["GITHUB_TOKEN"] == "ghp_secret"
    assert env["API_TOKEN"] == "hf_secret"


def test_orch_prefixed_never_injected_even_if_declared():
    env = build_scrubbed_base_env(
        ["ORCH_API_KEY", "ORCH_ENROLLMENT_TOKEN", "GITHUB_TOKEN"], source=SRC
    )
    assert "ORCH_API_KEY" not in env
    assert "ORCH_ENROLLMENT_TOKEN" not in env
    assert env["GITHUB_TOKEN"] == "ghp_secret"


def test_declared_missing_in_source_is_skipped():
    env = build_scrubbed_base_env(["DOES_NOT_EXIST"], source=SRC)
    assert "DOES_NOT_EXIST" not in env


def test_base_passthrough_only_when_present():
    env = build_scrubbed_base_env([], source={"PATH": "/bin"})
    assert set(env) <= set(BASE_ENV_PASSTHROUGH) | {"PATH"}


def _missing_records(caplog):
    """Records of the task_env warning (excludes the denylist names one)."""
    return [r for r in caplog.records if MISSING_MSG in r.getMessage()]


def test_missing_declared_names_one_missing():
    assert missing_declared_names(["GITHUB_TOKEN", "DOES_NOT_EXIST"], source=SRC) == (
        ["DOES_NOT_EXIST"],
        0,
    )


def test_missing_declared_names_several_missing_keep_declared_order():
    declared = ["ZED_MISSING", "GITHUB_TOKEN", "ALPHA_MISSING", "MID_MISSING"]
    assert missing_declared_names(declared, source=SRC) == (
        ["ZED_MISSING", "ALPHA_MISSING", "MID_MISSING"],
        0,
    )


def test_missing_declared_names_none_missing():
    assert missing_declared_names(["GITHUB_TOKEN", "API_TOKEN"], source=SRC) == ([], 0)


def test_missing_declared_names_none_is_legacy_and_reports_nothing():
    assert missing_declared_names(None, source=SRC) == ([], 0)


def test_missing_declared_names_empty_list():
    assert missing_declared_names([], source=SRC) == ([], 0)


def test_missing_declared_names_excludes_denylisted():
    # ORCH_* is never injected and already has its own message: it does not count as
    # "missing" even if it does not exist in the source either.
    declared = ["ORCH_NOT_IN_SOURCE", "ORCH_API_KEY", "DOES_NOT_EXIST"]
    assert missing_declared_names(declared, source=SRC) == (["DOES_NOT_EXIST"], 0)


def test_missing_declared_names_dedupes_keeping_first_position():
    declared = ["B_MISSING", "A_MISSING", "B_MISSING", "A_MISSING"]
    assert missing_declared_names(declared, source=SRC) == (["B_MISSING", "A_MISSING"], 0)


def test_missing_declared_names_uses_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("ORCASTER_PRESENT_646", "x")
    monkeypatch.delenv("ORCASTER_ABSENT_646", raising=False)
    assert missing_declared_names(["ORCASTER_PRESENT_646", "ORCASTER_ABSENT_646"]) == (
        ["ORCASTER_ABSENT_646"],
        0,
    )


def test_warn_missing_declared_one_record_for_one_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared(
            ["GITHUB_TOKEN", "DOES_NOT_EXIST"], task_id="12345", source=SRC
        )
    records = _missing_records(caplog)
    assert result == (["DOES_NOT_EXIST"], 0)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "DOES_NOT_EXIST" in message
    assert "task_id=12345 " in message
    assert "execution_id=" not in message
    assert "GITHUB_TOKEN" not in message  # present: not listed


def test_warn_missing_declared_one_record_naming_all_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared(
            ["FIRST_MISSING", "API_TOKEN", "SECOND_MISSING", "THIRD_MISSING"],
            task_id="12345",
            source=SRC,
        )
    records = _missing_records(caplog)
    assert result == (["FIRST_MISSING", "SECOND_MISSING", "THIRD_MISSING"], 0)
    assert len(records) == 1  # a single warning, not one per name
    message = records[0].getMessage()
    assert "3 name(s)" in message
    for name in ("FIRST_MISSING", "SECOND_MISSING", "THIRD_MISSING"):
        assert name in message


def test_warn_missing_declared_silent_when_none_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared(["GITHUB_TOKEN", "API_TOKEN"], task_id="12345", source=SRC)
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_silent_for_legacy_none(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared(None, task_id="12345", source={})
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_silent_for_empty_list(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared([], task_id="12345", source={})
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_denylisted_only_does_not_emit_new_message(caplog):
    # build_scrubbed_base_env already warns about ORCH_*; the new warning is not duplicated.
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared(
            ["ORCH_API_KEY", "ORCH_NOT_IN_SOURCE"], task_id="12345", source=SRC
        )
    assert result == ([], 0)
    assert _missing_records(caplog) == []
    assert caplog.records == []


def test_denylisted_keeps_its_own_message_and_no_missing_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        build_scrubbed_base_env(["ORCH_API_KEY"], source=SRC)
        warn_missing_declared(["ORCH_API_KEY"], task_id="12345", source=SRC)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["task declared denylisted env var 'ORCH_API_KEY'; refusing to inject"]


def test_build_scrubbed_base_env_does_not_log_for_missing_names(caplog):
    # The warning does not live in the build function (it is called several times per
    # execution): building the environment stays silent for absent names.
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        build_scrubbed_base_env(["DOES_NOT_EXIST"], source=SRC)
    assert caplog.records == []


def test_warn_missing_declared_never_logs_values(caplog):
    secret = "s3cr3t-value-must-not-leak-646"
    src = {**SRC, "PRESENT_WITH_SECRET": secret}
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        warn_missing_declared(["PRESENT_WITH_SECRET", "ABSENT_NAME"], task_id="12345", source=src)
    assert len(_missing_records(caplog)) == 1
    assert "ABSENT_NAME" in caplog.text
    assert secret not in caplog.text
    for value in src.values():
        assert value not in caplog.text


def test_warn_missing_declared_renders_each_name_with_repr(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared(["A_MISSING", "B_MISSING"], task_id="12345", source=SRC)
    records = _missing_records(caplog)
    assert len(records) == 1
    # Same as the denylist warning: each name with %r.
    assert "'A_MISSING', 'B_MISSING'" in records[0].getMessage()


def test_warn_missing_declared_never_logs_the_text_of_a_name_with_a_newline(caplog):
    # A name with a newline is not a valid name: its text is not logged
    # (not even escaped) and it cannot forge a log line.
    forged = "BAD_646\nFORGED line"
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = warn_missing_declared([forged], task_id="12345", source=SRC)
    assert result == ([], 1)
    records = _missing_records(caplog)
    assert len(records) == 1
    assert "\n" not in records[0].getMessage()
    assert "BAD_646" not in caplog.text
    assert "FORGED" not in caplog.text


INVALID_NAMES = [
    "FOO\n",  # `$` would accept the trailing newline; fullmatch does not
    "A B",
    "1ABC",
    "FOO-BAR",
    "  ",
    "API_TOKEN=x",
    "NAME=",
    "ÁRBOL",
]


@pytest.mark.parametrize("entry", INVALID_NAMES)
def test_missing_declared_names_counts_invalid_entries_without_listing_them(entry):
    assert missing_declared_names([entry], source=SRC) == ([], 1)


def test_missing_declared_names_returns_valid_missing_and_invalid_count():
    declared = ["A_MISSING", "API_TOKEN=hf_abc123", "GITHUB_TOKEN", "B_MISSING", "bad name"]
    assert missing_declared_names(declared, source=SRC) == (["A_MISSING", "B_MISSING"], 2)


def test_missing_declared_names_accepts_the_usual_variable_names():
    declared = ["_X", "a", "A1", "lower_case_9", "_"]
    assert missing_declared_names(declared, source={}) == (declared, 0)


def test_missing_declared_names_skips_empty_strings_silently():
    assert missing_declared_names([""], source=SRC) == ([], 0)
    assert missing_declared_names(["", "A_MISSING", ""], source=SRC) == (["A_MISSING"], 0)


def test_missing_declared_names_checks_the_denylist_before_validity():
    # An ORCH_* entry keeps its own message; it does not count as invalid.
    assert missing_declared_names(["ORCH_X=1", "ORCH_API_KEY"], source=SRC) == ([], 0)


def test_warn_missing_declared_never_logs_an_entry_that_carries_a_value(caplog):
    # `--env API_TOKEN=hf_abc123` (like `podman -e`): the secret's value must not
    # reach the log as if it were a name.
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            ["API_TOKEN=hf_abc123", "A_MISSING"], task_id="12345", source=SRC
        )
    assert result == (["A_MISSING"], 1)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "'A_MISSING'" in message
    assert "1 entry(ies) are not valid variable names (text omitted)" in message
    assert "hf_abc123" not in caplog.text
    assert "API_TOKEN=hf_abc123" not in caplog.text


def test_warn_missing_declared_only_invalid_emits_one_record_with_the_count(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(["API_TOKEN=hf_abc123", "A B"], task_id="12345", source=SRC)
    assert result == ([], 2)
    records = _missing_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "2 entry(ies) that are not valid variable names (text omitted, not injected)" in message
    assert "absent from the worker environment" not in message
    assert "hf_abc123" not in caplog.text
    assert "API_TOKEN" not in caplog.text
    assert "A B" not in caplog.text


def test_warn_missing_declared_skips_empty_strings_silently(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared([""], task_id="12345", source=SRC)
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_empty_string_is_neither_listed_nor_counted(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(["", "A_MISSING"], task_id="12345", source=SRC)
    assert result == (["A_MISSING"], 0)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "1 name(s)" in message
    assert "'A_MISSING'" in message
    assert "''" not in message
    assert "not valid variable names" not in message


def test_warn_missing_declared_labels_the_id_as_task_id_in_every_message(caplog):
    # main.py passes the task id; "execution_id=" would send people looking for an id
    # that does not exist.
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared(["A_MISSING"], task_id="12345", source=SRC)
        warn_missing_declared(["bad name"], task_id="12345", source=SRC)
        warn_missing_declared(["A_MISSING", "bad name"], task_id="12345", source=SRC)
    records = _missing_records(caplog)
    assert len(records) == 3
    for record in records:
        assert record.getMessage().startswith("task_id=12345 task_env ")
    assert "execution_id" not in caplog.text


def test_denylisted_entry_with_a_value_never_logs_the_value(caplog):
    # `--env ORCH_X=secret` (like `podman -e`): only the name reaches the log.
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env(["ORCH_X=secret"], source=SRC)
    assert [r.getMessage() for r in caplog.records] == [
        "task declared denylisted env var 'ORCH_X'; refusing to inject"
    ]
    assert "secret" not in caplog.text
    assert "ORCH_X=secret" not in caplog.text
    assert "ORCH_X" not in env
    assert "ORCH_X=secret" not in env
    assert "secret" not in env.values()


def test_denylisted_entry_with_a_value_is_not_injected_even_if_the_worker_has_the_name(caplog):
    src = {**SRC, "ORCH_X": "worker-side-value"}
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env(["ORCH_X=secret"], source=src)
    assert "ORCH_X" not in env
    assert "ORCH_X=secret" not in env
    assert "secret" not in env.values()
    assert "worker-side-value" not in env.values()
    assert "secret" not in caplog.text
    assert "worker-side-value" not in caplog.text
    assert [r.getMessage() for r in caplog.records] == [
        "task declared denylisted env var 'ORCH_X'; refusing to inject"
    ]


def test_denylisted_entry_without_a_value_keeps_logging_its_name(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env(["ORCH_FOO"], source=SRC)
    assert [r.getMessage() for r in caplog.records] == [
        "task declared denylisted env var 'ORCH_FOO'; refusing to inject"
    ]
    assert "ORCH_FOO" not in env


def test_denylisted_entry_whose_name_part_is_invalid_is_only_counted(caplog):
    # The text before the "=" is not a valid name (newline, space):
    # nothing of the entry is logged, only an aggregated warning with the count.
    declared = ["ORCH_X\nFORGED=secret", "ORCH_ X=secret"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env(declared, source=SRC)
    assert [r.getMessage() for r in caplog.records] == [
        "task declared 2 denylisted env entry(ies) that are not valid variable names "
        "(text omitted); refusing to inject"
    ]
    assert "secret" not in caplog.text
    assert "FORGED" not in caplog.text
    assert "ORCH_X" not in caplog.text
    for entry in declared:
        assert entry not in env
    assert "secret" not in env.values()


def test_denylisted_valid_and_invalid_entries_log_the_name_and_one_aggregated_count(caplog):
    declared = ["ORCH_X=secret", "ORCH_A\nFORGED=secret", "ORCH_FOO", "ORCH_ B=secret"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        build_scrubbed_base_env(declared, source=SRC)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "task declared denylisted env var 'ORCH_X'; refusing to inject",
        "task declared denylisted env var 'ORCH_FOO'; refusing to inject",
        "task declared 2 denylisted env entry(ies) that are not valid variable names "
        "(text omitted); refusing to inject",
    ]
    assert "secret" not in caplog.text
    assert "FORGED" not in caplog.text


# --- Server secrets: present = what `build_scrubbed_base_env` injects ---

SERVER_SECRET = "s3rv3r-secret-value-must-not-leak-646"


class _NamesOnly(dict):
    """Server secrets that fail if anyone reads a value.

    The warning may only ask about names (``in``): any other access
    to a value breaks the test."""

    def __getitem__(self, key):
        raise AssertionError("the warning read the value of a secret")

    def get(self, *args, **kwargs):
        raise AssertionError("the warning read the value of a secret")

    def values(self):
        raise AssertionError("the warning read the values of the secrets")

    def items(self):
        raise AssertionError("the warning read the values of the secrets")


def test_missing_declared_names_counts_a_server_secret_as_present():
    secrets = {"SERVER_ONLY_646": SERVER_SECRET}
    assert missing_declared_names(["SERVER_ONLY_646"], source=SRC, secrets=secrets) == ([], 0)


def test_missing_declared_names_lists_a_name_in_neither_origin():
    secrets = {"SERVER_ONLY_646": SERVER_SECRET}
    declared = ["SERVER_ONLY_646", "NOWHERE_646", "GITHUB_TOKEN"]
    assert missing_declared_names(declared, source=SRC, secrets=secrets) == (["NOWHERE_646"], 0)


def test_missing_declared_names_worker_only_and_both_origins_are_present():
    assert missing_declared_names(["GITHUB_TOKEN"], source=SRC, secrets={}) == ([], 0)
    assert missing_declared_names(["GITHUB_TOKEN"], source=SRC, secrets=None) == ([], 0)
    both = {"GITHUB_TOKEN": SERVER_SECRET}
    assert missing_declared_names(["GITHUB_TOKEN"], source=SRC, secrets=both) == ([], 0)


def test_missing_declared_names_ignores_secrets_that_are_not_declared():
    # A stored secret that the task does not declare injects nothing and does not
    # cover a missing one.
    secrets = {"OTHER_646": SERVER_SECRET}
    assert missing_declared_names(["NOWHERE_646"], source=SRC, secrets=secrets) == (
        ["NOWHERE_646"],
        0,
    )


@pytest.mark.parametrize("name", ["LD_PRELOAD", "HTTPS_PROXY", "http_proxy", "GIT_SSH_COMMAND"])
def test_missing_declared_names_a_reserved_name_from_the_server_is_not_present(name):
    # build_scrubbed_base_env discards the server's value for a reserved
    # name: with no value in the worker it is not injected, so it stays missing.
    secrets = {name: SERVER_SECRET}
    env = build_scrubbed_base_env([name], source=SRC, secrets=secrets)
    assert name not in env
    assert missing_declared_names([name], source=SRC, secrets=secrets) == ([name], 0)


@pytest.mark.parametrize("name", ["LD_PRELOAD", "HTTPS_PROXY", "http_proxy"])
def test_missing_declared_names_a_reserved_name_in_the_worker_is_present(name):
    secrets = {name: SERVER_SECRET}
    src = {**SRC, name: "worker-value"}
    assert build_scrubbed_base_env([name], source=src, secrets=secrets)[name] == "worker-value"
    assert missing_declared_names([name], source=src, secrets=secrets) == ([], 0)


def test_missing_declared_names_denylisted_stays_excluded_with_a_server_secret():
    secrets = {"ORCH_API_KEY": SERVER_SECRET, "ORCH_NOT_IN_SOURCE": SERVER_SECRET}
    declared = ["ORCH_API_KEY", "ORCH_NOT_IN_SOURCE", "NOWHERE_646"]
    assert missing_declared_names(declared, source=SRC, secrets=secrets) == (["NOWHERE_646"], 0)


def test_missing_declared_names_legacy_ignores_secrets():
    assert missing_declared_names(None, source={}, secrets={"A_646": SERVER_SECRET}) == ([], 0)


def test_missing_declared_names_counts_an_injected_entry_as_present_even_if_not_a_valid_name():
    # `build_scrubbed_base_env` injects any `name in src`; it cannot be
    # reported as "not injected" nor as invalid.
    src = {**SRC, "MY-VAR": "worker-value"}
    assert build_scrubbed_base_env(["MY-VAR"], source=src)["MY-VAR"] == "worker-value"
    assert missing_declared_names(["MY-VAR"], source=src) == ([], 0)


@pytest.mark.parametrize("in_worker", [False, True])
@pytest.mark.parametrize("in_server", [False, True])
@pytest.mark.parametrize(
    "name",
    [
        "PLAIN_646",
        "API_TOKEN",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "HTTPS_PROXY",
        "http_proxy",
        "UV_INDEX_URL",
    ],
)
def test_a_declared_name_is_present_exactly_when_it_is_injected(name, in_server, in_worker):
    # Anti-drift: the warning and the environment that gets built use the same rule.
    src = {"PATH": "/usr/bin"} if not in_worker else {"PATH": "/usr/bin", name: "worker-value"}
    secrets = {name: SERVER_SECRET} if in_server else {}
    env = build_scrubbed_base_env([name], source=src, secrets=secrets)
    missing, invalid = missing_declared_names([name], source=src, secrets=secrets)
    assert invalid == 0
    assert (name in env) == (name not in missing)


def test_warn_missing_declared_silent_for_a_name_only_in_server_secrets(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            ["SERVER_ONLY_646"],
            task_id="12345",
            source=SRC,
            secrets={"SERVER_ONLY_646": SERVER_SECRET},
        )
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_warns_for_a_name_in_neither_origin(caplog):
    secrets = {"SERVER_ONLY_646": SERVER_SECRET}
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            ["SERVER_ONLY_646", "NOWHERE_646"], task_id="12345", source=SRC, secrets=secrets
        )
    assert result == (["NOWHERE_646"], 0)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "1 name(s)" in message
    assert "'NOWHERE_646'" in message
    assert "SERVER_ONLY_646" not in message
    assert "absent from the worker environment and not provided by the task's stored secrets" in (
        message
    )


def test_warn_missing_declared_silent_for_a_name_only_in_the_worker(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            ["GITHUB_TOKEN"], task_id="12345", source=SRC, secrets={"OTHER_646": SERVER_SECRET}
        )
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_adds_no_message_for_a_denylisted_name_with_a_server_secret(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            ["ORCH_API_KEY", "ORCH_NOT_IN_SOURCE"],
            task_id="12345",
            source=SRC,
            secrets={"ORCH_API_KEY": SERVER_SECRET, "ORCH_NOT_IN_SOURCE": SERVER_SECRET},
        )
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_silent_in_legacy_mode_even_with_secrets(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(
            None, task_id="12345", source={}, secrets={"A_646": SERVER_SECRET}
        )
    assert result == ([], 0)
    assert caplog.records == []


def test_warn_missing_declared_never_reads_or_logs_a_secret_value(caplog):
    secrets = _NamesOnly(
        {"SERVER_ONLY_646": SERVER_SECRET, "UNUSED_646": SERVER_SECRET, "LD_PRELOAD": SERVER_SECRET}
    )
    declared = ["SERVER_ONLY_646", "LD_PRELOAD", "NOWHERE_646", "API_TOKEN=hf_abc123"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(declared, task_id="12345", source=SRC, secrets=secrets)
    assert result == (["LD_PRELOAD", "NOWHERE_646"], 1)
    assert len(_missing_records(caplog)) == 1
    assert SERVER_SECRET not in caplog.text
    assert "hf_abc123" not in caplog.text


# --- `task_env sources` line: same rules as the absent-names warning ---
#
# `describe_env_sources` / `EnvSources.render()` feed the worker's per-task
# `log.info`. An entry stored as `API_TOKEN=hf_abc123` or with newlines is never
# listed: it is only counted (`invalid_missing`, `invalid_denied`).

SOURCES_VALUE = "hf_abc123"


def test_describe_env_sources_valid_names_render_as_before():
    sources = describe_env_sources(
        ["API_TOKEN", "GITHUB_TOKEN", "NOT_SET_646", "ORCH_API_KEY"],
        source=SRC,
        secrets={"API_TOKEN": SERVER_SECRET, "UNDECLARED_646": SERVER_SECRET},
    )
    assert sources.invalid_missing == 0
    assert sources.invalid_denied == 0
    assert sources.render() == (
        "server=[API_TOKEN] worker_env=[GITHUB_TOKEN] missing=[NOT_SET_646] "
        "denied=[ORCH_API_KEY] ignored_server_secrets=[UNDECLARED_646] refused_server=[]"
    )


def test_describe_env_sources_legacy_render_is_unchanged():
    sources = describe_env_sources(None, source=SRC, secrets={"A_646": SERVER_SECRET})
    assert sources.render() == "legacy (full worker env) ignored_server_secrets=[A_646]"
    assert (sources.invalid_missing, sources.invalid_denied) == (0, 0)


def test_describe_env_sources_missing_entry_with_a_value_is_counted_not_listed():
    sources = describe_env_sources([f"API_TOKEN={SOURCES_VALUE}", "NOT_SET_646"], source={})
    assert sources.missing == ("NOT_SET_646",)
    assert sources.invalid_missing == 1
    line = sources.render()
    assert "missing=[NOT_SET_646] invalid_missing=1" in line
    assert SOURCES_VALUE not in line
    assert "API_TOKEN" not in line


def test_describe_env_sources_denied_entry_with_a_value_lists_only_the_name():
    sources = describe_env_sources(["ORCH_X=secret-646"], source=SRC)
    assert sources.denied == ("ORCH_X",)
    assert sources.invalid_denied == 0
    line = sources.render()
    assert "denied=[ORCH_X]" in line
    assert "secret-646" not in line
    assert "ORCH_X=" not in line
    assert "invalid_denied" not in line


def test_describe_env_sources_denied_entry_with_a_newline_in_the_value_lists_only_the_name():
    # The text before the first "=" is a valid name: only that is listed; the
    # value (with its newline) does not appear.
    sources = describe_env_sources(["ORCH_X=head-646\ntail-646"], source=SRC)
    assert sources.denied == ("ORCH_X",)
    line = sources.render()
    assert "denied=[ORCH_X]" in line
    assert "head-646" not in line
    assert "tail-646" not in line
    assert "\n" not in line


def test_describe_env_sources_denied_entry_whose_name_part_is_invalid_is_only_counted():
    declared = ["ORCH_X\nFORGED=secret-646", "ORCH_ X=secret-646", "ORCH_OK_646"]
    sources = describe_env_sources(declared, source=SRC)
    assert sources.denied == ("ORCH_OK_646",)
    assert sources.invalid_denied == 2
    line = sources.render()
    assert "denied=[ORCH_OK_646] invalid_denied=2" in line
    assert "FORGED" not in line
    assert "secret-646" not in line
    assert "\n" not in line


@pytest.mark.parametrize("entry", INVALID_NAMES)
def test_describe_env_sources_counts_every_invalid_missing_entry_without_listing_it(entry):
    sources = describe_env_sources([entry], source=SRC)
    assert sources.missing == ()
    assert sources.invalid_missing == 1
    line = sources.render()
    assert "missing=[] invalid_missing=1" in line
    assert entry not in line
    assert "\n" not in line


def test_describe_env_sources_skips_empty_strings_silently():
    sources = describe_env_sources(["", "NOT_SET_646", ""], source={})
    assert sources.missing == ("NOT_SET_646",)
    assert (sources.invalid_missing, sources.invalid_denied) == (0, 0)
    assert "invalid" not in sources.render()


def test_describe_env_sources_dedupes_valid_names_and_counts_each_invalid_entry():
    declared = ["B_646", "A_646", "B_646", "bad name", "bad name", "ORCH_X=1", "ORCH_X=2", "ORCH_X"]
    sources = describe_env_sources(declared, source={})
    assert sources.missing == ("B_646", "A_646")
    assert sources.invalid_missing == 2
    assert sources.denied == ("ORCH_X",)


def test_describe_env_sources_counts_agree_with_the_missing_warning():
    declared = ["A_646", "API_TOKEN=hf_abc123", "bad name", "FOO\n", "ORCH_X=1", ""]
    missing, invalid = missing_declared_names(declared, source=SRC)
    sources = describe_env_sources(declared, source=SRC)
    assert list(sources.missing) == missing
    assert sources.invalid_missing == invalid


def test_describe_env_sources_a_present_entry_is_never_counted_as_invalid():
    # Same as `missing_declared_names`: what gets injected is not "missing".
    src = {**SRC, "MY-VAR": "worker-value"}
    sources = describe_env_sources(["MY-VAR"], source=src)
    assert sources.worker_env == ("MY-VAR",)
    assert sources.missing == ()
    assert sources.invalid_missing == 0


def test_describe_env_sources_never_reads_a_secret_value():
    secrets = _NamesOnly({"SERVER_ONLY_646": SERVER_SECRET, "UNUSED_646": SERVER_SECRET})
    sources = describe_env_sources(
        ["SERVER_ONLY_646", "NOWHERE_646", "API_TOKEN=hf_abc123", "LD_PRELOAD"],
        source=SRC,
        secrets=secrets,
    )
    assert sources.server == ("SERVER_ONLY_646",)
    assert sources.invalid_missing == 1
    assert SERVER_SECRET not in sources.render()


def test_describe_env_sources_render_escapes_a_name_that_could_forge_a_line():
    # A name that comes from an origin (server secrets or worker environment)
    # and carries a newline is escaped with `repr`: it cannot split the line.
    forged = "SRV_646\nFORGED line"
    sources = describe_env_sources(
        [forged, "WRK_646"],
        source={**SRC, "WRK_646": "x"},
        secrets={forged: SERVER_SECRET, "UNUSED_646\nFORGED too": SERVER_SECRET},
    )
    line = sources.render()
    assert "\n" not in line
    assert repr(forged) in line
    assert repr("UNUSED_646\nFORGED too") in line
    assert "worker_env=[WRK_646]" in line
    assert SERVER_SECRET not in line


def test_render_names_leaves_valid_names_alone_and_escapes_the_rest():
    assert render_names(["A_646", "b2"]) == "A_646,b2"
    assert render_names([]) == ""
    assert render_names(["A_646", "X\nY", "it's"]) == "A_646,'X\\nY',\"it's\""


# --- Token-prefixed entries: they are counted, not listed ---------------------
#
# `hf_...`, `ghp_...` have the shape of a valid name, but an entry that STARTS with
# a known token prefix is almost certainly a secret pasted in place of the name.
# `_listable_name` treats it as not listable (it is only counted). Injection does not change:
# a declared name that exists in the worker environment or in the server secrets is
# injected just as before.

TOKEN_PREFIXED = [
    "hf_AbC123",
    "ghp_x",
    "gho_x",
    "ghs_x",
    "ghu_x",
    "ghr_x",
    "github_pat_x",
    "glpat-x",
    "xoxb-x",
    "xoxp-x",
    "sk-x",
    "sk-or-x",
]

# Usual names that merely resemble a prefix: the pattern is anchored and case-sensitive,
# so they remain listable.
LOOKALIKE_NAMES = [
    "HF_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "SK_X",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "Hf_x",
    "my_hf_x",
    "glpat_x",
    "sk_x",
]


@pytest.mark.parametrize("entry", TOKEN_PREFIXED)
def test_listable_name_is_none_for_a_token_prefixed_entry(entry):
    assert _listable_name(entry) is None


@pytest.mark.parametrize("name", LOOKALIKE_NAMES)
def test_listable_name_keeps_names_that_only_resemble_a_token_prefix(name):
    assert _listable_name(name) == name


def test_listable_name_of_a_denylisted_entry_is_still_the_part_before_the_equals():
    # The token prefix does not change the denylist rule: `ORCH_X=hf_abc` lists `ORCH_X`.
    assert _listable_name("ORCH_X=hf_abc123") == "ORCH_X"
    assert _listable_name("ORCH_X") == "ORCH_X"


@pytest.mark.parametrize("entry", TOKEN_PREFIXED)
def test_missing_declared_names_counts_a_token_prefixed_entry_without_listing_it(entry):
    assert missing_declared_names([entry], source=SRC) == ([], 1)
    assert missing_declared_names(["A_MISSING", entry, "B_MISSING"], source=SRC) == (
        ["A_MISSING", "B_MISSING"],
        1,
    )


@pytest.mark.parametrize("name", LOOKALIKE_NAMES)
def test_missing_declared_names_still_lists_a_lookalike_name_that_is_absent(name):
    assert missing_declared_names([name], source={}) == ([name], 0)


@pytest.mark.parametrize("entry", TOKEN_PREFIXED)
def test_warn_missing_declared_never_logs_a_token_prefixed_entry(caplog, entry):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(["A_MISSING", entry], task_id="12345", source=SRC)
    assert result == (["A_MISSING"], 1)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "'A_MISSING'" in message
    assert "1 entry(ies) are not valid variable names (text omitted)" in message
    assert entry not in caplog.text


def test_warn_missing_declared_only_token_prefixed_emits_one_record_with_the_count(caplog):
    declared = ["hf_AbC123", "ghp_x", "sk-or-x"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = warn_missing_declared(declared, task_id="12345", source=SRC)
    assert result == ([], 3)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "3 entry(ies) that are not valid variable names (text omitted, not injected)" in message
    for entry in declared:
        assert entry not in caplog.text


@pytest.mark.parametrize("entry", TOKEN_PREFIXED)
def test_describe_env_sources_counts_a_token_prefixed_entry_without_listing_it(entry):
    sources = describe_env_sources(["NOT_SET_646", entry], source={})
    assert sources.missing == ("NOT_SET_646",)
    assert sources.invalid_missing == 1
    line = sources.render()
    assert "missing=[NOT_SET_646] invalid_missing=1" in line
    assert entry not in line


def test_describe_env_sources_counts_each_appearance_of_a_token_prefixed_entry():
    sources = describe_env_sources(["hf_AbC123", "hf_AbC123", "ghp_x"], source={})
    assert sources.missing == ()
    assert sources.invalid_missing == 3
    assert "hf_AbC123" not in sources.render()


def test_describe_env_sources_counts_agree_with_the_missing_warning_for_token_prefixes():
    declared = ["A_646", "hf_AbC123", "ghp_x", "bad name", "ORCH_X=hf_abc123", ""]
    missing, invalid = missing_declared_names(declared, source=SRC)
    sources = describe_env_sources(declared, source=SRC)
    assert list(sources.missing) == missing == ["A_646"]
    assert sources.invalid_missing == invalid == 3
    assert sources.denied == ("ORCH_X",)
    assert sources.invalid_denied == 0


def test_build_scrubbed_base_env_does_not_log_a_token_prefixed_entry(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env(["hf_AbC123", "ghp_x"], source=SRC)
    assert env == {k: SRC[k] for k in ("HOME", "LANG", "PATH")}
    assert caplog.records == []


@pytest.mark.parametrize("name", ["hf_x", "ghp_x", "glpat-x", "sk-or-x"])
def test_a_token_prefixed_name_in_the_worker_env_is_still_injected(name, caplog):
    """Injection does not change: only which names get listed in the logs changes."""
    src = {**SRC, name: "worker-value-646"}
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        env = build_scrubbed_base_env([name], source=src)
    assert env[name] == "worker-value-646"
    assert caplog.records == []
    # Present: neither missing nor counted as invalid, same as before.
    assert missing_declared_names([name], source=src) == ([], 0)
    sources = describe_env_sources([name], source=src)
    assert sources.worker_env == (name,)
    assert sources.invalid_missing == 0


@pytest.mark.parametrize("name", ["hf_x", "ghp_x", "glpat-x", "sk-or-x"])
def test_a_token_prefixed_name_in_the_server_secrets_is_still_injected(name):
    secrets = {name: SERVER_SECRET}
    env = build_scrubbed_base_env([name], source=SRC, secrets=secrets)
    assert env[name] == SERVER_SECRET
    assert missing_declared_names([name], source=SRC, secrets=secrets) == ([], 0)
    sources = describe_env_sources([name], source=SRC, secrets=secrets)
    assert sources.server == (name,)
    assert sources.invalid_missing == 0


def test_injection_of_the_other_declared_names_is_unchanged_next_to_a_token_prefixed_entry():
    env = build_scrubbed_base_env(
        ["hf_AbC123", "GITHUB_TOKEN", "API_TOKEN", "NOT_SET_646"],
        source={**SRC, "hf_AbC123": "x"},
        secrets={"API_TOKEN": SERVER_SECRET},
    )
    assert env == {
        "HOME": SRC["HOME"],
        "LANG": SRC["LANG"],
        "PATH": SRC["PATH"],
        "hf_AbC123": "x",
        "GITHUB_TOKEN": SRC["GITHUB_TOKEN"],
        "API_TOKEN": SERVER_SECRET,
    }


# --- The missing warning is bounded and deduplication is linear ---


def test_the_log_bounds_match_the_issue():
    # At most 50 names listed; 128 characters, the same limit the
    # server applies to secret names (`SECRET_NAME_MAX_LEN`).
    assert MISSING_NAMES_LOG_LIMIT == 50
    assert TASK_ENV_NAME_MAX_LEN == 128


def test_cap_names_for_log_lists_up_to_the_limit_in_order_and_counts_the_rest():
    names = [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT + 7)]
    listed, left_out, too_long = cap_names_for_log(names)
    assert listed == names[:MISSING_NAMES_LOG_LIMIT]
    assert left_out == 7
    assert too_long == 0


def test_cap_names_for_log_counts_a_long_name_and_never_lists_it():
    long_name = "A" * (TASK_ENV_NAME_MAX_LEN + 1)
    at_limit = "B" * TASK_ENV_NAME_MAX_LEN
    listed, left_out, too_long = cap_names_for_log(["X_MISSING", long_name, at_limit])
    assert listed == ["X_MISSING", at_limit]
    assert left_out == 0
    assert too_long == 1


def test_cap_names_for_log_long_names_do_not_take_a_slot_of_the_limit():
    long_name = "L" * (TASK_ENV_NAME_MAX_LEN + 1)
    names = [long_name] + [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT)]
    listed, left_out, too_long = cap_names_for_log(names)
    assert listed == names[1:]
    assert (left_out, too_long) == (0, 1)


def test_warn_missing_declared_lists_at_most_the_limit_and_states_how_many_it_left_out(caplog):
    declared = [f"N{i}" for i in range(50_000)]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        missing, invalid = warn_missing_declared(declared, task_id="662", source={})
    assert (len(missing), invalid) == (50_000, 0)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "declares 50000 name(s)" in message  # the total, not what was listed
    assert "'N49'" in message
    assert "'N50'" not in message
    assert "49950 more name(s) not listed" in message
    assert len(message) < 2_000


def test_warn_missing_declared_at_the_limit_lists_every_name_and_says_nothing_more(caplog):
    declared = [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT)]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared(declared, task_id="662", source={})
    message = _missing_records(caplog)[0].getMessage()
    assert f"'N{MISSING_NAMES_LOG_LIMIT - 1}'" in message
    assert "not listed" not in message


def test_warn_missing_declared_never_logs_a_name_longer_than_the_limit(caplog):
    long_name = "A" * 1_000_000
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        missing, invalid = warn_missing_declared(
            ["SHORT_MISSING", long_name], task_id="662", source={}
        )
    assert (missing, invalid) == (["SHORT_MISSING", long_name], 0)
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "declares 2 name(s)" in message
    assert "'SHORT_MISSING'" in message
    assert "A" * (TASK_ENV_NAME_MAX_LEN + 1) not in message
    assert "1 name(s) longer than 128 characters (text omitted)" in message
    assert len(message) < 1_000


def test_warn_missing_declared_only_long_names_still_counts_them(caplog):
    long_name = "A" * (TASK_ENV_NAME_MAX_LEN + 1)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared([long_name], task_id="662", source={})
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "declares 1 name(s)" in message
    assert long_name not in message
    assert "1 name(s) longer than 128 characters (text omitted)" in message


def test_warn_missing_declared_reports_cap_long_names_and_invalid_entries_together(caplog):
    declared = (
        [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT + 2)]
        + ["Z" * (TASK_ENV_NAME_MAX_LEN + 5)]
        + ["API_TOKEN=hf_abc123"]
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared(declared, task_id="662", source={})
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert f"declares {MISSING_NAMES_LOG_LIMIT + 3} name(s)" in message
    assert "2 more name(s) not listed" in message
    assert "1 name(s) longer than 128 characters (text omitted)" in message
    assert "1 entry(ies) are not valid variable names (text omitted)" in message
    assert "hf_abc123" not in message


def test_missing_declared_names_dedupes_50000_names_in_under_a_second():
    declared = [f"N{i}" for i in range(50_000)]
    start = time.perf_counter()
    missing, invalid = missing_declared_names(declared, source={})
    elapsed = time.perf_counter() - start
    assert (len(missing), invalid) == (50_000, 0)
    assert elapsed < 1.0, f"missing_declared_names took {elapsed:.2f}s for 50,000 names"


def test_warn_missing_declared_with_50000_names_in_under_a_second(caplog):
    declared = [f"N{i}" for i in range(50_000)]
    start = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        warn_missing_declared(declared, task_id="662", source={})
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"warn_missing_declared took {elapsed:.2f}s for 50,000 names"


def test_missing_declared_names_large_list_keeps_order_and_first_occurrence():
    unique = [f"N{i}" for i in range(20_000)]
    declared = unique + list(reversed(unique)) + unique
    missing, invalid = missing_declared_names(declared, source={})
    assert missing == unique
    assert invalid == 0


# --- The `task_env sources` line bounds `missing=[...]` with the same rules ---


def test_describe_env_sources_render_is_byte_identical_at_the_limit():
    names = [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT - 1)] + ["B" * TASK_ENV_NAME_MAX_LEN]
    sources = describe_env_sources(["GITHUB_TOKEN", *names, "ORCH_API_KEY"], source=SRC)
    assert sources.render() == (
        "server=[] worker_env=[GITHUB_TOKEN] missing=[" + ",".join(names) + "] "
        "denied=[ORCH_API_KEY] ignored_server_secrets=[] refused_server=[]"
    )


def test_describe_env_sources_render_lists_at_most_the_limit_and_counts_the_rest():
    names = [f"N{i}" for i in range(50_000)]
    sources = describe_env_sources(["GITHUB_TOKEN", *names], source=SRC)
    assert (
        len(sources.missing) == 50_000
    )  # the dataclass keeps everything; only the render is bounded
    line = sources.render()
    listed = ",".join(names[:MISSING_NAMES_LOG_LIMIT])
    assert line == (
        f"server=[] worker_env=[GITHUB_TOKEN] missing=[{listed}] missing_not_listed=49950 "
        "denied=[] ignored_server_secrets=[] refused_server=[]"
    )
    assert len(line) < 1_000


def test_describe_env_sources_render_never_shows_a_long_missing_name():
    long_name = "A" * 1_000_000
    sources = describe_env_sources(["SHORT_MISSING", long_name, "API_TOKEN=hf_abc123"], source={})
    line = sources.render()
    assert "A" * (TASK_ENV_NAME_MAX_LEN + 1) not in line
    assert "missing=[SHORT_MISSING] missing_too_long=1 invalid_missing=1 " in line
    assert len(line) < 500


def test_describe_env_sources_render_only_long_missing_names():
    long_name = "L" * (TASK_ENV_NAME_MAX_LEN + 1)
    names = [long_name] + [f"N{i}" for i in range(MISSING_NAMES_LOG_LIMIT + 1)]
    sources = describe_env_sources(names, source={})
    line = sources.render()
    assert long_name not in line
    assert f"N{MISSING_NAMES_LOG_LIMIT - 1}]" in line
    assert "missing_not_listed=1 missing_too_long=1 denied=[]" in line
    only_long = describe_env_sources([long_name], source={}).render()
    assert "missing=[] missing_too_long=1 denied=[]" in only_long


def test_describe_env_sources_render_keeps_the_server_token_unbounded_and_unchanged():
    # Only `missing` is bounded: `server=[...]` and the rest stay as they were, so
    # the operators' `grep 'server=\[NAME\]'` keep matching.
    secrets = {f"S{i}": SERVER_SECRET for i in range(MISSING_NAMES_LOG_LIMIT + 5)}
    sources = describe_env_sources([*secrets, "NOT_SET_662"], source={}, secrets=secrets)
    line = sources.render()
    assert line.startswith("server=[" + ",".join(secrets) + "] ")
    assert "missing=[NOT_SET_662] denied=[]" in line
