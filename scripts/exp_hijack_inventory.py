#!/usr/bin/env python3
"""Systematic hijack inventory: for each served query, with winner W, find all
pool siblings that, when created BEFORE W, hijack (raise) the query's q-error.
This defines the 'bad siblings' per query that drive the ORDER-based density
axis (a). Impeccably clean protocol.
"""
import json
from pathlib import Path
from extstats.config import DBConfig
from extstats.db import connect
from extstats.estimate import estimate_count_query
from extstats.measure_mask import _analyze, _set_target
from extstats.parsers import parse_stats_ceb_single_dir
from extstats.stats import _qualify_table

SERVED = ["st.144", "st.588", "st.326", "st.308", "st.562"]
DOMINANT = {
    "st.144": ("AnswerCount", "FavoriteCount", "ViewCount"),
    "st.588": ("AnswerCount", "FavoriteCount", "ViewCount"),
    "st.326": ("AnswerCount", "FavoriteCount", "ViewCount"),
    "st.308": ("AnswerCount", "PostTypeId", "ViewCount"),
    "st.562": ("FavoriteCount", "PostTypeId", "ViewCount"),
}
ALL = ("ext_", "e2e_", "e4_", "p0_", "sw_", "od_", "sg_", "pi_", "cl_", "mask_",
       "w_", "zz_", "hh_", "pr_", "mm_", "st2_", "repro_", "prb_", "rc_", "ord_",
       "hj_")


def main():
    phase1 = json.load(open("results/phase1_ceb_single_mask_6level.json"))
    pool = set()
    for r in phase1["results"]:
        for ck, c in r["candidates"].items():
            if c["table"] == "posts" and "10000" in c["levels"]:
                pool.add(tuple(c["columns"]))
    qs = {q.qid: q for q in parse_stats_ceb_single_dir(
        Path("benchmarks/stats_CEB/queries"))}
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="stats",
                   password="postgres")
    with connect(cfg) as conn:
        conn.autocommit = True
        def clean():
            with conn.cursor() as cur:
                like = " OR ".join(f"stxname LIKE '{p}%'" for p in ALL)
                cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE {like}")
                for row in cur.fetchall():
                    n = row[0] if not isinstance(row, dict) else row["stxname"]
                    cur.execute(f"DROP STATISTICS IF EXISTS {n}")
            _set_target(conn, 100)
            _analyze(conn, "posts")
        def build(deploy):
            with conn.cursor() as cur:
                for i, W in enumerate(deploy):
                    cur.execute(f"CREATE STATISTICS hj_{i} (mcv) ON "
                                f"{', '.join(W)} FROM posts")
                    cur.execute(f"ALTER STATISTICS hj_{i} SET STATISTICS 10000")
            _set_target(conn, 10000)
            _analyze(conn, "posts")
        def qe(qid):
            r = estimate_count_query(conn, qs[qid].sql, actual=qs[qid].ground_truth)
            return (r.qerror if r.qerror is not None else float("nan")), r.estimate

        summary = {}
        for qid in SERVED:
            W = DOMINANT[qid]
            clean(); build([W])
            base_q, base_est = qe(qid)
            hij = []
            overlap = [s for s in pool if s != W and (set(s) & set(W))]
            # note: building W first, then a sibling AFTER doesn't hijack (first-built wins = W);
            # hijacking needs the bad sibling BEFORE W. So test [sib, W].
            for sib in overlap:
                clean(); build([sib, W])
                q, est = qe(qid)
                if q > base_q + 0.05:
                    hij.append((sib, round(q, 3), est))
            summary[qid] = {"winner": W, "alone_q": round(base_q, 3),
                            "alone_est": base_est, "n_overlapping": len(overlap),
                            "n_hijack_when_first": len(hij),
                            "hijackers": [[list(h[0]), h[1], h[2]] for h in hij]}
            print(f"\n{qid}: winner={'+'.join(W)} alone q={base_q:.3f} "
                  f"overlapping-siblings={len(overlap)} "
                  f"hijack-when-first={len(hij)}")
            for sib, q, est in hij:
                print(f"    first-built {sib} -> q={q:.3f} est={est}")
        clean()
        Path("results/p1_hijack_inventory.json").write_text(
            json.dumps(summary, indent=2))
        print("\n[saved] results/p1_hijack_inventory.json")


if __name__ == "__main__":
    main()
