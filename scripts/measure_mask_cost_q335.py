"""Measure the REAL per-measurement mask cost for query.335 (455 candidates).

Directly answers: "is mask really that expensive for the most-candidate query
at L=3?" Instead of trusting the probe's fitted mu, we run the ACTUAL mask cycle
(_mask_payload_all_but -> EXPLAIN -> _restore_payload) as in the real harness
and time it, at both L=1 (455 payloads) and L=3 (1365 payloads).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/measure_mask_cost_q335.py [--levels 1000]
"""
import argparse, sys, time
from pathlib import Path
from psycopg import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig
from extstats.db import connect as db_connect
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query
from extstats.measure_mask import (
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_oid, _stat_name_level, _KIND_DATA_COL,
)
from extstats.candidates import CandidateSet
from extstats.stats import _qualify_table

_TABLE = "climate"
_KIND = "mcv"
_QID = "query.335"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1000", help="e.g. 1000 -> L=1; 100,1000,10000 -> L=3")
    ap.add_argument("--n-cycles", type=int, default=5)
    args = ap.parse_args()
    levels = tuple(int(x) for x in args.levels.split(",") if x.strip())
    tbl = _qualify_table(_TABLE)

    queries = parse_census_dir(Path(__file__).resolve().parents[1] / "benchmarks/Census/queries")
    mp = generate_candidates_per_query(queries, arities=(2, 3))
    cands = mp[_QID]
    colkeys = [tuple(c.columns) for c in cands]
    L = len(levels)
    N_payload = len(colkeys) * L
    print(f"query {_QID}: {len(cands)} candidates, levels={levels} (L={L}), mounted payloads={N_payload}")

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")
    with db_connect(cfg) as conn:
        conn.autocommit = True
        _set_target(conn, max(levels))
        backup_table = "_q335_bk"

        # ---- build all (cand x level) objects ----
        created = []
        for idx, key in enumerate(colkeys):
            for lvl in levels:
                cand = CandidateSet(table=tbl, columns=list(key))
                nm = _stat_name_level(cand, _KIND, "ext_", lvl)
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                    cur.execute(f"CREATE STATISTICS {nm} ({_KIND}) ON {', '.join(key)} FROM {tbl}")
                    if L > 1:
                        cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
                created.append(nm)
        print(f"created {len(created)} objects")

        # ---- ONE ANALYZE builds all ----
        t0 = time.perf_counter(); _analyze(conn, tbl); t_an = time.perf_counter()-t0
        print(f"1 ANALYZE (at t={max(levels)}): {t_an:.1f}s")

        # oids + backup
        oids = [_stat_oid(conn, nm) for nm in created]
        _drop_backup(conn, backup_table)
        _backup_payload(conn, backup_table, oids, _KIND)

        # ---- time N real mask cycles ----
        mask_t, exp_t, res_t, tot_t = [], [], [], []
        keep = {oids[0]}
        explain_sql = "SELECT count(*) FROM climate WHERE dage=45 AND dancstry2=0"
        for _ in range(args.n_cycles):
            t = time.perf_counter(); _mask_payload_all_but(conn, backup_table, keep, _KIND); mask_t.append(time.perf_counter()-t)
            t = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute("EXPLAIN (FORMAT JSON) " + explain_sql); cur.fetchone()
            exp_t.append(time.perf_counter()-t)
            t = time.perf_counter(); _restore_payload(conn, backup_table, _KIND); res_t.append(time.perf_counter()-t)
            tot_t.append(mask_t[-1]+exp_t[-1]+res_t[-1])

        print(f"\nPer (cand,level) REAL measurement cycle (mean over {args.n_cycles}):")
        print(f"  mask   : {sum(mask_t)/len(mask_t)*1000:.2f} ms")
        print(f"  explain: {sum(exp_t)/len(exp_t)*1000:.2f} ms")
        print(f"  restore: {sum(res_t)/len(res_t)*1000:.2f} ms")
        print(f"  TOTAL  : {sum(tot_t)/len(tot_t)*1000:.2f} ms  (={sum(tot_t)/len(tot_t)*N_payload:.1f}s for all {N_payload} measurements)")
        print(f"  fitted model mu=0.0008 s/payload x {N_payload} = {0.0008*N_payload*1000:.1f} ms")

        # cleanup
        for nm in created:
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
        _drop_backup(conn, backup_table)
        _analyze(conn, tbl)
        print("\ncleanup done (0 stats, re-analyzed)")


if __name__ == "__main__":
    main()
