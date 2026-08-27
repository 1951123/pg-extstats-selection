#!/usr/bin/env python3
"""Experiment: workload runtime (EXPLAIN ANALYZE) vs. storage budget.

For each storage budget B in --budgets (0 = no extended statistics), take the
MILP-selected statistics from a 6-level phase-1 mask JSON, deploy them on the
real table(s), then measure the workload's real EXECUTION time (plus PLANNING
time) by truly running every query with EXPLAIN (ANALYZE, FORMAT JSON),
repeating --runs times per query and taking the median.

Design notes
------------
* single-column statistics_target is held at --target (default 10000) for BOTH
  the baseline (B=0) and every budgeted deployment, so the only thing that
  changes across conditions is the set of extended statistics. This lets the
  phase-1 q-error (measured at single_col_target=10000) be reproduced and lets
  any execute-time change be attributed to the extended statistics.
* Execution time is noisy, hence --runs medians per query.
* We also record the planner's ESTIMATED row count from the same ANALYZE plan,
  so q-error and bad-plan detection need no extra EXPLAIN pass.

Usage
-----
    source .venv/bin/activate
    python scripts/exp_runtime_budget.py \\
        --input results/phase1_ceb_single_mask_6level.json \\
        --workload stats_CEB_single --db stats \\
        --budgets 0,10000,40000,100000,250000 --runs 5 --target 10000 \\
        --out results/exp_runtime_stats.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.optimize import build_problem, solve_ilp  # noqa: E402
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402


def load_workload(workload: str):
    """Return list of (qid, sql, true_card)."""
    if workload == "stats_CEB_single":
        qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
        return [(q.qid, q.sql, q.ground_truth) for q in qs]
    elif workload == "stats_CEB":
        # stats_CEB join workload: format "qid||SQL" per line (no ground truth).
        # The base-table statistics come from the stats_CEB_single 6-level JSON,
        # so we reuse the same deployed statistics and measure whether they
        # change the join plan / execution time.
        out = []
        for line_no, raw in enumerate(
                Path("benchmarks/stats_CEB/queries/stats_CEB.sql").read_text().splitlines(), 1):
            raw = raw.strip()
            if not raw or raw.startswith("--"):
                continue
            if "||" not in raw:
                continue
            qid, _, sql = raw.partition("||")
            sql = sql.strip().rstrip(";")
            if sql:
                out.append((qid.strip(), sql, None))
        return out
    elif workload == "Census":
        # format: SQL||true_card per line
        out = []
        for line_no, raw in enumerate(
                Path("benchmarks/Census/queries/query.sql").read_text().splitlines(), 1):
            raw = raw.strip()
            if not raw or raw.startswith("--"):
                continue
            parts = raw.split("||")
            sql = parts[0].strip()
            tc = int(parts[1]) if len(parts) >= 2 and parts[1].strip() else None
            if sql:
                out.append((f"q.{line_no}", sql, tc))
        return out
    raise ValueError(f"unknown workload {workload!r}")


def _scan_estimate(node):
    """Return the estimated rows of the first table-access (Scan) node.

    For ``SELECT COUNT(*) ...`` the top-level node is an Aggregate whose
    ``Plan Rows`` is 1, so we recurse down to the first Seq/Index Scan (the
    filtered input) to recover the true selectivity estimate.
    """
    nt = node.get("Node Type", "")
    if "Scan" in nt:
        return int(node["Plan Rows"])
    for ch in node.get("Plans", []):
        est = _scan_estimate(ch)
        if est is not None:
            return est
    return None


def analyze_once(cur, sql: str):
    """EXPLAIN (ANALYZE, FORMAT JSON); real execution.

    Returns (exec_ms, plan_ms, est_rows, signature). est_rows is the estimated
    cardinality of the filtered table access (used for a q-error check without
    an extra estimate-only pass); signature is a canonical plan-class string
    (node-type chain + per-node shape tokens) so we can detect whether the
    query's plan CATEGORY changed across deployments.
    """
    cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)
    rows = cur.fetchall()
    plan = None
    for r in rows:
        val = r.get("QUERY PLAN") if isinstance(r, dict) else r[0]
        if isinstance(val, list):
            plan = val
            break
    if plan is None:
        return None, None, None, None
    top = plan[0]
    exec_ms = top.get("Execution Time")
    plan_ms = top.get("Planning Time")
    est = _scan_estimate(top["Plan"])
    sig = _plan_signature(top["Plan"])
    return exec_ms, plan_ms, est, sig


def _plan_signature(node) -> str:
    """Canonical plan-class string for one plan tree.

    Walks the tree and emits one token per node: the Node Type, and for a Scan
    node whether it carries a Filter and whether it is estimated selective. This
    distinguishes e.g. ``Aggregate -> Seq Scan (no filter)`` from
    ``Aggregate -> Seq Scan (filter)``; two plans with the same signature are
    treated as the same plan class. Deterministic (does not read timing).
    """
    nt = node.get("Node Type", "?")
    tokens = [nt]
    if "Scan" in nt:
        has_filter = "Filter" in node
        tokens.append("F" if has_filter else "~")
    children = node.get("Plans", [])
    for ch in sorted(children, key=lambda n: n.get("Node Type", "")):
        tokens.append(_plan_signature(ch))
    return ">".join(tokens)


def deploy_stats(conn, prefix: str, dep):
    """Create extended statistics for a deployment dict {(table,cols,lvl):lvl}.

    dep maps (table, columns_tuple, level) -> level. Returns list of created
    stat names.
    """
    cur = conn.cursor()
    by_table = {}
    for (table, cols, lvl), _ in dep.items():
        by_table.setdefault(table, []).append((cols, lvl))
    names = []
    for table, slots in by_table.items():
        for cols, lvl in slots:
            nm = f"{prefix}s_{len(names)}"
            names.append(nm)
            cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
            cur.execute(f"CREATE STATISTICS {nm} (mcv) ON "
                        f"{', '.join(cols)} FROM {table}")
            if lvl > 0:
                cur.execute(f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
        cur.execute(f"ANALYZE {table}")
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--workload", default="stats_CEB_single")
    ap.add_argument("--db", default="stats")
    ap.add_argument("--budgets", default="0,10000,40000,100000,250000")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--out", default="results/exp_runtime_stats.json")
    argv = argv if argv is not None else sys.argv[1:]
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    phys, opts, qb = build_problem(phase1)
    base_by_qid = {r["qid"]: r["qerror_base"] for r in phase1["results"]}
    actual_by_qid = {r["qid"]: r["actual"] for r in phase1["results"]}
    budgets = [int(x) for x in args.budgets.split(",") if x.strip() != ""]
    queries = load_workload(args.workload)
    cfg = DBConfig(host="localhost", port=5432, user="postgres",
                   password="postgres", dbname=args.db)

    # pre-solve each budget's deployment
    deployments = {}  # B -> {(table,cols,lvl):lvl}
    for B in budgets:
        if B == 0:
            deployments[B] = {}
        else:
            res = solve_ilp(phys, opts, qb, B, per_query_cap=1,
                            global_disjoint=False)
            deployments[B] = {(ps.table, tuple(ps.columns), ps.level): ps.level
                              for ps in res.selected_stats}

    prefix = "rt_"
    base_sig: dict = {}  # baseline (B=0) per-query plan class, for change detection
    summary = []
    conn = psycopg.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                           password=cfg.password, dbname=cfg.dbname,
                           autocommit=True)
    try:
        cur = conn.cursor()
        # clean slate once
        cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
        for (n,) in cur.fetchall():
            cur.execute(f"DROP STATISTICS IF EXISTS {n}")

        for B in budgets:
            dep = deployments[B]
            cur.execute(f"SET default_statistics_target={args.target}")
            # Re-ANALYZE every table that any candidate statistic lives on, so
            # that both the baseline (B=0) and each budgeted deployment see the
            # same single-column statistics at --target; the only thing that
            # changes across conditions is the set of extended statistics.
            involved = {c["table"] for r in phase1["results"]
                        for c in r.get("candidates", {}).values()}
            names = deploy_stats(conn, prefix, dep) if dep else []
            for table in involved:
                cur.execute(f"ANALYZE {table}")

            # ---- measure ----
            t0 = time.time()
            all_exec = {qid: [] for qid, _, _ in queries}
            all_plan = {qid: [] for qid, _, _ in queries}
            all_est = {qid: [] for qid, _, _ in queries}
            all_sig = {qid: [] for qid, _, _ in queries}
            n_fail = 0
            for qid, sql, tc in queries:
                for _ in range(args.runs):
                    try:
                        e, p, est, sig = analyze_once(cur, sql)
                    except Exception:
                        e = p = est = sig = None
                    if e is None or p is None:
                        n_fail += 1
                        continue
                    all_exec[qid].append(e)
                    all_plan[qid].append(p)
                    all_est[qid].append(est)
                    if sig is not None:
                        all_sig[qid].append(sig)
            wall = time.time() - t0
            exec_sums = []
            plan_sums = []
            qerrs = []
            # majority signature per query under this deployment
            sig_now = {}
            for qid, sql, tc in queries:
                if not all_exec[qid]:
                    continue
                em = statistics.median(all_exec[qid])
                pm = statistics.median(all_plan[qid])
                em_est = statistics.median(all_est[qid])
                exec_sums.append(em)
                plan_sums.append(pm)
                actual = actual_by_qid.get(qid, tc)
                if em_est is not None and actual:
                    q = max(em_est, actual) / min(em_est, actual)
                    qerrs.append(q)
                if all_sig[qid]:
                    sig_now[qid] = Counter(all_sig[qid]).most_common(1)[0][0]
            if B == 0:
                base_sig = dict(sig_now)
            # how many queries changed plan class vs baseline
            n_plan_changed = 0
            plan_changed_ids = []
            for qid, sql, tc in queries:
                if qid not in base_sig or qid not in sig_now:
                    continue
                if base_sig[qid] != sig_now[qid]:
                    n_plan_changed += 1
                    plan_changed_ids.append(qid)
            exec_sums_s = np.array(exec_sums) / 1000.0
            plan_sums_s = np.array(plan_sums) / 1000.0
            entry = {
                "budget_bytes": B,
                "n_stats": len(dep),
                "stats": [f"{t}({','.join(c)})@L{lvl}" for (t, c, lvl) in dep],
                "n_queries_measured": len(exec_sums),
                "total_exec_s": float(exec_sums_s.sum()),
                "mean_exec_ms": float(np.mean(exec_sums)),
                "median_exec_ms": float(np.median(exec_sums)),
                "p90_exec_ms": float(np.percentile(exec_sums, 90)),
                "max_exec_ms": float(np.max(exec_sums)),
                "total_plan_s": float(plan_sums_s.sum()),
                "mean_plan_ms": float(np.mean(plan_sums)),
                "wall_s": wall,
                "n_fail": n_fail,
                "n_plan_changed_vs_baseline": n_plan_changed,
                "plan_changed_ids": plan_changed_ids,
                "unique_plan_classes": sorted(set(sig_now.values())),
                "per_query": {qid: {"exec_median_ms": all_exec[qid][
                    len(all_exec[qid]) // 2] if all_exec[qid] else None,
                    "plan_median_ms": statistics.median(all_plan[qid])
                        if all_plan[qid] else None,
                    "plan_class": sig_now.get(qid)}
                    for qid, _, _ in queries if all_exec[qid]},
            }
            if qerrs:
                entry["mean_qerr"] = float(np.mean(qerrs))
                entry["max_qerr"] = float(np.max(qerrs))
                entry["n_qerr_gt2"] = int(np.sum(np.array(qerrs) > 2))
            summary.append(entry)

            print(f"\n=== budget {B//1000}KB  stats={len(dep)} ===")
            for (t, c, lvl) in dep:
                print(f"   {t}({','.join(c)})@L{lvl}")
            print(f"  total_exec={entry['total_exec_s']:.2f}s  "
                  f"mean={entry['mean_exec_ms']:.2f}ms med={entry['median_exec_ms']:.2f}ms "
                  f"p90={entry['p90_exec_ms']:.2f}ms max={entry['max_exec_ms']:.2f}ms")
            print(f"  total_plan={entry['total_plan_s']:.2f}s  mean_plan={entry['mean_plan_ms']:.2f}ms")
            print(f"  plan classes={len(entry['unique_plan_classes'])}; "
                  f"changed vs baseline={entry['n_plan_changed_vs_baseline']} "
                  f"(ids={entry['plan_changed_ids'][:5]}{'...' if len(entry['plan_changed_ids'])>5 else ''})")
            if "mean_qerr" in entry:
                print(f"  mean_qerr={entry['mean_qerr']:.3f} max={entry['max_qerr']:.2f} "
                      f"n>2={entry['n_qerr_gt2']}")

            # cleanup to restore prior stats state
            for nm in names:
                cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
            for table in involved:
                cur.execute(f"ANALYZE {table}")
    finally:
        conn.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"workload": args.workload, "target": args.target, "runs": args.runs,
         "budgets": summary}, indent=2))
    print(f"\n[saved] {args.out} (B in KB):", [f"{b['budget_bytes']//1000}K={b['total_exec_s']:.1f}s"
                                                for b in summary])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
