#!/usr/bin/env python3
"""Phase-2: solve the budgeted extended-statistics selection ILP (shared storage,
minimise mean q-error) with scipy.optimize.milp.

Reads a phase-1 results JSON (results/phase1_<bench>_<kind>.json) and a storage
budget, selects which (combo, capacity) statistics to create, and reports the
achieved mean q-error vs the baseline.

Usage
-----
    source .venv/bin/activate
    python scripts/solve_ilp.py --input results/phase1_stats_ceb_mcv.json \
        --budget 500000
    # sweep several budgets
    python scripts/solve_ilp.py --input results/phase1_stats_ceb_mcv.json \
        --budget 100000,500000,1000000

Options
-------
--input PATH   phase-1 JSON (default results/phase1_stats_ceb_mcv.json)
--budget B     storage budget in bytes (int or comma list)
--out PATH     output results path (default results/ilp_solution.json)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="results/phase1_stats_ceb_mcv.json")
    ap.add_argument("--budget", default="500000",
                    help="storage budget in bytes (int or comma-separated list)")
    ap.add_argument("--out", default="results/ilp_solution.json")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    phase1 = json.loads(input_path.read_text())
    budgets = [int(b) for b in args.budget.split(",") if b.strip()]

    # Build the problem once (shared across budget sweeps).
    all_options, phys_stats, queries_options, qerror_base = build_problem(
        phase1, budget_bytes=0
    )
    m = len(qerror_base)
    base_mean = float(np.mean(qerror_base))
    n_opt = len(all_options)

    print(f"=== phase-2 ILP ===")
    print(f"queries={m}  options={n_opt}  physical_stats={len(phys_stats)}")
    print(f"baseline mean q-error = {base_mean:.3f}")

    sweep: list[dict] = []
    for budget in budgets:
        t0 = time.time()
        res = solve_ilp(
            all_options, phys_stats, queries_options, qerror_base,
            budget_bytes=budget,
        )
        dt = time.time() - t0
        improvement = (
            (base_mean - res.mean_qerror) / base_mean * 100.0
            if base_mean else 0.0
        )
        print(
            f"budget={budget:>12,}B  -> mean q-error {res.mean_qerror:9.3f} "
            f"(base {base_mean:9.3f})  improve {improvement:6.2f}%  "
            f"stats={len(res.selected_stats)}  used={res.total_bytes:,}B  "
            f"({dt:.1f}s, status={res.status})"
        )
        sweep.append(
            {
                "budget_bytes": budget,
                "mean_qerror": res.mean_qerror,
                "base_mean_qerror": base_mean,
                "improvement_pct": improvement,
                "n_stats": len(res.selected_stats),
                "used_bytes": res.total_bytes,
                "status": res.status,
                "message": res.message,
                "selected_stats": [
                    {"key": ps.key, "table": ps.table, "columns": list(ps.columns),
                     "level": ps.level, "cost": ps.cost}
                    for ps in res.selected_stats
                ],
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "bench": phase1.get("bench"),
                "kind": phase1.get("kind"),
                "solved_at": datetime.now().isoformat(),
                "n_queries": m,
                "n_options": n_opt,
                "baseline_mean_qerror": base_mean,
                "budgets": sweep,
            },
            indent=2,
        )
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
