"""Workload-wide Protocol-M phase-1 measurement.

Builds ALL distinct (candidate x level) statistics on a single shared table in
ONE ANALYZE, then measures every (query, candidate, level) independently by
masking. This pays the fixed ANALYZE base cost once for the whole workload
(see extstats.measure_mask._measure_table_workload_mask / the "Scopes" note),
contrast the per-query measure_phase1_mask.py.

Usage:
  python scripts/measure_workload_mask.py --bench stats_ceb_single --kind mcv \
      --target-levels 100,1000,10000 --out results/phase1_stats_ceb_single_wl.json
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path
from psycopg import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DEFAULT_DB, DBConfig      # noqa: E402
from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.parsers import (                        # noqa: E402
    parse_census_dir, parse_job_dir, parse_job_light_dir,
    parse_job_light_full_dir, parse_stats_ceb_dir, parse_stats_ceb_single_dir,
)
from extstats.db import connect                        # noqa: E402
from extstats.measure_mask import measure_table_workload_mask  # noqa: E402

_PARSERS = {
    "census": parse_census_dir, "job": parse_job_dir,
    "job_light": parse_job_light_dir, "job_light_full": parse_job_light_full_dir,
    "stats_ceb": parse_stats_ceb_dir, "stats_ceb_single": parse_stats_ceb_single_dir,
}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB",
               "stats_ceb_single": "stats_CEB"}
_SINGLE_TABLE = {"census": "climate"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--arities", default="2,3")
    ap.add_argument("--target-levels", default="100,1000,10000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--table", help="force/restrict measurement to this one "
                                    "table (e.g. 'posts'); items whose candidates "
                                    "are on other tables are skipped")
    ap.add_argument("--dbname")
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", default=5432)
    args = ap.parse_args()

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    levels = tuple(int(x) for x in args.target_levels.split(",") if x.strip())

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    forced_table = _SINGLE_TABLE.get(args.bench)
    dbname = args.dbname or DEFAULT_DB[args.bench]
    cfg = DBConfig(host=args.pghost, port=args.pgport, user=args.pguser,
                   dbname=dbname)

    per_query_cands = generate_candidates_per_query(queries, arities=arities)
    items = [(q, per_query_cands.get(q.qid, [])) for q in queries]
    # optional single-table restriction: keep items whose candidates all live on
    # the requested table (skip items with candidates on other tables)
    if args.table:
        tname = args.table
        # candidate.table carries a leading dot (".posts"); strip it for compare
        items = [(q, c) for q, c in items
                 if all(cc.table.lstrip(".") == tname for cc in c)]
        forced_table = tname if "." not in tname else tname.split(".")[-1]
    total_cands = sum(len(c) for _, c in items)
    print(f"=== workload-wide [{args.bench}] (db={dbname}, kind={args.kind}, "
          f"levels={levels}) ===") 
    print(f"queries={len(items)}  candidates(total)={total_cands}  table={forced_table}")

    t_start = datetime.now()
    with connect(cfg) as conn:
        conn.autocommit = True
        results = measure_table_workload_mask(
            conn, items, kind=args.kind, levels=levels, table=forced_table)
    elapsed = (datetime.now() - t_start).total_seconds()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "bench": args.bench, "kind": args.kind, "arities": list(arities),
        "target_levels": list(levels),
        "n_queries": len(results),
        "n_candidates": total_cands,
        "scope": "workload-wide (single ANALYZE)",
        "elapsed_s": elapsed,
        "results": [{ "qid": r.qid, "actual": r.actual,
                      "qerror_base": r.qerror_base, "estimate_base": r.estimate_base,
                      "target_levels": list(levels), "candidates": r.candidates }
                    for r in results],
    }, indent=2))
    print(f"measured {len(results)}/{len(queries)} in {elapsed:.1f}s; wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
