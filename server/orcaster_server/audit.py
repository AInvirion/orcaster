"""Audit log of dashboard admin actions.

Every destructive action executed from `/admin/*` must call
`record_admin_action()` *before* the handler's `session.commit()`, so that the
action and its effect persist atomically.

`actor` is passed by the caller: this module does not derive it from any session on
its own. Several existing calls in admin_tasks.py (cancel_task, delete_task,
retry_task, fork_task, bulk_cancel_task, bulk_delete_task, bulk_retry_task) do not
pass it and are stored with a NULL `actor`; retrofitting them to pass it is the
technical debt TD-004. The worker actions (delete_worker, clear_worker_cache,
pause_worker, resume_worker) and the share-token actions (mint_share_token,
revoke_share_token) do persist it. The reason (`reason`) is persisted when the
endpoint exposes it (cancel_task, pause_worker, resume_worker) and is NULL when it
does not apply.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SQL_INSERT_ADMIN_ACTION = text(
    """
    INSERT INTO admin_actions
        (workspace_id, action, target_kind, target_id, reason, actor, payload)
    VALUES
        (:workspace_id, :action, :target_kind, :target_id, :reason, :actor,
         CAST(:payload AS jsonb))
    """
)


async def record_admin_action(
    session: AsyncSession,
    *,
    workspace_id: str,
    action: str,
    target_kind: str,
    target_id: str,
    reason: str | None = None,
    actor: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert an audit row. It does not commit (the caller does).

    `workspace_id` is required: it scopes the audit row to its tenant (migration 0023,
    which closes the cross-tenant leak in `/admin/actions`). The admin handlers pass
    it from `ctx.workspace_id`; the worker API auto-retry route takes it from the
    workspace_id of the task being retried.

    `action` and `target_kind` must match the table's CHECK constraints (migration
    0002_admin_actions); any value outside the enum raises `IntegrityError` and aborts
    the caller's transaction. This is the intended fail-fast behaviour for incorrect
    code.
    """
    await session.execute(
        _SQL_INSERT_ADMIN_ACTION,
        {
            "workspace_id": workspace_id,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "reason": reason,
            "actor": actor,
            "payload": json.dumps(payload) if payload is not None else None,
        },
    )
