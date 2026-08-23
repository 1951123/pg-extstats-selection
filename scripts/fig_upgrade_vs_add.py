#!/usr/bin/env python3
"""P1-2: 'Upgrade vs add' Pareto frontier figure.

The MILP's capacity axis means that, at the margin, spending budget
*upgrading* an already-selected statistic (the "how much" axis) can beat
installing a *new* statistic (the "what" axis). This figure makes that
economic decision concrete and data-driven, per representative served query:

For each query we plot, on (statistic size in bytes, per-query q-error):
   * A: the query's dominant statistic at L100 / L1000 / L10000 (the upgrade
        path of the SAME columns);
   * B: a competing overlapping candidate at L100 / L1000 / L10000 (the "add
        a new one" alternative).
The curve for A lies to the south-west (strictly better size for the same or
lower q-error) of B, so the per-candidate surrogate will rationally spend the
marginal budget upgrading A rather than adding B.

Data: per-query per-level (size_bytes, qerror) from
results/phase1_ceb_single_mask_6level.json.

Run:
    python scripts/fig_upgrade_vs_add.py [--out paper/figures/upgrade_vs_add]
"""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1", default="results/phase1_ceb_single_mask_6level.json")
    ap.add_argument("--out", default="paper/figures/upgrade_vs_add")
    args = ap.parse_args(argv)

    # (query, A_cols, B_cols) triples, chosen from the data so that A is the
    # dominant/winner statistic and B a competing overlapping candidate.
    PANELS = [
        ("st.308", ("AnswerCount", "PostTypeId", "ViewCount"),
         ("PostTypeId", "Score", "ViewCount")),
        ("st.144", ("AnswerCount", "FavoriteCount", "ViewCount"),
         ("AnswerCount", "CommentCount", "ViewCount")),
        ("st.562", ("FavoriteCount", "PostTypeId", "ViewCount"),
         ("CreationDate", "FavoriteCount", "PostTypeId")),
    ]
    d = json.load(open(args.phase1))
    byq = {r["qid"]: r for r in d["results"]}

    def lvls(qid, cols):
        rec = byq[qid]
        for ck, c in rec["candidates"].items():
            if c["table"] == "posts" and tuple(c["columns"]) == tuple(cols):
                out = {}
                for lv, data in c["levels"].items():
                    out[int(lv)] = (data["size_bytes"], data["qerror"])
                return out
        return {}

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(PANELS), figsize=(12.6, 3.9))
    levels = [100, 1000, 10000]
    for ax, (qid, acols, bcols) in zip(axes, PANELS):
        A = lvls(qid, acols)
        B = lvls(qid, bcols)
        # A points
        axs = [A[l][0] for l in levels]; aq = [A[l][1] for l in levels]
        ax.plot(axs, aq, "-o", color="#1f77b4", lw=1.8, ms=5, label="A: upgrade", zorder=3)
        for (x, y, l) in zip(axs, aq, levels):
            ax.annotate(f"L{l}", (x, y), textcoords="offset points",
                        xytext=(0, 7), fontsize=7, color="#1f77b4")
        # B points
        if B:
            bxs = [B[l][0] for l in levels]; bq = [B[l][1] for l in levels]
            ax.plot(bxs, bq, "--^", color="#d62728", lw=1.4, ms=5, label="B: add",
                    zorder=2)
            for (x, y, l) in zip(bxs, bq, levels):
                ax.annotate(f"L{l}", (x, y), textcoords="offset points",
                            xytext=(0, -11), fontsize=7, color="#d62728")
        ax.set_title(f"{qid}\n(base q={byq[qid]['qerror_base']:.2f})", fontsize=9)
        ax.set_xscale("log")
        ax.set_xlabel("statistic size (bytes, log)")
        if ax is axes[0]:
            ax.set_ylabel("per-query q-error")
        ax.set_ylim(0.9, 4.8)
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(alpha=0.3)
    fig.suptitle("Upgrade (A: same columns, higher capacity) vs add (B: new candidate)",
                 fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(f"{args.out}.pdf")
    print(f"[saved] {args.out}.pdf")

    # numeric summary to embed / cross-check in caption
    print("\nper-query level points (size_B, qerr):")
    for qid, acols, bcols in PANELS:
        A = lvls(qid, acols); B = lvls(qid, bcols)
        print(f"{qid}: A={acols} B={bcols}")
        for l in levels:
            print(f"   L{l}: A=({A[l][0]}B, q{A[l][1]:.3f})   "
                  f"B=({B[l][0]}B, q{B[l][1]:.3f})")


if __name__ == "__main__":
    main()
