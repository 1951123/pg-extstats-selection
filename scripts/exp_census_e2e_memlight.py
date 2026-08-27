#!/usr/bin/env python3
"""Census end-to-end deployment: planner-interference (RQ5) and Option-A/B (RQ6)
--- MEMORY-LIGHT version ---

Same experiment as exp_census_e2e.py but engineered to avoid the memory blow-up
that OOM'd the machine. On the wide Census problem (69,431 physical stats ->
159,158-variable MILP) the original script exploded because Option-A's
`global_disjoint=True` adds O(n_stats^2) pairwise column-overlap constraints
(-> tens of millions of constraints, tens of GB) and Option-B re-solved the big
MILP up to max-iters times per budget.

This version:
  * solves the overlap MILP ONCE per budget (global_disjoint=False, sparse
    O(n_opt) constraints, memory-friendly), then gc.collect().
  * Option-A = a HEURISTIC global-disjoint prune of the (small) chosen overlap
    set: among overlapping stats keep the highest-total-improvement one. This is
    the exact semantics of O-A (drop overlaps, keep best) at negligible memory.
  * Option-B = use the lightweight mechanism-correct `detect_hijackers`
    (pure phase-1 dictionary walk, no MILP) to find hijacked stats, then drop
    them to get the repaired set (optionally iterate a few times, again cheap
    because it reuses the same small chosen set).
  * measurement is EXPLAIN-only (estimate_count_query, no execution), so the
    468-query evaluation is fast; deployment ANALYZE on climate at
    statistics_target=10000 is ~25-45s (measured).

For each budget we deploy, on the live `census` database:
  overlap        : raw unconstrained MILP set (Option-B starting point)
  disjoint (A)   : heuristic global-disjoint prune of the overlap set
  repaired (B)   : overlap minus detected hijackers
and report real E2E geometric/arithmetic P90/max q-error, fidelity-ratio tail
(>1.05), storage cost, and deploy wall-clock.

Usage:
    source .venv/bin/activate
    python scripts/exp_census_e2e_memlight.py \
        --input results/phase1_census_mcv_6level.json \
        --budgets 40000,100000,250000 --db census --target 10000 \
        --max-iters 3 --out results/p_census_e2e_memlight.json
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query, qerror  # noqa: E402
from extstats.optimize import build_problem, solve_ilp  # noqa: E402

from exp_option_b_pilot import detect_hijackers  # noqa: E402


def math_gmean(v):
    vs = [x for x in v if x == x and x > 0]
    return math.exp(sum(math.log(x) for x in vs) / len(vs)) if vs else float("nan")


def load_census_queries(phase1):
    """Return dict qid -> (sql, actual). query.N <-> N-th query line."""
    sqls = []
    for raw in Path("benchmarks/Census/queries/query.sql").read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("--"):
            continue
        sqls.append(raw.split("||")[0].strip().rstrip(";"))
    by_actual = {r["qid"]: r.get("actual") for r in phase1["results"]}
    out = {}
    for i, sql in enumerate(sqls, 1):
        qid = f"query.{i}"
        if qid in by_actual:
            out[qid] = (sql, by_actual[qid])
    return out


def total_improvement(phase1, dep):
    """Sum over queries of (base - effective_pred) for a deployed stat set."""
    dep_map = {(t, tuple(sorted(c)), l): l for (t, c, l) in dep}
    total = 0.0
    for r in phase1["results"]:
        qbase = r["qerror_base"]
        best = qbase
        for cand in r.get("candidates", {}).values():
            cols = tuple(cand["columns"])
            for ls, lv in cand.get("levels", {}).items():
                if (cand["table"], tuple(sorted(cols)), int(ls)) in dep_map:
                    if lv["qerror"] < best:
                        best = lv["qerror"]
        if best < qbase:
            total += (qbase - best)
    return total


def heuristic_disjoint(overlap_keys, phase1):
    """Global-disjoint prune of the overlap set: keep column-disjoint stats,
    retaining the highest-total-improvement one per overlapping cluster.

    overlap_keys : list of (table, cols_tuple, level) (the chosen set).
    Returns a subset that is pairwise column-disjoint, as the O-A semantics.
    """
    imp = {}
    for key in overlap_keys:
        imp[key] = total_improvement(phase1, [key])
    order = sorted(overlap_keys, key=lambda k: -imp[k])
    kept = []
    kept_colsets = []
    for key in order:
        table, cols, lvl = key
        cs = frozenset(cols)
        if all(cs.isdisjoint(ks) for ks in kept_colsets):
            kept.append(key)
            kept_colsets.append(cs)
    return kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets", default="40000,100000,250000")
    ap.add_argument("--db", default="census")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--max-iters", type=int, default=3,
                    help="Option-B: how many drop-relabel iterations (cheap)")
    ap.add_argument("--tail", type=float, default=1.05)
    ap.add_argument("--out", default="results/p_census_e2e_memlight.json")
    ap.add_argument("--only", default="")
    argv = argv if argv is not None else sys.argv[1:]
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base_by_qid = {r["qid"]: r["qerror_base"] for r in phase1["results"]}
    key_to_si = {(ps.table, ps.columns, ps.level): i for i, ps in enumerate(phys)}
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    queries = load_census_queries(phase1)
    print(f"loaded {len(queries)} census queries; {len(phys)} phys stats; "
          f"n_queries={len(qb)}", flush=True)

    results = []
    for B in budgets:
        # ---- overlap MILP once (sparse, memory-friendly) ----
        gc.collect()
        t0 = time.time()
        res = solve_ilp(phys, opts, qb, B, per_query_cap=1, global_disjoint=False)
        print(f"  B={B//1000}K overlap solve {time.time()-t0:.1f}s "
              f"-> {len(res.selected_stats)} stats", flush=True)
        dep_overlap = {(ps.table, ps.columns, ps.level): key_to_si[(ps.table, ps.columns, ps.level)]
                       for ps in res.selected_stats}
        del res
        gc.collect()

        # ---- Option-A heuristic disjoint prune ----
        overlap_keys = list(dep_overlap.keys())
        disjoint_keys = heuristic_disjoint(overlap_keys, phase1)

        # ---- Option-B: drop detected hijackers (lightweight), optionally iterate ----
        repaired_keys = list(overlap_keys)
        for _ in range(args.max_iters):
            dep_r = {k: dep_overlap[k] for k in repaired_keys}
            build_r = list(repaired_keys)
            _, hij = detect_hijackers(phase1, dep_r, build_r)
            if not hij:
                break
            # hij are phase1 stat indices -> drop the corresponding dep keys
            drop_keys = {k for k, si in dep_r.items() if si in hij}
            if not drop_keys:
                break
            repaired_keys = [k for k in repaired_keys if k not in drop_keys]
        print(f"  B={B//1000}K  overlap={len(overlap_keys)} "
              f"disjointA={len(disjoint_keys)} repairedB={len(repaired_keys)}",
              flush=True)

        # ---- deploy + evaluate each set ----
        sets = [("overlap", overlap_keys, dep_overlap),
                ("disjoint(A)", disjoint_keys, {k: dep_overlap[k] for k in disjoint_keys}),
                ("repaired(B)", repaired_keys, {k: dep_overlap[k] for k in repaired_keys})]
        if args.only:
            keep = set(x.strip() for x in args.only.split(","))
            sets = [s for s in sets if s[0] in keep]

        prefix = f"ce{B}_"
        with connect(DBConfig(host="localhost", port=5432, user="postgres",
                              password="postgres", dbname=args.db)) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
            for (n,) in cur.fetchall():
                cur.execute(f"DROP STATISTICS IF EXISTS {n}")
            for label, keys, dep_map in sets:
                cur.execute(f"SET default_statistics_target={args.target}")
                names = []
                t_deploy = time.time()
                cur.execute(f"ANALYZE climate")  # restore clean single-col baseline
                for (table, cols, lvl) in keys:
                    nm = f"{prefix}s_{len(names)}"
                    names.append(nm)
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                    cur.execute(f"CREATE STATISTICS {nm} (mcv) ON "
                                f"{', '.join(cols)} FROM {table}")
                    if lvl > 0:
                        cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
                cur.execute(f"ANALYZE climate")
                deploy_s = time.time() - t_deploy

                # predicted per-query q-error under this deployment (min over
                # deployed serving options, else baseline): precompute once
                dep_map_flat = set()
                for (t, c, l) in dep_map:
                    dep_map_flat.add((t, tuple(sorted(c)), l))
                pred = {}
                for r_i in phase1["results"]:
                    pv = r_i["qerror_base"]
                    for cand in r_i.get("candidates", {}).values():
                        cols = tuple(cand["columns"])
                        for ls, lv in cand.get("levels", {}).items():
                            if (cand["table"], tuple(sorted(cols)), int(ls)) in dep_map_flat:
                                if lv["qerror"] < pv:
                                    pv = lv["qerror"]
                    pred[r_i["qid"]] = pv

                ratios, qerrs, n_tail, affected_ids = [], [], 0, []
                for qid, (sql, actual) in queries.items():
                    if actual is None:
                        continue
                    est = estimate_count_query(conn, sql, actual=actual).estimate
                    mv = qerror(est, actual)
                    qerrs.append(mv)
                    pv = pred.get(qid)
                    base = base_by_qid.get(qid)
                    if pv is not None and base is not None and pv < base - 1e-9:
                        r = mv / pv if pv > 0 else float("nan")
                        ratios.append(r)
                        if r == r and r > args.tail:
                            n_tail += 1
                            affected_ids.append(qid)
                ratios = [r for r in ratios if r == r]
                qe = [q for q in qerrs if q == q]
                cost = sum(phys[key_to_si[k]].cost for k in keys)
                entry = {
                    "budget_bytes": B, "label": label,
                    "n_stats": len(keys), "used_bytes": cost,
                    "ratio": {"n": len(ratios),
                              "mean": float(np.mean(ratios)) if ratios else None,
                              "median": float(np.median(ratios)) if ratios else None,
                              "max": float(np.max(ratios)) if ratios else None,
                              "n_gt_tail": n_tail},
                    "tail_threshold": args.tail, "affected_ids": affected_ids,
                    "deploy_s": round(deploy_s, 1),
                    "real_gmean_qerr": round(float(math_gmean(qe)), 4) if qe else None,
                    "real_arith_mean_qerr": round(float(np.mean(qe)), 4) if qe else None,
                    "real_p90_qerr": round(float(np.percentile(qe, 90)), 3) if qe else None,
                    "real_max_qerr": round(float(np.max(qe)), 2) if qe else None,
                }
                results.append(entry)
                print(f"[{label:12s}] B={B//1000:>4}K n_st={len(keys):>3} "
                      f"gmean={entry['real_gmean_qerr']} mean={entry['real_arith_mean_qerr']} "
                      f"p90={entry['real_p90_qerr']} max={entry['real_max_qerr']} "
                      f"tail>{args.tail}: {n_tail}/{len(ratios)} deploy={deploy_s:.0f}s",
                      flush=True)
                for nm in names:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                cur.execute("ANALYZE climate")
        gc.collect()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"input": args.input, "target": args.target, "max_iters": args.max_iters,
         "deployments": results}, indent=2))
    print(f"\n[saved] {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
