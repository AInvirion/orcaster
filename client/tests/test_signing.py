"""Tests for signing.py -- Ed25519 verification + canonical_json.

Coverage:
    * `canonical_json` is deterministic and reproduces lexicographic order.
    * `verify_signature` accepts a valid signature and rejects a tampered one.
    * `load_public_key` caches by path.
    * Cross-side: signs with `cryptography.Ed25519PrivateKey`, verifies with
      the client's `verify_signature` -- guarantee of byte-exact compatibility.
    * Optional cross-side: imports `server/orcaster_server/signing.py` and checks that
      `canonical_json(client)` produces the same bytestring as the server.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.signing import (
    build_command_message,
    canonical_json,
    load_public_key,
    reset_key_cache,
    verify_signature,
)


def test_canonical_json_is_deterministic_and_sorted():
    """sort_keys + separators without spaces always produce the same bytestring."""
    a = canonical_json({"b": 1, "a": 2, "c": [3, 2, 1]})
    b = canonical_json({"c": [3, 2, 1], "a": 2, "b": 1})
    assert a == b
    # Expected order: a, b, c and no spaces.
    assert a == b'{"a":2,"b":1,"c":[3,2,1]}'


def test_canonical_json_preserves_utf8_literal():
    """ensure_ascii=False avoids \\u escapes; UTF-8 goes literal."""
    out = canonical_json({"k": "ñ — 漢字"})
    assert "ñ".encode() in out
    assert "漢字".encode() in out


def test_canonical_json_handles_none_payload():
    """payload=None is serialized as a literal null, not omitted."""
    out = canonical_json({"payload": None, "uuid": "x"})
    assert out == b'{"payload":null,"uuid":"x"}'


def _gen_keypair(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path = tmp_path / "ed25519.pub.pem"
    pub_path.write_bytes(pub_pem)
    return priv, pub_path


def test_verify_signature_accepts_valid_and_rejects_tampered(tmp_path):
    """We sign with cryptography, we verify with the client's helper."""
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    pub = load_public_key(pub_path)

    msg = build_command_message(
        uuid="11111111-1111-1111-1111-111111111111",
        worker_id="22222222-2222-2222-2222-222222222222",
        command="install_dep",
        payload={"name": "ripgrep"},
        expires_at_iso="2030-01-01T00:00:00+00:00",
    )
    sig = priv.sign(msg)
    assert verify_signature(pub, msg, sig) is True

    tampered = bytearray(msg)
    tampered[5] ^= 0x01
    assert verify_signature(pub, bytes(tampered), sig) is False

    tampered_sig = bytearray(sig)
    tampered_sig[0] ^= 0x01
    assert verify_signature(pub, msg, bytes(tampered_sig)) is False


def test_load_public_key_caches_by_path(tmp_path):
    """A second load with the same path returns the cached instance."""
    _, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    k1 = load_public_key(pub_path)
    k2 = load_public_key(pub_path)
    assert k1 is k2


def test_load_public_key_rejects_non_ed25519(tmp_path):
    """If the PEM is not Ed25519 we raise a clear ValueError."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    p = tmp_path / "rsa.pub.pem"
    p.write_bytes(pub_pem)
    reset_key_cache()
    with pytest.raises(ValueError):
        load_public_key(p)


def _load_server_signing_module() -> types.ModuleType | None:
    """Loads `server/orcaster_server/signing.py` isolated from the client's `app` package.

    The server imports `from orcaster_server.config import settings`; in this test we only
    need `canonical_json` and `build_command_message`, so
    we inject a stub of `orcaster_server.config.settings` ONLY if it does not exist -- so as
    not to clobber the client's real module, we do it in an alternate
    sys.modules entry and then restore.
    """
    server_root = Path(__file__).resolve().parents[2] / "server"
    target = server_root / "orcaster_server" / "signing.py"
    if not target.exists():
        return None

    # Minimal stub for `orcaster_server.config.settings` that the server consults
    # on import (not in canonical_json's runtime).
    stub_pkg = types.ModuleType("orcaster_server")
    stub_pkg.__path__ = []
    stub_cfg = types.ModuleType("orcaster_server.config")
    stub_cfg.settings = types.SimpleNamespace(ed25519_private_key_path=None)
    stubs = {"orcaster_server": stub_pkg, "orcaster_server.config": stub_cfg}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("server_signing_xside", target)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, prev in saved.items():
            if prev is not None:
                sys.modules[name] = prev
            else:
                sys.modules.pop(name, None)


def test_canonical_json_matches_server_implementation():
    """Cross-side: client and server produce the same bytestring."""
    server_mod = _load_server_signing_module()
    if server_mod is None:
        pytest.skip("server/ not present in this checkout")

    sample = {
        "uuid": "u",
        "worker_id": "w",
        "command": "install_dep",
        "payload": {"x": 1, "a": [2, 3]},
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    server_str = server_mod.canonical_json(sample)
    assert isinstance(server_str, str)
    assert server_str.encode("utf-8") == canonical_json(sample)

    # build_command_message must also match.
    args = dict(
        uuid="u",
        worker_id="w",
        command="install_dep",
        payload={"x": 1, "a": [2, 3]},
        expires_at_iso="2030-01-01T00:00:00+00:00",
    )
    server_msg = server_mod.build_command_message(**args)
    client_msg = build_command_message(**args)
    assert client_msg == server_msg

    # E2E: sign with cryptography over the server's bytestring,
    # verify with the client's helper.
    priv = Ed25519PrivateKey.generate()
    sig = priv.sign(server_msg)
    assert verify_signature(priv.public_key(), client_msg, sig) is True


def test_canonical_json_roundtrip_with_json_load():
    """Sanity: the canonical bytestring is parseable as regular JSON."""
    obj = {"b": 2, "a": [1, {"nested": True}]}
    out = canonical_json(obj)
    parsed = json.loads(out.decode("utf-8"))
    assert parsed == obj
