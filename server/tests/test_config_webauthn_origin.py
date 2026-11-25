"""Validation and normalization of `WEBAUTHN_ORIGIN` when loading Settings.

Complements the CSRF check by `Origin`. `orcaster_server/auth/origin.py::same_origin`
compares the `Origin` header with `WEBAUTHN_ORIGIN` by exact equality, and a browser
sends the serialized origin: lowercase scheme and host, no default port for the
scheme, no path. A config value with `:443`, with uppercase letters or with a path
matches no real `Origin`, so every cookie-authenticated POST answers 403 and the
passkey login is broken as well.

Two kinds of assertion in this file:

- what can be normalized without guessing (uppercase, default port, trailing
  slash, whitespace) is normalized, and the proof that this works is that
  `same_origin` accepts a browser `Origin` against the normalized value;
- what cannot (path, query, fragment, userinfo, another scheme, missing host,
  invalid port) fails startup with an error that names the variable and the
  value received.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from orcaster_server.auth.origin import same_origin
from orcaster_server.config import Settings, canonical_origin

# (value written by the operator, canonical origin sent by the browser)
NORMALIZED = [
    ("http://localhost:8000", "http://localhost:8000"),
    ("http://localhost:8000/", "http://localhost:8000"),
    ("  https://orcaster.example.com  ", "https://orcaster.example.com"),
    ("https://Orcaster.Example.COM", "https://orcaster.example.com"),
    ("HTTPS://ORCASTER.EXAMPLE.COM/", "https://orcaster.example.com"),
    ("https://orcaster.example.com:443", "https://orcaster.example.com"),
    ("https://orcaster.example.com:443/", "https://orcaster.example.com"),
    ("http://orcaster.example.com:80", "http://orcaster.example.com"),
    # A port that is not the scheme default DOES appear in the `Origin`.
    ("https://orcaster.example.com:8443", "https://orcaster.example.com:8443"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    # IPv6: `hostname` returns it without brackets, and an origin includes them.
    ("http://[::1]:8000", "http://[::1]:8000"),
    ("http://[::1]:80", "http://[::1]"),
    ("HTTP://[::1]", "http://[::1]"),
    # An empty port is the scheme default.
    ("https://orcaster.example.com:", "https://orcaster.example.com"),
]
NORMALIZED_IDS = [value.strip() or "empty" for value, _ in NORMALIZED]

REFUSED = [
    ("", "empty"),
    ("   ", "whitespace"),
    ("/", "just-a-slash"),
    ("https://orcaster.example.com/dashboard", "path"),
    ("https://orcaster.example.com/dashboard/", "path-trailing-slash"),
    ("https://orcaster.example.com?next=/admin/", "query"),
    ("https://orcaster.example.com#top", "fragment"),
    ("https://user:pass@orcaster.example.com", "userinfo"),
    ("https://user@orcaster.example.com", "username"),
    ("orcaster.example.com", "no-scheme"),
    ("orcaster.example.com:8000", "host-port-no-scheme"),
    ("//orcaster.example.com", "scheme-relative"),
    ("ftp://orcaster.example.com", "other-scheme"),
    ("file:///srv/orcaster", "file-scheme"),
    ("https://", "no-host"),
    ("https:", "scheme-only"),
    ("https://orcaster.example.com:abc", "port-not-a-number"),
    ("https://orcaster.example.com:99999", "port-out-of-range"),
    ("https://orcaster.example.com:0", "port-zero"),
]
REFUSED_IDS = [name for _, name in REFUSED]


def _post_from(origin: str) -> Request:
    """A browser POST with the `Origin` header that browser sends."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/workspaces/rename",
            "query_string": b"",
            "headers": [(b"origin", origin.encode())],
        }
    )


def test_webauthn_origin_default() -> None:
    """Without env, the default is still the dev one and is already canonical."""
    assert Settings().webauthn_origin == "http://localhost:8000"


@pytest.mark.parametrize(("value", "expected"), NORMALIZED, ids=NORMALIZED_IDS)
def test_webauthn_origin_normalized_at_load(monkeypatch, value: str, expected: str) -> None:
    monkeypatch.setenv("WEBAUTHN_ORIGIN", value)
    assert Settings().webauthn_origin == expected


@pytest.mark.parametrize(("value", "expected"), NORMALIZED, ids=NORMALIZED_IDS)
def test_browser_origin_matches_the_normalized_setting(
    monkeypatch, value: str, expected: str
) -> None:
    """The reason to normalize: the CSRF check accepts the POST from its own origin.

    This is the path the client takes (`require_session` and
    `AuthOriginCheckMiddleware` call this `same_origin` with
    `settings.webauthn_origin`), not just the shape of the string.
    """
    monkeypatch.setenv("WEBAUTHN_ORIGIN", value)
    settings = Settings()
    assert same_origin(_post_from(expected), settings.webauthn_origin) is True


@pytest.mark.parametrize(("value", "expected"), NORMALIZED, ids=NORMALIZED_IDS)
def test_a_foreign_origin_is_still_refused(monkeypatch, value: str, expected: str) -> None:
    """Control for the test above: `same_origin` does not accept just anything.

    Without this, a `same_origin` that always returned True would pass anyway.
    """
    monkeypatch.setenv("WEBAUTHN_ORIGIN", value)
    settings = Settings()
    assert same_origin(_post_from("https://evil.example"), settings.webauthn_origin) is False


@pytest.mark.parametrize(("value", "name"), REFUSED, ids=REFUSED_IDS)
def test_webauthn_origin_refused_at_load(monkeypatch, value: str, name: str) -> None:
    """A value that could never match an `Origin` fails startup."""
    monkeypatch.setenv("WEBAUTHN_ORIGIN", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "WEBAUTHN_ORIGIN" in message
    # The operator must be able to read which value was rejected and what is expected.
    assert repr(value.strip()) in message
    assert "scheme://host[:port]" in message


def test_canonical_origin_names_the_variable_it_was_given() -> None:
    """The error names the variable the caller passes, not a fixed one."""
    with pytest.raises(ValueError, match="SOME_OTHER_ORIGIN"):
        canonical_origin("https://host/path", env_name="SOME_OTHER_ORIGIN")


def test_canonical_origin_is_idempotent() -> None:
    """Normalizing an already canonical value does not change it."""
    for _, expected in NORMALIZED:
        assert canonical_origin(expected, env_name="WEBAUTHN_ORIGIN") == expected
