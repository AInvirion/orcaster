"""ORM models of the workspaces module (documentary; the DDL lives in migration 0021).

They inherit from the domain `Base` (`orcaster_server/models.py`), NOT from
`AuthBase`: they share the domain metadata (workers/tasks), not that of the
portable auth module. As in the rest of the repo, the SQL migrations are the
source of truth and these models only serve typed queries. The FKs that cross to
`users.id`/`workers.id` are declared here for documentation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orcaster_server.models import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "artifact_quota_bytes IS NULL OR artifact_quota_bytes > 0",
            name="workspaces_artifact_quota_bytes_chk",
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Override of ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES (migration 0055).
    # NULL = global default. Only the host sets it (orcaster_server/cli/workspace_quota.py),
    # never the UI: a tenant does not assign itself more disk.
    artifact_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="workspace_members_role_chk",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerEnrollmentToken(Base):
    __tablename__ = "worker_enrollment_tokens"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL")
    )
    label: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_worker_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workers.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class WorkerEnrollmentTokenUse(Base):
    __tablename__ = "worker_enrollment_token_uses"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    token_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("worker_enrollment_tokens.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workers.id", ondelete="SET NULL")
    )
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserActiveWorkspace(Base):
    __tablename__ = "user_active_workspace"

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
