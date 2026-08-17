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

All candidate statistics are created with a deterministic name so we can
always DROP the right object, and every real measurement leaves the database
unchanged (the last ANALYZE restores the baseline).

Output: for each query a small dict of results consumed by the ILP stage.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from psycopg import Connection

from .candidates import CandidateSet
from .estimate import estimate_count_query
from .parsers.base import BenchQuery
from .stats import _qualify_table

# One object holding a single statistical kind, matching stats.py naming.
_KIND_PREFIX = {"dependencies": "d", "ndistinct": "n", "mcv": "m"}


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
    # Per-candidate results keyed by a candidate identifier.
    candidates: dict[str, dict] = field(default_factory=dict)


def stat_name(cand: CandidateSet, kind: str, prefix: str = "ext_") -> str:
    """Deterministic statistic object name, matching stats.py's scheme.

    PostgreSQL folds unquoted identifiers (including statistic object names)
    to lowercase, so we build the name in lowercase from the start to avoid a
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
    col = {
        "dependencies": "stxddependencies",
        "ndistinct": "stxdndistinct",
        "mcv": "stxdmcv",
    }[kind]
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


def measure_query(
    conn: Connection,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    kind: str = "mcv",
    stat_prefix: str = "ext_",
) -> QueryMeasurement:
    """Measure baseline + each candidate's q-error for a single query
    (protocol A: create/analyze/explain/drop/analyze per candidate).

    Parameters
    ----------
    conn : psycopg connection (autocommit recommended).
    query : the benchmark query to measure.
    candidates : candidate combinations belonging to this query.
    kind : which statistic kind to create per candidate (default ``mcv``).
    stat_prefix : name prefix for created statistic objects.
    """
    # Baseline estimate (no extended statistics).
    base = estimate_count_query(conn, query.sql, actual=query.ground_truth)
    mes = QueryMeasurement(
        qid=query.qid,
        bench=query.bench,
        qerror_base=base.qerror if base.qerror is not None else float("nan"),
        estimate_base=base.estimate,
        actual=query.ground_truth,
    )

    # Map table alias -> qualified table for ANALYZE.
    for cand in candidates:
        name = stat_name(cand, kind, stat_prefix)
        table = _qualify_table(cand.table)

        # 1. create the statistic
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE STATISTICS {name} ({kind}) ON "
                f"{', '.join(cand.columns)} FROM {table}"
            )
        # 2. analyze the base table so the statistic is populated
        _analyze(conn, table)
        # 3. measure the estimate + q-error under this single statistic
        try:
            res = estimate_count_query(conn, query.sql, actual=query.ground_truth)
            # capture on-disk size BEFORE dropping (row still exists)
            size = stat_size_bytes(conn, name, kind)
        finally:
            # 4/5. drop the statistic and restore baseline via ANALYZE
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS {name}")
            _analyze(conn, table)

        mes.candidates[f"{cand.table_unqualified}({','.join(cand.columns)})"] = {
            "table": cand.table,
            "columns": list(cand.columns),
            "estimate": res.estimate,
            "qerror": res.qerror if res.qerror is not None else float("nan"),
            "size_bytes": size,
            "kind": kind,
        }

    return mes
