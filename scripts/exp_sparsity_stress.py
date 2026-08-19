#!/usr/bin/env python3
"""Experiment 1: multi-select stress test -- where sparsity FAILS.

Motivation (from external review, Major Concern 1). The paper's linear MILP
relies on an empirically-observed *sparse* regime: one statistic per query is
enough. That restriction is workload-dependent. To characterise its *failure
boundary* we build a controlled synthetic table with TWO independent,
strongly-correlated column clusters (A = {a1,a2,a3}, B = {b1,b2,b3}) plus an
independent filler column x.

  * a2=a1, a3=a1   -> cluster A is perfectly correlated (all three equal)
  * b2=b1, b3=b1   -> cluster B is perfectly correlated (all three equal)
  * A and B are independent of each other (different digit positions of i)

Then we measure, per query, the JOINT q-error of the best k=1,2,3
non-overlapping candidate statistics (mask-free: build the picked subset,
ANALYZE, EXPLAIN):

  * single-cluster queries (WHERE a...=c AND x=...)  : one dominant cluster
        -> top-1 already ~1.0 ; top-2/top-3 add nothing      (sparsity HOLDS)
  * two-cluster query (WHERE a...=c AND b...=c)      : two independent clusters
        -> top-1 (A stat) leaves the B-clause error (qerr >> 1)
        -> top-2 (A + B) collapses to ~1.0                    (sparsity FAILS)

This gives the review's requested table: sparsity is a real regime with a
sharp, understood boundary, not a universal property.

Usage
-----
    source .venv/bin/activate
    python scripts/exp_sparsity_stress.py \
        --db stats --table syn_clusters --target 10000 \
        --out results/p1_sparsity_stress.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic schema & queries (controlled two-cluster design)
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE {table} AS
SELECT (i % 10)          AS a1,
       (i % 10)          AS a2,   -- a2 = a1  (cluster A, perfectly correlated)
       (i % 10)          AS a3,   -- a3 = a1
       ((i / 10) % 10)   AS b1,   -- cluster B, independent of A
       ((i / 10) % 10)   AS b2,   -- b2 = b1
       ((i / 10) % 10)   AS b3,   -- b3 = b1
       ((i / 100) % 10)  AS x     -- filler, independent of everything
FROM generate_series(0, 99999) AS i
"""

# (qid, sql-body-after-WHERE, label, predicate-columns)
_QUERIES = [
    ("qa", "a1=5 AND a2=5 AND a3=5",
     "single cluster A", ["a1", "a2", "a3"]),
    ("qa_fill", "a1=5 AND a2=5 AND a3=5 AND x=7",
     "single cluster A + filler", ["a1", "a2", "a3", "x"]),
    ("qb", "b1=3 AND b2=3 AND b3=3",
     "single cluster B", ["b1", "b2", "b3"]),
    ("qab", "a1=5 AND a2=5 AND a3=5 AND b1=3 AND b2=3 AND b3=3",
     "two independent clusters", ["a1", "a2", "a3", "b1", "b2", "b3"]),
    ("qab_fill", "a1=5 AND a2=5 AND a3=5 AND b1=3 AND b2=3 AND b3=3 AND x=7",
     "two clusters + filler", ["a1", "a2", "a3", "b1", "b2", "b3", "x"]),
]


def subsets_2_3(cols):
    """All 2- and 3-column subsets of `cols` (the paper's candidate space)."""
    out = []
    for k in (2, 3):
        for comb in itertools.combinations(sorted(cols), k):
            out.append(sorted(comb))
    return out


def stat_name(table, cols):
    return f"m_{table}_{'_'.join(cols)}"


def measure_qerr(conn, table, where, actual):
    sql = f"SELECT COUNT(*) FROM {table} WHERE {where}"
    r = estimate_count_query(conn, sql, actual=actual)
    return r.qerror if r.qerror is not None else float("nan")


def deploy(conn, table, statlist, target, include):
    """Build exactly the `include` subset of statlist; ANALYZE once.

    `statlist` is a list of column-tuples; `include` is a set of column-tuples.
    """
    statlist = [tuple(c) for c in statlist]
    include = {tuple(c) for c in include}
    with conn.cursor() as cur:
        for cols in statlist:
            cur.execute(f"DROP STATISTICS IF EXISTS {stat_name(table, cols)}")
    with conn.cursor() as cur:
        for cols in statlist:
            if cols in include:
                cur.execute(
                    f"CREATE STATISTICS {stat_name(table, cols)} (mcv) ON "
                    f"{', '.join(cols)} FROM {table}")
        cur.execute(f"SET default_statistics_target={target}")
        cur.execute(f"ANALYZE {table}")


