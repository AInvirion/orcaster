"""Retired data step: no-op

Revision ID: 0057_retired_variant_backfill
Revises: 0056_task_secrets
Create Date: 2026-10-27

This revision was a DATA migration, with no DDL, that reclassified the variant of
a group of existing tasks using the contract of an extractor that now lives in a
plugin. The core does not know that contract: `upgrade` and `downgrade` are
no-ops and the revision is kept only to preserve the chain.

Effect: a database that already went through this revision keeps what the
backfill wrote (the rows store their ``variant_kind``/``variant_label``). A
database that applies it again, from an earlier revision, does not receive that
backfill. The revision id is neutral (the previous id was more specific);
renaming it requires that no database sit exactly at this revision, because
`alembic_version` stores only the head.
"""

from __future__ import annotations

revision = "0057_retired_variant_backfill"
down_revision = "0056_task_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
