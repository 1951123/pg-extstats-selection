"""P4: top-1 sparsity characterization (shows B is a distribution, not anecdotes).

For every query that has at least one surviving candidate, compute how much of
the attainable single-stat improvement the single best candidate captures, and
how often a single candidate reaches the estimation floor. Produces:
  - a CDF of the "top-1 coverage ratio" (fraction of queries whose best single
    candidate captures >= x of the attainable improvement),
  - a baseline-regime table (queries in base 1-1.1 / 1.1-2 / 2-10 / >10 and
    the fraction repaired to <=1.05 by a single candidate),
  - a figure paper/figures/sparsity_cdf.pdf.

Usage:
    source .venv/bin/activate
    python scripts/analyze_sparsity.py \
        --input results/phase1_ceb_single_mask_6level.json \
        --out results/p4_sparsity.json --fig paper/figures/sparsity_cdf
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="results/p4_sparsity.json")
    ap.add_argument("--fig", default="paper/figures/sparsity_cdf")
    ap.add_argument("--level", default="10000",
                    help="capacity level used for the 'single best' q-error")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    res = phase1["results"]

    coverage = []          # top-1 coverage ratio (base-best)/(base-1)
    pairs = []             # (qid, base, best)
    floors = {"1.0-1.1": [0, 0], "1.1-2.0": [0, 0],
              "2-10": [0, 0], ">10": [0, 0]}
    for r in res:
        base = r["qerror_base"]
        if not r["candidates"]:
            continue
        best = min(
            (cd["levels"][args.level]["qerror"]
             for cd in r["candidates"].values()
             if args.level in cd["levels"]),
            default=base)
        pairs.append((r["qid"], base, best))
        # coverage ratio is only meaningful when there is real repair room;
        # for base near 1.0 the denominator (base-1) makes it unstable.
        if base > 1.2:
            coverage.append((base - best) / (base - 1.0))
        # baseline-regime floor-reached analysis
        if base >= 1.0 and base < 1.1:
            floors["1.0-1.1"][0] += 1
            floors["1.0-1.1"][1] += 1 if best <= 1.05 else 0
        elif base < 2.0:
            floors["1.1-2.0"][0] += 1; floors["1.1-2.0"][1] += 1 if best <= 1.05 else 0
        elif base < 10.0:
            floors["2-10"][0] += 1; floors["2-10"][1] += 1 if best <= 1.05 else 0
        else:
            floors[">10"][0] += 1; floors[">10"][1] += 1 if best <= 1.05 else 0

    cov = np.array(coverage)
    cov = cov[~np.isnan(cov)]
    print(f"queries with >=1 candidate: {len(pairs)}")
    print(f"(coverage computed on base>1.2, n={len(cov)})")
    print(f"top-1 coverage ratio: median={np.median(cov):.3f} "
          f"frac>=0.9={100*(cov>=0.9).mean():.1f}% "
          f"frac>=0.99={100*(cov>=0.99).mean():.1f}%")
    print("\nbaseline-regime: single candidate reaches floor (<=1.05):")
    regime_tbl = {}
    for k, (n, hit) in floors.items():
        frac = hit / n if n else 0.0
        regime_tbl[k] = {"n": n, "repaired_to_le_1_05": hit,
                         "frac": round(frac, 3)}
        print(f"  base {k:<8}: n={n:<4} repaired={hit:<4} ({100*frac:.0f}%)")

    # figure: CDF of coverage
    fig, ax = plt.subplots(figsize=(4.2, 3.0))
    xs = np.sort(cov)
    ys = np.linspace(0, 1, len(xs))
    ax.plot(xs, ys, drawstyle="steps-post", color="#1f77b4", lw=1.6)
    ax.axvline(0.99, color="grey", ls="--", lw=1)
    ax.text(0.993, 0.15, "0.99", fontsize=8, color="grey")
    ax.set_xlabel("top-1 coverage ratio (single best / attainable)")
    ax.set_ylabel("fraction of queries")
    ax.set_title("Single-statistic coverage (RQ2)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    figpath = Path(args.fig + ".pdf")
    figpath.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figpath, bbox_inches="tight")
    fig.savefig(Path(args.fig + ".png"), bbox_inches="tight", dpi=150)
    print(f"\nwrote figure {figpath}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "input": args.input, "level": args.level,
        "n_queries_with_candidates": len(pairs),
        "coverage_n_base_gt_1_2": int(len(cov)),
        "coverage_median": round(float(np.median(cov)), 4),
        "frac_ge_0_9": round(float((cov >= 0.9).mean()), 4),
        "frac_ge_0_99": round(float((cov >= 0.99).mean()), 4),
        "note": ("coverage ratio (base-best)/(base-1) computed only for queries "
                 "with baseline>1.2, where it is well-defined"),
        "baseline_regime": regime_tbl,
    }, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
