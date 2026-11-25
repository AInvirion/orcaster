"""Artifact quota per workspace: view, set or remove the override.

Only the host changes the quota (this script, against the DB); the UI shows it
but does not edit it, so that a tenant cannot assign itself more disk.

Usage:
    orcaster-workspace-quota
    orcaster-workspace-quota --workspace example --set 20GiB
    orcaster-workspace-quota --workspace example --clear

Without `--workspace` it lists all workspaces with their override, their
effective quota and their usage, and the growth headroom of the quotas against
the free space above the floor, using the same computation as the housekeeping
warning (`storage.quota_commitment`). `--set` accepts bytes or a binary suffix
(`MiB`, `GiB`, `TiB`), up to the maximum of a Postgres `bigint`. `--clear`
returns the workspace to the global default
(`ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES`). After any change it prints the report,
warns if the workspace is left with usage equal to or greater than its new quota
(it is paused by quota) and warns if the quota headroom exceeds the free space
above the floor.

The server reads the quota from the DB on every upload and every poll: the change
takes effect without a restart.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from orcaster_server import storage
from orcaster_server.config import Settings, settings

_UNITS = {"": 1, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
_SIZE_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")
# `workspaces.artifact_quota_bytes` is `bigint`: a larger value would fail in
# Postgres with a barely readable overflow error.
MAX_QUOTA_BYTES = 2**63 - 1


def parse_size(raw: str) -> int:
    """`"20GiB"` -> bytes. Only integers and explicit binary suffixes.

    It rejects decimal units (`GB`) on purpose: the floor and the default are in
    GiB and mixing bases is the kind of error this script must avoid.
    """
    m = _SIZE_RE.match(raw)
    if m is None or m.group(2).upper() not in _UNITS:
        raise ValueError(f"invalid size {raw!r}: use bytes or an integer with MiB, GiB or TiB")
    value = int(m.group(1)) * _UNITS[m.group(2).upper()]
    if value <= 0:
        raise ValueError(f"invalid size {raw!r}: the quota must be greater than 0")
    if value > MAX_QUOTA_BYTES:
        raise ValueError(
            f"invalid size {raw!r}: {value} bytes exceeds the maximum quota of "
            f"{MAX_QUOTA_BYTES} bytes (2**63 - 1, the bigint limit)"
        )
    return value


def gib(n: int) -> str:
    return f"{n / 1024**3:.2f} GiB"


@dataclass(frozen=True)
class WorkspaceQuotaRow:
    slug: str
    name: str
    override_bytes: int | None
    effective_bytes: int
    usage_bytes: int


async def list_quotas(engine: AsyncEngine, cfg: Settings) -> list[WorkspaceQuotaRow]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT w.slug, w.name, w.artifact_quota_bytes,
                           COALESCE(u.total_bytes, 0) AS usage_bytes
                      FROM workspaces w
                      LEFT JOIN workspace_artifact_usage u ON u.workspace_id = w.id
                     ORDER BY w.slug
                    """
                )
            )
        ).all()
    return [
        WorkspaceQuotaRow(
            slug=r.slug,
            name=r.name,
            override_bytes=r.artifact_quota_bytes,
            effective_bytes=(
                r.artifact_quota_bytes
                if r.artifact_quota_bytes is not None
                else cfg.artifact_workspace_max_total_bytes
            ),
            usage_bytes=int(r.usage_bytes),
        )
        for r in rows
    ]


async def set_quota(engine: AsyncEngine, slug: str, quota_bytes: int | None) -> None:
    """Set the override (or remove it with `None`). Fails if the slug does not exist."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("UPDATE workspaces SET artifact_quota_bytes = :q WHERE slug = :slug"),
            {"q": quota_bytes, "slug": slug},
        )
        if result.rowcount != 1:
            raise SystemExit(f"workspace {slug!r} not found")


def quota_pause_warning(row: WorkspaceQuotaRow) -> str | None:
    """Warning if the workspace already uses its effective quota or more.

    The poll pauses with `usage >= quota` and the upload gives 507 with any
    positive delta: the change is applied anyway, but the operator must know that
    it left the workspace paused by quota.
    """
    if row.usage_bytes < row.effective_bytes:
        return None
    return (
        f"  WARNING: {row.slug} already uses {gib(row.usage_bytes)} ({row.usage_bytes} bytes), "
        f"at or above its quota of {gib(row.effective_bytes)} ({row.effective_bytes} bytes): "
        "the quota is applied, but polling for this workspace stays paused (quota) and "
        "uploads get 507 until it frees space or gets a larger quota."
    )


def _print_report(rows: list[WorkspaceQuotaRow], commitment: storage.QuotaCommitment) -> None:
    for r in rows:
        source = "override" if r.override_bytes is not None else "default"
        print(
            f"  {r.slug:<24} quota {gib(r.effective_bytes):>12} ({source:<8})"
            f"  used {gib(r.usage_bytes):>12}"
        )
    print(
        f"\n  Committed: {gib(commitment.committed_bytes)} (sum of effective quotas, "
        f"{commitment.workspaces} workspace(s)); artifact usage {gib(commitment.usage_bytes)}"
    )
    print(
        f"  Headroom:  {gib(commitment.headroom_bytes)} (sum over workspaces of "
        "max(quota - used, 0): what artifacts can still grow)"
    )
    if commitment.available_bytes is None:
        print(f"  Available: unknown (cannot measure {settings.artifacts_root})")
        return
    print(
        f"  Available: {gib(commitment.available_bytes)} (free "
        f"{gib(commitment.free_bytes or 0)} minus the free-space floor "
        f"{gib(settings.artifact_disk_min_free_bytes)})"
    )
    if commitment.overcommitted:
        print(
            "  WARNING: quota headroom exceeds the free space above the floor; the "
            "shared floor can pause polling for every workspace."
        )


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Per-workspace artifact quota (host only)")
    parser.add_argument("--workspace", help="workspace slug")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--set", dest="size", help="override, e.g. 20GiB or bytes")
    action.add_argument("--clear", action="store_true", help="go back to the global default")
    args = parser.parse_args()

    if (args.size is not None or args.clear) and not args.workspace:
        parser.error("--set and --clear need --workspace")
    if args.workspace and args.size is None and not args.clear:
        parser.error("--workspace needs --set or --clear")
    if not settings.database_url:
        raise SystemExit(
            "DATABASE_URL is not set (environment variable or .env in the working directory)"
        )

    # Validate the size before opening connections: parser.error() exits the
    # process and must not leave an engine unclosed.
    quota: int | None = None
    if args.size is not None:
        try:
            quota = parse_size(args.size)
        except ValueError as exc:
            parser.error(str(exc))

    engine = create_async_engine(settings.database_url)
    try:
        if args.workspace:
            await set_quota(engine, args.workspace, quota)
            shown = "the global default" if quota is None else gib(quota)
            print(f"\n  {args.workspace}: artifact quota set to {shown}\n")
        rows = await list_quotas(engine, settings)
        commitment = await storage.quota_commitment(engine, settings)
    finally:
        await engine.dispose()
    _print_report(rows, commitment)
    if args.workspace:
        changed = next(r for r in rows if r.slug == args.workspace)
        warning = quota_pause_warning(changed)
        if warning is not None:
            print(warning)


def main() -> None:
    """Console entry point."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
