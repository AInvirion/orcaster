"""Tests of the pure error-signature functions."""

from orcaster_server.error_signatures import (
    cap_bytes,
    fallback_signature,
    is_valid_signature_hash,
    last_nonempty_line,
    normalize_signature,
    signature_hash,
)


def test_last_nonempty_line_skips_trailing_blanks():
    assert last_nonempty_line("a\nb\n\n  \n") == "b"


def test_last_nonempty_line_none_and_empty():
    assert last_nonempty_line(None) is None
    assert last_nonempty_line("   \n  ") is None


def test_normalize_signature_strips_numbers_and_units():
    line = "CUDA out of memory. Tried to allocate 2.50 GiB"
    assert normalize_signature(line) == "cuda out of memory. tried to allocate <n> gib"


def test_normalize_signature_dedupes_variable_paths_uuids_hex():
    a = normalize_signature(
        "Error loading /models/abc/weights.bin at 0x7f3a id 550e8400-e29b-41d4-a716-446655440000"
    )
    b = normalize_signature(
        "Error loading /models/xyz/other.bin at 0x12cd id 550e8400-e29b-41d4-a716-44665544ffff"
    )
    assert a == b
    assert "<path>" in a and "<hex>" in a and "<uuid>" in a


def test_fallback_signature():
    assert fallback_signature(137) == "no-stderr:exit=137"
    assert fallback_signature(None) == "no-stderr:exit=null"


def test_signature_hash_is_stable_and_short_hex():
    h = signature_hash("runtimeerror: cuda out of memory. tried <n> gib")
    assert h == signature_hash("runtimeerror: cuda out of memory. tried <n> gib")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_signature_hash_differs_for_distinct_signatures():
    assert signature_hash("boom: real failure") != signature_hash("no-stderr:exit=null")


def test_is_valid_signature_hash_accepts_real_digests_rejects_malformed():
    assert is_valid_signature_hash(signature_hash("boom: real failure"))
    assert is_valid_signature_hash("0123456789abcdef")
    # rejects: uppercase, length != 16, non-hex, empty
    assert not is_valid_signature_hash("0123456789ABCDEF")
    assert not is_valid_signature_hash("0" * 15)
    assert not is_valid_signature_hash("0" * 17)
    assert not is_valid_signature_hash("not-a-hash")
    assert not is_valid_signature_hash("")


def test_cap_bytes_keeps_short_text_intact():
    text = "l1\nl2\nl3"
    assert cap_bytes(text, max_bytes=4096) == text


def test_cap_bytes_none_and_empty():
    assert cap_bytes(None, max_bytes=10) == ""
    assert cap_bytes("", max_bytes=10) == ""


def test_cap_bytes_respects_max_bytes():
    text = "\n".join(f"line{i}" for i in range(100))
    out = cap_bytes(text, max_bytes=20)
    assert len(out.encode("utf-8")) <= 20
    assert out  # not empty
