#!/usr/bin/env python3
"""Review P1, action 1 — ORDER-based overlap-interference gradient (axis a)
with naive-density contrast panel (axis c).

Mechanism (proved in P0-II): the planner uses the FIRST-built overlapping MCV
that matches a query's clauses (OID order == CREATE order). So overlap
"interference" is decided by how many BAD overlapping siblings are created
BEFORE the query's winner. This experiment makes that dependence explicit:

  x-asix (a): k = number of BAD siblings created BEFORE the winner.
              Hold the winner W (built last) and prepend k bad siblings;
              measure the served query's E2E q-error / per-candidate prediction.

  contrast (c): the SAME deployments plotted against naive overlap density
              d(S) = #{overlapping pairs} / C(|S|,2), showing density alone
              does not predict the outcome (order does).

For st.144 the winner is AFV with 18 bad siblings, so we sweep k = 0..8.
For the other served queries (st.588, st.308, st.562) we sweep their smaller
bad-sibling sets too. st.326 has no bad sibling (robust) and stays a flat
control.

Run:
    python scripts/exp_order_density.py --db stats --level 10000 \
        --out results/p1_order_density.json --fig paper/figures/order_density
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

SERVED = ["st.144", "st.588", "st.308", "st.562", "st.326"]
DOMINANT = {
    "st.144": ("AnswerCount", "FavoriteCount", "ViewCount"),
    "st.588": ("AnswerCount", "FavoriteCount", "ViewCount"),
    "st.308": ("AnswerCount", "PostTypeId", "ViewCount"),
    "st.562": ("FavoriteCount", "PostTypeId", "ViewCount"),
    "st.326": ("AnswerCount", "FavoriteCount", "ViewCount"),
}
MAX_K = {"st.144": 8, "st.588": 4, "st.308": 4, "st.562": 4, "st.326": 0}
ALL = ("ext_", "e2e_", "e4_", "p0_", "sw_", "od_", "sg_", "pi_", "cl_", "mask_",
       "w_", "zz_", "hh_", "pr_", "mm_", "st2_", "repro_", "prb_", "rc_", "ord_",
       "hj_", "od_")


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
    ap.add_argument("--out", default="results/p1_order_density.json")
    ap.add_argument("--fig", default="paper/figures/order_density")
    args = ap.parse_args(argv)

    # load bad-sibling inventory
    inv = json.load(open("results/p1_hijack_inventory.json"))
    qs = {q.qid: q for q in parse_stats_ceb_single_dir(
        Path("benchmarks/stats_CEB/queries"))}
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.db,
                   password="postgres")
    prefix = "od_"

    # per-query prediction (winner's phase-1 per-query q-error) under the winner
    phase1 = json.load(open("results/phase1_ceb_single_mask_6level.json"))
    pred_by_q = {}
    for r in phase1["results"]:
        lvl = str(args.level)
        if r["qid"] not in DOMINANT:
            continue
        W = DOMINANT[r["qid"]]
        for ck, c in r["candidates"].items():
            if c["table"] == "posts" and tuple(c["columns"]) == W \
                    and lvl in c["levels"]:
                pred_by_q[r["qid"]] = c["levels"][lvl]["qerror"]
                break

    points = []
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
                    cur.execute(f"CREATE STATISTICS {prefix}{i} (mcv) ON "
                                f"{', '.join(W)} FROM posts")
                    cur.execute(
                        f"ALTER STATISTICS {prefix}{i} SET STATISTICS {args.level}")
            _set_target(conn, args.level)
            _analyze(conn, "posts")
        def qerr_est(qid):
            r = estimate_count_query(conn, qs[qid].sql, actual=qs[qid].ground_truth)
            return (r.qerror if r.qerror is not None else float("nan")), r.estimate

        for qid in SERVED:
            W = DOMINANT[qid]
            pred = pred_by_q.get(qid)
            bad = [tuple(h[0]) for h in inv[qid]["hijackers"]]
            k_max = min(MAX_K[qid], len(bad))
            clean(); build([W])
            q0, e0 = qerr_est(qid)
            print(f"\n{qid}: winner={'+'.join(W)} pred={pred:.3f} "
                  f"alone q={q0:.3f} est={e0}  bad_siblings={len(bad)}")
            # k = number of bad siblings prepended before the winner
            for k in range(0, k_max + 1):
                deploy_bad = bad[:k]
                deploy = deploy_bad + [W]      # bad siblings first, winner last
                clean(); build(deploy)
                q, est = qerr_est(qid)
                d = density(deploy)
                ratio = q / pred if pred and pred > 0 else float("nan")
                points.append({"qid": qid, "k": k, "d": d, "n_stats": len(deploy),
                               "qerr": q, "est": est, "pred": pred,
                               "ratio": ratio})
                print(f"    k={k} d={d:.2f} n={len(deploy)} q={q:.3f} "
                      f"est={est} ratio={ratio:.3f}")
        clean()

    # ---- aggregate: ratio vs k (axis a) ----
    agg_a = []
    for qid in SERVED:
        ks = sorted({p["k"] for p in points if p["qid"] == qid})
        for k in ks:
            rows = [p for p in points if p["qid"] == qid and p["k"] == k]
            agg_a.append({"qid": qid, "k": k,
                          "mean_ratio": float(np.mean([r["ratio"] for r in rows]))
                          if rows else None,
                          "mean_qerr": float(np.mean([r["qerr"] for r in rows]))})
    # ---- aggregate: ratio vs density bin (axis c), separating by k>=1 ----
    bins = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    agg_c = []
    for b in bins:
        for hasBadFirst in (True, False):
            rows = [p for p in points
                    if p["ratio"] == p["ratio"] and abs(p["d"] - b) < 0.11
                    and (p["k"] >= 1) == hasBadFirst]
            if rows:
                agg_c.append({"d_bin": b, "bad_first": hasBadFirst,
                              "n": len(rows),
                              "mean_ratio":
                                  float(np.mean([r["ratio"] for r in rows]))})

    # ---- plot ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (axa, axc) = plt.subplots(1, 2, figsize=(9.6, 4.2))

    # (a) ratio vs k, per query
    for qid in SERVED:
        xs = [p["k"] for p in points if p["qid"] == qid]
        ys = [p["ratio"] for p in points if p["qid"] == qid]
        axa.plot(xs, ys, "-o", lw=1.4, ms=4, label=qid)
    axa.axhline(1.0, color="k", ls=":", lw=1)
    axa.set_xlabel("bad siblings created before winner (k)")
    axa.set_ylabel("E2E / predicted q-error")
    axa.legend(fontsize=7)
    axa.set_title("(a) ORDER axis: bad siblings before winner")

    # (c) ratio vs density, split by whether any bad sibling is first
    for badFirst, color, lab in [(True, "tab:red", "bad sibling before winner"),
                                 (False, "tab:green", "none (or winner first)")]:
        for a in agg_c:
            if a["bad_first"] == badFirst:
                axc.scatter(a["d_bin"], a["mean_ratio"], s=40, color=color,
                            label=lab if a["d_bin"] == bins[0] else None, zorder=3)
                axc.plot(bins, [a["mean_ratio"] if abs(b - a["d_bin"]) < 0.11 else None
                                for b in bins], color=color, lw=1.4)
    axc.axhline(1.0, color="k", ls=":", lw=1)
    axc.set_xlabel("overlap density $d(S)$")
    axc.set_ylabel("E2E / predicted q-error")
    axc.legend(fontsize=7, loc="upper left")
    axc.set_title("(c) naive density (contrast)")

    fig.tight_layout()
    Path(args.fig).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{args.fig}.pdf")
    print(f"[saved] {args.fig}.pdf")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"agg_a": agg_a, "agg_c": agg_c, "points": points,
         "pred_by_q": pred_by_q}, indent=2))
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
