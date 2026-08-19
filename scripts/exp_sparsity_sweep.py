#!/usr/bin/env python3
"""Synthetic sparsity sweep: how sparsity decays with the number of independent
correlated clusters (review P1-02: characterise the failure regime).

We build a synthetic table with K independent, perfectly-correlated column
clusters (cluster j = 3 equal-valued columns over 10 values), for
K = 1, 2, 3, 4. Adding a cluster roughly multiplies the independence-assumption
error by ~10 while contributing an independent, perfectly-capturable cluster.

For each K we measure, per query that filters on ALL K clusters at once, the
JOINT q-error of the best k=1..K non-overlapping statistics:

  * if K = 1 (one dominant cluster): top-1 already ~1.0   (sparsity holds)
  * as K grows: top-1 repairs one cluster and leaves the rest at high q-error,
                and only top-K (one stat per cluster) collapses to ~1.0.

We report, for each K, the top-1 vs top-K coverage ratio
    (base - top1) / (base - topK)
which is the fraction of the best multi-statistic improvement the single best
statistic captures. 1.0 = sparsity holds perfectly; << 1 = sparsity fails.

Usage:
    source .venv/bin/activate
    python scripts/exp_sparsity_sweep.py \
        --db stats --table syn_sweep --target 10000 \
        --k 1,2,3,4 --out results/p4_sparsity_sweep.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402


def build_table(conn, table, K, rows=100000):
    # each cluster j: 3 columns, all equal to a value derived from a distinct
    # digit of i, so clusters are mutually independent and each is perfectly
    # correlated within itself.
    parts = []
    for j in range(K):
        base = 10 ** j          # distinct digit position (any large coprime ok
                                # but powers of 10 keep it exact & independent)
        for c in range(3):
            parts.append(f"((i / {base}) % 10) AS c{j}_{c}")
    cols = ", ".join(parts)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} AS SELECT {cols} "
                    f"FROM generate_series(0, {rows-1}) AS i")
        cur.execute("ANALYZE " + table)


def stat_name(table, cols):
    return f"sw_{'_'.join(cols)}"


def measure_qerr(conn, table, where, actual):
    sql = f"SELECT COUNT(*) FROM {table} WHERE {where}"
    r = estimate_count_query(conn, sql, actual=actual)
    return r.qerror if r.qerror is not None else float("nan")


def deploy(conn, table, statlist, target, include):
    statlist = [tuple(c) for c in statlist]
    include = {tuple(c) for c in include}
    with conn.cursor() as cur:
        for cols in statlist:
            cur.execute(f"DROP STATISTICS IF EXISTS {stat_name(table, cols)}")
        for cols in statlist:
            if cols in include:
                cur.execute(f"CREATE STATISTICS {stat_name(table, cols)} (mcv) ON "
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
    ap.add_argument("--table", default="syn_sweep")
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--k", default="1,2,3,4")
    ap.add_argument("--rows", type=int, default=100000)
    ap.add_argument("--out", default="results/p4_sparsity_sweep.json")
    ap.add_argument("--keep-table", action="store_true")
    args = ap.parse_args(argv)

    ks = [int(x) for x in args.k.split(",") if x.strip()]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", password="postgres",
                   dbname=args.db)
    table = args.table
    out = {"metric": "coverage_K = (base - top1) / (base - topK)", "rows": []}

    print(f"{'K':>2} {'base':>8} {'top-1':>8} {'top-2':>8} {'top-K':>8} "
          f"{'sparsity cov_K':>14}")
    start = time.time()
    for K in ks:
        with connect(cfg) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            build_table(conn, table, K, rows=args.rows)
            # query filters on all 3 columns of the FIRST cluster only (the
            # "single-cluster" control) and on all K clusters (the stress case)
            cluster_cols = [[f"c{j}_{c}" for c in range(3)] for j in range(K)]
            # case: filters on a single cluster j=0
            p0_cols = cluster_cols[0]
            where0 = " AND ".join(f"{c}=5" for c in p0_cols)
            # case: filters on ALL K clusters
            all_cols = [c for cl in cluster_cols for c in cl]
            whereK = " AND ".join(f"{c}=5" for c in all_cols)

            for label, where, pred_cols in [("1", where0, p0_cols),
                                            (str(K), whereK, all_cols)]:
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
                actual = cur.fetchone()["n"]
                base = measure_qerr(conn, table, where, actual)
                # candidates: one 3-column stat per cluster (the semantically
                # meaningful repairers; avoids combinatorial blowup for K>2)
                cands = [tuple(cl) for cl in cluster_cols]
                # per-candidate single q-error
                single = {}
                for cols in cands:
                    deploy(conn, table, [cols], args.target, [cols])
                    single[tuple(cols)] = measure_qerr(conn, table, where, actual)
                cleanup(conn, table, cands)
                # greedy non-overlap top-k
                order = sorted(cands, key=lambda c: single[tuple(c)])
                picked, used = [], set()
                for c in order:
                    if set(c) & used:
                        continue
                    picked.append(c)
                    used |= set(c)
                top = {}
                for k in (1, 2, K):
                    if k > len(picked):
                        top[k] = np.nan
                        continue
                    inc = picked[:k]
                    deploy(conn, table, cands, args.target, inc)
                    top[k] = measure_qerr(conn, table, where, actual)
                cleanup(conn, table, cands)
                cov = (base - top[1]) / (base - top[K]) if (
                    K >= 1 and top[K] == top[K] and base != top[K]) else float("nan")
                row = {"K": K, "pred_clusters": int(label), "base": base,
                       "top1": top[1], "top2": top.get(2, np.nan),
                       "topK": top[K], "coverage_K": cov}
                out["rows"].append(row)
                print(f"{K:>2} {base:>8.1f} {top[1]:>8.2f} "
                      f"{top.get(2, float('nan')):>8.2f} {top[K]:>8.2f} "
                      f"{cov:>14.3f}" +
                      (f"  <-- sparsity HOLDS ({label} cluster pred)"
                       if label == "1" else ""))
            if not args.keep_table:
                cur.execute(f"DROP TABLE IF EXISTS {table}")

    out["elapsed_s"] = round(time.time() - start, 1)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
