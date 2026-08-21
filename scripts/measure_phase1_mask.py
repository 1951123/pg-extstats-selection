"""Phase-1 measurement using the catalog-mask protocol (Protocol-M).

Identical CLI surface to ``measure_phase1.py``, but each query is measured
with ``extstats.measure_mask.measure_query_mask``: all candidate statistics
for a table are built in a SINGLE ANALYZE and each candidate's independent
q-error is obtained by masking the others (NULL-ing ``pg_statistic_ext_data``).
This makes per-candidate deterministic measurement feasible on wide tables
like Census ``climate``. The default build ``--target`` is 1000 (the
cost/quality sweet spot); pass ``--target 10000`` for exact deterministic
verification (slow ANALYZE on wide tables).

Output JSON is shaped like ``measure_phase1.py``: per-query
``{qid, actual, qerror_base, estimate_base, target_levels, candidates}`` where
each candidate carries a single capacity level (the build ``target``).

Usage
-----
    source .venv/bin/activate
    python scripts/measure_phase1_mask.py \
        --bench census --kind mcv --target 1000 --arities 2,3 \
        --limit 40 --out results/phase1_census_mask.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.config import DEFAULT_DB, DBConfig, RECOMMENDED_STATS_TARGET  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_job_light_dir,
    parse_job_light_full_dir,
    parse_stats_ceb_dir,
    parse_stats_ceb_single_dir,
)
from extstats.db import connect  # noqa: E402
from extstats.measure_mask import measure_query_mask  # noqa: E402

_PARSERS = {
    "census": parse_census_dir,
    "job": parse_job_dir,
    "job_light": parse_job_light_dir,
    "job_light_full": parse_job_light_full_dir,
    "stats_ceb": parse_stats_ceb_dir,
    "stats_ceb_single": parse_stats_ceb_single_dir,
}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB",
               "stats_ceb_single": "stats_CEB"}
# job-light sub-plans live in the End-to-End-CardEst-Benchmark repo, not benchmarks/.
_JOB_LIGHT_SUBPLAN_DIR = (
    Path(__file__).resolve().parents[1]
    / "End-to-End-CardEst-Benchmark" / "workloads" / "job-light" / "sub_plan_queries"
)
_JOB_LIGHT_DIR = (
    Path(__file__).resolve().parents[1]
    / "End-to-End-CardEst-Benchmark" / "workloads" / "job-light"
)
# persisted true cardinalities for the full 70-query job-light set
_JOB_LIGHT_TRUTH = (
    Path(__file__).resolve().parents[1] / "results" / "job_light_truth.json"
)# single-table benchmark -> force the shared table per query
_SINGLE_TABLE = {"census": "climate"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=list(_PARSERS) + ["all"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv",
                    choices=["dependencies", "ndistinct", "mcv"])
    ap.add_argument("--arities", default="2,3")
    ap.add_argument("--target", type=int, default=None,
                    help="single default_statistics_target (default uses "
                         "RECOMMENDED_STATS_TARGET). Mutually exclusive with "
                         "--target-levels.")
    ap.add_argument("--target-levels", default=None,
                    help="comma-separated capacity levels to measure ALL equal,"
                         " e.g. 100,1000,10000 (multi-object one-ANALYZE scheme).")
    ap.add_argument("--limit", type=int, default=0, help="measure only first N queries")
    ap.add_argument("--qids", default="",
                    help="comma-separated qids to measure (overrides --limit)")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--dbname", default=None)
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    args = ap.parse_args(argv)

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    if args.target_levels:
        level_list = tuple(int(x) for x in args.target_levels.split(",") if x.strip())
        target_arg = None
    else:
        level_list = (args.target,) if args.target is not None else (RECOMMENDED_STATS_TARGET,)
        target_arg = level_list[0]
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    benches = list(_PARSERS) if args.bench == "all" else [args.bench]

    for bench in benches:
        dbname = args.dbname or DEFAULT_DB[bench]
        cfg = DBConfig(host=args.pghost, port=args.pgport, user=args.pguser, dbname=dbname)
        forced_table = _SINGLE_TABLE.get(bench)

        if bench == "job_light":
            queries = _PARSERS[bench](_JOB_LIGHT_SUBPLAN_DIR)
        elif bench == "job_light_full":
            truth = {}
            if _JOB_LIGHT_TRUTH.exists():
                truth = json.loads(_JOB_LIGHT_TRUTH.read_text())["truth"]
            queries = parse_job_light_full_dir(_JOB_LIGHT_DIR, truth=truth)
            # attach a locally-forced dbname (same imdb schema)
            dbname = args.dbname or "imdb"
            cfg = DBConfig(host=args.pghost, port=args.pgport,
                           user=args.pguser, dbname=dbname)
        else:
            queries = _PARSERS[bench](bench_root / _BENCH_DIRS[bench] / "queries")
        if args.qids:
            want = {x.strip() for x in args.qids.split(",") if x.strip()}
            queries = [q for q in queries if q.qid in want]
        elif args.limit:
            queries = queries[: args.limit]

        per_query_cands = generate_candidates_per_query(queries, arities=arities)
        total_cands = sum(len(v) for v in per_query_cands.values())

        print(f"=== phase1[mask] {bench} (db={dbname}, kind={args.kind}, "
              f"levels={level_list}) ===")
        print(f"queries={len(queries)}  total_candidates={total_cands}")

        results: list[dict] = []
        t_start = datetime.now()
        measured = 0
        with connect(cfg) as conn:
            conn.autocommit = True
            for qi, q in enumerate(queries):
                cands = per_query_cands.get(q.qid, [])
                # unique backup table per query (avoids stale temp table reuse)
                backup_table = f"_ext_mask_backup_{qi}"
                mes = measure_query_mask(
                    conn, q, cands, kind=args.kind, target=target_arg,
                    target_levels=level_list if len(level_list) > 1 else None,
                    table=forced_table, backup_table=backup_table)
                results.append({
                    "qid": mes.qid,
                    "actual": mes.actual,
                    "qerror_base": mes.qerror_base,
                    "estimate_base": mes.estimate_base,
                    "target_levels": list(level_list),
                    "candidates": mes.candidates,
                })
                measured += 1
                if measured % 20 == 0 or measured == len(queries):
                    print(f"  measured {measured}/{len(queries)} "
                          f"({(datetime.now()-t_start).total_seconds():.1f}s)")

        out = args.out or f"results/phase1_{bench}_{args.kind}_mask.json"
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "bench": bench,
            "kind": args.kind,
            "arities": list(arities),
            "target_levels": list(level_list),
            "n_queries": len(results),
            "n_candidates": total_cands,
            "elapsed_s": (datetime.now() - t_start).total_seconds(),
            "results": results,
        }, indent=2))
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
