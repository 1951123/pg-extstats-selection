"""P5: component ablation — is each design choice necessary?

On a fixed budget, compare the achieved (predicted) mean q-error across
configurations that turn off individual components of the sparse MILP:
  - full        : levels {100,1000,10000}, per_query_cap=1, pruning ON
  - no-capacity : capacity disabled (only L10000 available) -> how much does the
                  capacity axis help?
  - multi-select: per_query_cap=None (overlap-free within query) -> how much does
                  the sparse cap cost?
  - no-prune    : skip_worse_than_baseline=False -> pruning affects only solve
                  size, not the optimum (we report n_vars / solve time).
  - no-disjoint : (reported separately; affects E2E predictability, see §7)

Usage:
    source .venv/bin/activate
    python scripts/ablate_components.py \
        --input results/phase1_ceb_single_mask_full_multi.json --budget 100000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budget", type=int, default=100000)
    ap.add_argument("--out", default="results/p5_ablation.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    base_mean = np.mean([r["qerror_base"] for r in phase1["results"]])

    def run(levels, cap, prune, label):
        # restrict phase1 to chosen levels for the no-capacity case
        if set(levels) != {100, 1000, 10000}:
            phase1_sub = dict(phase1)
            sub = []
            for r in phase1["results"]:
                rr = dict(r)
                cands = {}
                for k, cd in r.get("candidates", {}).items():
                    c = dict(cd)
                    c["levels"] = {lv: cd["levels"][str(lv)] for lv in levels
                                   if str(lv) in cd["levels"]}
                    if c["levels"]:
                        cands[k] = c
                rr["candidates"] = cands
                sub.append(rr)
            phase1_sub["results"] = sub
            phys, opts, qb = build_problem(phase1_sub, qerror_mode="first")
        else:
            phys, opts, qb = build_problem(phase1,
                skip_worse_than_baseline=prune)
        # for the no-prune case we still need skip_worse_than_baseline=False
        if prune is False and set(levels) == {100, 1000, 10000}:
            phys, opts, qb = build_problem(phase1,
                skip_worse_than_baseline=False)
        n_opt = sum(len(o) for o in opts)
        t0 = time.time()
        res = solve_ilp(phys, opts, qb, args.budget, per_query_cap=cap)
        dt = time.time() - t0
        sel_mean = float(np.mean(res.qerror_per_query))
        print(f"{label:>14}: stats={len(res.selected_stats):>3} vars={len(phys)+n_opt:>6} "
              f"mean={sel_mean:.4f} (base {base_mean:.3f}) solve={dt:.2f}s")
        return {"label": label, "n_stats": len(res.selected_stats),
                "n_phys": len(phys), "n_opt": n_opt,
                "n_vars": len(phys) + n_opt,
                "solve_s": round(dt, 3), "selected_mean": sel_mean}

    full = [100, 1000, 10000]
    rows = [
        run(full, 1, True, "full"),
        run([10000], 1, True, "no-capacity"),
        run(full, None, True, "multi-select"),
        run(full, 1, False, "no-prune"),
    ]
    # baseline for reference
    print(f"{'baseline':>14}: no stats, mean={base_mean:.3f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "input": args.input, "budget": args.budget,
        "baseline_mean": round(float(base_mean), 4),
        "configs": rows,
    }, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
