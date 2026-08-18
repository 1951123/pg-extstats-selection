#!/usr/bin/env python3
"""Compare selection methods (M1-M4) on a phase-1 results JSON, using a UNIFIED
session so all methods share the same database state and baseline.

Protocol (fairness)
-------------------
1. Open ONE connection; reset DB (drop any leftover ext/verify stats) and
   ANALYZE the benchmark tables to establish a single clean starting state.
2. Measure ONE shared baseline q-error (no extended stats).
3. For each method:
     a. produce its selected statistic set (M1: ILP; M4: greedy),
     b. within the SAME session, reset state again, build the method's stats,
        measure the REAL mean q-error,
     c. store predicted vs real and approx-vs-real ratio.
   Each method therefore starts from the same clean baseline, so the phase1-
   vs-verifier systematic gap (~12%, protocol/state dependent) no longer
   contaminates the comparison.

Methods
-------
M1  linear multiplicative ILP (scipy.milp, log-space linear objective)
M4  greedy + real-PG re-test   (build, re-measure, keep if improves)

Usage
-----
    source .venv/bin/activate
    python scripts/compare_methods.py \
        --input results/phase1_stats_ceb_mcv.json \
        --budget 500000 --bench stats_ceb --methods M1,M4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
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
_DB = {"census": "census", "job": "imdb", "stats_ceb": "stats"}
_TABLES = {"stats_ceb": ["badges", "comments", "posts", "posthistory", "postlinks",
                         "tags", "users", "votes"],
           "census": ["climate"],
           "job": ["title"]}

EPS_REL = 1e-4


def run_method_m1(phys_stats, queries_options, qerror_base, budget, kind):
    """M1: linear multiplicative ILP. Returns dict with selected stats + prediction."""
    t0 = time.time()
    res = solve_ilp(phys_stats, queries_options, qerror_base, budget)
    dt = time.time() - t0
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


def run_method_m4(phys_stats, queries_options, qerror_base, budget, kind, queries, cfg):
    """M4: greedy + real re-test. Returns (selected stats, real measured mean q-error)."""
    # Order by phase-1 help-score (ordering heuristic only).
    help_score = defaultdict(float)
    for q_idx, opts in enumerate(queries_options):
        qb = qerror_base[q_idx]
        for o in opts:
            if o.qerror < qb:
                help_score[o.stat_index] += (qb - o.qerror)
    order = sorted(range(len(phys_stats)), key=lambda s: -help_score.get(s, 0.0))

    # Greedy with real re-test: each candidate tried by building & measuring.
    selected: list[StatToBuild] = []
    selected_idx: set[int] = set()
    selected_cost = 0
    # Start: baseline empty-set measurement.
    with connect(cfg) as conn:
        conn.autocommit = True
        vr0 = verify_statistics(conn, queries, [])
        cur_real = vr0.mean_qerror
    t0 = time.time()

    for _ in range(10000):  # until no improvement
        improved = False
        for s_idx in order:
            if s_idx in selected_idx:
                continue
            ps = phys_stats[s_idx]
            if selected_cost + ps.cost > budget:
                continue
            trial = selected + [StatToBuild(table=ps.table, columns=ps.columns,
                                            level=ps.level, kind=kind)]
            with connect(cfg) as conn:
                conn.autocommit = True
                vr = verify_statistics(conn, queries, trial)
            new_real = vr.mean_qerror
            if new_real < cur_real * (1.0 - EPS_REL):
                selected = trial
                selected_idx.add(s_idx)
                selected_cost += ps.cost
                cur_real = new_real
                improved = True
                break
        if not improved:
            break
    dt = time.time() - t0

    return {
        "name": "M4_greedy_retest",
        "selected_stats": selected,
        "predicted_qerror_per_query": None,  # M4: no multiplicative prediction
        "predicted_mean": None,
        "selection_real_mean": cur_real,
        "n_stats": len(selected),
        "used_bytes": selected_cost,
        "solve_s": dt,
    }


def reset_db(conn, tables):
    """Drop leftover benchmark-created stats and ANALYZE tables to a clean state."""
    with conn.cursor() as cur:
        cur.execute("SELECT stxname FROM pg_statistic_ext "
                    "WHERE stxname LIKE 'verify_%' OR stxname LIKE 'ext_%'")
        for row in cur.fetchall():
            name = row["stxname"] if isinstance(row, dict) else row[0]
            with conn.cursor() as c2:
                c2.execute(f"DROP STATISTICS IF EXISTS {name}")
    for t in tables:
        with conn.cursor() as cur:
            cur.execute(f"ANALYZE {t}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_stats_ceb_mcv.json")
    ap.add_argument("--budget", type=int, default=500000)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--methods", default="M1,M4")
    ap.add_argument("--out", default="results/method_comparison.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    phys_stats, queries_options, qerror_base = build_problem(phase1)
    m = len(qerror_base)

    dbname = _DB[args.bench]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=dbname)
    tables = _TABLES[args.bench]

    # Unified session: reset once, measure shared baseline.
    base_mean_shared = None
    results = []
    methods = [x.strip() for x in args.methods.split(",") if x.strip()]
    print(f"=== unified-session method comparison ({args.bench}, {args.kind}, "
          f"budget={args.budget}) ===")

    # Pre-compute each method's selected set (M4 needs DB; M1 is offline).
    out_by_method = {}
    for method in methods:
        if method.upper() == "M1":
            out_by_method[method] = run_method_m1(
                phys_stats, queries_options, qerror_base, args.budget, args.kind)
        elif method.upper() == "M4":
            out = run_method_m4(phys_stats, queries_options, qerror_base,
                                args.budget, args.kind, queries, cfg)
            out_by_method[method] = out
        else:
            raise NotImplementedError(f"Method {method}")

    # Verifier connection shared across methods.
    with connect(cfg) as conn:
        conn.autocommit = True
        # single clean baseline at session start
        reset_db(conn, tables)
        vr_base = verify_statistics(conn, queries, [])
        base_mean_shared = vr_base.mean_qerror
    print(f"unified baseline real mean q-error = {base_mean_shared:.3f}")

    for method in methods:
        out = out_by_method[method]
        name = out["name"]
        pred_mean = out["predicted_mean"]
        pred_per_q = out["predicted_qerror_per_query"]
        print(f"\n--- {name}: {out['n_stats']} stats, {out['used_bytes']}B, "
              f"select {out['solve_s']:.2f}s")

        with connect(cfg) as conn:
            conn.autocommit = True
            reset_db(conn, tables)  # clean state before this method
            t0 = time.time()
            vr = verify_statistics(conn, queries, out["selected_stats"],
                                   predicted_per_query=pred_per_q)
            dt = time.time() - t0

        ratio = None
        if pred_mean is not None and pred_mean > 0:
            ratio = vr.mean_qerror / pred_mean
            tag = ("optimistic" if ratio > 1.05
                   else ("pessimistic" if ratio < 0.95 else "~accurate"))
            print(f"   predicted_mean={pred_mean:.3f}  real={vr.mean_qerror:.3f}  "
                  f"ratio={ratio:.3f} ({tag})")
        else:
            print(f"   real={vr.mean_qerror:.3f} (no approx; greedy-sel had "
                  f"{out.get('selection_real_mean'):.3f})")
        print(f"   baseline={base_mean_shared:.3f} -> real {vr.mean_qerror:.3f} "
              f"({(base_mean_shared - vr.mean_qerror)/base_mean_shared*100:.2f}% better)")

        results.append({
            "method": name,
            "predicted_mean_qerror": pred_mean,
            "real_mean_qerror": vr.mean_qerror,
            "baseline_mean_qerror": base_mean_shared,
            "approx_vs_real_ratio": ratio,
            "n_stats": out["n_stats"],
            "used_bytes": out["used_bytes"],
            "solve_s": out["solve_s"],
            "verify_s": dt,
        })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "bench": args.bench, "kind": args.kind,
        "budget_bytes": args.budget,
        "baseline_mean_qerror": base_mean_shared,
        "results": results,
    }, indent=2))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

