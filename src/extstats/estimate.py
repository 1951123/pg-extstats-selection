"""Extract PostgreSQL's cardinality estimate for a COUNT(*) query via EXPLAIN.

The benchmark queries are all of the form::

    SELECT COUNT(*) FROM <tables> WHERE <predicates>;

Optionally wrapped in ``||<truth>`` prefixes by the parser. To read the
optimizer's cardinality *estimate* of the query's result we:

  1. rewrite ``SELECT COUNT(*) FROM ...`` to ``SELECT * FROM ...`` (removes the
     Aggregate/Gather/partial-Aggregate layers so the top-level ``Plan Rows``
     is the size of the joined/filtered input relation), and
  2. run ``EXPLAIN (FORMAT JSON)`` and read ``Plan['Plan Rows']`` of the root.

The q-error (factor) of an estimate vs. a true count is defined as::

    qerr = max(estimate, actual) / min(estimate, actual)

which is >= 1, with 1 meaning a perfect estimate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from psycopg import Connection

# Leading ``SELECT COUNT(*)`` (case-insensitive, allowing whitespace).
_SELECT_COUNT_RE = re.compile(r"(?is)^\s*SELECT\s+COUNT\(\*\)\s+")

# Column names PostgreSQL uses for the estimated row count of a plan node.
_ROWS_KEY = "Plan Rows"


@dataclass(frozen=True)
class EstimateResult:
    """Cardinality estimate for one query plus the raw plan."""

    # Estimated output cardinality of the (rewritten) query.
    estimate: int
    # Full JSON EXPLAIN (FORMAT JSON) plan, for deeper inspection.
    plan: Any
    # Ground-truth count (None if unknown).
    actual: Optional[int] = None

    @property
    def qerror(self) -> Optional[float]:
        """q-error vs ``actual``; None if ``actual`` is unknown (or zero)."""
        if self.actual is None or self.actual <= 0:
            return None
        return qerror(self.estimate, self.actual)


def rewrite_count_to_select(sql: str) -> str:
    """Turn ``SELECT COUNT(*) FROM ...`` into ``SELECT * FROM ...``.

    This removes the aggregate so the top-level plan row estimate is the size
    of the filtered/joined input relation (the cardinality we want to compare
    against the COUNT ground truth).
    """
    sql = sql.strip().rstrip(";")
    rewritten = _SELECT_COUNT_RE.sub("SELECT ", sql, count=1)
    if rewritten == sql:
        raise ValueError(f"Could not rewrite COUNT(*) query: {sql!r}")
    return rewritten


def explain_json(conn: Connection, sql: str) -> Any:
    """Run ``EXPLAIN (FORMAT JSON)`` and return the parsed plan list.

    Handles both tuple rows (no row_factory) and dict rows (dict_row), so the
    function works regardless of how the connection was configured.
    """
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + sql)
        row = cur.fetchone()
    # psycopg returns the JSON value already parsed (JSON type). The column may
    # be accessed positionally (tuple) or by name (dict).
    if isinstance(row, dict):
        # The single column is 'QUERY PLAN' for EXPLAIN.
        return row.get("QUERY PLAN") or next(iter(row.values()))
    return row[0]


def top_estimate(plan: Any) -> int:
    """Extract the root node's estimated row count from an EXPLAIN JSON plan."""
    return int(plan[0]["Plan"][_ROWS_KEY])


def qerror(estimate: int | float, actual: int | float) -> float:
    """Return the factor q-error ``max(e,a)/min(e,a)`` (>= 1)."""
    if estimate <= 0 or actual <= 0:
        # Zero-side: treat degenerate cases as a large factor (>=1).
        if actual == 0 and estimate == 0:
            return 1.0
        # If actual is 0 but estimate >0, PG can't be perfect; use a floor of 1
        # plus the ratio. Keep it simple and robust for benchmarking.
        lo, hi = (float(actual), float(estimate)) if actual <= estimate else (float(estimate), float(actual))
        if lo <= 0:
            lo = 1.0
        return hi / lo
    return max(estimate, actual) / min(estimate, actual)


def estimate_count_query(conn: Connection, sql: str, actual: Optional[int] = None) -> EstimateResult:
    """Estimate the cardinality of a ``SELECT COUNT(*)`` query via EXPLAIN."""
    rewritten = rewrite_count_to_select(sql)
    plan = explain_json(conn, rewritten)
    est = top_estimate(plan)
    return EstimateResult(estimate=int(est), plan=plan, actual=actual)
