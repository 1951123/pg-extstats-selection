"""Phase-2 verifier (方案 2): actually create a chosen set of extended
statistics and measure the TRUE mean q-error on PostgreSQL.

For a given set of statistics to build (table, columns, capacity, kind) this
module:

  1. CREATE STATISTICS each (with the exact capacity level),
  2. ANALYZE each affected base table,
  3. EXPLAIN every query and record the TRUE per-query q-error,
  4. return the real mean/median q-error.

Its purpose is to close the loop on an approximate selection method (linear
multiplicative ILP, pairwise, powerset, greedy) by comparing the *predicted*
q-error against what PostgreSQL actually produces once the chosen statistics
exist. The database is restored afterwards (all created statistics dropped,
tables re-analysed) so it is safe to run repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from psycopg import Connection

from .estimate import estimate_count_query
from .parsers.base import BenchQuery


@dataclass
class StatToBuild:
    """A concrete statistic to create during verification."""

    table: str            # base table (unqualified; PG resolves via search_path)
    columns: tuple[str, ...]
    level: int            # statistics_target / capacity
    kind: str = "mcv"
    # deterministic object name (lowercase, as PG will resolve it)
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            kind_tag = {"mcv": "m", "ndistinct": "n", "dependencies": "d"}.get(
                self.kind, "x"
            )
            tbl = self.table.lstrip(".") or self.table
            cols = "_".join(c.lower() for c in self.columns)
            self.name = f"verify_{kind_tag}_{tbl.lower()}_{cols}"


@dataclass
class VerifyResult:
    """Result of verification with a chosen set of statistics built."""

    # per-query q-error measured from the real planner
    qerror_per_query: list[float]
    # baseline per-query q-error (no stats)
    baseline_per_query: list[float]
    mean_qerror: float
    median_qerror: float
    baseline_mean_qerror: float
    # predicted (approximation) per-query q-error, if supplied by caller
    predicted_per_query: Optional[list[float]] = None
    predicted_mean_qerror: Optional[float] = None

    @property
    def approx_vs_real_ratio(self) -> Optional[float]:
        """measured_mean / predicted_mean; 1.0 means the approximation matched
        reality, >1 means the approximation was optimistic (real error higher)."""
        if self.predicted_mean_qerror is None:
            return None
        return self.mean_qerror / self.predicted_mean_qerror if self.predicted_mean_qerror else None


def _set_target(conn: Connection, level: int) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target = {int(level)}")


def _reset_target(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("RESET default_statistics_target")


def _analyze(conn: Connection, table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")


def verify_statistics(
    conn: Connection,
    queries: list[BenchQuery],
    stats: list[StatToBuild],
    *,
    predicted_per_query: Optional[list[float]] = None,
    target: Optional[int] = None,
) -> VerifyResult:
    """Build ``stats`` on the live DB and measure the TRUE q-error on ``queries``.

    Restores the DB afterwards (drops every created statistic and re-runs
    ANALYZE). ``predicted_per_query`` is optional; if given, the caller's
    per-query approximations are kept for an approx-vs-real comparison.

    Parameters
    ----------
    conn : autocommit psycopg connection.
    queries : the workload queries to measure.
    stats : statistics to create (each with its own capacity level).
    predicted_per_query : optional per-query approximated q-errors (aligned
        with ``queries`` order) so we can compare prediction vs reality.
    target : optional global ``default_statistics_target`` for ALL EXPLAIN
        measurements (baseline AND with-stats). Under the deterministic
        protocol this should be 10000 so that single-column baselines and the
        planner's MCV reading are both full-scan/noise-free, matching the
        phase-1 predictions. If None, the baseline uses the session default
        (typically 100) and each stat's EXPLAIN uses RESET.
    """
    # baseline (no stats)
    if target is not None:
        _set_target(conn, target)
    else:
        _reset_target(conn)
    base_q: list[float] = []
    for q in queries:
        r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        base_q.append(r.qerror if r.qerror is not None else float("nan"))
    baseline_mean = float(np.mean([v for v in base_q if v == v]))

    # build stats
    seen_tables: set[str] = set()
    try:
        for st in stats:
            # qual table (strip leading dot from predicate keys like ".postHistory")
            table = st.table.lstrip(".") or st.table
            # Drop any leftover object of the same name first (robustness).
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {st.name}")
            _set_target(conn, st.level)
            cols = ", ".join(st.columns)
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE STATISTICS {st.name} ({st.kind}) ON {cols} FROM {table}"
                )
            _analyze(conn, table)
            seen_tables.add(table)
            _reset_target(conn)
        # keep the global measurement target for the with-stats EXPLAIN phase so
        # the planner reads the full deterministic MCV (matches phase-1).
        if target is not None:
            _set_target(conn, target)
        real_q: list[float] = []
        for q in queries:
            r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            real_q.append(r.qerror if r.qerror is not None else float("nan"))
    finally:
        # restore: drop stats and re-analyze (ANALYZE also rebuilds single-col)
        _reset_target(conn)
        for st in stats:
            with conn.cursor() as cur:
                # Ignore errors if it somehow does not exist.
                try:
                    cur.execute(f"DROP STATISTICS IF EXISTS {st.name}")
                except Exception:
                    pass
        for t in seen_tables:
            _analyze(conn, t)

    real_mean = float(np.mean([v for v in real_q if v == v]))
    real_median = float(np.median([v for v in real_q if v == v]))
    return VerifyResult(
        qerror_per_query=real_q,
        baseline_per_query=base_q,
        mean_qerror=real_mean,
        median_qerror=real_median,
        baseline_mean_qerror=baseline_mean,
        predicted_per_query=predicted_per_query,
        predicted_mean_qerror=(
            float(np.mean([v for v in predicted_per_query if v == v]))
            if predicted_per_query
            else None
        ),
    )
