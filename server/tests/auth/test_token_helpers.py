from orcaster_server.auth.config import generate_token, hash_token, verify_token


def test_generate_token_is_urlsafe_and_long():
    tok = generate_token()
    assert isinstance(tok, str)
    assert len(tok) >= 43  # 32 bytes urlsafe-b64 without padding


def test_hash_is_deterministic_hex():
    tok = "deadbeef"
    assert hash_token(tok) == hash_token(tok)
    assert len(hash_token(tok)) == 64  # sha256 hex


def test_verify_token_matches_and_rejects():
    tok = generate_token()
    h = hash_token(tok)
    assert verify_token(tok, h) is True
    assert verify_token(tok + "x", h) is False
