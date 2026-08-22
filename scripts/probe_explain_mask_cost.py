"""Probe the per-candidate MEASUREMENT cost of Protocol-M (the epsilon term).

The naive ANALYZE cost model is in probe_census_analyze_scale.py. Here we measure
the OTHER piece: the per-(candidate, level) measure loop, which is
    mask(keep one payload; NULL all others) -> EXPLAIN -> restore(all payloads)
i.e. the ``epsilon`` in "Protocol-M costs B + (c + epsilon) N".

The implementation (extstats/measure_mask.measure_table_workload_mask step 3)
does, per (candidate, level):
    _mask_payload_all_but  : N_total UPDATEs over pg_statistic_ext_data (NULL all but 1)
    estimate_count_query   : EXPLAIN (FORMAT JSON)
    _restore_payload       : one UPDATE ... FROM over all payloads

So epsilon = t_mask + t_explain + t_restore, and t_mask/t_restore grow with the
TOTAL number of masked payloads N_total on the table (not just this query's
candidates). We sweep N_total on CENSUS climate and time each sub-step.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/probe_explain_mask_cost.py \
      [--levels 100,1000,10000] [--n-stats 100,200,500,1000,2000,5000] [--repeat 3]
"""
import argparse, json, sys, time
from pathlib import Path
from psycopg import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig                         # noqa: E402
from extstats.db import connect                              # noqa: E402
from extstats.measure_mask import (                          # noqa: E402
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_oid, _stat_name_level,
    _KIND_DATA_COL,
)
from extstats.candidates import CandidateSet, generate_candidates  # noqa: E402
from extstats.parsers import parse_census_dir                 # noqa: E402
from extstats.stats import _qualify_table                     # noqa: E402

_DB = "census"
_KIND = "mcv"
_COL = _KIND_DATA_COL[_KIND]


def timed_steps(conn, backup_table, keep_oid, kind, col, explain_sql):
    t = time.perf_counter()
    _mask_payload_all_but(conn, backup_table, keep_oid, kind)
    t_mask = time.perf_counter() - t

    t = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + explain_sql)
        cur.fetchone()
    t_explain = time.perf_counter() - t

    t = time.perf_counter()
    _restore_payload(conn, backup_table, kind)
    t_restore = time.perf_counter() - t
    return t_mask, t_explain, t_restore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="100,1000,10000")
    ap.add_argument("--n-stats", default="100,200,500,1000,2000,5000")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--table", default="climate")
    ap.add_argument("--dbname", default=_DB)
    args = ap.parse_args()
    levels = tuple(int(x) for x in args.levels.split(",") if x.strip())
    n_stats_list = [int(x) for x in args.n_stats.split(",") if x.strip()]
    tbl = _qualify_table(args.table)
    kind = _KIND

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = parse_census_dir(bench_root / "Census" / "queries")
    # distinct (table, columns) candidates across the workload
    cands = generate_candidates(queries, arities=(2, 3), dedupe=True)
    colkeys = [c.columns for c in cands if c.table_unqualified == args.table]
    if not colkeys:
        raise SystemExit(f"no candidates on table {args.table}")
    # a valid predicate column for the EXPLAIN probe
    explain_col = colkeys[0][0]
    explain_sql = f"SELECT count(*) FROM {tbl} WHERE {explain_col} = 16"

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.dbname)
    results = {"table": tbl, "kind": kind, "levels": list(levels),
               "repeat": args.repeat, "explain_sql": explain_sql,
               "per_candidate_by_component_s": []}

    with connect(cfg) as conn:
        conn.autocommit = True
        _set_target(conn, max(levels))
        backup_table = "_probe_bk"

        for n_want in n_stats_list:
            # ---- create n_want distinct (candidate x level) objects ----
            created = []
            for idx in range(n_want):
                key = colkeys[idx % len(colkeys)]
                lvl = levels[idx % len(levels)]
                cand = CandidateSet(table=tbl, columns=list(key))
                nm = _stat_name_level(cand, kind, "ext_", lvl)
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                    cur.execute(f"CREATE STATISTICS {nm} ({kind}) ON "
                                f"{', '.join(key)} FROM {tbl}")
                    if len(levels) > 1:
                        cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
                created.append(nm)
            n_total = len(created)

            # ---- ONE ANALYZE builds all ----
            _analyze(conn, tbl)

            # snapshot payload oids + backup
            oids = [_stat_oid(conn, nm) for nm in created]
            _drop_backup(conn, backup_table)
            _backup_payload(conn, backup_table, oids, kind)

            # ---- time mask / explain / restore on one (candidate, level) ----
            keep_oid = {oids[0]}
            row = {"n_total": n_total, "runs": []}
            for r in range(args.repeat):
                tm, te, tr = timed_steps(
                    conn, backup_table, keep_oid, kind, _COL, explain_sql)
                row["runs"].append({"mask_s": round(tm, 6),
                                    "explain_s": round(te, 6),
                                    "restore_s": round(tr, 6),
                                    "total_s": round(tm + te + tr, 6)})
            results["per_candidate_by_component_s"].append(row)
            m = row["runs"][-1]
            mean = sum(x["total_s"] for x in row["runs"]) / len(row["runs"])
            print(f"N_total={n_total:>6}: mask={m['mask_s']:.4f}s "
                  f"explain={m['explain_s']:.4f}s restore={m['restore_s']:.4f}s "
                  f"total={m['total_s']:.4f}s  (mean {mean:.4f}s)")

            # cleanup: drop stats, restore table
            for nm in created:
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
            _drop_backup(conn, backup_table)
            _analyze(conn, tbl)

    out = Path("results/probe_explain_mask_cost.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
