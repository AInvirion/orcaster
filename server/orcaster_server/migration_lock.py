"""Advisory lock key that serializes `alembic upgrade head`.

With `uvicorn --workers N`, each process runs the migrations at startup
(`MIGRATE_ON_STARTUP=true`). Without serialization, two processes launch
`alembic upgrade head` at the same time: the winner applies the DDL (for example an
`ADD COLUMN`) and the other collides with the change that was just committed
(`DuplicateColumnError`), dies, and leaves a transient crash at boot.

`orcaster_server/migrations/env.py` takes `pg_advisory_xact_lock(MIGRATION_LOCK_KEY)`
as the first statement of the migration transaction. The lock is exclusive and is
released on its own at the COMMIT/ROLLBACK of that same transaction, so the other
processes block there until the winner finishes; when they continue (READ COMMITTED)
they already see `alembic_version` at head and re-apply nothing.

The value is an arbitrary but STABLE constant (any signed 64-bit integer works as an
advisory lock key); it lives here so it can be shared between `env.py` and the tests
without importing `env.py` (which would run migrations on import).
"""

from __future__ import annotations

# Fixed constant. Do not change between versions: if it changed, an old process and
# a new one would take different locks and the serialization would break during a
# deploy with overlap.
MIGRATION_LOCK_KEY = 728_160_142  # arbitrary but stable; fits in a bigint.
