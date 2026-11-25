import pytest
from pydantic import ValidationError

from orcaster_server.env_names import is_valid_env_name, validate_task_env_entries
from orcaster_server.schemas.tasks import AdminCreateTaskRequest

# Distinctive marker: no error message may contain it.
MARK = "SECRETMARK123"

_INVALID_NAME_MSG = (
    "payload.task_env[{i}] is not a valid environment variable name; declare the "
    "variable name only (letters, digits and underscores, not starting with a digit), "
    "not NAME=value"
)
_ORCH_MSG = (
    "payload.task_env[{i}] may not declare ORCH_ vars; those are worker/control-plane secrets"
)
_CREDENTIAL_MSG = (
    "payload.task_env[{i}] looks like a credential (known token prefix), not a variable "
    "name; declare the variable name only"
)

# Each alternative of the token prefix (`sk-or-` is covered by `sk-`).
TOKEN_PREFIXES = [
    "hf_",
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "ghr_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "sk-",
    "sk-or-",
]
TOKEN_PREFIXED_ENTRIES = [
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


def _req(payload: dict) -> AdminCreateTaskRequest:
    return AdminCreateTaskRequest(payload=payload, target="1")


def test_task_env_and_egress_absent_is_allowed():
    r = _req({"commands": ["echo hi"]})
    assert "task_env" not in r.payload
    assert "egress_allowlist" not in r.payload


def test_task_env_empty_list_is_allowed():
    r = _req({"commands": ["echo hi"], "task_env": [], "egress_allowlist": []})
    assert r.payload["task_env"] == []
    assert r.payload["egress_allowlist"] == []


def test_task_env_valid_names_allowed():
    r = _req(
        {
            "task_env": ["GITHUB_TOKEN", "PYPI_TOKEN"],
            "egress_allowlist": ["pypi.org", ".pythonhosted.org"],
        }
    )
    assert r.payload["task_env"] == ["GITHUB_TOKEN", "PYPI_TOKEN"]


def test_task_env_rejects_non_list():
    with pytest.raises(ValidationError, match="task_env"):
        _req({"task_env": "GITHUB_TOKEN"})


def test_task_env_rejects_non_string_items():
    with pytest.raises(ValidationError, match="task_env"):
        _req({"task_env": ["GITHUB_TOKEN", 123]})


def test_task_env_rejects_orch_prefixed_names():
    with pytest.raises(ValidationError, match="ORCH_"):
        _req({"task_env": ["ORCH_API_KEY"]})


def test_egress_rejects_non_list_of_strings():
    with pytest.raises(ValidationError, match="egress_allowlist"):
        _req({"egress_allowlist": [{"host": "x"}]})


# --- Each task_env entry is a valid variable name ----------------------------


@pytest.mark.parametrize(
    "name",
    [
        "HF_TOKEN",
        "GITHUB_TOKEN",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "_X",
        "A1",
        "GIT_CONFIG_KEY_0",
        "PATH",
        "orch_lower",
        "ORCH",
    ],
)
def test_is_valid_env_name_accepts(name: str) -> None:
    assert is_valid_env_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN=hf_abc123",
        "ORCH_X=secret",
        "A B",
        " A",
        "A ",
        "API_TOKEN\n",
        "\nAPI_TOKEN",
        "",
        "weird-name",
        "foo.bar",
        "1ABC",
        "A=",
        "A\u0663",  # Arabic-Indic digit: `\d` would accept it, the rule is ASCII
        "\u00e9x",
        "\uff21BC",  # fullwidth A
    ],
)
def test_is_valid_env_name_rejects(name: str) -> None:
    assert is_valid_env_name(name) is False


@pytest.mark.parametrize(
    "entry",
    [
        "API_TOKEN=hf_abc123",
        "ORCH_X=secret",
        "A B",
        "API_TOKEN\n",
        "",
        "weird-name",
        "foo.bar",
        "1ABC",
        "\u00e9x",
    ],
)
def test_task_env_rejects_invalid_names_naming_the_index(entry: str) -> None:
    """The invalid entry is the 2nd (index 1): the message names the index."""
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": ["OK", entry]})
    errors = ei.value.errors()
    assert len(errors) == 1
    assert errors[0]["msg"] == "Value error, " + _INVALID_NAME_MSG.format(i=1)


def test_task_env_reports_the_first_invalid_index() -> None:
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": ["A", "B", "bad one", "also-bad"]})
    assert ei.value.errors()[0]["msg"] == "Value error, " + _INVALID_NAME_MSG.format(i=2)


