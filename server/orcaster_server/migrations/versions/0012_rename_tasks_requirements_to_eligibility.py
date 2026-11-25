"""tasks: rename column ``requirements`` -> ``eligibility``

Revision ID: 0012_tasks_rename_eligibility
Revises: 0011_tasks_signature_not_null
Create Date: 2026-05-20

The top-level field ``tasks.requirements`` always had the semantics of
"worker eligibility matching" (by default ``{}`` matches any worker; for
now it is stored unfiltered and remains available for future pre-INSERT
matching in Python).

It coexists with the ``payload.requirements`` subfield introduced together
with `commands`, which is something COMPLETELY different: the list of pip
specs that the worker installs in an ephemeral venv before running
``commands``. That ``payload.requirements`` stays untouched — it lives inside
``payload`` (JSONB) and was never a column of its own.

To disambiguate the domains (matching vs. pip), the top-level column is
renamed to ``eligibility``. Parallel changes in Pydantic + ORM + SQL
INSERTs ship in the accompanying commit.

Reversible: ``downgrade()`` undoes the rename. No data loss.
"""

from __future__ import annotations

from alembic import op

revision = "0012_tasks_rename_eligibility"
down_revision = "0011_tasks_signature_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "requirements",
        new_column_name="eligibility",
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "eligibility",
        new_column_name="requirements",
    )
