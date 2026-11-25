"""tasks: signature NOT NULL (universal signature)

Revision ID: 0011_tasks_signature_not_null
Revises: 0010_target_worker_pool
Create Date: 2026-05-19

Previously, `tasks.signature` was NULLABLE because only `signed=true` tasks
(admin push commands) carried a signature; regular claim tasks were left
unsigned. The signature now extends to EVERY task — the server signs at
INSERT in `admin_create_task` and re-signs at claim in
`/v1/workers/{id}/poll` with the UUID of the polling worker. This requires
the column to be NOT NULL.

**Destructive migration**: we directly apply
``DELETE FROM tasks WHERE signature IS NULL`` before the ALTER. The FK
``task_executions.task_id`` has ``ON DELETE CASCADE`` (defined in
``0001_init``), so orphan executions are cleaned up by themselves at no
extra cost.

**Rationale**: legacy unsigned tasks do not need to be kept — there is no
coexistence with legacy clients and no external operators waiting for results
of old tasks; marking the legacy tasks as ``failed`` with a synthetic
``cancelled_reason`` would only add noise to the audit log and reopen the
contradiction of keeping ``signature`` NULLABLE.

**Reversible**: ``downgrade()`` allows NULL in the column again. It does NOT
restore the deleted rows (the legacy data was lost in the upgrade — this is
acceptable because the migration is documented as destructive).

Compatibility with the existing constraint: ``tasks_signed_requires_signature_chk``
(0007) says ``signed = false OR (signed = true AND signature IS NOT NULL)``.
After this migration every record has ``signed=true`` and ``signature NOT NULL``,
which still satisfies the constraint.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0011_tasks_signature_not_null"
down_revision = "0010_tasks_target_worker_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Direct delete of legacy unsigned tasks. The CASCADE to task_executions
    # runs automatically (ON DELETE CASCADE in 0001_init).
    op.execute(text("DELETE FROM tasks WHERE signature IS NULL;"))

    # 2) Promote the column to NOT NULL. Postgres would reject the ALTER if
    # any row were left with signature IS NULL — the previous DELETE
    # prevents that.
    op.execute(text("ALTER TABLE tasks ALTER COLUMN signature SET NOT NULL;"))


def downgrade() -> None:
    # Allow NULL again to go back to the previous behaviour. The data deleted
    # in upgrade is NOT restored — the migration is documented as
    # destructive.
    op.execute(text("ALTER TABLE tasks ALTER COLUMN signature DROP NOT NULL;"))
