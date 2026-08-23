"""P3: budget-quality curve + capacity (level) distribution + multi-metric eval.

For a multi-level phase-1 (candidates carry per-level q-error + size), solves
the sparse ILP at many budgets and reports, per budget:
  - mean / median / P90 / max q-error (baseline vs selected)
  - number of selected physical stats and the L100 / L1000 / L10000 counts
It also writes a single publication figure (paper/figures/budget_quality.pdf)
with two panels: (a) budget(log, x) vs mean q-error, and (b) the stacked
level distribution (L100/L1000/L10000) across budgets.

This answers the reviewer challenge "mean q-error is dominated by outliers":
we report the full distribution, while keeping mean as the optimisation
objective.

Usage:
    source .venv/bin/activate
    python scripts/analyze_budget_metrics.py \\
        --input results/phase1_ceb_single_mask_6level.json \\
        --budgets 5000,10000,20000,40000,100000,250000,500000,1000000 \\
        --out results/p3_metrics.json --fig paper/figures/budget_quality
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def summarize(vals: list[float]) -> dict:
    a = np.array(vals, dtype=float)
    a = a[~np.isnan(a)]
    if len(a) == 0:
        return {"mean": float("nan"), "median": float("nan"),
                "p90": float("nan"), "max": float("nan")}
    return {
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets", default="5000,10000,20000,40000,100000,250000,500000,1000000")
    ap.add_argument("--out", default="results/p3_metrics.json")
    ap.add_argument("--fig", default="paper/figures/budget_quality")
    ap.add_argument("--qerror-mode", default="first")
    args = ap.parse_args(argv)

    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    phase1 = json.loads(Path(args.input).read_text())
    phys_stats, queries_options, qerror_base = build_problem(
        phase1, qerror_mode=args.qerror_mode)

    base_metrics = summarize(qerror_base)

    rows = []
    for B in budgets:
        res = solve_ilp(phys_stats, queries_options, qerror_base, B,
                        per_query_cap=1)
        sel_metrics = summarize(res.qerror_per_query)
        level_counts = {"100": 0, "1000": 0, "10000": 0}
        for ps in res.selected_stats:
            level_counts[str(ps.level)] = level_counts.get(str(ps.level), 0) + 1
        rows.append({
            "budget_bytes": B,
            "n_stats": len(res.selected_stats),
            "used_bytes": res.total_bytes,
            "baseline": base_metrics,
            "selected": sel_metrics,
            "level_counts": level_counts,
            "solve_s": None,
        })

    # ---- console report ----
    print(f"baseline ({len(qerror_base)} queries): "
          f"mean={base_metrics['mean']:.4f} median={base_metrics['median']:.4f} "
          f"p90={base_metrics['p90']:.3f} max={base_metrics['max']:.1f}\n")
    print(f"{'budget':>9} {'stats':>5} | {'mean':>7} {'median':>7} {'P90':>7} "
          f"{'max':>7} | {'L100':>5} {'L1000':>6} {'L10000':>7}")
    for r in rows:
        s = r["selected"]; lc = r["level_counts"]
        print(f"{r['budget_bytes']:>9} {r['n_stats']:>5} | "
              f"{s['mean']:>7.3f} {s['median']:>7.3f} {s['p90']:>7.3f} "
              f"{s['max']:>7.1f} | "
              f"{lc['100']:>5} {lc['1000']:>6} {lc['10000']:>7}")

    # ---- figure ----
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(9, 3.6), gridspec_kw={"width_ratios": [1.5, 1.0]})
    xs = [r["budget_bytes"] for r in rows]
    ax1.semilogx()
    ax1.axhline(base_metrics["mean"], color="grey", ls="--", lw=1,
                label=f"baseline mean ({base_metrics['mean']:.3f})")
    ax1.plot(xs, [r["selected"]["mean"] for r in rows], "o-", color="#d62728",
             label="selected mean")
    ax1.plot(xs, [r["selected"]["median"] for r in rows], "s-", color="#1f77b4",
             label="selected median")
    ax1.plot(xs, [r["selected"]["p90"] for r in rows], "^-", color="#2ca02c",
             label="selected P90")
    ax1.set_xlabel("storage budget (bytes, log)")
    ax1.set_ylabel("q-error")
    ax1.set_title("Budget-quality curve (multi-metric)")
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend(fontsize=7)

    # stacked level distribution
    L100 = [r["level_counts"]["100"] for r in rows]
    L1000 = [r["level_counts"]["1000"] for r in rows]
    L10000 = [r["level_counts"]["10000"] for r in rows]
    inds = np.arange(len(xs))
    ax2.bar(inds, L100, color="#ff9896", label="L100")
    ax2.bar(inds, L1000, bottom=L100, color="#ffbb78", label="L1000")
    ax2.bar(inds, L10000, bottom=np.array(L100)+np.array(L1000),
            color="#98df8a", label="L10000")
    ax2.set_xticks(inds)
    ax2.set_xticklabels([f"{b//1000}K" for b in xs], rotation=45, fontsize=7)
    ax2.set_xlabel("budget")
    ax2.set_ylabel("# physical stats")
    ax2.set_title("Capacity level mix")
    ax2.legend(fontsize=7)

    fig.tight_layout()
    fig_path = Path(args.fig + ".pdf")
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path, bbox_inches="tight")
    fig.savefig(Path(args.fig + ".png"), bbox_inches="tight", dpi=150)
    print(f"\nwrote figure {fig_path}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "input": args.input,
        "n_queries": len(qerror_base),
        "baseline": base_metrics,
        "budgets": rows,
    }, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
