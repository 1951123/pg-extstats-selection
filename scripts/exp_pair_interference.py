#!/usr/bin/env python3
"""Review P1, action 1 (mechanism): map WHICH overlapping pair actually hijacks.

Finding so far: co-installing *arbitrary* overlapping siblings around a served
query's winner does NOT degrade its prediction (ratio stays 1.000) -- the
planner deterministically uses the best stat. Only *specific* overlapping pairs
hijack: e.g. winner AFV co-installed with sibling ACV degrades st.144/st.588 to
~2.1. This script exhaustively maps pair-interference over the Stage-2 winner
pool, and reports, per pair and per served query, the measured E2E ratio.

Output: a pair-interference matrix and a per-deployment density/ratio table,
used to state honestly in the paper that prediction error is driven by specific
overlapping pairs, not by column-overlap density alone.

Run:
    python scripts/exp_pair_interference.py \
        --db stats --level 10000 --out results/p1_pair_interference.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.measure_mask import _analyze, _set_target  # noqa: E402
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402

# The Stage-2 deployment (from sparse_solution_2m.json): pairwise-overlap winners.
STAGE2 = [
    ("AnswerCount", "CommentCount", "ViewCount"),      # ACV
    ("AnswerCount", "FavoriteCount", "ViewCount"),     # AFV  (serves st.144/326/588)
    ("AnswerCount", "FavoriteCount", "PostTypeId"),    # AFP
    ("AnswerCount", "PostTypeId", "ViewCount"),        # APV
    ("FavoriteCount", "PostTypeId", "ViewCount"),      # FPV
]
SERVED = ["st.144", "st.326", "st.588", "st.308", "st.562"]


def overlaps(a, b):
    return bool(set(a) & set(b))


def density(colsets):
    m = len(colsets)
    if m < 2:
        return 0.0
    ov = sum(1 for (x, y) in itertools.combinations(colsets, 2) if overlaps(x, y))
    return ov / (m * (m - 1) / 2.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--level", type=int, default=10000)
    ap.add_argument("--out", default="results/p1_pair_interference.json")
    args = ap.parse_args(argv)

    qs = {q.qid: q for q in parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))}
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.db,
                   password="postgres")
    prefix = "pi_"

    def clean(cur):
        cur.execute(
            f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
        for row in cur.fetchall():
            n = row[0] if not isinstance(row, dict) else row["stxname"]
            cur.execute(f"DROP STATISTICS IF EXISTS {n}")

    results = []
    with connect(cfg) as conn:
        conn.autocommit = True
        # single-winner baselines (density 0)
        for W in STAGE2:
            with conn.cursor() as cur:
                clean(cur)
                cur.execute(f"CREATE STATISTICS {prefix}s (mcv) ON "
                            f"{', '.join(W)} FROM posts")
                cur.execute(f"ALTER STATISTICS {prefix}s SET STATISTICS {args.level}")
            _set_target(conn, args.level)
            _analyze(conn, _qualify_table("posts"))
            errs = {}
            for qid in SERVED:
                r = estimate_count_query(conn, qs[qid].sql, actual=qs[qid].ground_truth)
                errs[qid] = r.qerror if r.qerror is not None else float("nan")
            results.append({"deployment": [list(W)], "d": 0.0,
                            "n_stats": 1, "e2e": errs})
            print(f"single {W} d=0.0: " +
                  " ".join(f"{q}={errs[q]:.2f}" for q in SERVED))

        # full Stage-2 deployment (density 1.0)
        with conn.cursor() as cur:
            clean(cur)
            for i, W in enumerate(STAGE2):
                cur.execute(f"CREATE STATISTICS {prefix}s_{i} (mcv) ON "
                            f"{', '.join(W)} FROM posts")
                cur.execute(f"ALTER STATISTICS {prefix}s_{i} SET STATISTICS {args.level}")
        _set_target(conn, args.level)
        _analyze(conn, _qualify_table("posts"))
        errs = {}
        for qid in SERVED:
            r = estimate_count_query(conn, qs[qid].sql, actual=qs[qid].ground_truth)
            errs[qid] = r.qerror if r.qerror is not None else float("nan")
        results.append({"deployment": [list(W) for W in STAGE2],
                        "d": density(STAGE2), "n_stats": len(STAGE2), "e2e": errs})
        print(f"stage2(all 5) d={density(STAGE2):.2f}: " +
              " ".join(f"{q}={errs[q]:.2f}" for q in SERVED))

        # all pairwise 2-stat deployments among winners (density: 1 if overlap else 0)
        for (A, B) in itertools.combinations(STAGE2, 2):
            with conn.cursor() as cur:
                clean(cur)
                for i, W in enumerate([A, B]):
                    cur.execute(f"CREATE STATISTICS {prefix}s_{i} (mcv) ON "
                                f"{', '.join(W)} FROM posts")
                    cur.execute(f"ALTER STATISTICS {prefix}s_{i} SET STATISTICS {args.level}")
            _set_target(conn, args.level)
            _analyze(conn, _qualify_table("posts"))
            errs = {}
            for qid in SERVED:
                r = estimate_count_query(conn, qs[qid].sql, actual=qs[qid].ground_truth)
                errs[qid] = r.qerror if r.qerror is not None else float("nan")
            results.append({"deployment": [list(A), list(B)],
                            "d": density([A, B]), "n_stats": 2, "e2e": errs})
            print(f"pair {A} & {B} d={density([A,B]):.1f}: " +
                  " ".join(f"{q}={errs[q]:.2f}" for q in SERVED))

        with conn.cursor() as cur:
            clean(cur)
        _analyze(conn, _qualify_table("posts"))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"results": results}, indent=2))
    print(f"[saved] {args.out}")


if __name__ == "__main__":
    sys.exit(main())