def cleanup(conn, table, statlist):
    with conn.cursor() as cur:
        for cols in statlist:
            cur.execute(f"DROP STATISTICS IF EXISTS {stat_name(table, cols)}")
        cur.execute(f"ANALYZE {table}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--table", default="syn_clusters")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--out", default="results/p1_sparsity_stress.json")
    ap.add_argument("--keep-table", action="store_true",
                    help="do not DROP the synthetic table at the end")
    args = ap.parse_args(argv)

    cfg = DBConfig(host="localhost", port=5432, user="postgres", password="postgres",
                   dbname=args.db)
    table = args.table

    start = time.time()
    with connect(cfg) as conn:
        conn.autocommit = True
        cur = conn.cursor()

        # ---- 1. build the synthetic table ----
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(CREATE_TABLE_SQL.format(table=table))
        print(f"[setup] created {table}; ANALYZE base ...")
        cur.execute(f"SET default_statistics_target={args.target}")
        cur.execute(f"ANALYZE {table}")

        # ---- 2. per-query data: actual counts and candidate lists ----
        queries = []
        for qid, where, label, cols in _QUERIES:
            with conn.cursor() as c:
                c.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
                actual = c.fetchone()["n"]
            cands = subsets_2_3(cols)
            queries.append(dict(qid=qid, where=where, label=label, cols=cols,
                                actual=actual, cands=cands))

        # ---- 3. baseline q-error (no extended stats) ----
        print("\n=== baseline (no extended stats) ===")
        for q in queries:
            q["baseline"] = measure_qerr(conn, table, q["where"], q["actual"])
            print(f"  {q['qid']:8s} {q['label']:28s} base qerr={q['baseline']:.2f} "
                  f"(truth={q['actual']})")

        # ---- 4. per-candidate single q-error ----
        print("\n=== per-candidate single q-error ===")
        for q in queries:
            cand_err = {}
            for cols in q["cands"]:
                deploy(conn, table, [cols], args.target, [cols])
                e = measure_qerr(conn, table, q["where"], q["actual"])
                cand_err[tuple(cols)] = e
                print(f"  {q['qid']:8s} [{','.join(cols):12s}] qerr={e:8.2f}")
            cleanup(conn, table, q["cands"])
            q["single"] = {",".join(c): e for c, e in cand_err.items()}

        # ---- 5. greedy non-overlapping top-1/2/3, joint measurement ----
        print("\n=== joint q-error of top-k non-overlapping set ===")
        for q in queries:
            # order candidates by single q-error (best = lowest), skip overlaps
            order = sorted(q["cands"], key=lambda c: q["single"][",".join(c)])
            picked, used = [], set()
            for c in order:
                if set(c) & used:
                    continue
                picked.append(c)
                used |= set(c)
            for k in (1, 2, 3):
                include = picked[:k]
                deploy(conn, table, q["cands"], args.target, [tuple(c) for c in include])
                e = measure_qerr(conn, table, q["where"], q["actual"])
                q[f"top{k}"] = e
                print(f"  {q['qid']:8s} top-{k} [{';'.join(','.join(c) for c in include)}] "
                      f"joint qerr={e:.3f}")
            cleanup(conn, table, q["cands"])

        # ---- 6. teardown ----
        if not args.keep_table:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"\n[teardown] dropped {table}")

    # ---- 7. summarize ----
    summary = {
        "experiment": "p1_sparsity_stress",
        "table": table,
        "target": args.target,
        "design": ("two independent perfectly-correlated clusters "
                   "A={a1,a2,a3}, B={b1,b2,b3}, filler x; 100k rows"),
        "queries": queries,
        "elapsed_s": round(time.time() - start, 2),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2))
    print(f"\n[saved] {args.out}  ({summary['elapsed_s']}s)")

    # ---- 8. review-style table ----
    print("\n=== REVIEW TABLE: top-1/2/3 joint q-error by query type ===")
    print(f"{'query type':28s} {'base':>7} {'top-1':>7} {'top-2':>7} {'top-3':>7}")
    for q in queries:
        print(f"{q['label']:28s} {q['baseline']:7.1f} {q['top1']:7.2f} "
              f"{q['top2']:7.2f} {q['top3']:7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
