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
    single_col_target: int = 10000  # capacity of the fixed per-column baseline


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
    single_col_target: int = 10000,
    table: Optional[str] = None,
    backup_table: str = "_ext_mask_backup",
) -> QueryMaskMeasurement:
    """Measure ``query`` with the catalog-mask protocol.

    Capacity levels: pass either ``target`` (a single level) or
    ``target_levels`` (a tuple, e.g. ``(100, 1000, 10000)``). When more than
    one level is given, each candidate is materialised as **one statistic
    object per level** (``ALTER STATISTICS ... SET STATISTICS``) and ALL levels
    are built by a SINGLE ANALYZE (sampling at the single-column target), then
    each (candidate, level) is measured independently by masking the others.

    ``single_col_target`` fixes the capacity of the *per-column* statistics
    (i.e. ``default_statistics_target``), which drive the baseline and the
    ANALYZE; it is deliberately decoupled from the extended statistics' own
    per-object targets (set via ``ALTER STATISTICS ... SET STATISTICS``).
    The default 10000 follows the paper's design: fixed, generous single-column
    baseline so that ext-stat improvements reflect column correlations only.
    This should always be recorded in results metadata so that two runs whose
    ``single_col_target`` differs are not incorrectly merged.

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

    max_level = max(levels)  # only used for the extended stats' own targets

    # deterministic single-column baseline at the FIXED single_col_target
    if single_col_target > 0:
        _set_target(conn, single_col_target)
    base = estimate_count_query(conn, query.sql, actual=query.ground_truth)
    mes = QueryMaskMeasurement(
        qid=query.qid,
        bench=query.bench,
        qerror_base=base.qerror if base.qerror is not None else float("nan"),
        estimate_base=base.estimate,
        actual=query.ground_truth,
        single_col_target=single_col_target,
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
                           levels, single_col_target, backup_table, mes,
                           multi=len(levels) > 1)

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
    single_col_target: int,
    backup_table: str,
    mes: QueryMaskMeasurement,
    *,
    multi: bool,
) -> None:
    """Create (optionally multi-level) stats on one table, ANALYZE once,
    mask-measure each (candidate, level).

    When ``multi`` is True, each candidate gets one statistic object PER level,
    each with ``ALTER STATISTICS ... SET STATISTICS <level>``; a SINGLE ANALYZE
    (sampling at the fixed ``single_col_target``) builds all of them. The
    per-column statistics (and hence the baseline and every EXPLAIN) are always
    measured at ``single_col_target`` (default 10000), independent of the
    extended statistics' own per-object targets. Each (candidate, level) is
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

        # ONE ANALYZE builds all objects (sampling at the fixed single-col target)
        if single_col_target > 0:
            _set_target(conn, single_col_target)
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
                if single_col_target > 0:
                    _set_target(conn, single_col_target)
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