@pytest.mark.parametrize(
    "entry",
    [
        f"API_TOKEN={MARK}",
        f"ORCH_X={MARK}",
        f"{MARK} x",
        f"{MARK}-x",
        f"{MARK}.x",
        f"{MARK}\n",
    ],
)
def test_task_env_error_never_echoes_the_entry(entry: str) -> None:
    """Neither the message nor the `ValueError` repeat the entry or any part of it."""
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries(["OK", entry], field="payload.task_env")
    assert MARK not in str(ei.value)
    with pytest.raises(ValidationError) as ei2:
        _req({"task_env": ["OK", entry]})
    for err in ei2.value.errors():
        assert MARK not in err["msg"]


def test_task_env_orch_prefixed_name_names_the_index_without_echo() -> None:
    """`ORCH_` with a valid name shape: message by index, without the name."""
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": ["OK", f"ORCH_{MARK}"]})
    msg = ei.value.errors()[0]["msg"]
    assert msg == "Value error, " + _ORCH_MSG.format(i=1)
    assert MARK not in msg


def test_task_env_orch_with_value_reports_invalid_name_not_orch() -> None:
    """`ORCH_X=secret` fails the shape check first: neither name nor value is mentioned."""
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": ["ORCH_X=secret"]})
    msg = ei.value.errors()[0]["msg"]
    assert msg == "Value error, " + _INVALID_NAME_MSG.format(i=0)
    assert "secret" not in msg


def test_task_env_non_string_item_names_the_index_without_echo() -> None:
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": ["OK", {"API_TOKEN": MARK}]})
    msg = ei.value.errors()[0]["msg"]
    assert msg == "Value error, payload.task_env[1] must be a string"
    assert MARK not in msg


def test_task_env_non_list_keeps_its_message() -> None:
    with pytest.raises(ValidationError) as ei:
        _req({"task_env": MARK})
    msg = ei.value.errors()[0]["msg"]
    assert msg == "Value error, payload.task_env must be a list of strings"
    assert MARK not in msg


def test_task_env_accepts_proxy_and_lowercase_names() -> None:
    names = ["API_TOKEN", "HTTPS_PROXY", "http_proxy", "no_proxy", "_X", "GIT_CONFIG_KEY_0"]
    assert _req({"task_env": names}).payload["task_env"] == names


@pytest.mark.parametrize("entry", TOKEN_PREFIXED_ENTRIES)
def test_task_env_rejects_a_known_token_prefix_naming_the_index(entry: str) -> None:
    """An entry that STARTS with a known token prefix is rejected.

    It is a prefix guard against a common paste (`--env hf_...` instead of the name),
    not a secret detector. The message carries only the index: neither the entry nor
    any part of it, nor the prefix that gave it away.
    """
    expected = _CREDENTIAL_MSG.format(i=1)
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries(["OK", entry], field="payload.task_env")
    assert str(ei.value) == expected
    with pytest.raises(ValidationError) as ei2:
        _req({"task_env": ["OK", entry]})
    errors = ei2.value.errors()
    assert len(errors) == 1
    assert errors[0]["msg"] == "Value error, " + expected
    assert entry not in errors[0]["msg"]


@pytest.mark.parametrize("prefix", TOKEN_PREFIXES)
def test_task_env_token_prefix_error_never_echoes_the_rest_of_the_entry(prefix: str) -> None:
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries([f"{prefix}{MARK}"], field="payload.task_env")
    assert MARK not in str(ei.value)
    assert str(ei.value) == _CREDENTIAL_MSG.format(i=0)


def test_task_env_token_prefix_is_checked_before_the_shape() -> None:
    """`glpat-x` is not a valid name (hyphen) and is a token: the useful message wins."""
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries(["glpat-x"], field="payload.task_env")
    assert str(ei.value) == _CREDENTIAL_MSG.format(i=0)


def test_task_env_name_equals_value_keeps_the_shape_message() -> None:
    """`ORCH_X=secret` and `API_TOKEN=hf_abc` do not start with a token prefix."""
    for entry in ("ORCH_X=secret", "API_TOKEN=hf_abc123"):
        with pytest.raises(ValueError) as ei:
            validate_task_env_entries([entry], field="payload.task_env")
        assert str(ei.value) == _INVALID_NAME_MSG.format(i=0)


