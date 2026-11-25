"""Software WebAuthn authenticator for the passkey tests.

Replaces the ``soft-webauthn`` package: that one requires ``fido2<2.0`` and
``fido2`` 1.x caps ``cryptography<45``, which pinned the lock to versions with
security advisories. It keeps the same interface (``create`` / ``get``) and
depends only on ``cryptography`` and ``cbor2``.

It holds a single ES256 (P-256) credential with ``none`` attestation.
"""

import hashlib
import json
import os
from base64 import urlsafe_b64encode
from struct import pack

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

_ES256 = -7
_FLAG_USER_PRESENT = 0x01
_FLAG_ATTESTED_DATA = 0x40


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _client_data_json(client_data: dict) -> bytes:
    # Compact serialization, like the browser's (WebAuthn §5.8.1.1).
    return json.dumps(client_data, separators=(",", ":")).encode("utf-8")


class SoftWebauthnDevice:
    """Simulate the browser WebAuthn API with an attached authenticator."""

    def __init__(self) -> None:
        self.credential_id: bytes | None = None
        self.private_key: ec.EllipticCurvePrivateKey | None = None
        self.aaguid = b"\x00" * 16
        self.rp_id: str | None = None
        self.user_handle: bytes | None = None
        self.sign_count = 0

    def _cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        # Clave COSE EC2 (RFC 9053): kty=EC2, alg=ES256, crv=P-256, x, y.
        return cbor2.dumps(
            {
                1: 2,
                3: _ES256,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            },
            canonical=True,
        )

    def create(self, options: dict, origin: str) -> dict:
        """Create a credential and return the PublicKeyCredential (attestation)."""
        public_key = options["publicKey"]
        if {"alg": _ES256, "type": "public-key"} not in public_key["pubKeyCredParams"]:
            raise ValueError("pubKeyCredParams does not include ES256")
        if public_key.get("attestation") not in (None, "none"):
            raise ValueError("only 'none' attestation is supported")

        self.credential_id = os.urandom(32)
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.rp_id = public_key["rp"]["id"]
        self.user_handle = public_key["user"]["id"]

        client_data = {
            "type": "webauthn.create",
            "challenge": _b64url(public_key["challenge"]),
            "origin": origin,
            "crossOrigin": False,
        }
        auth_data = (
            _sha256(self.rp_id.encode("ascii"))
            + bytes([_FLAG_USER_PRESENT | _FLAG_ATTESTED_DATA])
            + pack(">I", self.sign_count)
            + self.aaguid
            + pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_public_key()
        )
        attestation_object = {"fmt": "none", "attStmt": {}, "authData": auth_data}

        return {
            "id": urlsafe_b64encode(self.credential_id),
            "rawId": self.credential_id,
            "response": {
                "clientDataJSON": _client_data_json(client_data),
                "attestationObject": cbor2.dumps(attestation_object, canonical=True),
            },
            "type": "public-key",
        }

    def get(self, options: dict, origin: str) -> dict:
        """Sign a challenge and return the PublicKeyCredential (assertion)."""
        public_key = options["publicKey"]
        if self.rp_id != public_key["rpId"]:
            raise ValueError("requested rpId does not match the stored credential")

        self.sign_count += 1
        client_data = _client_data_json(
            {
                "type": "webauthn.get",
                "challenge": _b64url(public_key["challenge"]),
                "origin": origin,
                "crossOrigin": False,
            }
        )
        authenticator_data = (
            _sha256(self.rp_id.encode("ascii"))
            + bytes([_FLAG_USER_PRESENT])
            + pack(">I", self.sign_count)
        )
        signature = self.private_key.sign(
            authenticator_data + _sha256(client_data), ec.ECDSA(hashes.SHA256())
        )

        return {
            "id": urlsafe_b64encode(self.credential_id),
            "rawId": self.credential_id,
            "response": {
                "authenticatorData": authenticator_data,
                "clientDataJSON": client_data,
                "signature": signature,
                "userHandle": self.user_handle,
            },
            "type": "public-key",
        }
