#!/usr/bin/env python3
"""Option-B Phase B: real-DB E2E verification.

Offline Phase A predicted that "drop interfering stats + re-solve" (Option-B)
keeps quality but does NOT fully converge to zero hijacked queries under the
first-built-wins model. This script deploys the actual candidate sets on the
REAL PostgreSQL `posts` table and measures the true E2E ratio, which is the
decisive test of whether Option-B truly restores predictability.

Deployments measured at one budget (all on real `posts`, build-order = CREATE
order from the MILP selection):
  * overlap        : unconstrained MILP (best nominal quality, expected to have
                     real planner cross-talk tail)
  * repaired1      : one-shot drop-all-flagged-hijackers + re-solve (unconstrained)
  * repaired_iter  : iterative accumulate-permanent-banned + re-solve
  * disjoint (OptA): global_disjoint=True (Option-A, predictability guarantee)

For each query the E2E measured ratio = measured_qerror / predicted_qerror
(predicted from phase-1 singles). We report mean/median/max ratio and how many
queries exceed a tail threshold, plus the *actual* arithmetic mean q-error of
the whole 632-query workload under each deployment.

Usage:
    python scripts/exp_option_b_phaseB.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --budgets 100000 --db stats --table posts --target 10000 \
        --max-iters 8 --out results/p_option_b_phaseB.json
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

from exp_option_b_pilot import detect_hijackers, rebuild_problem_without  # noqa: E402


def pred_per_query(phase1, deployed):
    """predicted per-query q-error: min over deployed serving options, else base."""
    deployed_map = {}
    for (table, cols, level) in deployed:
        deployed_map[(table, tuple(sorted(cols)), level)] = True
    out = {}
    for r in phase1["results"]:
        base = r["qerror_base"]
        best = base
        for cand in r.get("candidates", {}).values():
            table = cand["table"]
            cols = tuple(cand["columns"])
            for ls, lv in cand.get("levels", {}).items():
                if (table, tuple(sorted(cols)), int(ls)) in deployed_map:
                    best = min(best, lv["qerror"])
        out[r["qid"]] = best
    return out


def dep_from_ilp(res, key_to_si):
    d = {}
    for ps in res.selected_stats:
        d[(ps.table, ps.columns, ps.level)] = key_to_si[(ps.table, ps.columns, ps.level)]
    return d


def build_deployments(phase1, phys, opts, qb, B, key_to_si, max_iters):
    """Return list of (label, kind, dep {(t,cols,lvl):si}) in build order."""
    out = []
    # overlap
    res_o = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=False)
    out.append(("overlap", "overlap", dep_from_ilp(res_o, key_to_si)))
    # Option-A disjoint
    res_d = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=True)
    out.append(("disjoint(A)", "disjoint", dep_from_ilp(res_d, key_to_si)))
    # one-shot repair
    dep_o = dep_from_ilp(res_o, key_to_si)
    build_o = list(dep_o.keys())
    _, hij = detect_hijackers(phase1, dep_o, build_o)
    if hij:
        p2, o2, q2, _ = rebuild_problem_without(phys, opts, qb, sorted(hij))
        res_r = solve_ilp(p2, o2, q2, B, per_query_cap=1, global_disjoint=False)
        dep_r = {}
        for ps2 in res_r.selected_stats:
            ori = key_to_si[(ps2.table, ps2.columns, ps2.level)]
            dep_r[(ps2.table, ps2.columns, ps2.level)] = ori
        out.append(("repaired1", "repaired", dep_r))
    # iterative repair (accumulate permanent banned set)
    banned = set()
    cur_dep = dict(dep_o)
    cur_build = list(dep_o.keys())
    cur_prob = (phys, opts, qb)
    for _ in range(max_iters):
        aff, h = detect_hijackers(phase1, cur_dep, cur_build)
        if not aff:
            break
        banned |= h
        p2, o2, q2, _ = rebuild_problem_without(*cur_prob, sorted(banned))
        rr = solve_ilp(p2, o2, q2, B, per_query_cap=1, global_disjoint=False)
        cur_dep = {}
        for ps2 in rr.selected_stats:
            ori = key_to_si[(ps2.table, ps2.columns, ps2.level)]
            cur_dep[(ps2.table, ps2.columns, ps2.level)] = ori
        cur_build = list(cur_dep.keys())
        cur_prob = (p2, o2, q2)
    aff_f, _ = detect_hijackers(phase1, cur_dep, cur_build)
    out.append(("repaired_iter", "repaired", cur_dep))
    return out, len(banned), len(aff_f)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets", default="100000")
    ap.add_argument("--db", default="stats")
    ap.add_argument("--table", default="posts")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--max-iters", type=int, default=8)
    ap.add_argument("--tail", type=float, default=1.05)
    ap.add_argument("--out", default="results/p_option_b_phaseB.json")
    ap.add_argument("--only", default="", help="dash-separated list of deployment labels to measure (e.g. overlap,repaired1)")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base_by_qid = {r["qid"]: r["qerror_base"] for r in phase1["results"]}
    key_to_si = {}
    for si, ps in enumerate(phys):
        key_to_si[(ps.table, ps.columns, ps.level)] = si
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))

    results = []

    for B in budgets:
        deploys, n_banned, n_aff_iter = build_deployments(
            phase1, phys, opts, qb, B, key_to_si, args.max_iters)
        if args.only:
            keep = set(x.strip() for x in args.only.split(","))
            deploys = [d for d in deploys if d[0] in keep]

        prefix = "ob_"
        with connect(DBConfig(host="localhost", port=5432, user="postgres",
                              password="postgres", dbname=args.db)) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            # clean slate
            cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
            for (n,) in cur.fetchall():
                cur.execute(f"DROP STATISTICS IF EXISTS {n}")
            for label, kind, dep in deploys:
                # predicted per-query (from phase-1 singles)
                pred = pred_per_query(phase1, list(dep.keys()))
                # build stats in dep order (build order = planner OID walk)
                cur.execute(f"SET default_statistics_target={args.target}")
                by_table = {}
                for (table, cols, lvl) in dep:
                    by_table.setdefault(table, []).append((cols, lvl))
                names = []
                for table, slots in by_table.items():
                    for (cols, lvl) in slots:
                        nm = f"{prefix}s_{len(names)}"
                        names.append(nm)
                        cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                        cur.execute(f"CREATE STATISTICS {nm} (mcv) ON "
                                    f"{', '.join(cols)} FROM {table}")
                        if lvl > 0:
                            cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
                    cur.execute(f"ANALYZE {table}")
                # measure E2E
                ratios = []
                qerrs = []
                n_tail = 0
                affected_ids = []
                for q in qs:
                    if q.qid not in pred:
                        continue
                    pv = pred[q.qid]
                    mv = estimate_count_query(conn, q.sql, actual=q.ground_truth).qerror
                    if mv is None:
                        continue
                    qerrs.append(mv if mv == mv else base_by_qid[q.qid])
                    # only meaningful where the model claims to act
                    if pv < base_by_qid[q.qid] - 1e-9:
                        r = mv / pv if pv > 0 else float("nan")
                        ratios.append(r)
                        if r == r and r > args.tail:
                            n_tail += 1
                            affected_ids.append(q.qid)
                ratios = [r for r in ratios if r == r]
                stats = {
                    "n": len(ratios),
                    "mean": float(np.mean(ratios)) if ratios else None,
                    "median": float(np.median(ratios)) if ratios else None,
                    "max": float(np.max(ratios)) if ratios else None,
                    "n_gt_tail": n_tail,
                }
                actual_mean = float(np.mean([q for q in qerrs if q == q])) if qerrs else None
                print(f"[{label:14s}] n_st={len(dep):2d} pred_mean_ratio {stats['mean']:.4f} "
                      f"median {stats['median']:.4f} max {stats['max']:.4f} "
                      f"tail>{args.tail}: {n_tail}/{len(ratios)}  actual_mean_qerr {actual_mean:.4f}")
                results.append({
                    "budget_bytes": B, "label": label, "kind": kind,
                    "n_stats": len(dep), "ratio": stats, "actual_mean_qerr": actual_mean,
                    "tail_threshold": args.tail, "affected_ids": affected_ids,
                    "keys": [f"{t}|{','.join(c)}|L{lvl}" for (t, c, lvl) in dep],
                })
                # cleanup
                for nm in names:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                for table in by_table:
                    cur.execute(f"ANALYZE {table}")

    out = {"budgets": budgets, "n_banned_iter": n_banned, "n_affected_iter": n_aff_iter,
           "deployments": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
