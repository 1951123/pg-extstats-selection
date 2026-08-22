"""Empirically verify that finer-grained (two-level) batching is faster than
one-shot per-query (b=1) for a high-candidate query.

Compares, on query.335 (455 candidates), L=1, target=1000:
  scheme A (b=1): mount ALL 455 cands, ONE ANALYZE, measure all 455 cells
  scheme B (b=S): split into S sub-batches, ANALYZE once per sub-batch,
                  measure that sub-batch's cells (finer granularity, less mask)

Measures real wall-clock per scheme to test the model prediction that b>1
reduces mask cost (mask ~ 1/b) at the price of more ANALYZEs.
"""
import sys, time
from pathlib import Path
from psycopg import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig
from extstats.db import connect as db_connect
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query, CandidateSet
from extstats.measure_mask import (
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_oid, _stat_name_level,
)
from extstats.stats import _qualify_table

_TABLE = "climate"; _KIND = "mcv"; _QID = "query.335"; TARGET = 1000


def measure_subbatch(conn, backup_table, cands, tbl, level):
    """Mount cands, one ANALYZE, measure all cells, cleanup. Returns wall time."""
    L = 1
    created = []
    for key in cands:
        cand = CandidateSet(table=tbl, columns=list(key))
        nm = _stat_name_level(cand, _KIND, "ext_", level)
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
            cur.execute(f"CREATE STATISTICS {nm} ({_KIND}) ON {', '.join(key)} FROM {tbl}")
        created.append(nm)
    t0 = time.perf_counter(); _analyze(conn, tbl); t_an = time.perf_counter()-t0
    oids = [_stat_oid(conn, nm) for nm in created]
    _drop_backup(conn, backup_table); _backup_payload(conn, backup_table, oids, _KIND)
    keep = {oids[0]}
    t1 = time.perf_counter()
    for i, key in enumerate(cands):
        keep = {oids[i]}
        _mask_payload_all_but(conn, backup_table, keep, _KIND)
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) SELECT count(*) FROM {tbl} WHERE {key[0]}=45")
            cur.fetchone()
        _restore_payload(conn, backup_table, _KIND)
    t_meas = time.perf_counter()-t1
    for nm in created:
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
    _drop_backup(conn, backup_table)
    _analyze(conn, tbl)
    return t_an, t_meas


def main():
    queries = parse_census_dir(Path(__file__).resolve().parents[1] / "benchmarks/Census/queries")
    mp = generate_candidates_per_query(queries, arities=(2, 3))
    colkeys = [tuple(c.columns) for c in mp[_QID]]
    n = len(colkeys)
    tbl = _qualify_table(_TABLE)
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")
    print(f"query {_QID}: {n} candidates, L=1, target={TARGET}")

    with db_connect(cfg) as conn:
        conn.autocommit = True
        _set_target(conn, TARGET)

        # --- Scheme A: b=1, one-shot all candidates ---
        an, me = measure_subbatch(conn, "_bkA", colkeys, tbl, TARGET)
        print(f"\nscheme A (b=1, all {n} in one batch):")
        print(f"  1 ANALYZE={an:.1f}s, measure all={me:.1f}s, TOTAL={an+me:.1f}s")

        # --- Scheme B: split into S sub-batches ---
        import math
        for S in [3, 7]:
            size = math.ceil(n/S)
            batches = [colkeys[i*size:(i+1)*size] for i in range(S)]
            batches = [b for b in batches if b]
            tot_an=tot_me=0
            t=time.perf_counter()
            for bi, b in enumerate(batches):
                a,m = measure_subbatch(conn, f"_bkB{bi}", b, tbl, TARGET)
                tot_an+=a; tot_me+=m
            print(f"\nscheme B (S={len(batches)} sub-batches, ~{size} cands each):")
            print(f"  ANALYZE total={tot_an:.1f}s, measure total={tot_me:.1f}s, TOTAL={tot_an+tot_me:.1f}s")

    print("\ncleanup done (0 stats, re-analyzed)")


if __name__ == "__main__":
    main()
