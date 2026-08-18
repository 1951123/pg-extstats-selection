#!/usr/bin/env python3
"""Measure how `default_statistics_target` affects baseline q-error stability.

PostgreSQL's ANALYZE draws at most ``30000 * statistics_target`` rows per column.
Setting a very large target makes ANALYZE effectively scan (nearly) the whole
table, so histograms / MCVs approximate the true distribution and should be
much more stable across runs — potentially removing the ~10% cross-session
baseline drift.

For each target level we repeat (ANALYZE all tables + EXPLAIN a sample of
queries) K rounds and report the per-round mean-of-estimates CV, plus per-query
CV and the spread of the aggregate q-error.

Usage
-----
    source .venv/bin/activate
    python scripts/measure_stability_vs_target.py \
        --targets 100,1000,10000,100000 \ --queries 10 --rounds 5
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
from extstats.parsers import parse_stats_ceb_dir  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402

_TABLES = ["badges", "comments", "posts", "posthistory", "postlinks", "tags", "users", "votes"]


def run_target(conn, queries, target, rounds):
    """Return per-round aggregate mean estimate and per-query estimate lists."""
    round_means = []
    per_q = {q.qid: [] for q in queries}
    for _ in range(rounds):
        with conn.cursor() as cur:
            cur.execute(f"SET default_statistics_target = {int(target)}")
            for t in _TABLES:
                cur.execute(f"ANALYZE {t}")
        ests = []
        for q in queries:
            r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            per_q[q.qid].append(int(r.estimate))
            ests.append(int(r.estimate))
        round_means.append(float(np.mean(ests)))
    return round_means, per_q


def cv(vals):
    v = np.array(vals, dtype=float)
    v = v[v == v]
    return float(v.std() / v.mean()) if v.mean() > 0 else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", default="100,1000,10000,100000")
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--out", default="results/stability_vs_target.json")
    args = ap.parse_args(argv)

    targets = [int(x) for x in args.targets.split(",") if x.strip()]
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = parse_stats_ceb_dir(bench_root / "stats_CEB" / "queries")[: args.queries]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="stats")

    out = {"targets": {}, "per_query_cv_by_target": {}}
    with connect(cfg) as conn:
        conn.autocommit = True
        for target in targets:
            round_means, per_q = run_target(conn, queries, target, args.rounds)
            out["targets"][str(target)] = {
                "round_mean_cv": cv(round_means),
                "round_means": [int(m) for m in round_means],
            }
            out["per_query_cv_by_target"][str(target)] = {
                qid: cv(v) for qid, v in per_q.items()
            }
            print(f"target={target:>7}: round-mean CV={cv(round_means):.4f} "
                  f"| per-query CV max={max(cv(v) for v in per_q.values()):.4f}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
