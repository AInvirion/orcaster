"""Signed egress allowlist.

The task runs with the worker's uid and can write the allowlist file.
These tests prove that a task-side write does not widen the egress
the keyed proxy applies, and that the keyless proxy (and the pre-signature
format) keep working the same.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from orcaster_worker import egress_proxy
from orcaster_worker.config import Settings
from orcaster_worker.egress import (
    EGRESS_KEY_ENV,
    MAC_HEADER_PREFIX,
    AllowlistRefused,
    parse_egress_key,
    read_allowlist,
    read_signed_allowlist,
    write_allowlist,
)
from orcaster_worker.egress_proxy import ProxyPolicy
from orcaster_worker.main import (
    _apply_task_egress,
    _clear_task_egress,
    _take_egress_key_from_environ,
)

KEY_HEX = "11" * 32
KEY = bytes.fromhex(KEY_HEX)
OTHER_KEY = bytes.fromhex("22" * 32)


def _policy(f: Path, key: bytes | None = KEY) -> ProxyPolicy:
    return ProxyPolicy(base=["pypi.org"], allow_file=f, key=key)


# --- format and verification -------------------------------------------------


def test_signed_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    write_allowlist(f, ["data.example.org", ".example.net"], KEY)
    assert f.read_text().startswith(MAC_HEADER_PREFIX)
    assert read_signed_allowlist(f, KEY) == ["data.example.org", ".example.net"]
    # Reading without verification (worker, keyless proxy) ignores the header.
    assert read_allowlist(f) == ["data.example.org", ".example.net"]


def test_signed_empty_list_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    write_allowlist(f, [], KEY)
    assert read_signed_allowlist(f, KEY) == []


def test_missing_or_empty_unsigned_file_is_not_a_refusal(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    assert read_signed_allowlist(f, KEY) == []
    f.write_text("")
    assert read_signed_allowlist(f, KEY) == []


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda f: f.write_text("evil.example\n"), id="unsigned-rewrite"),
        pytest.param(
            lambda f: f.write_text(f.read_text() + "evil.example\n"), id="append-to-signed"
        ),
        pytest.param(
            lambda f: write_allowlist(f, ["data.example.org", "evil.example"], OTHER_KEY),
            id="signed-with-another-key",
        ),
        pytest.param(
            lambda f: f.write_text(f"{MAC_HEADER_PREFIX}{'0' * 64}\nevil.example\n"),
            id="forged-header",
        ),
        pytest.param(
            lambda f: f.write_text(f"{MAC_HEADER_PREFIX}zzé\nevil.example\n"),
            id="non-ascii-header",
        ),
        pytest.param(lambda f: f.write_bytes(b"\xff\xfeevil.example\n"), id="binary-garbage"),
    ],
)
def test_task_side_write_cannot_widen_the_allowlist(tmp_path: Path, tamper, caplog) -> None:
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org"], KEY)
    pol = _policy(f)
    assert pol.allows("data.example.org", 443)
    assert not pol.allows("evil.example", 443)

    tamper(f)

    with caplog.at_level(logging.WARNING, logger=egress_proxy.log.name):
        assert not pol.allows("evil.example", 443)
        assert not pol.allows("evil.example", 80)
    # A tampered file is discarded entirely: the task only removes egress from
    # itself. The base allowlist stays in place.
    assert not pol.allows("data.example.org", 443)
    assert pol.allows("pypi.org", 443)
    assert "egress allowlist refused" in caplog.text
    assert "evil.example" not in caplog.text  # the reason, never the content


def test_write_from_a_separate_process_cannot_widen(tmp_path: Path) -> None:
    """The write is done by another process of the same uid, like a real task."""
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org"], KEY)
    pol = _policy(f)
    code = (
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text(p.read_text() + 'evil.example\\n')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != EGRESS_KEY_ENV}
    subprocess.run([sys.executable, "-c", code, str(f)], check=True, env=env)
    assert "evil.example" in f.read_text()
    assert not pol.allows("evil.example", 443)


def test_refusal_is_logged_once_per_file_version(tmp_path: Path, caplog) -> None:
    f = tmp_path / "allow.txt"
    f.write_text("evil.example\n")
    pol = _policy(f)
    with caplog.at_level(logging.WARNING, logger=egress_proxy.log.name):
        for _ in range(5):
            assert not pol.allows("evil.example", 443)
    assert caplog.text.count("egress allowlist refused") == 1


def test_worker_rewrite_after_tamper_restores_task_egress(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    pol = _policy(f)
    f.write_text("evil.example\n")
    assert not pol.allows("evil.example", 443)
    _apply_task_egress(f, ["data.example.org"], KEY)  # next task
    assert pol.allows("data.example.org", 443)
    _clear_task_egress(f, ["data.example.org"], KEY)
    assert not pol.allows("data.example.org", 443)
    assert read_signed_allowlist(f, KEY) == []


def test_unsigned_with_entries_raises(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    f.write_text("evil.example\n")
    with pytest.raises(AllowlistRefused, match="no signature"):
        read_signed_allowlist(f, KEY)


# --- backward compatibility ---------------------------------------------------


def test_proxy_without_key_keeps_unsigned_behaviour(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    pol = _policy(f, key=None)
    f.write_text("data.example.org\n")
    assert pol.allows("data.example.org", 443)


def test_proxy_without_key_reads_signed_file(tmp_path: Path) -> None:
    """New worker + keyless proxy: the header does not break reading."""
    f = tmp_path / "allow.txt"
    write_allowlist(f, ["data.example.org"], KEY)
    pol = _policy(f, key=None)
    assert pol.allows("data.example.org", 443)
    assert not pol.allows("evil.example", 443)


def test_pre_816_reader_treats_header_as_non_matching_entry(tmp_path: Path) -> None:
    """A pre-signature proxy reads the header as one more entry; it does not match
    any valid host (the character guard rejects `#`)."""
    f = tmp_path / "allow.txt"
    write_allowlist(f, ["data.example.org"], KEY)
    legacy_entries = [ln.strip() for ln in f.read_text().splitlines() if ln.strip()]
    header = legacy_entries[0]
    assert header.startswith("#")
    assert not egress_proxy._HOST_RE.match(header.split()[0])


def test_worker_without_key_writes_legacy_format(tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org"])
    assert f.read_text() == "data.example.org\n"


# --- key: parsing, Settings, environment ---------------------------------------


@pytest.mark.parametrize("raw", ["not-hex", "11" * 16, "abc"])
def test_parse_egress_key_rejects_without_echoing_value(raw: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_egress_key(raw)
    assert raw not in str(exc.value)


def test_parse_egress_key_accepts_hex_with_whitespace() -> None:
    assert parse_egress_key(f" {KEY_HEX}\n") == KEY


def _settings_kwargs(tmp_path: Path) -> dict:
    return {
        "server_url": "http://server.test",
        "state_dir": tmp_path / "state",
        "cache_root": tmp_path / "cache",
    }


def test_settings_reads_key_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(EGRESS_KEY_ENV, KEY_HEX)
    s = Settings(**_settings_kwargs(tmp_path))
    assert s.egress_key_bytes == KEY
    assert KEY_HEX not in repr(s)


def test_settings_without_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(EGRESS_KEY_ENV, raising=False)
    assert Settings(**_settings_kwargs(tmp_path)).egress_key_bytes is None


def test_settings_rejects_malformed_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(EGRESS_KEY_ENV, "deadbeef")
    with pytest.raises(ValidationError) as exc:
        Settings(**_settings_kwargs(tmp_path))
    assert "deadbeef" not in str(exc.value.errors(include_input=False))


# A non-hex value and a short hex one (16 bytes): the two malformed-key formats
# that `parse_egress_key` rejects. Values are chosen without substrings
# that the error message could contain by chance.
_BAD_KEYS = ["zz-not-hex-0123456789secret", "00112233445566778899aabbccddeeff"]


@pytest.mark.parametrize("raw", _BAD_KEYS)
def test_settings_malformed_key_error_hides_value_from_env(
    monkeypatch, tmp_path: Path, raw: str
) -> None:
    """The ``ValidationError`` text names the variable and does not carry the key.

    Without `hide_input_in_errors`, pydantic adds ``input_value='<key>'`` to the
    message, and an uncaught traceback leaves it in the container log.
    """
    monkeypatch.setenv(EGRESS_KEY_ENV, raw)
    with pytest.raises(ValidationError) as exc:
        Settings(**_settings_kwargs(tmp_path))
    text = str(exc.value)
    assert EGRESS_KEY_ENV in text
    assert raw not in text
    assert "input_value" not in text


@pytest.mark.parametrize("raw", _BAD_KEYS)
def test_settings_malformed_key_error_hides_value_from_env_file(
    monkeypatch, tmp_path: Path, raw: str
) -> None:
    """Same contract via the ``--config-path`` route (`cli_entry` does not catch it)."""
    monkeypatch.delenv(EGRESS_KEY_ENV, raising=False)
    env_file = tmp_path / "worker.env"
    env_file.write_text(f"{EGRESS_KEY_ENV}={raw}\n")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env_file, **_settings_kwargs(tmp_path))  # type: ignore[call-arg]
    text = str(exc.value)
    assert EGRESS_KEY_ENV in text
    assert raw not in text


def test_worker_removes_key_from_environ(monkeypatch, tmp_path: Path, caplog) -> None:
    """Without the pop, a legacy task (``dict(os.environ)``) would inherit the key."""
    monkeypatch.setenv(EGRESS_KEY_ENV, KEY_HEX)
    s = Settings(**_settings_kwargs(tmp_path))
    with caplog.at_level(logging.INFO):
        _take_egress_key_from_environ(s)
    assert EGRESS_KEY_ENV not in os.environ
    assert s.egress_key_bytes == KEY
    assert "signed" in caplog.text
    assert KEY_HEX not in caplog.text


def test_worker_warns_when_behind_proxy_without_key(monkeypatch, tmp_path: Path, caplog) -> None:
    monkeypatch.delenv(EGRESS_KEY_ENV, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://orcaster-proxy:3128")
    s = Settings(**_settings_kwargs(tmp_path))
    with caplog.at_level(logging.WARNING):
        _take_egress_key_from_environ(s)
    assert f"{EGRESS_KEY_ENV} not set" in caplog.text


def test_proxy_load_key(monkeypatch, caplog) -> None:
    monkeypatch.setenv(EGRESS_KEY_ENV, KEY_HEX)
    assert egress_proxy._load_key() == KEY
    monkeypatch.delenv(EGRESS_KEY_ENV)
    with caplog.at_level(logging.WARNING, logger=egress_proxy.log.name):
        assert egress_proxy._load_key() is None
    assert "accepted unsigned" in caplog.text


def test_proxy_refuses_to_start_with_malformed_key(monkeypatch, caplog) -> None:
    monkeypatch.setenv(EGRESS_KEY_ENV, "deadbeef")
    with caplog.at_level(logging.ERROR, logger=egress_proxy.log.name), pytest.raises(SystemExit):
        egress_proxy._load_key()
    assert "deadbeef" not in caplog.text