def test_task_env_token_prefix_rejection_stops_at_the_first_invalid_entry() -> None:
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries(["A", "ghp_x", "bad one"], field="payload.task_env")
    assert str(ei.value) == _CREDENTIAL_MSG.format(i=1)


@pytest.mark.parametrize(
    "name",
    [
        "HF_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GLPAT_X",
        "SK_X",
        "SLACK_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        # The prefix is anchored at the start and is case-sensitive.
        "HF_x",
        "Hf_x",
        "GHP_x",
        "xhf_x",
        "my_hf_x",
        "ghx_x",
        "github_pat",
        "glpat",
        "glpat_x",
        "sk",
        "sk_x",
    ],
)
def test_task_env_accepts_names_that_only_resemble_a_token_prefix(name: str) -> None:
    assert _req({"task_env": ["OK", name]}).payload["task_env"] == ["OK", name]


@pytest.mark.parametrize("entry", ["xoxa-x", "my-sk-x", "xox-x"])
def test_task_env_entry_outside_the_prefix_list_keeps_the_shape_message(entry: str) -> None:
    """Outside the closed prefix list, the shape decides as usual."""
    with pytest.raises(ValueError) as ei:
        validate_task_env_entries([entry], field="payload.task_env")
    assert str(ei.value) == _INVALID_NAME_MSG.format(i=0)


@pytest.mark.parametrize("token", ["AKIAIOSFODNN7EXAMPLE", "AbCdEf1234567890"])
def test_task_env_credential_without_a_known_prefix_passes_by_design(token: str) -> None:
    """The rule is a PREFIX and SHAPE guard, not a secret detector.

    A string of letters, digits and underscores without a prefix from the closed list
    is a valid name even if it is a token pasted by mistake (an AWS key, an opaque
    token): the server uses no entropy or heuristics beyond the prefix. That is why
    the 422 never repeats any entry. Extending the prefix list is a deliberate change,
    not a case of this test.
    """
    assert is_valid_env_name(token) is True
    assert _req({"task_env": [token]}).payload["task_env"] == [token]


@pytest.mark.parametrize("entry", TOKEN_PREFIXED_ENTRIES)
def test_is_valid_env_name_judges_the_shape_only(entry: str) -> None:
    """`is_valid_env_name` knows no prefixes: secret names reuse it."""
    assert is_valid_env_name(entry) is ("-" not in entry)


# --- Count and length bounds -------------------------------------------------


def test_task_env_accepts_exactly_the_max_entries() -> None:
    from orcaster_server.env_names import TASK_ENV_MAX_ENTRIES

    validate_task_env_entries(
        [f"VAR_{i}" for i in range(TASK_ENV_MAX_ENTRIES)], field="payload.task_env"
    )


def test_task_env_rejects_more_than_max_entries_without_echoing() -> None:
    from orcaster_server.env_names import TASK_ENV_MAX_ENTRIES

    entries = [f"{MARK}_{i}" for i in range(TASK_ENV_MAX_ENTRIES + 1)]
    with pytest.raises(ValueError) as exc:
        validate_task_env_entries(entries, field="payload.task_env")
    assert str(TASK_ENV_MAX_ENTRIES) in str(exc.value)
    assert MARK not in str(exc.value)


def test_task_env_accepts_a_name_of_exactly_the_max_length() -> None:
    from orcaster_server.env_names import TASK_ENV_NAME_MAX_LEN

    validate_task_env_entries(["A" * TASK_ENV_NAME_MAX_LEN], field="payload.task_env")


def test_task_env_rejects_a_name_longer_than_max_without_echoing() -> None:
    from orcaster_server.env_names import TASK_ENV_NAME_MAX_LEN

    entry = MARK + "A" * TASK_ENV_NAME_MAX_LEN
    with pytest.raises(ValueError) as exc:
        validate_task_env_entries(["OK", entry], field="payload.task_env")
    assert "payload.task_env[1]" in str(exc.value)
    assert MARK not in str(exc.value)


def test_create_request_rejects_oversized_task_env_without_echoing() -> None:
    from orcaster_server.env_names import TASK_ENV_MAX_ENTRIES

    with pytest.raises(ValidationError) as exc:
        AdminCreateTaskRequest(
            payload={"task_env": [f"{MARK}{i}" for i in range(TASK_ENV_MAX_ENTRIES + 1)]}
        )
    assert MARK not in str(exc.value.errors(include_input=False))
