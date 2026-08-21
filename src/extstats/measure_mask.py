"""Catalog-mask measurement engine (Protocol-M).

Classical Protocol A isolates each candidate by CREATE -> ANALYZE -> EXPLAIN
-> DROP -> ANALYZE, so every candidate costs (at least) two ANALYZEs. On a wide
table (e.g. Census ``climate``, 69 columns, ~2.5M rows) a single ANALYZE at the
deterministic ``default_statistics_target = 10000`` takes ~20s, making a
per-candidate scan infeasible.

Protocol-M (the *catalog-mask* protocol) builds ALL of a table's candidate
statistics in a SINGLE ANALYZE, then measures each candidate's independent
contribution by *masking* (NULL-ing out) the ``pg_statistic_ext_data`` payload
of every other candidate, EXPLAINing, and restoring the payload from a backup.
Because ``get_relation_statistics()`` only adds a statistic to ``rel->statlist``
when ``statext_is_kind_built()`` finds its data tuple, a NULL/absent payload
makes the planner gracefully ignore that statistic (no ERROR). Restoring is done
entirely inside PostgreSQL via a temporary backup table + ``UPDATE ... FROM``
(same-type ``pg_mcv_list`` assignment), because `pg_mcv_list` has no bytea cast
for driver-parameter round-trip.

Output is shaped like :mod:`extstats.measure`'s ``QueryMeasurement`` so the
ILP / phase-1 pipeline consumes it unchanged (a single capacity level = build
``target``).

Scopes (drive the same masking scheme at two granularities):
- *per-query* (default): each query's candidates are built by their own ANALYZE
  and measured independently. Preferred for multi-table / heterogeneous
  workloads (queries on different tables cannot share a build) and for
  per-query failure isolation; but it pays the fixed ANALYZE base cost (table
  sampling + per-column statistics) once per query.
- *workload-wide*: all of a single table's distinct (candidate x level) objects
  are built and ANALYZEd once, then every query is measured by masking against
  that one build. Preferred when one table carries most of the queries and the
  fixed ANALYZE cost dominates (e.g. CENSUS ``climate``, stats_CEB_single
  ``posts``): the fixed base is paid a single time, at the price of one large
  ANALYZE (all candidates simultaneously) and no per-query isolation.
The fixed ANALYZE cost is sampled/measured in ``probe_census_analyze_scale.py``;
see also the paper's ``sec:measure-runtime``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from psycopg import Connection

from .candidates import CandidateSet
from .estimate import estimate_count_query
from .measure import stat_name, stat_size_bytes
from .parsers.base import BenchQuery
from .stats import _qualify_table

# Per-kind catalog payload column in pg_statistic_ext_data.
_KIND_DATA_COL = {
    "dependencies": "stxddependencies",
    "ndistinct": "stxdndistinct",
    "mcv": "stxdmcv",
}
# Only these kinds use a single varlena payload that can be NULL-masked.
_MASKABLE_KINDS = {"dependencies", "ndistinct", "mcv"}


@dataclass
class QueryMaskMeasurement:
    """Mask-protocol measurement for one query (Protocol-A compatible)."""

    qid: str
    bench: str
    qerror_base: float
    estimate_base: int
    actual: Optional[int]
    candidates: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Low-level catalog helpers
# ---------------------------------------------------------------------------

def _data_col(kind: str) -> str:
    return _KIND_DATA_COL[kind]


def _backup_payload(conn: Connection, backup_table: str, oids: list[int],
                    kind: str) -> None:
    col = _data_col(kind)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {backup_table}")
        # NOTE: no ON COMMIT DROP — this engine runs with autocommit=True, so
        # ON COMMIT DROP would drop the table immediately after creation.
        cur.execute(
            f"CREATE TEMP TABLE {backup_table} "
            f"(stxoid oid, payload pg_mcv_list)")
        for oid in oids:
            cur.execute(
                f"INSERT INTO {backup_table} "
                f"SELECT stxoid, {col} FROM pg_statistic_ext_data "
                f"WHERE stxoid=%s", (oid,))


def _restore_payload(conn: Connection, backup_table: str, kind: str) -> None:
    col = _data_col(kind)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE pg_statistic_ext_data d SET {col} = b.payload "
            f"FROM {backup_table} b WHERE d.stxoid = b.stxoid")


def _drop_backup(conn: Connection, backup_table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {backup_table}")


def _mask_payload_all_but(conn: Connection, backup_table: str,
                          keep_oids: set[int], kind: str) -> None:
    """NULL every backed-up payload except those in ``keep_oids``."""
    col = _data_col(kind)
    with conn.cursor() as cur:
        cur.execute("SELECT stxoid FROM " + backup_table)
        for row in cur.fetchall():
            oid = row[0] if not isinstance(row, dict) else row["stxoid"]
            if oid not in keep_oids:
                cur.execute(
                    f"UPDATE pg_statistic_ext_data SET {col} = NULL "
                    f"WHERE stxoid=%s", (oid,))


def _stat_oid(conn: Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT oid FROM pg_statistic_ext WHERE stxname=%s", (name,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"statistic {name!r} not found")
        return row["oid"] if isinstance(row, dict) else row[0]


def _set_target(conn: Connection, level: int) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target = {int(level)}")


def _analyze(conn: Connection, table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")


def _stat_name_level(cand: CandidateSet, kind: str, prefix: str, level: int) -> str:
    """Statistic object name carrying its capacity level, e.g.
    ``ext_m_posts_afv_l1000`` (distinct from other levels of same combo).

    NOTE: PostgreSQL folds unquoted identifiers to lowercase, so the level
    suffix must be lowercase (``l1000`` not ``L1000``) or a CREATE/query name
    mismatch occurs.
    """
    base = stat_name(cand, kind, prefix)
    return f"{base}_l{int(level)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def measure_query_mask(
    conn: Connection,
    query: BenchQuery,
    candidates: list[CandidateSet],
    *,
    kind: str = "mcv",
    stat_prefix: str = "ext_",
    target: Optional[int] = None,
    target_levels: Optional[tuple[int, ...]] = None,
    table: Optional[str] = None,
    backup_table: str = "_ext_mask_backup",
) -> QueryMaskMeasurement:
    """Measure ``query`` with the catalog-mask protocol.

    Capacity levels: pass either ``target`` (a single level) or
    ``target_levels`` (a tuple, e.g. ``(100, 1000, 10000)``). When more than
    one level is given, each candidate is materialised as **one statistic
    object per level** (``ALTER STATISTICS ... SET STATISTICS``) and ALL levels
    are built by a SINGLE ANALYZE (sampling takes the max target), then each
    (candidate, level) is measured independently by masking the others. This is
    the "multi-object one-ANALYZE" scheme, so a 3-level phase-1 needs only ONE
    ANALYZE per table (sampling at max target), not one per level.

    ``table`` forces a base table for all candidates (use when every candidate
    shares one table, e.g. Census ``climate``); otherwise each candidate's own
    table is used (grouped, one ANALYZE each). ``backup_table`` must be unique
    per concurrent session.

    All statistics are created then built with ONE ANALYZE per table; each
    (candidate, level) is measured by masking every other statistic object.
    Created stats are dropped and tables re-ANALYZEd at the end (restored).
    """
    if kind not in _MASKABLE_KINDS:
        raise ValueError(
            f"kind {kind!r} not maskable; use one of {sorted(_MASKABLE_KINDS)}")

    # Normalise the level set: prefer target_levels, fall back to [target] or [0]
    if target_levels:
        levels = tuple(int(x) for x in target_levels)
    elif target is not None:
        levels = (int(target),)
    else:
        levels = (0,)

    max_level = max(levels)

    # deterministic single-column baseline
    if max_level > 0:
        _set_target(conn, max_level)
    base = estimate_count_query(conn, query.sql, actual=query.ground_truth)
    mes = QueryMaskMeasurement(
        qid=query.qid,
        bench=query.bench,
        qerror_base=base.qerror if base.qerror is not None else float("nan"),
        estimate_base=base.estimate,
        actual=query.ground_truth,
    )
    if not candidates:
        _drop_backup(conn, backup_table)
        return mes

    by_table: dict[str, list[CandidateSet]] = {}
    for c in candidates:
        tbl = _qualify_table(table if table else c.table)
        by_table.setdefault(tbl, []).append(c)

    for tbl, cands in by_table.items():
        _measure_one_table(conn, query, tbl, cands, kind, stat_prefix,
                           levels, backup_table, mes, multi=len(levels) > 1)

    # _measure_one_table already drops the stats it created and re-ANALYZEs the
    # table (in a finally); just drop any leftover backup table for safety.
    _drop_backup(conn, backup_table)
    return mes



def _measure_one_table(
    conn: Connection,
    query: BenchQuery,
    tbl: str,
    cands: list[CandidateSet],
    kind: str,
    stat_prefix: str,
    levels: tuple[int, ...],
    backup_table: str,
    mes: QueryMaskMeasurement,
    *,
    multi: bool,
) -> None:
    """Create (optionally multi-level) stats on one table, ANALYZE once,
    mask-measure each (candidate, level).

    When ``multi`` is True, each candidate gets one statistic object PER level,
    each with ``ALTER STATISTICS ... SET STATISTICS <level>``; a SINGLE ANALYZE
    (sampling at the max level) builds all of them. Each (candidate, level) is
    then measured by keeping only that object's payload and NULL-ing every
    other object (all other candidates AND the same candidate's other levels).
    """
    # name for a (cand, level): with level suffix when multi, else plain name
    def name_of(c: CandidateSet, lvl: int) -> str:
        if multi:
            return _stat_name_level(c, kind, stat_prefix, lvl)
        return stat_name(c, kind, stat_prefix)

    created_names: list[str] = []
    try:
        # create all (candidate, level) objects
        for c in cands:
            for lvl in levels:
                name = name_of(c, lvl)
                created_names.append(name)
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                    cur.execute(f"CREATE STATISTICS {name} ({kind}) ON "
                                f"{', '.join(c.columns)} FROM {tbl}")
                    if multi:
                        cur.execute(
                            f"ALTER STATISTICS {name} SET STATISTICS {int(lvl)}")

        # ONE ANALYZE builds all objects (sampling at max level)
        if max(levels) > 0:
            _set_target(conn, max(levels))
        _analyze(conn, tbl)

        # oids + snapshot: one payload per (cand, level) object
        oids: dict[tuple, int] = {}
        for c in cands:
            for lvl in levels:
                oids[(c, lvl)] = _stat_oid(conn, name_of(c, lvl))
        _backup_payload(conn, backup_table, list(oids.values()), kind)

        # measure each (candidate, level) by masking every other object
        for c in cands:
            for lvl in levels:
                keep = {oids[(c, lvl)]}
                _mask_payload_all_but(conn, backup_table, keep, kind)
                if max(levels) > 0:
                    _set_target(conn, max(levels))
                res = estimate_count_query(conn, query.sql,
                                           actual=query.ground_truth)
                key = f"{tbl}({','.join(c.columns)})"
                qerr = res.qerror if res.qerror is not None else float("nan")
                size = stat_size_bytes(conn, name_of(c, lvl), kind)
                level_dict = mes.candidates.setdefault(key, {
                    "table": tbl,
                    "columns": list(c.columns),
                    "kind": kind,
                    "levels": {},
                })
                level_dict["levels"][int(lvl)] = {
                    "estimate": res.estimate,
                    "qerror": qerr,
                    "size_bytes": size,
                }
                _restore_payload(conn, backup_table, kind)
        _drop_backup(conn, backup_table)
    finally:
        # always drop every created object + re-analyze to restore the table
        for name in created_names:
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {name}")
        _drop_backup(conn, backup_table)
        _analyze(conn, tbl)
