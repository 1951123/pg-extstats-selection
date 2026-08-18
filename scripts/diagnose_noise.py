#!/usr/bin/env python3
"""Diagnose ANALYZE-sampling noise in measured q-error values.

Quantifies two things on a subset of the workload:
  1. measurement noise of baseline q-error and per-(combo, capacity) q-error,
     by repeating the full ANALYZE-based measurement K times (each repeat
     re-creates the statistic, re-ANALYZEs -> a fresh random sample of hist.
     statistics -> a fresh q-error).
  2. selection bias: when a naive selection picks the combo whose SINGLE
     measured q-error is best, how much does the "picked" value differ from a
     fresh re-measurement (i.e. how optimistic is selecting on one noisy draw).

Output: results/diagnose_noise.json with per-query CV stats + aggregate.

Usage
-----
    source .venv/bin/activate
    python scripts/diagnose_noise.py --queries 20 --repeats 5 --bench stats_ceb
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
)
from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.measure import stat_name  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402

_PARSERS = {"census": parse_census_dir, "job": parse_job_dir, "stats_ceb": parse_stats_ceb_dir}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB"}
_DB = {"census": "census", "job": "imdb", "stats_ceb": "stats"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--queries", type=int, default=15, help="number of query samples")
    ap.add_argument("--repeats", type=int, default=5, help="repeat measurements K")
    ap.add_argument("--out", default="results/diagnose_noise.json")
    args = ap.parse_args(argv)

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    all_queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    per_q = generate_candidates_per_query(all_queries)

    # Pick queries that have candidates.
    qs_with_cand = [q for q in all_queries if per_q.get(q.qid)]
    sample_qs = qs_with_cand[: args.queries]
    print(f"=== diagnose noise: bench={args.bench} kind={args.kind} queries={len(sample_qs)} "
          f"repeats={args.repeats} ===")

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=_DB[args.bench])
    K = args.repeats

    # Collect per-query data.
    per_query_report = []
    all_base_cv = []
    all_combo_cv = []
    selection_bias_ratios = []

    with connect(cfg) as conn:
        conn.autocommit = True
        for qi, q in enumerate(sample_qs):
            cands = per_q[q.qid]
            # baseline repeated
            base_vals = []
            for _ in range(K):
                from extstats.estimate import estimate_count_query
                r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                base_vals.append(r.qerror if r.qerror is not None else np.nan)
                # between baseline repeats we re-analyze base table? No — baseline
                # has no stats, so just EXPLAIN repeatedly (deterministic-ish).

            # per-combo repeated: for each combo, K repeats of create/analyze/explain/drop
            combo_rows = []
            for cand in cands:
                name = stat_name(cand, args.kind)
                table = _qualify_table(cand.table)
                vals = []
                for _ in range(K):
                    with conn.cursor() as cur:
                        cur.execute(
                            f"CREATE STATISTICS {name} ({args.kind}) ON "
                            f"{', '.join(cand.columns)} FROM {table}"
                        )
                    # ANALYZE under default target (baseline/single measurement)
                    with conn.cursor() as cur:
                        cur.execute("RESET default_statistics_target")
                        cur.execute(f"ANALYZE {table}")
                    r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                    vals.append(r.qerror if r.qerror is not None else np.nan)
                    with conn.cursor() as cur:
                        cur.execute(f"DROP STATISTICS {name}")
                combo_rows.append(
                    {"comboid": f"{cand.table_unqualified}({','.join(cand.columns)})",
                     "vals": vals, "mean": float(np.mean(vals)), "std": float(np.std(vals)),
                     "cv": float(np.std(vals) / (np.mean(vals) + 1e-12))}
                )

            bvals = [v for v in base_vals if v == v]
            bmean = float(np.mean(bvals)) if bvals else np.nan
            bstd = float(np.std(bvals)) if bvals else np.nan
            bcv = bstd / (bmean + 1e-12) if bmean == bmean else np.nan

            # selection bias: pick the combo that is best on its FIRST repeat,
            # then compare to that same combo's mean over all repeats.
            if combo_rows:
                first_vals = [r["vals"][0] for r in combo_rows]
                best_idx = int(np.nanargmin(first_vals))
                first_best = first_vals[best_idx]
                mean_best = combo_rows[best_idx]["mean"]
                # ratio mean/ (mean of random picks) not meaningful; compare first vs mean of same combo
                # ratio of the picked (optimistic) value to the combo's true mean
                if mean_best > 0 and first_best < mean_best:
                    selection_bias_ratios.append(mean_best / first_best)

            if bcv == bcv:
                all_base_cv.append(bcv)
            for r in combo_rows:
                if r["cv"] == r["cv"]:
                    all_combo_cv.append(r["cv"])

            per_query_report.append(
                {"qid": q.qid, "base_mean": bmean, "base_cv": bcv,
                 "n_combos": len(combo_rows),
                 "combos": [{"comboid": r["comboid"], "mean": r["mean"], "cv": r["cv"]} for r in combo_rows]}
            )
            print(f"  [{qi+1}/{len(sample_qs)}] qid={q.qid} base_cv={bcv:.3f} n_combos={len(combo_rows)}")

    summary = {
        "bench": args.bench,
        "kind": args.kind,
        "n_queries": len(sample_qs),
        "repeats": K,
        "baseline_cv": {"mean": float(np.mean(all_base_cv)), "median": float(np.median(all_base_cv)),
                        "p90": float(np.percentile(all_base_cv, 90))} if all_base_cv else None,
        "combo_cv": {"mean": float(np.mean(all_combo_cv)), "median": float(np.median(all_combo_cv)),
                     "p90": float(np.percentile(all_combo_cv, 90)), "n": len(all_combo_cv)} if all_combo_cv else None,
        "selection_bias_ratio_mean": float(np.mean(selection_bias_ratios)) if selection_bias_ratios else None,
        "selection_bias_ratio_n": len(selection_bias_ratios),
        "per_query": per_query_report,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
