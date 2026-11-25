"""Pinned vectors for the task-secrets HKDF derivations.

The HKDF labels in `task_secrets/keyring.py` are frozen wire constants: they
derive keys for data already stored (secret fingerprints, share-link token
ciphertexts, key ids). If a label changes, these vectors fail. The expected
values were computed independently of the keyring module.
"""

from __future__ import annotations

import base64

from orcaster_server.task_secrets.keyring import TaskSecretsKeyring, key_id

# Fixed, non-secret test key: bytes 0..31.
_KEY = base64.urlsafe_b64encode(bytes(range(32)))

# Share-link ciphertext produced with the frozen label, a fixed timestamp
# and a zero IV.
_SHARE_CIPHERTEXT = (
    b"gAAAAABlU_EAAAAAAAAAAAAAAAAAAAAAAI4J348xlrzAqc0aXEu8dIf3TLYN5M_PbxSz_rOG"
    b"CVgbvfTcPBKJkKiLmiQD3lUecMj9cbB5mmBn0Je9oZLL-sM="
)


def test_key_id_vector_is_stable():
    assert key_id(_KEY) == "d058a4f5609c"


def test_fingerprint_vector_is_stable():
    ring = TaskSecretsKeyring([_KEY])
    assert ring.fingerprint("s3cret-value") == "hmac-sha256:84f0a93cead0"


def test_stored_share_link_ciphertext_still_decrypts():
    ring = TaskSecretsKeyring([_KEY])
    assert ring.share_links.decrypt(_SHARE_CIPHERTEXT) == b"share-token-0123"
