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
    table: Optional[str] = None,
    backup_table: str = "_ext_mask_backup",
) -> QueryMaskMeasurement:
    """Measure ``query`` with the catalog-mask protocol.

    ``target`` (optional) is the single ``default_statistics_target`` used to
    build statistics (pass 10000 for a deterministic full-table build), and
    also sets the single-column baseline. ``table`` forces a base table for
    all candidates (use when every candidate shares one table, e.g. Census
    ``climate``); otherwise each candidate's own table is used (grouped, one
    ANALYZE each). ``backup_table`` must be unique per concurrent session.

    All statistics are created then built with ONE ANALYZE per table; each
    candidate is measured independently by masking the others. Created stats
    are dropped and tables re-ANALYZEd at the end (database restored).
    """
    if kind not in _MASKABLE_KINDS:
        raise ValueError(
            f"kind {kind!r} not maskable; use one of {sorted(_MASKABLE_KINDS)}")

    if target is not None:
        _set_target(conn, target)
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
                           target, backup_table, mes)

    # drop all + re-analyze to restore
    for tbl, cands in by_table.items():
        for c in cands:
            with conn.cursor() as cur:
                cur.execute(
                    f"DROP STATISTICS IF EXISTS {stat_name(c, kind, stat_prefix)}")
        _analyze(conn, tbl)
    _drop_backup(conn, backup_table)
    return mes


def _measure_one_table(
    conn: Connection,
    query: BenchQuery,
    tbl: str,
    cands: list[CandidateSet],
    kind: str,
    stat_prefix: str,
    target: Optional[int],
    backup_table: str,
    mes: QueryMaskMeasurement,
) -> None:
    """Create all stats on one table, ANALYZE once, mask-measure each candidate."""
    for c in cands:
        name = stat_name(c, kind, stat_prefix)
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            cur.execute(f"CREATE STATISTICS {name} ({kind}) ON "
                        f"{', '.join(c.columns)} FROM {tbl}")
    level = target if target is not None else 0
    if target is not None:
        _set_target(conn, target)
    _analyze(conn, tbl)

    oids = {c: _stat_oid(conn, stat_name(c, kind, stat_prefix)) for c in cands}
    _backup_payload(conn, backup_table, list(oids.values()), kind)

    for c in cands:
        _mask_payload_all_but(conn, backup_table, {oids[c]}, kind)
        if target is not None:
            _set_target(conn, target)
        res = estimate_count_query(conn, query.sql, actual=query.ground_truth)
        key = f"{tbl}({','.join(c.columns)})"
        qerr = res.qerror if res.qerror is not None else float("nan")
        size = stat_size_bytes(conn, stat_name(c, kind, stat_prefix), kind)
        mes.candidates[key] = {
            "table": tbl,
            "columns": list(c.columns),
            "kind": kind,
            "levels": {int(level): {
                "estimate": res.estimate,
                "qerror": qerr,
                "size_bytes": size,
            }},
        }
        _restore_payload(conn, backup_table, kind)
    _drop_backup(conn, backup_table)
