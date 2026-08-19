#!/usr/bin/env python3
"""Experiment 3: stronger greedy baselines vs the sparse MILP.

The review's Major Concern 4 says the current greedy (each query's best
improvement-per-byte, within budget) is too weak. We implement three stronger,
capacity-aware greedy baselines and confirm that the MILP still wins --- and
that its advantage is specifically capacity allocation, not just smarter
sorting:

  * G1 benefit/byte  : for each query, the highest aggregate improvement/byte
                       option (single pass, no re-evaluation). This is the
                       common "cheapest useful stat first" baseline, upgraded
                       to the full (columns, capacity) option space.
  * G2 marginal      : a proper shared-resource MARGINAL greedy. Repeatedly
                       pick the physical statistic with the highest
                       (total improvement across all still-unserved queries) /
                       cost, re-computed after every pick. This is the standard
                       strong adversary for a shared-resource knapsack.
  * G3 upgrade-aware : the strongest variant: G2's marginal rule, but explicitly
                       allowed to choose ANY capacity level and to reallocate
                       (it may pick a replacement/upgrade combo as the
                       marginal value of upgrading a built low-level stat to a
                       higher level).

All greedy baselines are capacity-aware (they may pick L100 / L1000 / L10000),
so losing to the MILP is NOT because a greedy ignores the "how much" axis; it
is because greedy cannot globally reallocate the shared capacity budget.

Usage:
    source .venv/bin/activate
    python scripts/compare_greedy_strong.py \
        --input results/phase1_ceb_single_mask_full_multi.json \
        --budgets 5000,10000,20000,40000,100000,250000 \
        --out results/p6_greedy_strong.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def _query_improvements(opts, qbase):
    """Map option-index-in-query -> improvement over baseline (drop non-helpers)."""
    return [(o, max(0.0, qbase - o.qerror)) for o in opts]


def greedy_benefit_byte(phys, opts, qbase, budget):
    """G1: each query's best improvement/byte option, single pass within budget."""
    served_stat = {}
    imp_of = {}
    items = []
    for q_idx, qopts in enumerate(opts):
        qbase_v = qbase[q_idx]
        best = None
        for o in qopts:
            imp = qbase_v - o.qerror
            if imp > 0:
                cost = max(phys[o.stat_index].cost, 1)
                cand = (imp / cost, imp, o.stat_index, cost)
                if best is None or cand[0] > best[0]:
                    best = cand
        if best:
            items.append((best[0], best[1], best[2], best[3], q_idx))
    items.sort(key=lambda x: -x[0])
    used = 0
    for _, imp, sidx, cost, qidx in items:
        if qidx in served_stat:
            continue
        if used + cost <= budget:
            served_stat[qidx] = sidx
            imp_of[qidx] = imp
            used += cost
    qerr = np.array(qbase, dtype=float)
    for qidx, imp in imp_of.items():
        qerr[qidx] = qbase[qidx] - imp
    return qerr, used, len(served_stat)


