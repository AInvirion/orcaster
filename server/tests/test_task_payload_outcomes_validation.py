"""Shape validation of the payload.outcomes block in create/fork."""

import pytest
from pydantic import ValidationError

from orcaster_server.schemas.api_v1 import ApiCreateTaskRequest
from orcaster_server.schemas.tasks import AdminCreateTaskRequest

_OK = {"outcomes": {"exit_codes": {"3": {"name": "no_result", "closes_as": "completed"}}}}


def test_create_accepts_valid_block() -> None:
    req = AdminCreateTaskRequest(payload={"commands": ["x"], **_OK})
    assert req.payload["outcomes"]["exit_codes"]["3"]["name"] == "no_result"


def test_create_without_block_is_still_ok() -> None:
    AdminCreateTaskRequest(payload={"commands": ["x"]})


@pytest.mark.parametrize(
    "outcomes",
    [
        {"exit_codes": {"0": {"name": "x", "closes_as": "completed"}}},
        {"exit_codes": {"3": {"name": "queued", "closes_as": "failed"}}},
        {"exit_codes": {"3": {"name": "x", "closes_as": "cancelled"}}},
        "no-un-dict",
    ],
)
def test_create_rejects_invalid_block(outcomes) -> None:
    with pytest.raises(ValidationError, match="outcomes"):
        AdminCreateTaskRequest(payload={"outcomes": outcomes})


def test_api_v1_inherits_the_validation() -> None:
    # ApiCreateTaskRequest delega en AdminCreateTaskRequest via to_admin_request;
    # payload validation runs when the admin request is built.
    payload = {"outcomes": {"exit_codes": {"0": {"name": "x", "closes_as": "completed"}}}}
    api = ApiCreateTaskRequest(payload=payload)
    with pytest.raises(ValidationError, match="outcomes"):
        api.to_admin_request()
