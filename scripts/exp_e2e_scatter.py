#!/usr/bin/env python3
"""Experiment 4: systematic predicted-vs-E2E validation (scatter).

Upgrades the three-stage E2E story from a few hand-picked deployments to a
systematic sweep. We generate MANY deployment sets on the real PostgreSQL
`posts` table (92k rows, fast to ANALYZE):

  * singletons      : each high-benefit statistic deployed alone
  * disjoint MILP   : whole-workload budgeted solutions with
                      `global_disjoint=True` (pairwise column-disjoint sets)
  * overlapping MILP: whole-workload budgeted solutions WITHOUT the constraint

For every (deployment, query) we compare the model's PREDICTED per-query
q-error (read from the mask phase-1 data: per-candidate per-level q-error) to
the E2E MEASURED q-error on the real planner, and write a predicted-vs-measured
scatter (diagonal = perfectly predictable).

If the review's Major Concern 5 is unfounded --- disjointness generally restores
exact predictability --- the disjoint points should hug the diagonal while the
overlapping points scatter above it.

Usage:
    source .venv/bin/activate
    python scripts/exp_e2e_scatter.py \\
        --input results/phase1_ceb_single_mask_full_multi.json \\
        --db stats --table posts --target 10000 \\
        --budgets 10000,40000,100000,250000 \\
        --out results/p4_e2e_scatter.json \\
        --fig paper/figures/e2e_predict
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


def pred_per_query(phase1, deployed):
    """For a set of deployed (table, columns, level), predicted per-query q-error.

    deployed: dict {(table, columns_tuple, level): level}.
    Returns dict qid -> predicted qerror (min option among deployed, else base).
    """
    deployed_map = {}
    for (table, cols, level), _ in deployed.items():
        deployed_map[(table, tuple(sorted(cols)), level)] = True
    out = {}
    for r in phase1["results"]:
        base = r["qerror_base"]
        best = base
        for ck, cand in r.get("candidates", {}).items():
            table = cand["table"]
            cols = tuple(cand["columns"])
            for ls, lv in cand.get("levels", {}).items():
                if (table, tuple(sorted(cols)), int(ls)) in deployed_map:
                    best = min(best, lv["qerror"])
        out[r["qid"]] = best
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--table", default="posts")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--budgets", default="10000,40000,100000,250000")
    ap.add_argument("--n-singleton", type=int, default=5)
    ap.add_argument("--out", default="results/p4_e2e_scatter.json")
    ap.add_argument("--fig", default="paper/figures/e2e_predict")
    args = ap.parse_args(argv)

    # ---- data ----
    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base_by_qid = {r["qid"]: r["qerror_base"] for r in phase1["results"]}
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
    byid = {q.qid: q for q in qs}

    # ---- generate deployment sets ----
    deployments = []  # list of (label, kind, dict[cols->level])
    # singletons: top-N highest aggregated improvement single options
    # aggregate improvement per physical stat = sum over queries
    agg = {}
    for q_idx, qopts in enumerate(opts):
        for o in qopts:
            ps = phys[o.stat_index]
            key = (ps.table, tuple(ps.columns), ps.level)
            agg[key] = agg.get(key, 0.0) + max(0.0, qb[q_idx] - o.qerror)
    top = sorted(agg.items(), key=lambda kv: -kv[1])[: args.n_singleton]
    for key, _ in top:
        table, cols, level = key
        deployments.append((f"singleton {'+'.join(cols)}@L{level}", "singleton",
                            {(table, cols, level): level}))
    for B in budgets:
        res_d = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=True)
        dep = {(ps.table, tuple(ps.columns), ps.level): ps.level
               for ps in res_d.selected_stats}
        deployments.append((f"disjoint@{B//1000}KB", "disjoint", dep))
        res_o = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=False)
        dep_o = {(ps.table, tuple(ps.columns), ps.level): ps.level
                 for ps in res_o.selected_stats}
        deployments.append((f"overlap@{B//1000}KB", "overlap", dep_o))

    # ---- run E2E over deployments ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    records = []   # list of dict(qid, kind, label, pred, meas, base)
    dep_summary = []
    cfg = DBConfig(host="localhost", port=5432, user="postgres", password="postgres",
                   dbname=args.db)
    prefix = "e4_"
    with connect(cfg) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        # ensure clean slate
        cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
        for (n,) in cur.fetchall():
            cur.execute(f"DROP STATISTICS IF EXISTS {n}")
        for label, kind, dep in deployments:
            # predicted (read, not from ILP, to be exact for arbitrary dep)
            pred = pred_per_query(phase1, dep)
            # build stats, grouped by table
            cur.execute(f"SET default_statistics_target={args.target}")
            by_table = {}
            for (table, cols, lvl), _ in dep.items():
                by_table.setdefault(table, []).append((cols, lvl))
            names = []
            for table, slots in by_table.items():
                for j, (cols, lvl) in enumerate(slots):
                    name = f"{prefix}s_{len(names)}"
                    names.append(name)
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                    cur.execute(f"CREATE STATISTICS {name} (mcv) ON "
                                f"{', '.join(cols)} FROM {table}")
                    if lvl > 0:
                        cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {int(lvl)}")
                cur.execute(f"ANALYZE {table}")
            # measure all queries with candidates
            meas = {}
            for q in qs:
                if q.qid not in pred:
                    continue
                r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                meas[q.qid] = r.qerror if r.qerror is not None else float("nan")
            # collect records where the query is actually affected by this dep
            ratio_list = []
            for qid in pred:
                if qid not in meas:
                    continue
                pv = pred[qid]
                mv = meas[qid]
                # only meaningful where the model claims to act (pred < base)
                if not (pv < base_by_qid[qid] - 1e-9):
                    continue
                records.append({"qid": qid, "kind": kind, "label": label,
                                "pred": pv, "meas": mv, "base": base_by_qid[qid],
                                "ratio": mv / pv if pv > 0 else float("nan")})
                if mv == mv and pv > 0 and mv >= 1:
                    ratio_list.append(mv / pv)
            rmean = float(np.mean(ratio_list)) if ratio_list else float("nan")
            dep_summary.append({"label": label, "kind": kind,
                                "n_stats": len(dep), "pred_mean": float(np.mean(
                                    [pred[q] for q in pred if pred[q] < base_by_qid[q]])) if any(
                                        pred[q] < base_by_qid[q] for q in pred) else None,
                                "mean_ratio": rmean})
            # cleanup
            for name in names:
                cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            for table in by_table:
                cur.execute(f"ANALYZE {table}")
            print(f"[e2e] {label:22s} kind={kind:9s} stats={len(dep):>2} "
                  f"mean_ratio={rmean:.3f}")

    # ---- plot ----
    kinds = {"disjoint": "tab:green", "overlap": "tab:red", "singleton": "tab:blue"}
    fig, ax = plt.subplots(figsize=(5.2, 4.2))
    for kind in kinds:
        pts = [r for r in records if r["kind"] == kind and r["meas"] == r["meas"]]
        if not pts:
            continue
        xs = [r["pred"] for r in pts]
        ys = [r["meas"] for r in pts]
        ax.scatter(xs, ys, s=18, alpha=0.75, c=kinds[kind], label=kind,
                   edgecolors="none")
    lim = (0.8, max([r["meas"] for r in records if r["meas"] == r["meas"]] + [2.0]) * 1.05)
    ax.plot([lim[0], lim[1]], [lim[0], lim[1]], "k--", lw=1, label="diagonal (ratio 1)")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("predicted q-error")
    ax.set_ylabel("E2E measured q-error")
    ax.legend(fontsize=8)
    ax.set_xlim(lim); ax.set_ylim(lim)
    fig.tight_layout()
    Path(args.fig).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.fig + ".pdf")
    print(f"[saved] {args.fig}.pdf")

    # ---- summary table (per deployment class) ----
    print(f"\n=== per-deployment mean ratio (E2E / predicted) ===")
    for d in dep_summary:
        print(f"  {d['label']:22s} {d['kind']:9s} n={d['n_stats']:>2} "
              f"mean_ratio={d['mean_ratio']:.3f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"deployments": dep_summary,
         "records_kind_counts": {k: sum(1 for r in records if r["kind"] == k)
                                 for k in kinds},
         "records": records}, indent=2))
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
