"""Protocol-A measurement engine for per-query candidate statistics.

Protocol A ("clean isolation"): for every candidate combination of a query we
measure the query's q-error under *exactly that one* extended statistic:

  1. (baseline) measure q-error with no extended statistics on any table.
  2. For each candidate ``s`` of the query:
       a. CREATE STATISTICS ``s``
       b. ANALYZE its base table (this is what makes the statistic usable and
          dominates the runtime)
       c. EXPLAIN the (rewritten) query -> estimated cardinality -> q-error
       d. read the statistic's on-disk size from ``pg_statistic_ext_data``
       e. DROP STATISTICS ``s``
       f. ANALYZE the base table again to restore the pre-``s`` state
          (protocol A requirement: no residual statistic interferes later).

Capacity levels: the MCV/ndistinct capacity is controlled by
``default_statistics_target``. For every candidate we repeat the cycle for each
requested capacity level in ``target_levels`` (e.g. ``{100, 1000, 10000}``),
setting the GUC before ANALYZE. This reveals that for high-cardinality column
combinations the statistic may be EMPTY at low capacity and only materialise at
higher capacity, so per-(candidate, level) results are needed.

All candidate statistics are created with a deterministic (lowercase) name so
we can always DROP the right object, and every real measurement leaves the
database unchanged (the last ANALYZE restores the baseline).

Output: for each query a dict of results consumed by the ILP stage, keyed by
capacity level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from psycopg import Connection

from .candidates import CandidateSet
from .estimate import estimate_count_query
from .parsers.base import BenchQuery
from .stats import _qualify_table

# One object holding a single statistical kind, matching stats.py naming.
_KIND_PREFIX = {"dependencies": "d", "ndistinct": "n", "mcv": "m"}
_KIND_DATA_COL = {
    "dependencies": "stxddependencies",
    "ndistinct": "stxdndistinct",
    "mcv": "stxdmcv",
}

# Default capacity levels to probe (default_statistics_target values).
DEFAULT_TARGET_LEVELS: tuple[int, ...] = (100, 1000, 10000)


@dataclass
class QueryMeasurement:
    """Measurements for one query."""

    qid: str
    bench: str
    # Baseline q-error (no extended statistics).
    qerror_base: float
    # estimated baseline cardinality (root plan rows).
    estimate_base: int
    actual: Optional[int]
    # Per-candidate results: {candidate_key: {level: {...}}}.
    candidates: dict[str, dict[int, dict]] = field(default_factory=dict)


def stat_name(cand: CandidateSet, kind: str, prefix: str = "ext_") -> str:
    """Deterministic (lowercase) statistic object name.

    PostgreSQL folds unquoted identifiers (including statistic object names)
    to lowercase, so we build the name lowercase from the start to avoid a
    CREATE/DROP/SELECT `stxname` mismatch.
    """
    tbl = cand.table_unqualified.lower()
    cols = "_".join(c.lower() for c in cand.columns)
    kind_tag = _KIND_PREFIX.get(kind, "x")
    return f"{prefix}{kind_tag}_{tbl}_{cols}"


def stat_size_bytes(conn: Connection, name: str, kind: str = "mcv") -> int:
    """Return the on-disk size in bytes of a created extended statistic.

    ``kind`` selects which data column carries the payload (dependencies,
    ndistinct, mcv) since a statistic created with a single kind only populates
    that column; the others are NULL.
    """
    col = _KIND_DATA_COL[kind]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(pg_column_size(se.{col}), 0) AS size
            FROM pg_statistic_ext s
            JOIN pg_statistic_ext_data se ON se.stxoid = s.oid
            WHERE s.stxname = %s
            """,
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return 0
    # Access 'size' robustly whether the cursor is dict_row or plain tuple.
    value = row.get("size") if isinstance(row, dict) else row[0]
    if value is None:
        return 0
    return int(value)


def _analyze(conn: Connection, table: str) -> None:
    """Run ANALYZE on a base table (rebuilds all its statistics)."""
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")


def _set_target(conn: Connection, level: int) -> None:
    """Set default_statistics_target to ``level`` for the session."""
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target = {int(level)}")


def _reset_target(conn: Connection) -> None:
    """Reset default_statistics_target to its default (100)."""
    with conn.cursor() as cur:
        cur.execute("RESET default_statistics_target")


def measure_query(
    conn: Connection,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    kind: str = "mcv",
    stat_prefix: str = "ext_",
    target_levels: tuple[int, ...] = DEFAULT_TARGET_LEVELS,
) -> QueryMeasurement:
    """Measure baseline + each (candidate, capacity level) q-error for a query.

    Protocol A, extended to capacity levels: for each candidate combination we
    create the statistic once, then for each ``target_level`` we ANALYZE the
    table under ``SET default_statistics_target = level`` and record the q-error
    and on-disk size. The statistic is dropped and the table re-analysed after
    each level to keep measurements isolated.

    Parameters
    ----------
    conn : psycopg connection (autocommit recommended).
    query : the benchmark query to measure.
    candidates : candidate combinations belonging to this query.
    kind : which statistic kind to create per candidate (default ``mcv``).
    stat_prefix : name prefix for created statistic objects.
    target_levels : capacity levels to probe (default (100, 1000, 10000)).
    """
    # Baseline estimate (no extended statistics), with default target.
    _reset_target(conn)
    base = estimate_count_query(conn, query.sql, actual=query.ground_truth)
    mes = QueryMeasurement(
        qid=query.qid,
        bench=query.bench,
        qerror_base=base.qerror if base.qerror is not None else float("nan"),
        estimate_base=base.estimate,
        actual=query.ground_truth,
    )

    for cand in candidates:
        name = stat_name(cand, kind, stat_prefix)
        table = _qualify_table(cand.table)

        # Create the statistic object once; ANALYZE per level under its target.
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE STATISTICS {name} ({kind}) ON "
                f"{', '.join(cand.columns)} FROM {table}"
            )
        per_level: dict[int, dict] = {}
        try:
            for level in target_levels:
                _set_target(conn, level)
                _analyze(conn, table)
                res = estimate_count_query(conn, query.sql, actual=query.ground_truth)
                size = stat_size_bytes(conn, name, kind)
                per_level[int(level)] = {
                    "estimate": res.estimate,
                    "qerror": res.qerror if res.qerror is not None else float("nan"),
                    "size_bytes": size,
                }
            _reset_target(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS {name}")
            _analyze(conn, table)
            _reset_target(conn)

        key = f"{cand.table_unqualified}({','.join(cand.columns)})"
        mes.candidates[key] = {
            "table": cand.table,
            "columns": list(cand.columns),
            "kind": kind,
            "levels": per_level,
        }

    return mes

