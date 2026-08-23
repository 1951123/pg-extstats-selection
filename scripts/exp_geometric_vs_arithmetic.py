#!/usr/bin/env python3
"""R10: robustness of the deployment to the mean choice (geometric vs arithmetic).

The review asks: the MILP minimises log-q-error (== geometric mean, since log is
monotone) per query; if arithmetic mean also preserves linearity, why not use it?
This experiment answers empirically: at a set of budgets on the full workload,
solve the sparse (one-stat-per-query) MILP twice -- once with the geometric
(log) objective, once with the arithmetic (linear) objective -- and measure how
similar the selected statistic sets are (Jaccard) and how close their resulting
surrogate q-error is.

If the two objectives pick near-identical deployments, the conclusion is robust
to the mean choice. If they diverge, we explain the (geometric) choice.

Run:
    python scripts/exp_geometric_vs_arithmetic.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --budgets 10000,40000,100000,250000,500000 \
        --out results/p10_mean_robustness.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem  # noqa: E402


def build_sparse_lp(phys, opts, qbase, budget_bytes,
                    per_query_cap=1, objective="geometric"):
    """Sparse (single-best per query) MILP with a switchable objective.

    Variables: y_s (create), x_is (query selects option), x_is <= y_s,
    per-query cap sum x_is <= 1, level-exclusivity per column combo.
    objective:
      - geometric : min sum_i log e_i = const + sum w_is x_is, w=log(e_is/e_i0)
      - arithmetic: min sum_i e_i     = const + sum (e_is - e_i0) x_is
    Returns (c, A, lb, ub, integrality).
    """
    n_stats = len(phys)
    n_opt = sum(len(o) for o in opts)
    n_var = n_stats + n_opt
    m = len(qbase)

    # objective coefficients
    c = np.zeros(n_var)
    gi = 0
    for q_idx, os in enumerate(opts):
        qb = qbase[q_idx]
        for o in os:
            if objective == "geometric":
                w = float(np.log(max(o.qerror, 1e-12) / max(qb, 1e-12)))
            else:  # arithmetic
                w = float(o.qerror - qb)
            c[n_stats + gi] = w
            gi += 1

    # combo-level exclusivity groups
    combo_groups: dict = {}
    for s_idx, ps in enumerate(phys):
        combo_groups.setdefault((ps.table, ps.columns), []).append(s_idx)
    n_combo = len(combo_groups)

    n_con = 1 + n_opt + m + n_combo
    rows, cols, data, ub = [], [], [], []

    def add(con_row, u):
        for (r, c_, v) in con_row:
            rows.append(r); cols.append(c_); data.append(v)
        ub.append(u)

    nrow = 0
    # 1) storage budget
    rows += [0] * n_stats; cols += list(range(n_stats)); data += [ps.cost for ps in phys]
    ub.append(budget_bytes); nrow += 1
    budget_row = range(1)
    # 2) x - y <= 0
    gi = 0
    for os in opts:
        for o in os:
            rows += [nrow, nrow]; cols += [n_stats + gi, o.stat_index]
            data += [1.0, -1.0]; ub.append(0.0); nrow += 1
            gi += 1
    # 3) per-query cap sum x <= 1
    gi = 0
    for os in opts:
        row = nrow
        for _ in os:
            rows.append(row); cols.append(n_stats + gi); data.append(1.0)
            gi += 1
        ub.append(per_query_cap); nrow += 1
    # 4) level exclusivity per combo
    for members in combo_groups.values():
        row = nrow
        for s_idx in members:
            rows.append(row); cols.append(s_idx); data.append(1.0)
        ub.append(1.0); nrow += 1

    A = np.zeros((nrow, n_var))
    for (r, c_, v) in zip(rows, cols, data):
        A[r, c_] += v
    return c, A, ub, np.ones(n_var)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="results/phase1_ceb_single_mask_6level.json")
    ap.add_argument("--budgets", default="10000,40000,100000,250000,500000")
    ap.add_argument("--out", default="results/p10_mean_robustness.json")
    args = ap.parse_args(argv)

    phase1 = json.load(open(args.input))
    phys, opts, qbase = build_problem(phase1)
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    m = len(opts)
    print(f"queries={m} options={sum(len(o) for o in opts)} "
          f"phys_stats={len(phys)}")

    results = []
    for B in budgets:
        def solve(obj):
            c, A, ub, integrality = build_sparse_lp(phys, opts, qbase, B, 1, obj)
            res = milp(c, integrality=integrality,
                       bounds=Bounds(0, 1),
                       constraints=LinearConstraint(A, -np.inf, ub),
                       options={"time_limit": 30})
            if not res.success:
                return None, None, None, None
            x = np.round(res.x).astype(int)
            y = x[: len(phys)]
            X = x[len(phys):]
            chosen = [i for i, v in enumerate(y.tolist()) if v]
            # resulting surrogate q-errors
            qerrs = []
            gi = 0
            for q_idx, os in enumerate(opts):
                q = qbase[q_idx]
                for o in os:
                    if X[gi]:
                        q = o.qerror
                    gi += 1
                qerrs.append(q)
            return chosen, np.mean(qerrs), np.exp(np.mean(np.log(qerrs))), res
        cg, mg, gg, rg = solve("geometric")
        ca, ma, ga, ra = solve("arithmetic")
        if cg is None or ca is None:
            print(f"  B={B}: one solve failed")
            continue
        # Jaccard of chosen physical stat sets
        sG, sA = set(cg), set(ca)
        jac = len(sG & sA) / len(sG | sA) if (sG | sA) else 1.0
        results.append({
            "budget": B, "n_geo": len(sG), "n_arith": len(sA),
            "jaccard": round(jac, 4),
            "geo_mean_arith": round(mg, 4), "geo_mean_geo": round(gg, 4),
            "arith_mean_arith": round(ma, 4), "arith_mean_geo": round(ga, 4),
        })
        print(f"  B={B:>6}: geo n={len(sG)} arith n={len(sA)} "
              f"Jaccard={jac:.3f} | geoObj:mean={mg:.4f}/geomean={gg:.4f} "
              f"| arithObj:mean={ma:.4f}/geomean={ga:.4f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
