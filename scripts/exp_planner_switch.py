#!/usr/bin/env python3
"""Review P0, action 2: prove the Stage-2 causal claim with the planner itself.

Stage 2 claims that when an overlapping statistic is co-installed, the planner
*switches* to the better (selected) statistic, which is why the measured E2E
q-error matches the per-candidate prediction. That claim is currently an
inference. Here we make it a *measured* systems fact by reading the planner's
own cardinality estimate via EXPLAIN under a controlled swap:

For each test query we take an overlapping pair on the same table,
    A = the good / selected statistic (low q-error),
    B = a different, overlapping statistic on shared columns (high q-error),
and measure the planner's EXPLAIN estimate of the query's count under three
conditions (mask protocol keeps exactly the requested payloads live):

    (B) A masked          -> planner is forced onto B
    (A) B masked          -> planner is forced onto A
    (A+B) both live       -> the real deployment: which one wins?

Under co-installation the estimated rows should track the A-only value (the
planner picked A), proving the switch causally. If (A+B) instead tracked B,
that would flag a real arbitration failure for the paper's Discussion.

We also record the actual COUNT q-error under each condition.

Run:
    source .venv/bin/activate
    python scripts/exp_planner_switch.py --db stats --level 10000 \
        --out results/p0_planner_switch.json
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
from extstats.estimate import estimate_count_query, top_estimate, qerror  # noqa: E402
from extstats.measure_mask import (  # noqa: E402
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_oid,
)
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402

# Overlapping (A=good, B=bad) pairs found in phase-1 multi data: shared column
# noted. Rows are (qid, table, A_cols, B_cols). A = selected good stat,
# B = overlapping stat that would badly mis-estimate if the planner picked it.
TEST_PAIRS = [
    # (qid, table, A_cols, B_cols)
    ("st.308", "posts", ("AnswerCount", "PostTypeId", "ViewCount"), ("PostTypeId", "Score")),
    ("st.144", "posts", ("AnswerCount", "FavoriteCount", "ViewCount"), ("CommentCount", "FavoriteCount")),
    ("st.567", "posts", ("FavoriteCount", "PostTypeId", "ViewCount"), ("CommentCount", "FavoriteCount")),
    ("st.562", "posts", ("FavoriteCount", "PostTypeId", "ViewCount"), ("CreationDate", "FavoriteCount")),
    ("st.182", "posts", ("AnswerCount", "FavoriteCount", "PostTypeId"), ("FavoriteCount", "Score")),
    ("st.588", "posts", ("AnswerCount", "FavoriteCount", "ViewCount"), ("CommentCount", "FavoriteCount")),
    ("st.326", "posts", ("AnswerCount", "FavoriteCount", "ViewCount"), ("AnswerCount", "Score")),
]


def build_stat(cur, name, cols, table, level):
    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
    cur.execute(f"CREATE STATISTICS {name} (mcv) ON {', '.join(cols)} FROM {table}")
    if level > 0:
        cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {level}")


def measure(conn, sql, actual):
    """Return (estimate_rows, qerror) via EXPLAIN estimate."""
    r = estimate_count_query(conn, sql, actual=actual)
    q = r.qerror if r.qerror is not None else float("nan")
    return r.estimate, q


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--level", type=int, default=10000)
    ap.add_argument("--out", default="results/p0_planner_switch.json")
    args = ap.parse_args(argv)

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.db)
    qs = {q.qid: q for q in parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))}

    results = []
    prefix = "sw_"
    with connect(cfg) as conn:
        conn.autocommit = True
        for (qid, table, acols, bcols) in TEST_PAIRS:
            q = qs[qid]
            tbl = table
            nameA = f"{prefix}a"
            nameB = f"{prefix}b"

            _set_target(conn, args.level)
            r0 = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            base = r0.qerror if r0.qerror is not None else float("nan")

            with conn.cursor() as cur:
                build_stat(cur, nameA, acols, tbl, args.level)
                build_stat(cur, nameB, bcols, tbl, args.level)
            _set_target(conn, args.level)
            _analyze(conn, _qualify_table(tbl))

            oidA = _stat_oid(conn, nameA)
            oidB = _stat_oid(conn, nameB)
            backup = f"_swb_{len(results)}"
            _backup_payload(conn, backup, [oidA, oidB], "mcv")

            # --- condition B-only: force planner onto B ---
            _mask_payload_all_but(conn, backup, {oidB}, "mcv")
            _set_target(conn, args.level)
            estB, qB = measure(conn, q.sql, q.ground_truth)
            _restore_payload(conn, backup, "mcv")

            # --- condition A-only: force planner onto A ---
            _mask_payload_all_but(conn, backup, {oidA}, "mcv")
            _set_target(conn, args.level)
            estA, qA = measure(conn, q.sql, q.ground_truth)
            _restore_payload(conn, backup, "mcv")

            # --- condition A+B: both live (the real deployment) ---
            _restore_payload(conn, backup, "mcv")
            _set_target(conn, args.level)
            estAB, qAB = measure(conn, q.sql, q.ground_truth)
            _restore_payload(conn, backup, "mcv")

            # --- classify the switch ---
            # does the co-installed estimate track A (good) or B (bad)?
            d_to_A = abs(estAB - estA)
            d_to_B = abs(estAB - estB)
            if d_to_A <= d_to_B:
                switch = "A"          # planner followed the selected (good) stat
                track = 1.0
            else:
                switch = "B"          # planner fell back to the overlapping bad stat
                track = 0.0

            results.append({
                "qid": qid, "table": table,
                "A_cols": list(acols), "B_cols": list(bcols),
                "actual": q.ground_truth, "base": base,
                "B_only": {"est": estB, "qerror": qB},
                "A_only": {"est": estA, "qerror": qA},
                "both": {"est": estAB, "qerror": qAB},
                "follows": switch,
            })
            print(f"{qid:8s} base={base:6.2f} "
                  f"B_only(est={estB:>7d},q={qB:5.2f}) "
                  f"A_only(est={estA:>7d},q={qA:5.2f}) "
                  f"both(est={estAB:>7d},q={qAB:5.2f})  -> follows {switch}")

            # cleanup
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {nameA}")
                cur.execute(f"DROP STATISTICS IF EXISTS {nameB}")
            _drop_backup(conn, backup)
            _analyze(conn, _qualify_table(tbl))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"level": args.level, "results": results},
                                         indent=2))
    nA = sum(1 for r in results if r["follows"] == "A")
    print(f"\n== summary ==")
    print(f"  planner follows the selected (good) stat under co-installation: "
          f"{nA}/{len(results)}")
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
