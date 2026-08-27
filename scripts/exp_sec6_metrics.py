#!/usr/bin/env python3
"""Sec-6 (advisor 4-RQ structure) unified metric generator.

Produces, per workload (stats_CEB_single, Census) and per storage budget, a
consistent table of arithmetic-AND-geometric q-error metrics for every method:

  methods:
    no_stats            : native PG, no extended statistics (baseline q-error)
    milp                : joint what+how-much sparse MILP (log objective ==
                          geometric-mean-optimal; also reports mean/P90/max)
    all_default         : naive DBA baseline -- every (table,cols) candidate at
                          L=100, truncated to budget by improvement/byte
    what_only_L100      : our MILP with the how-much axis disabled, all levels
                          locked to 100 (column-only selection)
    greedy_G2           : marginal shared-resource greedy (re-picks highest
                          improvement-per-byte across still-unserved queries)

Because solve_ilp minimises sum log e_i (monotone in geometric mean), the MILP
row is already geometric-mean-optimal; we report it alongside mean/median/P90/max
(advisor wants geometric primary + tail metrics). All methods are evaluated on
the Catalog-Mask oracle measurements (phase-1 6-level JSON) -- no real DB needed.

Each method is forced to respect the same storage budget (fair comparison).

Usage
-----
    source .venv/bin/activate
    python scripts/exp_sec6_metrics.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --budgets 5000,10000,20000,40000,100000,250000,500000 \
        --out results/sec6_stats.json
    python scripts/exp_sec6_metrics.py \
        --input results/phase1_census_mcv_6level.json \
        --budgets 5000,10000,20000,40000,100000,250000,500000,1000000,2000000 \
        --out results/sec6_census.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def geom_mean(vals):
    """Geometric mean of a list of q-errors (all >= 1, finite)."""
    a = [v for v in vals if v == v and v > 0]
    if not a:
        return float("nan")
    return float(math.exp(sum(math.log(v) for v in a) / len(a)))


def summarize(vals):
    """Return {geomean, mean, median, p90, max} over q-errors."""
    a = np.array([v for v in vals if v == v], dtype=float)
    if len(a) == 0:
        return {"geomean": float("nan"), "mean": float("nan"),
                "median": float("nan"), "p90": float("nan"),
                "max": float("nan"), "n": 0}
    return {
        "geomean": float(geom_mean(a)),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
        "n": int(len(a)),
    }


def level_counts(stats):
    lc = {}
    for s in stats:
        lc[str(s.level)] = lc.get(str(s.level), 0) + 1
    return lc


def build_restricted(phys_stats, queries_options, qerror_base, allowed_levels):
    """Sub-problem containing only statistics at `allowed_levels`.

    Re-indexes physical statistics so solve_ilp sees a compact, consistent
    (phys_stats, queries_options) pair (stat_index must stay aligned).
    """
    from extstats.optimize import Option
    keep_stat = [s for s in phys_stats if s.level in allowed_levels]
    old_to_new = {id(s): i for i, s in enumerate(keep_stat)}
    new_opts = []
    for opts_ in queries_options:
        keep = []
        for o in opts_:
            s = phys_stats[o.stat_index]
            if s.level in allowed_levels:
                keep.append(Option(stat_index=old_to_new[id(s)], qerror=o.qerror,
                                   level=o.level, query=o.query, cand=o.cand))
        new_opts.append(keep)
    return keep_stat, new_opts, list(qerror_base)


def build_all_default(phase1):
    """Build every (table, cols) candidate at L=100, truncated to budget.

    Returns a dict {(table,cols_tuple): (cost_at_L100, effective_qerr_by_qidx)}
    plus per-query baseline array, for use in an improvement/byte truncation.
    """
    from extstats.optimize import build_problem as _bp
    phys, opts, qb = _bp(phase1)
    # map (table,cols)->cost at L100 from phase1 candidates
    combo_cost = {}   # (table, cols) -> size at L100
    combo_qerr = {}   # (table, cols) -> dict qidx -> effective qerror at L100
    base_by_idx = list(qb)
    # locate qidx per opt: build_problem keeps opt order aligned to results
    # We'll rebuild an index: qidx -> list of (combo_key, L100 qerror)
    opt_key_by_q = {}
    for q_idx, opts_ in enumerate(opts):
        for o in opts_:
            ps = phys[o.stat_index]
            combo = (ps.table, tuple(sorted(ps.columns)))
            opt_key_by_q.setdefault(q_idx, []).append((combo, ps.level, o.qerror))
    # assemble L100 info
    for q_idx, lst in opt_key_by_q.items():
        for combo, level, qerr in lst:
            if level != 100:
                continue
            if combo not in combo_cost:
                # find cost: search phys for this combo at L100
                for ps in phys:
                    if ps.level == 100 and (ps.table, tuple(sorted(ps.columns))) == combo:
                        combo_cost[combo] = ps.cost
                        break
            combo_qerr.setdefault(combo, {})[q_idx] = min(
                combo_qerr.get(combo, {}).get(q_idx, float("inf")), qerr)
    return combo_cost, combo_qerr, base_by_idx


def all_default_metrics(phase1, budget):
    """All-default-candidates at a budget: return per-query q-error list."""
    combo_cost, combo_qerr, base = build_all_default(phase1)
    # improvement per combo = sum_q max(0, base_e - e)
    imp = {}
    for combo, qd in combo_qerr.items():
        total = 0.0
        for qi, e in qd.items():
            total += max(0.0, base[qi] - e)
        imp[combo] = total
    # order by improvement/byte desc, pack within budget (weighted value knapsack,
    # greedy approx as a naive DBA would)
    order = sorted(combo_cost.keys(),
                   key=lambda c: -imp[c] / max(combo_cost[c], 1))
    chosen = set()
    used = 0
    for c in order:
        if used + combo_cost[c] <= budget:
            chosen.add(c)
            used += combo_cost[c]
    # effective per-query q-error
    out = list(base)
    for combo, qd in combo_qerr.items():
        if combo not in chosen:
            continue
        for qi, e in qd.items():
            if e < out[qi]:
                out[qi] = e
    return out, len(chosen), used


def greedy_G2(phys, opts, qb, budget):
    """Marginal shared-resource greedy across still-unserved queries."""
    n = len(qb)
    cur = list(qb)
    used = 0
    selected = set()
    served = [False] * n
    changed = True
    while changed:
        changed = False
        best = None  # (ratio, stat_idx, total_imp, used_chosen_queries)
        # candidate stats not yet selected
        avail = {}
        for q_idx, opts_ in enumerate(opts):
            if served[q_idx]:
                continue
            for o in opts_:
                ps = phys[o.stat_index]
                key = (ps.table, ps.columns, ps.level)
                if key in selected:
                    continue
                if cur[q_idx] - o.qerror <= 0:
                    continue
                if key not in avail:
                    avail[key] = (0.0, o.stat_index, ps.cost)
                avail[key] = (avail[key][0] + (cur[q_idx] - o.qerror),
                              avail[key][1], avail[key][2])
        for key, (tot_imp, sidx, cost) in avail.items():
            if used + cost <= budget:
                r = tot_imp / max(cost, 1)
                if best is None or r > best[0]:
                    best = (r, key, sidx, cost, tot_imp)
        if best is None:
            break
        _, key, sidx, cost, tot_imp = best
        selected.add(key)
        used += cost
        # apply to all unserved queries that benefit
        for q_idx, opts_ in enumerate(opts):
            if served[q_idx]:
                continue
            for o in opts_:
                ps = phys[o.stat_index]
                if (ps.table, ps.columns, ps.level) == key:
                    if o.qerror < cur[q_idx]:
                        cur[q_idx] = o.qerror
                        served[q_idx] = True  # one stat per query (sparse)
        changed = True
    return cur, len(selected), used


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets", default="5000,10000,20000,40000,100000,250000,500000")
    ap.add_argument("--out", default="results/sec6_metrics.json")
    argv = argv if argv is not None else sys.argv[1:]
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    base_metrics = summarize(qb)
    # problem scale (well-defined, independent of budget)
    n_phys = len(phys)
    n_opt = sum(len(o) for o in opts)
    print(f"workload n_queries={len(qb)}  n_phys_stats={n_phys}  "
          f"n_options={n_opt}  n_vars={(n_phys + n_opt)}")

    LEVELS = ["10", "25", "50", "100", "1000", "10000"]
    rows = []
    for B in budgets:
        row = {"budget_bytes": B, "baseline": summarize(qb)}
        # No-stats == baseline
        row["methods"] = {}
        # MILP (log obj => geomean optimal)
        t0 = time.time()
        res = solve_ilp(phys, opts, qb, B, per_query_cap=1)
        row["methods"]["milp"] = {
            **summarize(res.qerror_per_query),
            "n_stats": len(res.selected_stats),
            "used": res.total_bytes,
            "level_counts": level_counts(res.selected_stats),
            "solve_s": round(time.time() - t0, 3),
            "n_phys": n_phys, "n_options": n_opt, "n_vars": n_phys + n_opt,
            "n_queries": len(qb), "status": res.status,
        }
        # what-only L100
        keep100, opts100, qb100 = build_restricted(phys, opts, qb, {100})
        if keep100:
            res100 = solve_ilp(keep100, opts100, qb100, B, per_query_cap=1)
            row["methods"]["what_only_L100"] = {
                **summarize(res100.qerror_per_query),
                "n_stats": len(res100.selected_stats),
                "used": res100.total_bytes,
                "level_counts": level_counts(res100.selected_stats),
            }
        # greedy G2
        t0 = time.time()
        gq, gsel, gused = greedy_G2(phys, opts, qb, B)
        row["methods"]["greedy_G2"] = {
            **summarize(gq), "n_stats": gsel, "used": gused, "solve_s": round(time.time() - t0, 3),
        }
        # all-default-candidates
        t0 = time.time()
        aq, asel, aused = all_default_metrics(phase1, B)
        row["methods"]["all_default"] = {
            **summarize(aq), "n_stats": asel, "used": aused, "solve_s": round(time.time() - t0, 3),
        }
        rows.append(row)
        # console
        m = row["methods"]
        print(f"B={B//1000:>5}K  base.mean={base_metrics['mean']:.3f} "
              f"base.geomean={base_metrics['geomean']:.3f}")
        for name, d in m.items():
            print(f"    {name:14s} geomean={d.get('geomean',float('nan')):.4f} "
                  f"mean={d.get('mean',float('nan')):.4f} "
                  f"p90={d.get('p90',float('nan')):.3f} max={d.get('max',float('nan')):.2f} "
                  f"n_stats={d.get('n_stats','-')} used={d.get('used','-')}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"input": args.input, "n_queries": len(qb), "baseline": base_metrics,
         "budgets": rows}, indent=2))
    print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
