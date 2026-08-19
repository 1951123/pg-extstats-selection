#!/usr/bin/env python3
"""Corrected top-1 sparsity metric: top-1 vs. MEASURED best k-subset.

The earlier "top-1 coverage ratio" (base-best)/(base-best single) was
tautological (numerator == denominator). This recomputes RQ2's central claim
with the correct, non-circular definition:

    coverage_k = (e0 - joint_1) / (e0 - joint_k)

where joint_1 and joint_k are the *measured* joint q-errors of the best k=1 and
k=2,3 non-overlapping candidate subsets (from https exp_multi full-workload run
on all 632 queries, mask protocol). A value of ~1 means the single best
statistic already captures essentially all of the improvement that adding a
2nd/3rd statistic would yield.

Usage:
    python scripts/analyze_sparsity_corrected.py \
        --input results/exp_multi_full.json --out results/p4_sparsity_corrected.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--level", default="10000")
    ap.add_argument("--out", default="results/p4_sparsity_corrected.json")
    args = ap.parse_args(argv)

    d = json.loads(Path(args.input).read_text())
    res = d["results"]

    cov2, cov3 = [], []
    gaps = []
    n_top1_eq_top2 = 0
    n_total = 0
    for r in res:
        lv = r.get("levels", {}).get(args.level, {})
        if not lv:
            continue
        base = r["base"]
        k = lv.get("k", {})
        j1 = (k.get("1") or {}).get("joint_qerror")
        j2 = (k.get("2") or {}).get("joint_qerror")
        j3 = (k.get("3") or {}).get("joint_qerror")
        if j1 is None:
            continue
        n_total += 1
        if j2 is not None:
            gaps.append(j1 - j2)
            if abs(j1 - j2) < 1e-6:
                n_top1_eq_top2 += 1
            if base > 1.2 and j2 < base - 1e-9:
                cov2.append((base - j1) / (base - j2))
        if j3 is not None and base > 1.2 and j3 < base - 1e-9:
            cov3.append((base - j1) / (base - j3))

    cov2 = np.array(cov2); cov3 = np.array(cov3); gaps = np.array(gaps)

    def summ(a):
        return {"n": int(len(a)),
                "median": round(float(np.median(a)), 4),
                "frac_ge_0_9": round(float((a >= 0.9).mean()), 4),
                "frac_ge_0_95": round(float((a >= 0.95).mean()), 4),
                "frac_ge_0_99": round(float((a >= 0.99).mean()), 4)}

    out = {
        "metric": ("coverage_k = (e0 - joint_1) / (e0 - joint_k), measured "
                   "joint k=1/2/3 subset q-errors (non-circular)"),
        "level": args.level,
        "n_queries_with_candidates": n_total,
        "coverage_vs_best_2_subset": summ(cov2),
        "coverage_vs_best_3_subset": summ(cov3),
        "top2_minus_top1_gap": {
            "median": round(float(np.median(gaps)), 5),
            "max": round(float(gaps.max()), 5),
            "frac_gap_gt_0_01": round(float((gaps > 0.01).mean()), 4),
            "frac_top1_eq_top2_1e6": round(n_top1_eq_top2 / n_total, 4),
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))

    print(f"queries with candidates: {n_total}")
    print(f"coverage vs best-2 subset (n={len(cov2)}): "
          f"median={out['coverage_vs_best_2_subset']['median']} "
          f">=0.9: {100*out['coverage_vs_best_2_subset']['frac_ge_0_9']:.0f}% "
          f">=0.99: {100*out['coverage_vs_best_2_subset']['frac_ge_0_99']:.0f}%")
    print(f"coverage vs best-3 subset (n={len(cov3)}): "
          f"median={out['coverage_vs_best_3_subset']['median']}")
    print(f"top1==top2 exactly ({1e-6}): "
          f"{100*out['top2_minus_top1_gap']['frac_top1_eq_top2_1e6']:.0f}% ; "
          f"max gap={out['top2_minus_top1_gap']['max']}")
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
