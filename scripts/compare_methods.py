#!/usr/bin/env python3
"""Compare selection methods (M1-M4) on a phase-1 results JSON.

Each method picks a budgeted set of extended statistics to create. We then
*verify* against the live PostgreSQL by actually building the chosen statistics
and measuring the TRUE mean q-error, and compare it to the method's predicted
(approximation) value — showing how optimistic/pessimistic each approximation is.

Methods
-------
M1  linear multiplicative ILP   (scipy.milp, log-space linear objective)
M2  pairwise (2nd-order) ILP     (quadratic term linearised; TODO)
M3  bounded powerset per query   (exact within top-k combos; TODO)
M4  greedy + incremental re-test (build, re-measure, keep if improves; TODO)

For now M1 is fully wired; the driver is structured so M2-M4 slot in.

Usage
-----
    source .venv/bin/activate
    python scripts/compare_methods.py \
        --input results/phase1_stats_ceb_mcv.json \
        --budget 500000 --bench stats_ceb
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.optimize import build_problem, solve_ilp  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
)
from extstats.verify import StatToBuild, verify_statistics  # noqa: E402

_PARSERS = {"census": parse_census_dir, "job": parse_job_dir, "stats_ceb": parse_stats_ceb_dir}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB"}


def run_method_m1(phys_stats, queries_options, qerror_base, budget, kind):
    """M1: linear multiplicative ILP. Returns (selected stats, predicted qerrors)."""
    t0 = time.time()
    res = solve_ilp(phys_stats, queries_options, qerror_base, budget)
    dt = time.time() - t0
    # selected PhysicalStat -> StatToBuild for verification
    to_build = [
        StatToBuild(table=ps.table, columns=ps.columns, level=ps.level, kind=kind)
        for ps in res.selected_stats
    ]
    return {
        "name": "M1_linear_ILP",
        "selected_stats": to_build,
        "predicted_qerror_per_query": res.qerror_per_query,
        "predicted_mean": res.mean_qerror,
        "n_stats": len(res.selected_stats),
        "used_bytes": res.total_bytes,
        "solve_s": dt,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_stats_ceb_mcv.json")
    ap.add_argument("--budget", type=int, default=500000)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--methods", default="M1",
                    help="comma list of methods to run (M1,M2,M3,M4)")
    ap.add_argument("--out", default="results/method_comparison.json")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    phase1 = json.loads(input_path.read_text())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")

    phys_stats, queries_options, qerror_base = build_problem(phase1)
    base_mean = float(np.mean(qerror_base))
    m = len(qerror_base)

    print(f"=== method comparison ({args.bench}, kind={args.kind}, budget={args.budget}) ===")
    print(f"queries={m}  physical_stats={len(phys_stats)}  baseline_mean_qerr={base_mean:.3f}")

    methods = [x.strip() for x in args.methods.split(",") if x.strip()]
    results = []
    # Map bench key -> actual database name (matches init_*.sh / config.DEFAULT_DB).
    dbname = {"census": "census", "job": "imdb", "stats_ceb": "stats"}[args.bench]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=dbname)

    for method in methods:
        if method.upper() == "M1":
            out = run_method_m1(phys_stats, queries_options, qerror_base, args.budget, args.kind)
        else:
            raise NotImplementedError(f"Method {method} not implemented yet")
        name = out["name"]

        print(f"\n--- {name}: predicted_mean={out['predicted_mean']:.3f}, "
              f"{out['n_stats']} stats, {out['used_bytes']}B, solve {out['solve_s']:.2f}s")

        # verify against real PG
        t0 = time.time()
        with connect(cfg) as conn:
            conn.autocommit = True
            vr = verify_statistics(
                conn,
                queries,
                out["selected_stats"],
                predicted_per_query=out["predicted_qerror_per_query"],
            )
        dt = time.time() - t0
        print(f"   REAL   mean q-error = {vr.mean_qerror:.3f} (base {vr.baseline_mean_qerror:.3f})")
        if vr.predicted_mean_qerror is not None:
            ratio = vr.mean_qerror / vr.predicted_mean_qerror if vr.predicted_mean_qerror else float("inf")
            print(f"   pred = {vr.predicted_mean_qerror:.3f}  ->  approx_vs_real ratio = {ratio:.3f} "
                  f"({'>1 optimistic' if ratio > 1.05 else ('<1 pessimistic' if ratio < 0.95 else '~accurate')})")
        print(f"   verify took {dt:.1f}s")

        results.append(
            {
                "method": name,
                "predicted_mean_qerror": out["predicted_mean"],
                "real_mean_qerror": vr.mean_qerror,
                "baseline_mean_qerror": vr.baseline_mean_qerror,
                "median_qerror": vr.median_qerror,
                "approx_vs_real_ratio": (
                    vr.mean_qerror / vr.predicted_mean_qerror if vr.predicted_mean_qerror else None
                ),
                "n_stats": out["n_stats"],
                "used_bytes": out["used_bytes"],
                "solve_s": out["solve_s"],
                "verify_s": dt,
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "bench": args.bench,
                "kind": args.kind,
                "budget_bytes": args.budget,
                "baseline_mean_qerror": base_mean,
                "results": results,
            },
            indent=2,
        )
    )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
