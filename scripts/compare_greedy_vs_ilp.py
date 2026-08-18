"""Contrast GLOBAL ILP vs naive per-query GREEDY for the sparse (<=1/query) model.

Greedy: for each query pick its single best candidate (lowest q-error at the
best capacity), then union the chosen physical stats (pay each combo once).
This is per-query optimal but ignores cross-query sharing / budget competition.

Global ILP: ``solve_ilp(..., per_query_cap=1)`` optimises the whole workload on
storage AND shares physical stats across queries (a stat built once can serve
every query that picks it).

We compare, across multiple budgets:
  * number of physical stats
  * used bytes
  * achieved mean q-error
  * whether ILP drops/shares to fit the budget (vs greedy which ignores budget)

Usage
-----
    source .venv/bin/activate
    python scripts/compare_greedy_vs_ilp.py \
        --input results/phase1_ceb_single_mask_top10_multi.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def greedy_solution(phase1, qerror_mode="first"):
    """Per-query independent best candidate -> union of physical stats."""
    phys_stats_single = build_problem(phase1, qerror_mode=qerror_mode)  # uses one mode
    # build_problem already gives per-level PhysicalStats; for greedy we just
    # pick, per query, the single non-dominated option with the lowest q-error,
    # then union their (combo, level) keys.
    phys_stats, queries_options, qerror_base = build_problem(
        phase1, qerror_mode="first")
    chosen_stats = {}  # stat_key -> cost
    qerr_per_query = []
    for q_idx, opts in enumerate(queries_options):
        base = qerror_base[q_idx]
        best = None
        for o in opts:
            q = o.qerror
            if best is None or q < best.qerror:
                best = o
        if best is None or best.qerror >= base:
            qerr_per_query.append(base)
        else:
            ps = phys_stats[best.stat_index]
            chosen_stats[ps.key] = ps.cost
            qerr_per_query.append(best.qerror)
    n_stats = len(chosen_stats)
    used = sum(chosen_stats.values())
    mean = sum(qerr_per_query) / max(len(qerr_per_query), 1)
    return n_stats, used, mean


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_ceb_single_mask_top10_multi.json")
    ap.add_argument("--budgets", default="20000,40000,100000,2000000")
    ap.add_argument("--out", default="results/greedy_vs_ilp.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]

    gn, gu, gm = greedy_solution(phase1)
    print("=== per-query greedy (independent best, union stats) ===")
    print(f"  stats={gn}  used={gu}B  mean_qerr={gm:.3f}")
    print("  (NOTE: greedy ignores budget; this is the 'per-query best' baseline)\n")

    print(f"{'budget':>9} | {'ILP stats':>9} | {'ILP used':>9} | {'ILP mean':>8} | "
          f"{'ILP improv':>10}")
    rows = []
    for bud in budgets:
        phys, opts, base = build_problem(phase1)
        res = solve_ilp(phys, opts, base, bud, per_query_cap=1)
        bm = sum(base) / max(len(base), 1)
        sm = sum(res.qerror_per_query) / max(len(res.qerror_per_query), 1)
        rows.append({"budget": bud, "n_stats": len(res.selected_stats),
                     "used": res.total_bytes, "mean": sm,
                     "improv": (1 - sm / bm) * 100})
        print(f"{bud:>9} | {len(res.selected_stats):>9} | {res.total_bytes:>9} | "
              f"{sm:>8.3f} | {(1-sm/bm)*100:>+9.1f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "greedy": {"n_stats": gn, "used_bytes": gu, "mean_qerror": gm},
        "ilp_by_budget": rows,
    }, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
