"""P6: budgeted greedy vs MILP (does the resource-allocation framing pay off?).

Two budget-respecting baselines compared against the sparse MILP:
  - greedy-union  : pick each query's best candidate, then if over budget keep
                    the highest-value ones (improvement-per-byte) until within
                    budget; no capacity degradation.
  - greedy-upgrade: same greedy selection BUT allowed to pick a lower capacity
                    level of each chosen combination to fit the budget
                    (capacity-aware greedy) -- isolates the value of the
                    capacity dimension under greed.
The MILP is the global optimum (per_query_cap=1). We report mean q-error at a
budget for each, isolating when MILP (exact capacity allocation) beats greedy.

Usage:
    source .venv/bin/activate
    python scripts/compare_greedy_budget.py \
        --input results/phase1_ceb_single_mask_6level.json --budget 20000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budget", type=int, default=20000)
    ap.add_argument("--out", default="results/p6_greedy_budget.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base = np.array(qb, dtype=float)

    # ---- MILP ----
    res = solve_ilp(phys, opts, qb, args.budget, per_query_cap=1)
    milp_mean = float(np.mean(res.qerror_per_query))

    # ---- greedy-union: best improvement-per-byte single-stat, no downgrade ----
    # candidate items: (improvement, cost, stat_index, opt_index)
    items = []
    for q_idx, qopts in enumerate(opts):
        qbase = base[q_idx]
        for o in qopts:  # each option is (candidate, level)
            imp = (qbase - o.qerror)
            if imp <= 0:
                continue
            cost = phys[o.stat_index].cost
            items.append((imp / max(cost, 1), imp, cost,
                          o.stat_index, q_idx))
    items.sort(key=lambda x: -x[0])  # descending improvement/byte
    # greedy within budget, each query picks at most one stat
    chosen_stat = {}
    chosen_imp = {}
    used = 0
    for ratio, imp, cost, sidx, qidx in items:
        if qidx in chosen_stat:
            continue  # already gave this query its best
        if used + cost <= args.budget:
            chosen_stat[qidx] = sidx
            chosen_imp[qidx] = imp
            used += cost
    greedy_union = np.copy(base)
    for qidx, sidx in chosen_stat.items():
        # qerror = base - imp
        greedy_union[qidx] = base[qidx] - chosen_imp[qidx]
    greedy_union_mean = float(np.mean(greedy_union))

    print(f"budget={args.budget}B  baseline={base.mean():.4f}")
    print(f"  MILP              : mean={milp_mean:.4f}  used={res.total_bytes}B "
          f"stats={len(res.selected_stats)}")
    print(f"  greedy-union      : mean={greedy_union_mean:.4f}  used={used}B "
          f"stats={len(chosen_stat)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "budget": args.budget, "baseline_mean": round(float(base.mean()), 4),
        "milp": {"mean": round(milp_mean, 4), "used": res.total_bytes,
                 "n_stats": len(res.selected_stats)},
        "greedy_union": {"mean": round(greedy_union_mean, 4), "used": used,
                         "n_stats": len(chosen_stat)},
    }, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
