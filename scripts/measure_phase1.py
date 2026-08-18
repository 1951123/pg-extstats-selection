#!/usr/bin/env python3
"""Phase-1 measurement: per-query baseline + per-candidate q-error (protocol A).

For every query of a benchmark we measure:
  - its baseline q-error (no extended statistics)
  - the q-error when *exactly one* candidate statistic is created on its base
    table (protocol A: create / analyze / explain / drop / analyze)

Results are written as JSON to ``results/`` for the phase-2 ILP solver.

Usage
-----
    source .venv/bin/activate
    python scripts/measure_phase1.py --bench stats_ceb --kind mcv

Options
-------
--bench NAME      census | job | stats_ceb | all            (default: stats_ceb)
--kind K          dependencies | ndistinct | mcv           (default: mcv)
--arities N[,N]   candidate combination sizes              (default: 2,3)
--limit N         only measure the first N queries (sanity run)
--out PATH        output JSON path (default results/phase1_<bench>_<kind>.json)
--pguser/--pghost/--pgport/--dbname  (defaults match benchmarks)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.config import DEFAULT_DB, DBConfig  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
)
from extstats.db import connect  # noqa: E402
from extstats.measure import measure_query  # noqa: E402

_PARSERS = {
    "census": parse_census_dir,
    "job": parse_job_dir,
    "stats_ceb": parse_stats_ceb_dir,
}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=list(_PARSERS) + ["all"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv",
                    choices=["dependencies", "ndistinct", "mcv"])
    ap.add_argument("--arities", default="2,3")
    ap.add_argument("--target-levels", default="100,1000,10000",
                    help="comma-separated statistics_target capacity levels")
    ap.add_argument("--repeats", type=int, default=1,
                    help="re-ANALYZE + re-measure each level K times "
                         "(records qerror_repeats for conservative selection)")
    ap.add_argument("--limit", type=int, default=0, help="measure only first N queries")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--dbname", default=None)
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    args = ap.parse_args(argv)

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    target_levels = tuple(int(x) for x in args.target_levels.split(",") if x.strip())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    benches = list(_PARSERS) if args.bench == "all" else [args.bench]

    for bench in benches:
        dbname = args.dbname or DEFAULT_DB[bench]
        cfg = DBConfig(host=args.pghost, port=args.pgport, user=args.pguser, dbname=dbname)

        queries = _PARSERS[bench](bench_root / _BENCH_DIRS[bench] / "queries")
        if args.limit:
            queries = queries[: args.limit]

        per_query_cands = generate_candidates_per_query(queries, arities=arities)
        total_cands = sum(len(v) for v in per_query_cands.values())

        print(f"=== phase1 {bench} (db={dbname}, kind={args.kind}) ===")
        print(f"queries={len(queries)}  total_candidates={total_cands}")

        results: list[dict] = []
        t_start = datetime.now()
        measured = 0
        with connect(cfg) as conn:
            conn.autocommit = True
            for q in queries:
                cands = per_query_cands.get(q.qid, [])
                mes = measure_query(conn, q, cands, kind=args.kind,
                                    target_levels=target_levels, repeats=args.repeats)
                results.append(
                    {
                        "qid": mes.qid,
                        "actual": mes.actual,
                        "qerror_base": mes.qerror_base,
                        "estimate_base": mes.estimate_base,
                        "target_levels": list(target_levels),
                        "candidates": mes.candidates,
                    }
                )
                measured += 1
                if measured % 20 == 0 or measured == len(queries):
                    print(f"  measured {measured}/{len(queries)} "
                          f"({(datetime.now()-t_start).total_seconds():.1f}s)")

        out = args.out or f"results/phase1_{bench}_{args.kind}.json"
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "bench": bench,
                    "kind": args.kind,
                    "arities": list(arities),
                    "n_queries": len(results),
                    "n_candidates": total_cands,
                    "elapsed_s": (datetime.now() - t_start).total_seconds(),
                    "results": results,
                },
                indent=2,
            )
        )
        print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
