"""Sparse ILP solver: select at most ONE (candidate, capacity) per query.

Reads a multi-level phase-1 JSON (candidates have per-level q-error + size),
builds the physical-stat / per-query-option problem, and solves it with
``per_query_cap=1`` so every query picks at most one candidate statistic
(possibly none) to minimise mean q-error under a storage budget.

Usage
-----
    source .venv/bin/activate
    python scripts/solve_sparse_ilp.py \
        --input results/phase1_ceb_single_mask_top10.json \
        --budget 200000 --out results/sparse_solution.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_ceb_single_mask_top10.json")
    ap.add_argument("--budget", type=int, default=100000)
    ap.add_argument("--qerror-mode", default="first",
                    help="first|mean|worst|p90 (per-level summary)")
    ap.add_argument("--out", default="results/sparse_solution.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys_stats, queries_options, qerror_base = build_problem(
        phase1, qerror_mode=args.qerror_mode)

    n_opt = sum(len(o) for o in queries_options)
    print(f"phys_stats={len(phys_stats)}  queries={len(qerror_base)}  "
          f"options={n_opt}  budget={args.budget}B")

    t0 = time.time()
    res = solve_ilp(phys_stats, queries_options, qerror_base, args.budget,
                    per_query_cap=1)
    dt = time.time() - t0

    # baseline vs selected mean q-error
    base_mean = sum(qerror_base) / max(len(qerror_base), 1)
    sel_mean = sum(res.qerror_per_query) / max(len(res.qerror_per_query), 1)

    print(f"\nselected {res.total_bytes}B used / {args.budget}B budget, "
          f"{len(res.selected_stats)} physical stats (solve {dt:.2f}s)")
    print(f"baseline mean q-error : {base_mean:.3f}")
    print(f"selected mean q-error : {sel_mean:.3f}  "
          f"({(1 - sel_mean/base_mean)*100:+.1f}%)")

    print("\nselected stats:")
    for ps in res.selected_stats:
        print(f"  {ps.table}({','.join(ps.columns)}) L{ps.level}  cost={ps.cost}B")

    # how many queries improved / unchanged / got nothing
    improved = sum(1 for q, q0 in zip(res.qerror_per_query, qerror_base)
                   if q < q0 * 0.99)
    unchanged = sum(1 for q, q0 in zip(res.qerror_per_query, qerror_base)
                    if q0 * 0.99 <= q <= q0 * 1.01)
    print(f"\nqueries improved={improved}, ~unchanged={unchanged}, "
          f"of {len(qerror_base)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "budget_bytes": args.budget,
        "baseline_mean_qerror": base_mean,
        "selected_mean_qerror": sel_mean,
        "n_stats": len(res.selected_stats),
        "used_bytes": res.total_bytes,
        "selected": [{"table": ps.table, "columns": list(ps.columns),
                      "level": ps.level, "cost": ps.cost}
                     for ps in res.selected_stats],
    }, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
