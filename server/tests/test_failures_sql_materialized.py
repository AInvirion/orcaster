"""The CTEs of `_SQL_TERMINAL_TASKS` must be declared AS MATERIALIZED.

Postgres >= 12 inlines CTEs referenced only once, and the planner
underestimates `sel` (the `status::text` cast and the variant OR leave it without
useful statistics): with that estimate it picks a nested loop that re-runs
the full stderr aggregation for every row of `rep` (O(n^2), 282 s for
a 7-day window with ~1,300 tasks in production, vs 0.6 s materialized).

Source anti-drift (same pattern as test_migration_0049): if someone
adds, renames or de-materializes a CTE of this query, the test fails.
"""

from __future__ import annotations

import re

from orcaster_server.failures import _SQL_TERMINAL_TASKS

# CTEs at the WITH level: `<name> AS (` or `<name> AS MATERIALIZED (`. Subquery
# aliases (`AS t(line, ord)`) do not match: they require `(` right after the
# (optional) MATERIALIZED.
_CTE_RE = re.compile(r"(\w+)\s+AS\s+(MATERIALIZED\s+)?\(", re.IGNORECASE)


def test_terminal_tasks_ctes_materialized() -> None:
    sql = _SQL_TERMINAL_TASKS.text
    ctes = _CTE_RE.findall(sql)
    names = [name for name, _ in ctes]
    # The four known passes are still present (if the query is restructured,
    # update this test together with the reason for each CTE).
    assert {"sel", "rep", "stderr_concat", "last_stderr"} <= set(names)
    not_materialized = [name for name, mat in ctes if not mat]
    assert not_materialized == [], (
        f"CTEs without AS MATERIALIZED in _SQL_TERMINAL_TASKS: {not_materialized}; "
        "inlined, they make the aggregation O(n^2)"
    )
