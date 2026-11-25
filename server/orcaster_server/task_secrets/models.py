"""Documentary ORM model of `task_secrets` (the DDL lives in migrations 0056 and 0059).

As in the rest of the repo, the SQL migration is the source of truth; this model
only serves as a mirror (anti-drift in `tests/test_migration_0056.py` and
`tests/test_migration_0059.py`). The service uses explicit SQL.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orcaster_server.models import Base


class TaskSecret(Base):
    __tablename__ = "task_secrets"
    # 0059: the UNIQUE (workspace_id, name) of 0056 becomes two partial unique
    # indexes, one per scope. `worker_id IS NULL` is a workspace-wide secret;
    # with a value, only that worker receives it.
    __table_args__ = (
        Index(
            "task_secrets_workspace_name_wide_uk",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("worker_id IS NULL"),
        ),
        Index(
            "task_secrets_workspace_name_worker_uk",
            "workspace_id",
            "name",
            "worker_id",
            unique=True,
            postgresql_where=text("worker_id IS NOT NULL"),
        ),
        Index("task_secrets_worker_idx", "worker_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL = workspace-wide. Deleting the worker deletes its secrets (0059).
    worker_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workers.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Fernet token. Never returned through any surface.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL")
    )
