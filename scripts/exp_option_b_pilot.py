#!/usr/bin/env python3
"""Option-B pilot: does "solve overlap, then drop interfering stats, re-solve"
recover BOTH nominal quality AND near-exact predictability?

Option-A (global disjointness) restores predictability (E2E ratio ~1.000) but
pays a nominal-quality loss (it forgoes overlapping-but-valuable candidates,
e.g. predicted 2.00 vs 1.02). Option-B should keep the good overlapping
solution and remove ONLY the statistics that *hijack* other queries (planner
"first-built-wins" over overlapping MCVs), so it keeps quality while restoring
predictability.

This pilot:

  Phase A (offline, no DB): solve the unconstrained overlap MILP at a budget,
    apply the mechanism-correct hijacker rule to find interfering stats,
    predict which queries are affected, drop the hijackers, and re-solve.
    Compares predicted mean q-error of the overlap set vs the repaired set.

  Phase B (real PostgreSQL): deploy both the raw overlap set and the repaired
    set on `posts`, measure E2E per query, and verify that repair keeps
    quality (mean q-error ~ overlap) while restoring predictability
    (E2E/predicted ratio ~1 for the repaired set, >1 for raw overlap).

Hijacker rule (mechanism-correct, uses phase-1 mask data, no SQL parsing):
  A deployed stat s "serves" query q iff s's (table, columns, level) appears
  in q's phase-1 candidates (the model's own matching convention).  The
  planner uses the FIRST-BUILT among the stats serving q (OID = CREATE order).
  effective(q) = first in build order among serving(q).
  best(q)      = argmin phase-1 q-error among serving(q).
  If effective(q) != best(q), q is hijacked by effective(q); the stat
  effective(q) is an *interfering* stat for q.

Usage:
    python scripts/exp_option_b_pilot.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --budgets 100000 --out results/p_option_b.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.optimize import build_problem, solve_ilp  # noqa: E402
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402


# --------------------------------------------------------------------------
# Hijacker detection (Phase A)
# --------------------------------------------------------------------------
def serving_stats_for_query(r, deployed, phase1_phys):
    """Return deployed physical-stats (as phase1 indices/keys) serving query r.

    A stat serves q iff its (table, cols, level) is present among q's phase-1
    candidates at that level.  Returns list of (stat_index, phase1 q-error for q).
    """
    out = []
    for cand_key, cand in r.get("candidates", {}).items():
        table = cand["table"]
        cols = tuple(cand["columns"])
        for ls, lv in cand.get("levels", {}).items():
            lvl = int(ls)
            key = (table, cols, lvl)
            if key in deployed:
                # map back to the phase-1 physical stat index
                si = deployed[key]
                out.append((si, lv["qerror"]))
    return out


def detect_hijackers(phase1, dep, build_order):
    """For a deployed set `dep`, detect hijacked queries and the interfering
    (hijacking) physical stats.

    dep : dict {(table, cols_tuple, level): phase1_stat_index}  (build order
          of .items() = deployment/CREATE order = planner's OID walk).
    build_order : list of deployed stat keys in CREATE/OID order (first = winner).

    Returns (affected qids, hijack_stat_indices, serve_map).
    """
    affected = []
    hijack_stats = set()
    for r in phase1["results"]:
        qid = r["qid"]
        srv = []
        for key, si in dep.items():
            table, cols, lvl = key
            hit = None
            for cand in r.get("candidates", {}).values():
                if tuple(cand["columns"]) == cols and cand["table"] == table:
                    for ls, lv in cand.get("levels", {}).items():
                        if int(ls) == lvl:
                            hit = lv["qerror"]
                            break
                    if hit is not None:
                        break
            if hit is not None:
                srv.append((key, si, hit))
        if not srv:
            continue
        # effective = first-built among serving stats (planner OID walk)
        srv_sorted = sorted(srv, key=lambda t: build_order.index(t[0]))
        eff_key, eff_si, eff_qerr = srv_sorted[0]
        _, best_si, best_qerr = min(srv_sorted, key=lambda t: t[2])
        # hijacked iff the first-built (effective) stat is NOT the best, and
        # the difference is non-trivial
        if best_si != eff_si and best_qerr < eff_qerr - 1e-9:
            affected.append(qid)
            hijack_stats.add(eff_si)
    return affected, hijack_stats


def rebuild_problem_without(phys, opts, qb, drop_indices):
    """Return (phys', opts', qb, remap) with the given physical stat indices
    removed from the problem (both phys list and every query's options)."""
    drop = set(drop_indices)
    keep_idx = [i for i in range(len(phys)) if i not in drop]
    remap = {old: new for new, old in enumerate(keep_idx)}
    phys2 = [phys[i] for i in keep_idx]
    opts2 = []
    for q_opts in opts:
        q2 = []
        for o in q_opts:
            if o.stat_index in drop:
                continue
            # copy with remapped stat index
            from dataclasses import replace
            q2.append(replace(o, stat_index=remap[o.stat_index]))
        opts2.append(q2)
    return phys2, opts2, qb, remap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets", default="100000")
    ap.add_argument("--db", default="stats")
    ap.add_argument("--table", default="posts")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--ratio-threshold", type=float, default=1.05)
    ap.add_argument("--max-iters", type=int, default=8)
    ap.add_argument("--out", default="results/p_option_b.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base_by_qid = {r["qid"]: r["qerror_base"] for r in phase1["results"]}
    res_by_qid = {r["qid"]: r for r in phase1["results"]}
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
    byid = {q.qid: q for q in qs}

    summary = []

    # map (table, cols, level) tuple -> phase-1 physical stat index
    key_to_si = {}
    for si, ps in enumerate(phys):
        key_to_si[(ps.table, ps.columns, ps.level)] = si

    def dep_from_ilp(res):
        """Build {(table, cols_tuple, level): si} from an ILPResult, in
        solve_ilp's selected order (= our deployment/CREATE order)."""
        d = {}
        for ps in res.selected_stats:
            d[(ps.table, ps.columns, ps.level)] = key_to_si[(ps.table, ps.columns, ps.level)]
        return d

    for B in budgets:
        print(f"\n===== budget {B//1000} KB =====")
        # 1) raw overlap solution (unconstrained) = Option-B STARTING set
        res_o = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=False)
        dep_o = dep_from_ilp(res_o)
        build_order = list(dep_o.keys())  # iteration order = build/CREATE order
        pred_overlap = res_o.mean_qerror

        # 2) detect hijackers (offline, phase-1 truth + first-built-wins)
        affected, hijack_si = detect_hijackers(phase1, dep_o, build_order)
        n_affected = len(affected)
        print(f"  overlap set: {len(dep_o)} stats, predicted mean q-error "
              f"{pred_overlap:.4f}")
        print(f"  hijacked queries (predicted): {n_affected}/{len(res_by_qid)}")
        print(f"  interfering stats flagged: {len(hijack_si)}")

        # 3) Option-A baseline at same budget (for the quality comparison)
        res_d = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=True)
        print(f"  [Option-A] disjoint: {len(res_d.selected_stats)} stats, "
              f"predicted mean q-error {res_d.mean_qerror:.4f}")

        # 4) repair: drop hijackers, re-solve unconstrained.
        #    NOTE: the re-solve operates on the ORIGINAL (unpruned) problem
        #    minus the hijacking stats, so it may add new candidates.
        phys2, opts2, qb2, remap = rebuild_problem_without(phys, opts, qb, hijack_si)
        # remap old si -> new si using only surviving orig stat indices
        res_r = solve_ilp(phys2, opts2, qb2, B, per_query_cap=1, global_disjoint=False)
        dep_r = {}
        for ps2 in res_r.selected_stats:
            # find original si via (table, cols, level)
            orig_si = key_to_si[(ps2.table, ps2.columns, ps2.level)]
            dep_r[(ps2.table, ps2.columns, ps2.level)] = orig_si
        print(f"  [Option-B repaired] {len(dep_r)} stats, predicted mean "
              f"q-error {res_r.mean_qerror:.4f}")

        # 5) how much of the quality gap to Option-A is recovered?
        print(f"  quality: overlap={pred_overlap:.4f} repaired="
              f"{res_r.mean_qerror:.4f} disjoint(A)={res_d.mean_qerror:.4f}")

        # 6) residual hijacking in the REPAIRED set (should shrink toward 0)
        build_order_r = list(dep_r.keys())
        affected_r, hijack_si_r = detect_hijackers(phase1, dep_r, build_order_r)
        print(f"  repaired residual hijacked queries: {len(affected_r)}")

        # 7) ITERATIVE repair: accumulate a PERMANENT drop set so the MILP never
        #    re-adds a previously-flagged hijacker (prevents re-solve churn).
        banned: set[int] = set()
        cur_dep = dict(dep_o)
        cur_build = list(dep_o.keys())
        cur_problem = (phys, opts, qb)
        hist = []
        for it in range(1, args.max_iters + 1):
            aff, hij = detect_hijackers(phase1, cur_dep, cur_build)
            hist.append({"iter": it, "n_affected": len(aff),
                         "n_banned": len(banned)})
            if not aff:
                break
            banned |= hij
            p2, o2, q2, _ = rebuild_problem_without(*cur_problem, sorted(banned))
            rr = solve_ilp(p2, o2, q2, B, per_query_cap=1, global_disjoint=False)
            cur_dep = {}
            for ps2 in rr.selected_stats:
                orig_si = key_to_si[(ps2.table, ps2.columns, ps2.level)]
                cur_dep[(ps2.table, ps2.columns, ps2.level)] = orig_si
            cur_build = list(cur_dep.keys())
            cur_problem = (p2, o2, q2)
            if it >= args.max_iters:
                break
        aff_final, hij_final = detect_hijackers(phase1, cur_dep, cur_build)
        fn = solve_ilp(*cur_problem, B, per_query_cap=1,
                       global_disjoint=False).mean_qerror
        final_pred = fn
        print(f"  [Option-B iterative x{len(hist)}] {len(cur_dep)} stats, "
              f"predicted mean {final_pred:.4f}, final banned={len(banned)}, "
              f"final hijacked={len(aff_final)}")

        summary.append({
            "budget_bytes": B,
            "overlap_n": len(dep_o),
            "overlap_pred_mean": pred_overlap,
            "overlap_affected": n_affected,
            "hijack_stats_n": len(hijack_si),
            "option_a_n": len(res_d.selected_stats),
            "option_a_pred_mean": res_d.mean_qerror,
            "repaired_n": len(dep_r),
            "repaired_pred_mean": res_r.mean_qerror,
            "repaired_residual_hijacked": len(affected_r),
            "iter_final_n": len(cur_dep),
            "iter_final_pred_mean": final_pred,
            "iter_final_banned": len(banned),
            "iter_final_hijacked": len(aff_final),
            "iter_hist": hist,
            "iter_final_keys": [f"{t}|{','.join(c)}|L{lvl}" for (t, c, lvl) in cur_dep],
            "overlap_keys": [f"{t}|{','.join(c)}|L{lvl}" for (t, c, lvl) in dep_o],
            "hijack_keys": [phys[si].key for si in hijack_si],
            "repaired_keys": [f"{t}|{','.join(c)}|L{lvl}" for (t, c, lvl) in dep_r],
            "option_a_keys": [ps.key for ps in res_d.selected_stats],
        })

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
