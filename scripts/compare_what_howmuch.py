#!/usr/bin/env python3
"""Experiment 2: What-only vs What+How-much (capacity-aware) selection.

Directly tests the paper's core claim: the *capacity* axis ("how much", i.e.
which statistics_target) adds measurable value over column-only selection
("what", i.e. which column combination) under a storage budget.

We solve the sparse MILP (per_query_cap=1) on the 632-query stats_CEB_single
workload at many budgets, under four models:
  * what+how-much (ours) : free choice of (columns, capacity) for each selected
                          statistic (may upgrade L100->L1000->L10000).
  * what-only @L100  : chosen statistics all locked to target 100.
  * what-only @L1000 : all locked to target 1000.
  * what-only @L10000: all locked to target 10000 (the "fixed-capacity"
                       column-selection baseline the review requested).

Because a statistic's storage cost grows with its capacity, the what-only
curves are NOT comparable by simply picking "more columns": under the same
budget, locking to L10000 buys fewer, heavier statistics. The what+how-much
model can spend each budget unit where it helps most --- adding a cheap L100 on
one combination or upgrading a dominant one to L10000. If the capacity axis
matters, ours is strictly better at tight budgets and never worse.

Usage
-----
    source .venv/bin/activate
    python scripts/compare_what_howmuch.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --budgets 1000,2000,5000,10000,20000,40000,100000,250000,500000 \
        --out results/p2_what_vs_howmuch.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, Option, solve_ilp  # noqa: E402


def summarize_mean(vals: list[float]) -> float:
    a = np.array([v for v in vals if v == v], dtype=float)
    return float(a.mean()) if len(a) else float("nan")


def summarize_full(vals: list[float]) -> dict:
    a = np.array([v for v in vals if v == v], dtype=float)
    if len(a) == 0:
        return {"mean": float("nan"), "median": float("nan"),
                "p90": float("nan"), "max": float("nan")}
    return {
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


def level_counts(stats) -> dict:
    lc = {str(l): 0 for l in (100, 1000, 10000)}
    for s in stats:
        lc[str(s.level)] = lc.get(str(s.level), 0) + 1
    return lc


def build_restricted(phys_stats, queries_options, qerror_base, allowed_levels):
    """Return a sub-problem containing only statistics at `allowed_levels`.

    Re-indexes physical statistics so solve_ilp sees a compact, consistent
    (phys_stats, queries_options) pair.
    """
    keep_stat = [s for s in phys_stats if s.level in allowed_levels]
    old_to_new = {id(s): i for i, s in enumerate(keep_stat)}

    new_opts = []
    for opts in queries_options:
        keep = []
        for o in opts:
            s = phys_stats[o.stat_index]
            if s.level in allowed_levels:
                # rebuild Option with the new compact stat index
                keep.append(
                    Option(stat_index=old_to_new[id(s)], qerror=o.qerror,
                           level=o.level, query=o.query, cand=o.cand)
                )
        new_opts.append(keep)
    return keep_stat, new_opts, qerror_base


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets",
                    default="1000,2000,5000,10000,20000,40000,100000,250000,500000")
    ap.add_argument("--out", default="results/p2_what_vs_howmuch.json")
    ap.add_argument("--qerror-mode", default="first")
    args = ap.parse_args(argv)

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    phase1 = json.loads(Path(args.input).read_text())
    phys_stats, queries_options, qerror_base = build_problem(
        phase1, qerror_mode=args.qerror_mode)

    base_mean = summarize_mean(qerror_base)
    print(f"workload: {phase1.get('bench')}  n_queries={len(qerror_base)}  "
          f"baseline mean={base_mean:.4f}")

    models = {
        "what+how-much": None,        # free level choice = full problem
        "what-only-L100": {100},
        "what-only-L1000": {1000},
        "what-only-L10000": {10000},
    }

    rows = []
    header = f"{'budget':>9} " + " ".join(f"{name:>14}" for name in models)
    print("\n" + header)
    for B in budgets:
        row = {"budget_bytes": B, "baseline": base_mean, "models": {}}
        line = f"{B:>9} "
        for name, allowed in models.items():
            if allowed is None:
                ps, qo, qb = phys_stats, queries_options, qerror_base
            else:
                ps, qo, qb = build_restricted(phys_stats, queries_options,
                                               qerror_base, allowed)
            res = solve_ilp(ps, qo, qb, B, per_query_cap=1)
            row["models"][name] = {
                "metrics": summarize_full(res.qerror_per_query),
                "n_stats": len(res.selected_stats),
                "used_bytes": res.total_bytes,
                "level_counts": level_counts(res.selected_stats),
            }
            line += f"{res.mean_qerror:>14.4f} "
        rows.append(row)
        print(line)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"models": list(models), "rows": rows},
                                          indent=2))
    print(f"\n[saved] {args.out}")

    # ---- summary: is what+how-much ever worse than the best what-only? ----
    print("\n=== advantage of what+how-much over best what-only (mean q-error) ===")
    print(f"{'budget':>9} {'ours':>9} {'best what-only':>15} {'delta(ours-best)':>17}")
    for row in rows:
        ours = row["models"]["what+how-much"]["metrics"]["mean"]
        what_best = min(row["models"][n]["metrics"]["mean"]
                        for n in models if n != "what+how-much")
        print(f"{row['budget_bytes']:>9} {ours:>9.4f} {what_best:>15.4f} "
              f"{ours - what_best:>17.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