def measure_table_workload_mask(
    conn: Connection,
    queries_and_cands: list[tuple[BenchQuery, list[CandidateSet]]],
    *,
    kind: str = "mcv",
    stat_prefix: str = "ext_",
    levels: tuple[int, ...] = (100, 1000, 10000),
    single_col_target: int = 10000,
    table: Optional[str] = None,
    backup_table: str = "_ext_workload_backup",
) -> list[QueryMaskMeasurement]:
    """Workload-wide Protocol-M: build ALL distinct (candidate x level)
    statistics on one shared table in a SINGLE ANALYZE, then measure every
    (query, candidate, level) independently by masking.

    Contrast with :func:`measure_query_mask`, which ANALYZEs once *per query*.
    Here the fixed ANALYZE base cost (table sampling + per-column statistics,
    see the module docstring "Scopes") is paid exactly once for the whole
    workload, at the price of one large ANALYZE over every distinct candidate
    simultaneously and no per-query failure isolation.

    ``single_col_target`` fixes the capacity of the per-column statistics /
    baseline (default 10000), decoupled from the extended stats' own targets.

    ``queries_and_cands`` must all share the same base table (pass ``table``
    to force one, e.g. Census ``climate`` / stats_CEB_single ``posts``).
    """
    if kind not in _MASKABLE_KINDS:
        raise ValueError(f"kind {kind!r} not maskable; use {sorted(_MASKABLE_KINDS)}")

    max_level = max(levels)

    # ---- 0) build the deduplicated (columns, level) universe across queries ----
    # object name -> (candidate, level); columns key -> candidate for lookup
    by_cols: dict[tuple, CandidateSet] = {}
    for _, cands in queries_and_cands:
        for c in cands:
            by_cols.setdefault(tuple(c.columns), c)
    dedup = sorted(by_cols.values(), key=lambda c: tuple(c.columns))

    tbl = None
    # determine the forced table from the (single) candidate table if not given
    if table is None:
        tabs = {_qualify_table(c.table) for _, cands in queries_and_cands for c in cands}
        if len(tabs) != 1:
            raise ValueError(
                f"workload-wide masking requires a single shared table; found {tabs}")
        tbl = tabs.pop()
    else:
        tbl = _qualify_table(table)

    def name_of(c: CandidateSet, lvl: int) -> str:
        return _stat_name_level(c, kind, stat_prefix, lvl)

    # ---- 1) create every distinct (candidate, level) object ----
    created: list[str] = []
    try:
        with conn.cursor() as cur:
            for c in dedup:
                for lvl in levels:
                    nm = name_of(c, lvl)
                    created.append(nm)
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                    cur.execute(f"CREATE STATISTICS {nm} ({kind}) ON "
                                f"{', '.join(c.columns)} FROM {tbl}")
                    if len(levels) > 1:
                        cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")

        # ---- 2) ONE ANALYZE builds all objects (sampling at the fixed
        #          single-column target; extended stats use their own targets) ----
        if single_col_target > 0:
            _set_target(conn, single_col_target)
        _analyze(conn, tbl)

        # snapshot all payload oids
        oids: dict[tuple, int] = {}
        for c in dedup:
            for lvl in levels:
                oids[(tuple(c.columns), lvl)] = _stat_oid(conn, name_of(c, lvl))
        _backup_payload(conn, backup_table, list(oids.values()), kind)

        # ---- 3) measure each query: baseline (all masked) + each (cand, level) ----
        results: list[QueryMaskMeasurement] = []
        for query, cands in queries_and_cands:
            # baseline: no extended stat active (all payloads NULL)
            _mask_payload_all_but(conn, backup_table, set(), kind)
            if single_col_target > 0:
                _set_target(conn, single_col_target)
            base = estimate_count_query(conn, query.sql, actual=query.ground_truth)
            mes = QueryMaskMeasurement(
                qid=query.qid, bench=query.bench,
                qerror_base=base.qerror if base.qerror is not None else float("nan"),
                estimate_base=base.estimate, actual=query.ground_truth,
                single_col_target=single_col_target)
            # per (candidate, level) isolation
            for c in cands:
                key_cols = tuple(c.columns)
                for lvl in levels:
                    keep = {oids[(key_cols, lvl)]}
                    _mask_payload_all_but(conn, backup_table, keep, kind)
                    res = estimate_count_query(conn, query.sql, actual=query.ground_truth)
                    key = f"{tbl}({','.join(c.columns)})"
                    qerr = res.qerror if res.qerror is not None else float("nan")
                    size = stat_size_bytes(conn, name_of(c, lvl), kind)
                    level_dict = mes.candidates.setdefault(key, {
                        "table": tbl, "columns": list(c.columns),
                        "kind": kind, "levels": {},
                    })
                    level_dict["levels"][int(lvl)] = {
                        "estimate": res.estimate, "qerror": qerr, "size_bytes": size,
                    }
                    _restore_payload(conn, backup_table, kind)
            results.append(mes)
        _drop_backup(conn, backup_table)
        return results
    finally:
        # always drop every created object + re-analyze to restore the table
        for nm in created:
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
        _drop_backup(conn, backup_table)
        _analyze(conn, tbl)

