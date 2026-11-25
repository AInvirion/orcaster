"""SQLAlchemy 2.0 declarative models.

Documentary: the authoritative source of the schema is the Alembic migration
`orcaster_server/migrations/versions/0001_init.py` (raw SQL). These models must be kept in
sync with the migrations' DDL for ORM use in the server code, but they are
not used to autogenerate migrations.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TASK_STATUS_VALUES = (
    "queued",
    "dispatching",
    "paused",
    "completed",
    "cancelled",
    "failed",
)
# StrEnum derived from TASK_STATUS_VALUES (single source) for declarative
# validation of query params in the API: FastAPI rejects with 422 any value
# outside this set. Do not duplicate the values here.
# StrEnum (3.13): each member IS its own str, so
# `str(member) == member.value == member`. That removes the trap of the classic
# `Enum(..., type=str)`, where the member is NOT comparable/serializable as the
# raw string and forces you to remember `.value` to avoid leaking `TaskStatus.queued`.
TaskStatus = StrEnum("TaskStatus", {v: v for v in TASK_STATUS_VALUES})

EXECUTION_STATUS_VALUES = (
    "assigned",
    "claimed",
    "running",
    "completed",
    "failed",
    "expired",
    "cancelled",
)

# --- Sets derived from EXECUTION_STATUS_VALUES (single source) ---
# The set of "executions that consume budget" must not be encoded in independent
# forms (claim predicate in poll.py, close IN-lists in tasks.py, frozenset in
# admin_ui.py), which would risk drift when adding statuses. Everything is
# derived from here.

# 'expired' is the ONLY execution status that does NOT consume budget (a worker
# crash returns its slot). A new status added to EXECUTION_STATUS_VALUES consumes
# budget by default unless explicitly decided otherwise here.
EXPIRED_EXECUTION_STATUS = "expired"

# Statuses with an in-flight execution (non-terminal).
ACTIVE_EXECUTION_STATUSES = ("assigned", "claimed", "running")

# Terminal = everything that is not in flight.
TERMINAL_EXECUTION_STATUSES = tuple(
    s for s in EXECUTION_STATUS_VALUES if s not in ACTIVE_EXECUTION_STATUSES
)

# Terminal statuses that count toward the count-mode close: the SAME
# non-expired set as the claim predicate, restricted to terminals.
TERMINAL_NON_EXPIRED_EXECUTION_STATUSES = tuple(
    s for s in TERMINAL_EXECUTION_STATUSES if s != EXPIRED_EXECUTION_STATUS
)


def sql_status_set(statuses: tuple[str, ...]) -> str:
    """SQL fragment `('a','b',...)` for `IN (...)` from the constants of this
    module. Interpolates ONLY literals from EXECUTION_STATUS_VALUES at build
    time (never external input); the statements remain static."""
    return "(" + ",".join(f"'{s}'" for s in statuses) + ")"


# create_type=False: the ENUMs are created explicitly by migration 0001_init.
task_status_enum = ENUM(
    *TASK_STATUS_VALUES,
    name="task_status",
    create_type=False,
)
execution_status_enum = ENUM(
    *EXECUTION_STATUS_VALUES,
    name="execution_status",
    create_type=False,
)


class Base(DeclarativeBase):
    pass


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('online','paused','offline')",
            name="workers_status_chk",
        ),
        Index("workers_api_key_hash_idx", "api_key_hash", unique=True),
        Index("workers_heartbeat_idx", "last_heartbeat_at"),
        Index(
            "workers_online_idx",
            "status",
            postgresql_where="status = 'online'",
        ),
        # Migration 0021 (multi-tenancy). Declared here for documentary parity.
        Index("workers_workspace_idx", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    # Migration 0021: workspace scope. FK ON DELETE RESTRICT. The compatibility
    # bridge (column DEFAULT + FetchedValue) was removed in 0022: workspace_id is
    # now always set explicitly by whoever does the INSERT (register_worker passes
    # it from the enrollment token).
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    os: Mapped[str] = mapped_column(Text, nullable=False)
    arch: Mapped[str] = mapped_column(Text, nullable=False)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    ram_used_mb: Mapped[int | None] = mapped_column(Integer)
    disk_mb: Mapped[int | None] = mapped_column(Integer)
    disk_used_mb: Mapped[int | None] = mapped_column(Integer)
    # Migration 0039: CPU % attributable to the worker's container
    # (0..100), reported in the poll. NULL = no sample yet or older worker.
    cpu_pct: Mapped[int | None] = mapped_column(SmallInteger)
    version: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default="{}",
    )
    # FLAT map name -> boolean|number (e.g. `{"gpu": true,
    # "gpu_memory": 8192}`). The register is the only writer and validates the
    # shape (`orcaster_server/eligibility.py::validate_worker_capabilities`); the
    # reader is the dispatch predicate (`capability_match_sql`). Strings are
    # excluded on purpose: without a total order there is no evaluable `>=`.
    capabilities: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="online",
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Migration 0018: soft-delete marker. NULL = active.
    # The row is not physically deleted so as to preserve the history of
    # `task_executions` (FK ON DELETE CASCADE).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Migration 0034: lifecycle of the on-demand cache cleanup request.
    # Declared here for documentary parity (the real state is managed by the
    # admin endpoint and the poll handler via SQL). NULL/NULL = never requested.
    cache_flush_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cache_flush_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Migration 0053: last `status` the worker self-reported in the poll
    # (`idle`/`busy`). Independent of `status`, which is the state the server
    # decides (`online`/`paused`/`offline`) and which the CHECK bounds: a worker
    # can be `online` and report itself `busy` at the same time. Written by the
    # poll UPDATE (`_SQL_POLL_UPDATE_WORKER`) and read by the open-claim
    # fairness-yield, which does not yield to a worker that declares itself
    # unavailable. NULL = worker that has not polled yet since this deploy; it
    # behaves as before the migration (it does force a yield).
    last_reported_status: Mapped[str | None] = mapped_column(Text)
    # Migration 0060: last time the server refused to deliver a task to this
    # worker because of a stored secret it could not decrypt (or no keys). Written
    # by the poll and read by the open-claim fairness-yield: for a short window,
    # the other workers do not yield to it the tasks that use stored secrets.
    # NULL = never refused.
    task_secrets_refused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Migration 0054: free disk fraction (free/total of the cache volume) that
    # the worker self-reports in the poll. It is the input of the server-side disk
    # gate: the server applies the central threshold
    # `worker_claim_min_free_fraction` and decides whether to withhold tasks and
    # whether to exclude the worker from the fairness-yield and from the
    # `all_available` snapshot. NULL = "unknown" (older worker that does not send
    # the field, or unmeasurable volume): it is NOT gated, NOT excluded, NOT
    # skipped, preserving the previous behavior and bidirectional compatibility.
    # Independent of `disk_used_mb` (which is display telemetry): the fraction is
    # measured in bytes and excludes reserved blocks, so it is not derived from
    # `total_mb - used_mb`.
    free_disk_fraction: Mapped[float | None] = mapped_column(Float)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "target_mode IN ('count','all_available')",
            name="tasks_target_mode_chk",
        ),
        CheckConstraint("target_count >= 1", name="tasks_target_count_chk"),
        # Migration 0032: attempt budget separate from the nominal fan-out.
        # See the comment on the `attempt_budget` column.
        CheckConstraint("attempt_budget >= 1", name="tasks_attempt_budget_chk"),
        CheckConstraint(
            "delivery_mode IN ('claim','push')",
            name="tasks_delivery_mode_chk",
        ),
        CheckConstraint(
            "delivery_mode = 'claim' OR (delivery_mode = 'push' AND target_worker_id IS NOT NULL)",
            name="tasks_push_requires_target_worker_chk",
        ),
        CheckConstraint(
            "target_worker_id IS NULL OR (target_mode = 'count' AND target_count = 1)",
            name="tasks_target_worker_exclusive_chk",
        ),
        CheckConstraint(
            "signed = false OR (signed = true AND signature IS NOT NULL)",
            name="tasks_signed_requires_signature_chk",
        ),
        Index(
            "tasks_dispatch_idx",
            "priority",
            "run_at",
            postgresql_where="status IN ('queued','dispatching')",
        ),
        Index(
            "tasks_push_pending_idx",
            "target_worker_id",
            "created_at",
            postgresql_where="delivery_mode = 'push' AND status IN ('queued','dispatching')",
        ),
        Index(
            "tasks_idempotency_key_idx",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL",
        ),
        Index(
            "tasks_parent_task_id_idx",
            "parent_task_id",
            postgresql_where="parent_task_id IS NOT NULL",
        ),
        # Migration 0047: time-range index (workspace_id, status, completed_at)
        # for the outcomes hot path. Declared here for documentary parity; it
        # replaces the old (workspace_id, status) one from migration 0021, which
        # 0047 drops because it is subsumed by prefix.
        Index("tasks_terminal_completed_idx", "workspace_id", "status", "completed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Migration 0021: workspace scope. FK ON DELETE RESTRICT. The compatibility
    # bridge (column DEFAULT + FetchedValue) was removed in 0022: workspace_id is
    # now always set explicitly by whoever does the INSERT (the admin task
    # creation routes bind it in the SQL).
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    queue: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(
        task_status_enum,
        nullable=False,
        server_default="queued",
    )
    command: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="run_batch")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    target_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="count",
    )
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Migration 0032: `target_count` is the NOMINAL fan-out requested by the
    # operator (close success threshold: exactly target_count 'completed'
    # executions); `attempt_budget` is the attempt budget that the claim predicate
    # counts (non-expired) and that every retry (manual or auto) bumps by +1. They
    # are born equal on INSERT; only attempt_budget drifts. Mutators:
    # _SQL_BUMP_ATTEMPT_BUDGET (_retry_internals.py), _SQL_UPDATE_TARGET_COUNT
    # (all_available re-snapshot: sets both).
    attempt_budget: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Migration 0033: epoch of the CURRENT snapshot of an `all_available` task.
    # Born at 0; the admin retry (re-snapshot) bumps it by +1
    # (`_SQL_RESET_TASK_DISPATCHING`) and the snapshot stamps the current value
    # on the new executions. The `all_available` close (`_SQL_MAYBE_CLOSE_TASK`)
    # evaluates only the current epoch; count-mode is cumulative over the whole
    # history and does NOT use this column.
    snapshot_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Migration 0050: provenance of the current `target_mode`. `'all_available'`
    # when the sticky auto-retry converted the task to count-mode
    # (`_SQL_RETARGET_CLAIM_SAME_WORKER`); NULL = native count (current behavior,
    # no backfill). The count-mode close (`_SQL_MAYBE_CLOSE_TASK`) reads the mark
    # to count only the current epoch on converted tasks, instead of the whole
    # history.
    converted_from: Mapped[str | None] = mapped_column(Text)
    # The ONLY key with a reader is `capabilities`, a LIST of requirements
    # `name` or `name<op>number` that is evaluated against
    # `workers.capabilities` at dispatch (`orcaster_server/eligibility.py`). The
    # rest (`min_ram_mb`, `os`, `arch`, `tags_any`, ...) remains free-form JSON
    # with no reader and no validation. Without the `capabilities` key, the task
    # matches any worker.
    eligibility: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    claim_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    execution_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Unify /poll columns (added by migration 0007). The real FK ondelete is
    # CASCADE (see 0007). The model is documentary; the DDL is the truth.
    delivery_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="claim",
    )
    target_worker_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workers.id", ondelete="CASCADE"),
    )
    signed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    # Migration 0011: the signature is universal: the server signs every task at
    # INSERT and re-signs at claim. NOT NULL since 0011.
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    # Migration 0010: restricted pool of worker IDs (list of UUIDs as strings)
    # for targeting. NULL = no restriction (compatible with the "N" and
    # "all-available" syntax).
    target_worker_pool: Mapped[list[str] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Migration 0017: sticky auto-retry on throttling. NULL = backward
    # compatible, no automatic retry. Pydantic shape in
    # `orcaster_server/schemas/tasks.py::AutoRetryPolicy`.
    auto_retry_policy: Mapped[dict | None] = mapped_column(JSONB)
    auto_retry_attempts_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    # Migration 0019: fork lineage. parent_task_id points to the parent's task_id
    # (self-FK). ON DELETE SET NULL: deleting the parent does not delete the
    # child. Modeled as a plain column (the repo's raw-SQL style); no relationship.
    parent_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="SET NULL"),
    )
    # Migration 0019: operator in-place retries (POST /tasks/{id}/retry).
    # Independent of auto_retry_attempts_used.
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    # Migration 0045: variant derived from the payload to segment the dashboard.
    # `variant_kind` = tool (kind of a plugin's extractor, e.g. 'example-tool';
    # NULL = other/unrecognized), `variant_label` = version (e.g. '1.2.3';
    # NULL = other). Derived by `orcaster_server/task_variant.py` when the task is
    # created (`_create_task`); rows already written keep their value. Index
    # `tasks_variant_idx (workspace_id, variant_kind, variant_label, status)`.
    # `Text` with no constraint on purpose. The cap on the `kind~label` token
    # (`VARIANT_TOKEN_MAX_LEN`, the one the `?variant=` query param that the UI
    # re-emits requires) is applied at INGESTION (`task_variant.clamp_variant`,
    # inside `extract_variant`), not as a DB CHECK: it is an application rule on
    # derived columns, and a CHECK would have required a migration plus validation
    # of the history for an invariant the data already satisfies.
    variant_kind: Mapped[str | None] = mapped_column(Text)
    variant_label: Mapped[str | None] = mapped_column(Text)
    # Generic terminal outcome: nuance of the close declared by the task
    # (payload.outcomes) or by a plugin's outcome profile.
    # NULL = ordinary terminal. Partial index `tasks_outcome_idx
    # (workspace_id, outcome) WHERE outcome IS NOT NULL`.
    outcome: Mapped[str | None] = mapped_column(Text)


class TaskExecution(Base):
    __tablename__ = "task_executions"
    __table_args__ = (
        Index(
            "te_lease_idx",
            "lease_expires_at",
            postgresql_where="status IN ('claimed','running')",
        ),
        Index(
            "te_assigned_idx",
            "worker_id",
            "status",
            postgresql_where="status = 'assigned'",
        ),
        Index("te_worker_idx", "worker_id", "status"),
        # Migration 0061: the per-worker `max(assigned_at)` of the open-claim
        # fairness-yield reads this index from the end.
        Index("te_worker_assigned_idx", "worker_id", "assigned_at"),
        # Migration 0014: stable UNIQUE + PARTIAL UNIQUE to guarantee a single
        # active execution per (task_id, worker_id) while preserving history on
        # retry. Declared as a documentary Index; the real creation is done by the
        # migration.
        Index(
            "task_executions_attempt_uk",
            "task_id",
            "worker_id",
            "attempt_index",
            unique=True,
        ),
        Index(
            "task_executions_active_uk",
            "task_id",
            "worker_id",
            unique=True,
            postgresql_where="status IN ('assigned','claimed','running')",
        ),
    )

    # Migration 0014: the PK becomes an independent UUID. The identity of the
    # attempt is expressed by (task_id, worker_id, attempt_index) via a UNIQUE
    # constraint.
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
    )
    # Migration 0033: epoch of the snapshot that created this execution.
    # Those of the initial snapshot carry 0; each re-snapshot of the admin retry
    # stamps the bumped epoch. See the comment on `Task.snapshot_epoch`.
    snapshot_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(
        execution_status_enum,
        nullable=False,
        server_default="assigned",
    )
    lease_token: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    progress: Mapped[int | None] = mapped_column(SmallInteger)
    progress_message: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    # Outcome of THIS attempt; tasks.outcome is derived at close.
    outcome: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Unify /poll: exactly-once delivery marker for cancel signals (0007).
    cancel_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminAction(Base):
    __tablename__ = "admin_actions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('delete_worker','cancel_task','delete_task',"
            "'bulk_cancel_task','bulk_delete_task',"
            "'retry_task','auto_retry_task','fork_task',"
            "'mint_pat','revoke_pat','delete_pat',"
            "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token',"
            "'clear_worker_cache','bulk_retry_task','consume_enrollment_token',"
            "'pause_worker','resume_worker','mint_share_token','revoke_share_token',"
            "'set_task_secret','delete_task_secret','deliver_task_secrets')",
            name="admin_actions_action_check",
        ),
        CheckConstraint(
            "target_kind IN ('worker','task','pat','enrollment_token','share_token','task_secret')",
            name="admin_actions_target_kind_check",
        ),
        Index("admin_actions_target_idx", "target_kind", "target_id"),
        Index("admin_actions_ts_idx", "ts"),
        # Migration 0023 (multi-tenancy). Declared here for documentary parity.
        Index("admin_actions_workspace_ts_idx", "workspace_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Migration 0023: workspace scope to close the audit log's cross-tenant leak.
    # FK ON DELETE RESTRICT. No server_default: it is stamped explicitly on every
    # audit write (record_admin_action).
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)


# WorkerCommand model removed (migration 0009 dropped worker_commands table).
# Push delivery is now expressed as tasks with `delivery_mode='push'`.


class OutputEvent(Base):
    """Output event of an execution (migration 0031).

    Each stdout/stderr chunk that the worker sends is persisted as an independent
    row with a sequence number `seq` (unique per execution) and a timestamp `ts`.
    The PK `id` is BIGSERIAL (physical insertion order); `seq` is the logical
    order per execution.

    UNIQUE (execution_id, seq) allows idempotent worker retries.
    """

    __tablename__ = "output_events"
    __table_args__ = (
        # Documentary: the real index is created by migration 0031.
        Index(
            "uq_output_events_execution_seq",
            "execution_id",
            "seq",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("task_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class TaskArtifact(Base):
    """Real file produced by the task (migration 0035).

    Distinct from the log (output_events): the log is the on-demand transcript of
    stdout/stderr; an artifact is a file that the task wrote in
    ORCASTER_ARTIFACTS_DIR. The bytes live on the filesystem
    (ARTIFACTS_ROOT/<execution_id>/<rel_path>); this row is metadata only.
    Associated per execution (same as output_events). UNIQUE(execution_id,
    rel_path) gives idempotency to the worker's upload retries.
    """

    __tablename__ = "task_artifacts"
    __table_args__ = (
        Index(
            "uq_task_artifacts_execution_relpath",
            "execution_id",
            "rel_path",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("task_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Collection (migration 0052): the sync-aware operational routine purged the
    # bytes from disk because the artifact is already backed up remotely, and
    # marked the row instead of deleting it. NULL = artifact available. The
    # metadata survives as evidence that the task produced the file; the download
    # answers 410 Gone and the quota stops counting the row.
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceShareToken(Base):
    """Opaque revocable token for the public status page.

    Documentary: the real DDL lives in migrations 0044 and 0058. The secret is
    NEVER persisted in clear: the lookup uses its `sha256` hex in `token_hash`
    (UNIQUE, O(1)) and `token_ciphertext` (0058) stores it encrypted with the task
    secrets keyring only to show the URL again in /admin; NULL on earlier rows,
    without a keyring, or when revoked. `revoked_at` NOT NULL ⟹ unusable
    (soft-revoke). FK to the workspace with ON DELETE CASCADE.
    """

    __tablename__ = "workspace_share_token"
    __table_args__ = (Index("workspace_share_token_workspace_idx", "workspace_id"),)

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
