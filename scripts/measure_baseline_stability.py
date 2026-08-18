#!/usr/bin/env python3
"""Quantify baseline stability under the FULL measurement protocol.

Each round re-runs ANALYZE on every table, then EXPLAINs a sample of queries
and reads the estimated cardinality. Repeating K rounds yields the run-to-run
variability of the baseline estimate that the phase-1 / verification pipeline
would actually observe. This distinguishes ANALYZE *sampling noise* (what this
measures) from a *systematic state/protocol* difference (which the diagnostic
preceding showed is the cause of the phase1 33784 vs verifier 37973 gap).

Usage
-----
    source .venv/bin/activate
    python scripts/measure_baseline_stability.py --queries 10 --rounds 6
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--out", default="results/baseline_stability.json")
    args = ap.parse_args(argv)

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = parse_stats_ceb_dir(bench_root / "stats_CEB" / "queries")[: args.queries]
    # Which tables each query touches doesn't matter here; we ANALYZE all.

    dbname = {"census": "census", "job": "imdb", "stats_ceb": "stats"}[args.bench]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=dbname)

    all_est: dict[str, list[int]] = {q.qid: [] for q in queries}
    round_means: list[float] = []

    with connect(cfg) as conn:
        conn.autocommit = True
        for _ in range(args.rounds):
            # Re-run ANALYZE on every table (each is a fresh random sample).
            for t in _TABLES:
                with conn.cursor() as cur:
                    cur.execute(f"ANALYZE {t}")
            # Measure baseline estimates.
            for q in queries:
                r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                all_est[q.qid].append(int(r.estimate))
            round_means.append(float(np.mean([all_est[q.qid][-1] for q in queries])))

    def cv(vals):
        v = np.array(vals, dtype=float)
        v = v[v == v]
        return float(v.std() / v.mean()) if v.mean() > 0 else 0.0

    summary = {
        "n_queries": len(queries),
        "n_rounds": args.rounds,
        "round_mean_cv": cv(round_means),
        "round_means": [int(m) for m in round_means],
        "per_query_cv": {qid: cv(v) for qid, v in all_est.items()},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))

    print(f"baseline stability: {args.rounds} rounds, {len(queries)} queries")
    print(f"  round-mean CV = {summary['round_mean_cv']:.3f}")
    for qid, c in summary["per_query_cv"].items():
        print(f"  qid={qid} CV={c:.3f}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
