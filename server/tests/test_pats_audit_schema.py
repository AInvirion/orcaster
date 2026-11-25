"""Migration 0024 extends the CHECK constraints of `admin_actions` to audit PATs and
enrollment tokens. These tests verify that the new
`action`/`target_kind` values are accepted by the CHECK and that an invented value is still
rejected.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from orcaster_server.audit import record_admin_action
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

pytestmark = pytest.mark.asyncio

_NEW_ACTIONS = [
    ("mint_pat", "pat"),
    ("revoke_pat", "pat"),
    ("delete_pat", "pat"),
    ("mint_enrollment_token", "enrollment_token"),
    ("revoke_enrollment_token", "enrollment_token"),
    ("delete_enrollment_token", "enrollment_token"),
]

# Actions/target_kinds that already existed BEFORE 0024 (state after 0019). The
# DROP+recreate of the CHECK in 0024 must NOT lose any (regression: the first
# version of 0024 started from the 0013 state and dropped auto_retry_task/fork_task).
_PREEXISTING_ACTIONS = [
    ("delete_worker", "worker"),
    ("cancel_task", "task"),
    ("delete_task", "task"),
    ("bulk_cancel_task", "task"),
    ("bulk_delete_task", "task"),
    ("retry_task", "task"),
    ("auto_retry_task", "task"),
    ("fork_task", "task"),
]


@pytest.mark.parametrize(("action", "target_kind"), _PREEXISTING_ACTIONS)
async def test_admin_actions_preserves_preexisting_values(db_session, action, target_kind):
    """After 0024, the previous `action` values (incl. auto_retry_task/fork_task) are still OK."""
    await record_admin_action(
        db_session,
        workspace_id=DEFAULT_WORKSPACE_ID,
        action=action,
        target_kind=target_kind,
        target_id="00000000-0000-0000-0000-000000000abc",
    )
    await db_session.commit()
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM admin_actions WHERE action = :a"), {"a": action}
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(("action", "target_kind"), _NEW_ACTIONS)
async def test_admin_actions_accepts_new_credential_audit_values(db_session, action, target_kind):
    await record_admin_action(
        db_session,
        workspace_id=DEFAULT_WORKSPACE_ID,
        action=action,
        target_kind=target_kind,
        target_id="00000000-0000-0000-0000-000000000abc",
    )
    await db_session.commit()
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM admin_actions WHERE action = :a"), {"a": action}
        )
    ).scalar_one()
    assert count == 1


async def test_admin_actions_still_rejects_unknown_action(db_session):
    with pytest.raises(IntegrityError):
        await record_admin_action(
            db_session,
            workspace_id=DEFAULT_WORKSPACE_ID,
            action="totally_made_up_action",
            target_kind="pat",
            target_id="x",
        )
        await db_session.commit()
