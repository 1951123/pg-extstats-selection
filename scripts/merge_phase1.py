"""Merge two per-level CENSUS phase-1 snapshots into one full-level axis.

Each of the two CENSUS runs measures the SAME candidate set (same 468 queries,
same candidates, same ``single_col_target``) but at complementary capacity
levels, because a single wide-table ANALYZE run cannot cheaply sweep the whole
6-level menu:

  phase1_census_mcv_multi.json        levels [100, 1000, 10000]   (high 3)
  phase1_census_mcv_low_t10000.json   levels [10, 25, 50]         (low 3)

This script verifies that the two runs agree on qids, ordering and candidate
sets, then stitches the per-candidate ``levels`` dicts together into the full
6-level menu ``[10, 25, 50, 100, 1000, 10000]``.

The output keeps the same element schema as each input
``{qid, actual, qerror_base, estimate_base, target_levels, n_subbatches,
candidates}`` so downstream scripts (analyze_budget_metrics, etc.) work
unchanged.

Usage
-----
    source .venv/bin/activate
    python scripts/merge_phase1.py \
        --low results/phase1_census_mcv_low_t10000.json \
        --high results/phase1_census_mcv_multi.json \
        --out results/phase1_census_mcv_6level.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(p: Path) -> dict:
    with open(p) as f:
        return json.load(f)


COMBINED_LEVELS = [10, 25, 50, 100, 1000, 10000]


def merge(low_path: Path, high_path: Path, out_path: Path) -> None:
    low = load(low_path)
    high = load(high_path)

    low_res = low["results"]
    high_res = high["results"]

    # ---- validation -----------------------------------------------------
    nq = len(low_res)
    assert len(high_res) == nq, (
        f"query-count mismatch: low={nq} high={len(high_res)}")
    assert [q["qid"] for q in low_res] == [q["qid"] for q in high_res], (
        "qid order mismatch between the two runs")

    levels_low = list(map(int, low["target_levels"]))
    levels_high = list(map(int, high["target_levels"]))
    assert sorted(levels_low + levels_high) == COMBINED_LEVELS, (
        f"levels don't tile the 6-level menu: low={levels_low} high={levels_high}")

    # ---- stitch ---------------------------------------------------------
    out_res = []
    mism = 0
    for lq, hq in zip(low_res, high_res):
        lc = lq["candidates"]
        hc = hq["candidates"]
        if set(lc.keys()) != set(hc.keys()):
            mism += 1
            if mism <= 5:
                print(f"  !! candidate-set mismatch @ {lq['qid']}: "
                      f"low-only={set(lc)-set(hc)} high-only={set(hc)-set(lc)}")
        merged_cands = {}
        for cname in lc:
            cl = lc[cname].get("levels", {})
            ch = hc[cname].get("levels", {})
            levels = {**cl, **ch}
            merged_cands[cname] = {
                "table": lc[cname].get("table", hc[cname].get("table")),
                "columns": lc[cname].get("columns", hc[cname].get("columns")),
                "kind": lc[cname].get("kind", hc[cname].get("kind")),
                "levels": levels,
            }
        rec = {
            "qid": lq["qid"],
            "actual": lq["actual"],
            "qerror_base": lq["qerror_base"],
            "estimate_base": lq["estimate_base"],
            "target_levels": COMBINED_LEVELS,
            "n_subbatches": lq.get("n_subbatches", hq.get("n_subbatches")),
            "candidates": merged_cands,
        }
        out_res.append(rec)

    if mism:
        print(f"  WARNING: {mism}/{nq} queries had candidate-set mismatches")

    # verify every candidate now has the full 6 levels
    incomplete = 0
    for q in out_res:
        for cname, c in q["candidates"].items():
            if set(map(int, c["levels"].keys())) != set(COMBINED_LEVELS):
                incomplete += 1
    print(f"  candidates missing full 6 levels: {incomplete}")

    out = {
        "bench": low.get("bench", "census"),
        "kind": low.get("kind", "mcv"),
        "target_levels": COMBINED_LEVELS,
        "single_col_target": low.get("single_col_target", high.get("single_col_target")),
        "cands_per_batch": low.get("cands_per_batch", high.get("cands_per_batch")),
        "scope": "intra-query sub-batched (Cor.) merged to 6-level axis",
        "n_queries": len(out_res),
        "n_sub_batches": low.get("n_sub_batches") + high.get("n_sub_batches"),
        "results": out_res,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {out_path}  ({len(out_res)} queries, "
          f"levels {COMBINED_LEVELS})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--low", default="results/phase1_census_mcv_low_t10000.json",
                    help="low-levels [10,25,50] phase-1 snapshot")
    ap.add_argument("--high", default="results/phase1_census_mcv_multi.json",
                    help="high-levels [100,1000,10000] phase-1 snapshot")
    ap.add_argument("--out", default="results/phase1_census_mcv_6level.json",
                    help="merged 6-level output path")
    args = ap.parse_args()
    merge(Path(args.low), Path(args.high), Path(args.out))
