#!/usr/bin/env python3
"""Generate the RQ2 main figure: geometric-mean q-error vs. storage budget.

Two panels (stats_CEB_single, CENSUS); each panel plots a log-x budget axis
against geometric-mean q-error for the MILP and the three baselines of
Sec. Setup. Data are read from the RQ4/sec-6 metrics files produced by
scripts/exp_sec6_metrics.py.

Usage:
    source .venv/bin/activate
    python scripts/fig_sec6_rq2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

METHODS = [("milp", "MILP", "tab:blue"),
           ("what_only_L100", "what-only", "tab:orange"),
           ("all_default", "all-default", "tab:green"),
           ("greedy_G2", "greedy-G2", "tab:red")]


def load(f):
    return json.loads(Path(f).read_text())


def panel(ax, data, base_gmean, title, ymax):
    budgets = [b["budget_bytes"] for b in data["budgets"]]
    xs = np.array(budgets)
    # No-stats baseline constant line
    ax.axhline(base_gmean, ls="--", c="gray", lw=1.2, label="No-stats")
    for key, label, color in METHODS:
        ys = [b["methods"][key].get("geomean") for b in data["budgets"]]
        if all(y is not None for y in ys):
            ax.plot(xs, ys, marker="o", ms=3.5, lw=1.4, c=color, label=label)
    ax.set_xscale("log")
    ax.set_xlabel("storage budget (bytes)")
    ax.set_ylabel("geometric-mean q-error")
    ax.set_title(title, fontsize=9)
    ax.set_ylim(0.99, ymax)
    ax.grid(alpha=0.3, which="both")


def main() -> int:
    stats = load("results/sec6_stats.json")
    census = load("results/sec6_census.json")
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.8))
    panel(axes[0], stats, stats["baseline"]["geomean"],
          "stats-CEB-single", 1.06)
    panel(axes[1], census, census["baseline"]["geomean"],
          "CENSUS", 1.9)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=5, fontsize=7,
               frameon=False)
    fig.tight_layout(rect=[0, 0.08, 1, 1])
    out = Path("paper/figures/sec6_rq2")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out) + ".pdf")
    fig.savefig(str(out) + ".png", dpi=150)
    print(f"[saved] {out}.pdf/.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