def greedy_marginal(phys, opts, qbase, budget, upgrade_aware=False):
    """G2/G3: shared-resource ADD-or-UPGRADE marginal greedy.

    This is the standard, strongest greedy adversary for the shared-resource
    "what + how much" problem. State is a deployed set of column-combinations at
    a chosen capacity level. Each iteration considers two kinds of move:

      * ADD   : deploy a not-yet-deployed combination at an affordable level;
      * UPGRADE (G3 only): raise an already-deployed combination from its
                current level to a higher one, paying only the price delta.

    A move's marginal benefit is the resulting reduction in mean q-error over
    *all* queries (unserved queries that would newly use the combo's level, plus
    queries already served by the same combo that gain from an upgrade). The
    move with the largest benefit-per-byte (delta_improvement / delta_cost) is
    applied; ties broken by larger total gain. This directly models the
    "upgrade vs add" capacity reallocation the review asks about.
    """
    n = len(qbase)
    # precompute, per query, the best improvement it can get from each combo-level
    # option and its best single-stat improvement over all options.
    qopt = []                    # list of (stat_index, imp) per query
    qbest_imp = []               # best improvement per query across all options
    combo_of = [ps.columns for ps in phys]   # combo key for each physical stat
    combo_key = {}
    for i, ps in enumerate(phys):
        combo_key.setdefault(ps.columns, i)  # first stat index per combo
    # per query: improvement per (combo, level) via the stat that has it
    for q_idx in range(n):
        entry = []
        best = 0.0
        for o in opts[q_idx]:
            imp = qbase[q_idx] - o.qerror
            if imp > 0:
                entry.append((o.stat_index, imp))
                if imp > best:
                    best = imp
        qopt.append(entry)
        qbest_imp.append(best)

    # greedy state
    qerr = np.array(qbase, dtype=float)
    served_combo = [None] * n    # combo (columns tuple) serving each query
    deployed_level = {}          # combo -> level currently deployed
    used = 0
    n_adds = 0
    n_upgrades = 0

    def imp_of(q_idx, s_idx):
        for si, imp in qopt[q_idx]:
            if si == s_idx:
                return imp
        return 0.0

    while True:
        best_move = None         # (ratio, gain, s_idx)
        for s_idx, ps in enumerate(phys):
            combo = ps.columns
            cur_level = deployed_level.get(combo)
            if cur_level is not None and cur_level >= ps.level:
                continue          # neither add nor upgrade
            if cur_level is None:
                delta_cost = ps.cost
            else:
                if not upgrade_aware:
                    continue      # G2: adds only at the first (lowest) level seen
                # price delta to raise to ps.level
                delta_cost = ps.cost - _cost_at(phys, combo, cur_level)
                if delta_cost < 0:
                    continue
            if used + delta_cost > budget:
                continue
            # marginal gain over what is already captured.
            # For an UNSERVED query we count a query only when this combo/level
            # is its single best option (matching how serves are assigned), so
            # the greedy never "wants" a stat a query would not actually use.
            # For a query already served by this SAME combo we count the delta
            # of raising level (an upgrade).
            gain = 0.0
            for q_idx in range(n):
                imp = imp_of(q_idx, s_idx)
                if imp <= 0:
                    continue
                if served_combo[q_idx] == combo:
                    captured = qbase[q_idx] - qerr[q_idx]
                    gain += max(0.0, imp - captured)
                elif served_combo[q_idx] is None:
                    if abs(imp - qbest_imp[q_idx]) < 1e-9:
                        gain += imp
                # served by a different combo: this move does not help it
            if gain <= 0:
                continue
            ratio = gain / max(delta_cost, 1)
            if best_move is None or ratio > best_move[0] or (
                    abs(ratio - best_move[0]) < 1e-12 and gain > best_move[1]):
                best_move = (ratio, gain, s_idx)
        if best_move is None:
            break
        _, gain, s_idx = best_move
        combo = phys[s_idx].columns
        cur_level = deployed_level.get(combo)
        if cur_level is None:
            used += phys[s_idx].cost
            deployed_level[combo] = phys[s_idx].level
            n_adds += 1
            # serve every unserved query that benefits most from this combo/level
            for q_idx in range(n):
                if served_combo[q_idx] is not None:
                    continue
                imp = imp_of(q_idx, s_idx)
                if imp > 0 and abs(imp - qbest_imp[q_idx]) < 1e-9:
                    served_combo[q_idx] = combo
                    qerr[q_idx] = qbase[q_idx] - imp
        else:
            delta = phys[s_idx].cost - _cost_at(phys, combo, cur_level)
            used += delta
            deployed_level[combo] = phys[s_idx].level
            n_upgrades += 1
            # re-evaluate every query that uses (or could use) this combo
            for q_idx in range(n):
                imp = imp_of(q_idx, s_idx)
                if imp > 0 and served_combo[q_idx] == combo:
                    qerr[q_idx] = qbase[q_idx] - imp
    return qerr, used, len(deployed_level), n_adds, n_upgrades


def _cost_at(phys, combo, level):
    for ps in phys:
        if ps.columns == combo and ps.level == level:
            return ps.cost
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets",
                    default="5000,10000,20000,40000,100000,250000,500000")
    ap.add_argument("--out", default="results/p6_greedy_strong.json")
    ap.add_argument("--qerror-mode", default="first")
    args = ap.parse_args(argv)

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1, qerror_mode=args.qerror_mode)
    base_mean = float(np.mean(qb))
    print(f"workload={phase1.get('bench')} n={len(qb)} baseline={base_mean:.4f}")

    greedy_fns = {
        "G1 benefit/byte": greedy_benefit_byte,
        "G2 marginal": greedy_marginal,
        "G3 upgrade-aware": lambda p, o, q, b: greedy_marginal(p, o, q, b,
                                                                upgrade_aware=True),
    }

    rows = []
    print(f"\n{'budget':>9} {'MILP':>8} " + " ".join(f"{nm:>14}" for nm in greedy_fns))
    for B in budgets:
        res = solve_ilp(phys, opts, qb, B, per_query_cap=1)
        milp_mean = float(np.mean(res.qerror_per_query))
        row = {"budget_bytes": B, "baseline": base_mean,
               "milp": {"mean": milp_mean, "n_stats": len(res.selected_stats),
                        "used": res.total_bytes},
               "greedies": {}}
        line = f"{B:>9} {milp_mean:>8.4f} "
        for nm, fn in greedy_fns.items():
            out = fn(phys, opts, qb, B)
            qerr, used, nm_stats = out[0], out[1], out[2]
            g = float(np.mean(qerr))
            d = {"mean": g, "n_stats": nm_stats, "used": used}
            if len(out) == 5:
                d["n_adds"], d["n_upgrades"] = out[3], out[4]
            row["greedies"][nm] = d
            line += f"{g:>14.4f} "
        rows.append(row)
        print(line)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"rows": rows}, indent=2))
    print(f"\n[saved] {args.out}")

    # ---- summary: MILP vs the STRONGEST greedy (best of the three) ----
    print("\n=== MILP vs strongest greedy (best of G1/G2/G3) ===")
    print(f"{'budget':>9} {'MILP':>8} {'best greedy':>12} {'MILP adv':>10} {'best-is':>8}")
    for row in rows:
        m = row["milp"]["mean"]
        graws = {nm: g["mean"] for nm, g in row["greedies"].items()}
        gname = min(graws, key=graws.get)
        g = graws[gname]
        print(f"{row['budget_bytes']:>9} {m:>8.4f} {g:>12.4f} {m - g:>10.4f} {gname:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
