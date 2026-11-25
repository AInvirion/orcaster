"""Unit tests for `idempotency_key` validation with the canonical UUID format.

Verifies that the `AdminCreateTaskRequest`, `AdminControlOpRequest`,
`ApiCreateTaskRequest` and `ApiForkTaskRequest` schemas accept only the
canonical 8-4-4-4-12 hyphenated format, closing the gap between the
server and the Go client validator (`isUUID` in `orcaster/cmd/task.go`).

Pure-Pydantic tests: they need no DB or async fixtures.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orcaster_server.schemas.api_v1 import ApiCreateTaskRequest, ApiForkTaskRequest
from orcaster_server.schemas.tasks import AdminControlOpRequest, AdminCreateTaskRequest

# Valid canonical UUID
_VALID_LOWER = "550e8400-e29b-41d4-a716-446655440000"
_VALID_UPPER = "550E8400-E29B-41D4-A716-446655440000"
# Non-canonical forms that plain Pydantic would accept but the Go client rejects
_NO_HYPHENS = "550e8400e29b41d4a716446655440000"
_BRACES = "{550e8400-e29b-41d4-a716-446655440000}"
_URN = "urn:uuid:550e8400-e29b-41d4-a716-446655440000"
_GARBAGE = "not-a-uuid"


# ---------------------------------------------------------------------------
# AdminCreateTaskRequest
# ---------------------------------------------------------------------------


class TestAdminCreateTaskRequestIdempotencyKey:
    def test_valid_lowercase_uuid(self) -> None:
        req = AdminCreateTaskRequest(idempotency_key=_VALID_LOWER)
        assert req.idempotency_key is not None

    def test_valid_uppercase_uuid(self) -> None:
        req = AdminCreateTaskRequest(idempotency_key=_VALID_UPPER)
        assert req.idempotency_key is not None

    def test_none_is_valid(self) -> None:
        req = AdminCreateTaskRequest()
        assert req.idempotency_key is None

    def test_absent_is_valid(self) -> None:
        req = AdminCreateTaskRequest(payload={"x": 1})
        assert req.idempotency_key is None

    def test_rejects_no_hyphens(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminCreateTaskRequest(idempotency_key=_NO_HYPHENS)

    def test_rejects_braces(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminCreateTaskRequest(idempotency_key=_BRACES)

    def test_rejects_urn_prefix(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminCreateTaskRequest(idempotency_key=_URN)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminCreateTaskRequest(idempotency_key=_GARBAGE)


# ---------------------------------------------------------------------------
# AdminControlOpRequest
# ---------------------------------------------------------------------------


class TestAdminControlOpRequestIdempotencyKey:
    def _base(self, **kwargs: object) -> dict:
        return {"control_op": "__reload_config__", **kwargs}

    def test_valid_lowercase_uuid(self) -> None:
        req = AdminControlOpRequest(**self._base(idempotency_key=_VALID_LOWER))
        assert req.idempotency_key is not None

    def test_valid_uppercase_uuid(self) -> None:
        req = AdminControlOpRequest(**self._base(idempotency_key=_VALID_UPPER))
        assert req.idempotency_key is not None

    def test_none_is_valid(self) -> None:
        req = AdminControlOpRequest(**self._base())
        assert req.idempotency_key is None

    def test_rejects_no_hyphens(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminControlOpRequest(**self._base(idempotency_key=_NO_HYPHENS))

    def test_rejects_braces(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminControlOpRequest(**self._base(idempotency_key=_BRACES))

    def test_rejects_urn_prefix(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminControlOpRequest(**self._base(idempotency_key=_URN))

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            AdminControlOpRequest(**self._base(idempotency_key=_GARBAGE))


# ---------------------------------------------------------------------------
# ApiCreateTaskRequest
# ---------------------------------------------------------------------------


class TestApiCreateTaskRequestIdempotencyKey:
    def test_valid_lowercase_uuid(self) -> None:
        req = ApiCreateTaskRequest(idempotency_key=_VALID_LOWER)
        assert req.idempotency_key is not None

    def test_valid_uppercase_uuid(self) -> None:
        req = ApiCreateTaskRequest(idempotency_key=_VALID_UPPER)
        assert req.idempotency_key is not None

    def test_none_is_valid(self) -> None:
        req = ApiCreateTaskRequest()
        assert req.idempotency_key is None

    def test_rejects_no_hyphens(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiCreateTaskRequest(idempotency_key=_NO_HYPHENS)

    def test_rejects_braces(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiCreateTaskRequest(idempotency_key=_BRACES)

    def test_rejects_urn_prefix(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiCreateTaskRequest(idempotency_key=_URN)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiCreateTaskRequest(idempotency_key=_GARBAGE)


# ---------------------------------------------------------------------------
# ApiForkTaskRequest
# ---------------------------------------------------------------------------


class TestApiForkTaskRequestIdempotencyKey:
    def test_valid_lowercase_uuid(self) -> None:
        req = ApiForkTaskRequest(idempotency_key=_VALID_LOWER)
        assert req.idempotency_key is not None

    def test_valid_uppercase_uuid(self) -> None:
        req = ApiForkTaskRequest(idempotency_key=_VALID_UPPER)
        assert req.idempotency_key is not None

    def test_none_is_valid(self) -> None:
        req = ApiForkTaskRequest()
        assert req.idempotency_key is None

    def test_rejects_no_hyphens(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiForkTaskRequest(idempotency_key=_NO_HYPHENS)

    def test_rejects_braces(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiForkTaskRequest(idempotency_key=_BRACES)

    def test_rejects_urn_prefix(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiForkTaskRequest(idempotency_key=_URN)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError, match="canonical UUID"):
            ApiForkTaskRequest(idempotency_key=_GARBAGE)


# ---------------------------------------------------------------------------
# Internal step ApiCreateTaskRequest.to_admin_request
# ---------------------------------------------------------------------------


class TestToAdminRequestPreservesIdempotencyKey:
    """The `BeforeValidator` only validates strings: an already parsed `UUID` (the one
    `to_admin_request` passes when building the internal `AdminCreateTaskRequest`)
    must pass without reprocessing or error. Guards the `isinstance(v, str)` of
    `_require_canonical_uuid` against a future regression.
    """

    def test_to_admin_request_keeps_uuid(self) -> None:
        req = ApiCreateTaskRequest(idempotency_key=_VALID_LOWER)
        admin = req.to_admin_request()
        assert admin.idempotency_key is not None
        assert admin.idempotency_key == req.idempotency_key

    def test_to_admin_request_none_key(self) -> None:
        req = ApiCreateTaskRequest()
        admin = req.to_admin_request()
        assert admin.idempotency_key is None
