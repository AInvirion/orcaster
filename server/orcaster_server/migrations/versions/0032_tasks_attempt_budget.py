"""tasks.attempt_budget: attempt budget separate from the nominal fan-out

Revision ID: 0032_tasks_attempt_budget
Revises: 0031_output_events
Create Date: 2026-06-04

The claim predicate needs a budget that grows +1 with each retry, while the
close step needs the nominal fan-out to stay immutable as the success threshold
("exactly target_count executions completed"). A single column cannot fill both
roles: if target_count drifts with retries, a retry that succeeds after a failure
would close the task as 'failed' (completed < drifted target_count).

- `target_count` is the nominal fan-out requested by the operator (immutable
  except for a re-snapshot of all_available). Success threshold of the close.
- `attempt_budget` starts equal to `target_count` and is bumped +1 per retry
  (manual or automatic). It is what the claim predicate counts (non-expired) and
  the failure threshold of the close (budget exhausted without reaching the
  nominal value).

Backfill: attempt_budget = target_count. For existing tasks whose target_count
had already received bumps, the drifted value becomes the nominal one. This is
acceptable: it only affects pre-existing rows in development environments.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0032_tasks_attempt_budget"
down_revision = "0031_output_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN attempt_budget integer NOT NULL DEFAULT 1;"))
    op.execute(text("UPDATE tasks SET attempt_budget = target_count;"))
    op.execute(
        text(
            "ALTER TABLE tasks ADD CONSTRAINT tasks_attempt_budget_chk CHECK (attempt_budget >= 1);"
        )
    )


def downgrade() -> None:
    # The CHECK is effectively owned by the column; DROP COLUMN removes it
    # implicitly.
    op.execute(text("ALTER TABLE tasks DROP COLUMN attempt_budget;"))
