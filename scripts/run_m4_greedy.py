#!/usr/bin/env python3
"""M4: greedy selection with real-PG incremental re-testing.

This method does NOT use the multiplicative approximation. It greedily adds
candidate statistics to a working set, and after building that set actually
re-measures the workload's mean q-error on a live PostgreSQL. Statistics that
improve the real error (per unit storage) are kept; others are skipped. The
final set's q-error is therefore measured, not approximated — providing a
clean control against M1's multiplicative approximation.

Algorithm
---------
1. Candidate stats + costs come from phase-1 JSON. An ordering uses the phase-1
   single-stat q-errors ONLY to visit promising stats first (not to decide).
2. Greedy loop:
     S = working set (starts empty), E = real mean q-error of S (measured).
     for candidate s in order:
        if cost(S∪{s}) > budget: skip.
        build S∪{s} on PG and measure real mean q-error E'.
        if E' < E*(1-eps): accept s (S = S∪{s}, E = E').
3. Return S and its real mean q-error.

Because every acceptance is based on a REAL incremental re-test, no
approximation error is involved (up to the greedy ordering). This isolates
whether the discrepancies seen in M1 come from the stage-2 multiplicative
modelling rather than the pipeline.

Usage
-----
    source .venv/bin/activate
    python scripts/run_m4_greedy.py --input results/phase1_stats_ceb_mcv.json \
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
from extstats.optimize import build_problem  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
)
from extstats.verify import StatToBuild, verify_statistics  # noqa: E402

_PARSERS = {"census": parse_census_dir, "job": parse_job_dir, "stats_ceb": parse_stats_ceb_dir}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB"}
_DB = {"census": "census", "job": "imdb", "stats_ceb": "stats"}

# Improvement threshold: accept only if real mean q-error drops by >= this
# relative amount (guards against tiny/noise fluctuations).
EPS_REL = 1e-4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_stats_ceb_mcv.json")
    ap.add_argument("--budget", type=int, default=500000)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--max-iters", type=int, default=100,
                    help="max accepted stats (greedy steps)")
    ap.add_argument("--out", default="results/m4_greedy.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")

    phys_stats, queries_options, qerror_base = build_problem(phase1)

    # Order candidates by aggregate phase-1 improvement (ordering heuristic only,
    # not used to decide final acceptance — that is done by real re-testing).
    from collections import defaultdict
    help_score = defaultdict(float)
    for q_idx, opts in enumerate(queries_options):
        qb = qerror_base[q_idx]
        for o in opts:
            if o.qerror < qb:
                help_score[o.stat_index] += (qb - o.qerror)
    order = sorted(range(len(phys_stats)), key=lambda s: -help_score.get(s, 0.0))

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=_DB[args.bench])
    print(f"=== M4 greedy (real re-test), bench={args.bench}, budget={args.budget} ===")
    print(f"queries={len(queries)}  phys_stats={len(phys_stats)}")

    selected: list[StatToBuild] = []
    selected_idx: set[int] = set()
    selected_cost = 0

    # Baseline (empty set) real measurement.
    with connect(cfg) as conn:
        conn.autocommit = True
        vr0 = verify_statistics(conn, queries, [])
        cur_real = vr0.mean_qerror
    base_real = cur_real
    print(f"  baseline real mean q-error = {base_real:.3f}")

    for step in range(1, args.max_iters + 1):
        improved = False
        for s_idx in order:
            if s_idx in selected_idx:
                continue
            ps = phys_stats[s_idx]
            if selected_cost + ps.cost > args.budget:
                continue
            trial = selected + [StatToBuild(table=ps.table, columns=ps.columns,
                                            level=ps.level, kind=args.kind)]
            with connect(cfg) as conn:
                conn.autocommit = True
                vr = verify_statistics(conn, queries, trial)
            new_real = vr.mean_qerror
            if new_real < cur_real * (1.0 - EPS_REL):
                # Accept: keep this statistic in the working set.
                selected = trial
                selected_idx.add(s_idx)
                selected_cost += ps.cost
                cur_real = new_real
                improved = True
                print(f"  step {step}: +{ps.key} (+{ps.cost}B) -> real_qerr "
                      f"{cur_real:.3f} ({selected_cost}B used, {len(selected)} stats)")
                break
        if not improved:
            break

    out = {
        "bench": args.bench,
        "kind": args.kind,
        "budget_bytes": args.budget,
        "base_mean_qerror": base_real,
        "final_real_mean_qerror": cur_real,
        "n_stats": len(selected),
        "used_bytes": selected_cost,
        "selected": [s.name for s in selected],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nfinal real mean q-error = {cur_real:.3f} (base {base_real:.3f}), "
          f"{len(selected)} stats, {selected_cost}B used")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
