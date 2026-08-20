#!/usr/bin/env python3
"""Quantify how ANALYZE-sampling stability depends on default_statistics_target.

Q (user): does a LARGER target give MORE STABLE results? (Their hypothesis.)
A precise model:
  - ANALYZE resamples ~30000*statistics_target rows, so larger target -> larger
    sample -> both (i) closer to true distribution (less bias) and (ii) less
    run-to-run sampling noise (lower CV).
  - We measure, per target in {100, 1000, 10000}, for a set of selective queries
    on `posts`, the run-to-run estimate spread across N repeated ANALYZEs, with
    NO extended stats (pure single-column histograms/MCVs) and with a fixed MCV.

Impeccably clean protocol: drop all experimental stats first.
"""
from pathlib import Path
import statistics
from extstats.config import DBConfig
from extstats.db import connect
from extstats.estimate import estimate_count_query
from extstats.measure_mask import _analyze, _set_target
from extstats.parsers import parse_stats_ceb_single_dir
from extstats.stats import _qualify_table

TARGETS = [100, 1000, 10000]
REPEATS = 6
QIDS = ["st.144", "st.326", "st.308"]

ALL_EXP = ("ext_", "e2e_", "e4_", "p0_", "sw_", "od_", "sg_", "pi_", "cl_",
           "mask_", "w_", "zz_", "hh_", "pr_", "mm_", "st2_", "repro_")


def main():
    qs = {q.qid: q for q in parse_stats_ceb_single_dir(
        Path("benchmarks/stats_CEB/queries"))}
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="stats",
                   password="postgres")
    with connect(cfg) as conn:
        conn.autocommit = True
        def clean():
            with conn.cursor() as cur:
                like = " OR ".join(f"stxname LIKE '{p}%'" for p in ALL_EXP)
                cur.execute(
                    f"SELECT stxname FROM pg_statistic_ext WHERE {like}")
                for row in cur.fetchall():
                    n = row[0] if not isinstance(row, dict) else row["stxname"]
                    cur.execute(f"DROP STATISTICS IF EXISTS {n}")

        print("=== Baseline (no extended stats): per-target run-to-run spread ===")
        for target in TARGETS:
            clean()
            _set_target(conn, target)
            est_by_q = {q: [] for q in QIDS}
            for i in range(REPEATS):
                _analyze(conn, "posts")
                for q in QIDS:
                    r = estimate_count_query(conn, qs[q].sql, actual=qs[q].ground_truth)
                    est_by_q[q].append(r.estimate)
            line = [f"t={target}"]
            for q in QIDS:
                vals = est_by_q[q]
                cv = statistics.pstdev(vals) / statistics.mean(vals) * 100
                line.append(f"{q} n={statistics.mean(vals):.0f} CV={cv:.2f}% "
                            f"[{min(vals)}..{max(vals)}]")
            print("  " + "  ".join(line))

        # fixed single MCV: build AFV, then vary target, re-ANALYZE each time
        print("\n=== With ONE fixed MCV (AnswerCount,FavoriteCount,ViewCount) ===")
        for target in TARGETS:
            clean()
            with conn.cursor() as cur:
                cur.execute("CREATE STATISTICS cl_a (mcv) ON "
                            "AnswerCount, FavoriteCount, ViewCount FROM posts")
                cur.execute(f"ALTER STATISTICS cl_a SET STATISTICS {target}")
            _set_target(conn, target)
            est_by_q = {q: [] for q in QIDS}
            for i in range(REPEATS):
                _analyze(conn, "posts")
                for q in QIDS:
                    r = estimate_count_query(conn, qs[q].sql, actual=qs[q].ground_truth)
                    est_by_q[q].append(r.estimate)
            line = [f"t={target}"]
            for q in QIDS:
                vals = est_by_q[q]
                cv = statistics.pstdev(vals) / statistics.mean(vals) * 100
                line.append(f"{q} n={statistics.mean(vals):.0f} CV={cv:.2f}% "
                            f"[{min(vals)}..{max(vals)}]")
            print("  " + "  ".join(line))
        clean()
        _set_target(conn, 100)
        _analyze(conn, "posts")


if __name__ == "__main__":
    main()
