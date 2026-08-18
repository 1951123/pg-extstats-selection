#!/usr/bin/env python3
"""Diagnose whether multi-measurement (option A) fixes 'measure-then-select'
unreliability.

For a sample of queries we measure each candidate statistic K times. We then
compare, on real PostgreSQL:
  - the per-query BEST combo chosen by a SINGLE draw (M1-style),
  - the per-query BEST combo chosen by a CONSERVATIVE multi-measure rule
    (e.g. worst-of-K, or mean, or p90),
each built and measured once in a clean session, against the baseline.

If the conservative rule yields q-error <= baseline more reliably than the
single-draw rule, then option A (multi-measure + conservative selection) is a
sound mitigation — justifying a full implementation.

Usage
-----
    source .venv/bin/activate
    python scripts/explore_multi_measure.py --bench stats_ceb --queries 10 --repeats 5
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
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.measure import stat_name  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402
from extstats.verify import StatToBuild, verify_statistics  # noqa: E402

_PARSERS = {"census": parse_census_dir, "job": parse_job_dir, "stats_ceb": parse_stats_ceb_dir}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB"}
_DB = {"census": "census", "job": "imdb", "stats_ceb": "stats"}


def measure_combo_k(conn, q, cand, kind, K, reset_target=True):
    """Measure a combo's q-error K times (create/analyze/explain/drop each time)."""
    name = stat_name(cand, kind)
    table = _qualify_table(cand.table)
    vals = []
    with conn.cursor() as cur:
        cur.execute("RESET default_statistics_target")
    for _ in range(K):
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE STATISTICS {name} ({kind}) ON {', '.join(cand.columns)} FROM {table}"
            )
            cur.execute(f"ANALYZE {table}")
        r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        vals.append(r.qerror if r.qerror is not None else np.nan)
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS {name}")
    return vals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=["census", "job", "stats_ceb"], default="stats_ceb")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--out", default="results/multi_measure_explore.json")
    args = ap.parse_args(argv)

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    all_q = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    per_q_cands = generate_candidates_per_query(all_q)
    sample = [q for q in all_q if per_q_cands.get(q.qid)][: args.queries]

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=_DB[args.bench])
    print(f"=== multi-measure explore: bench={args.bench} queries={len(sample)} repeats={args.repeats} ===")

    rows = []
    with connect(cfg) as conn:
        conn.autocommit = True
        for q in sample:
            cands = per_q_cands[q.qid]
            # baseline real
            with conn.cursor() as cur:
                cur.execute("RESET default_statistics_target")
            r0 = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            base = r0.qerror if r0.qerror is not None else np.nan

            # K measurements per combo
            combo_vals = {}
            for cand in cands:
                combo_vals[cand] = measure_combo_k(conn, q, cand, args.kind, args.repeats)

            # choose best by single first draw vs conservative rules
            single_best = min(combo_vals, key=lambda c: combo_vals[c][0])
            # conservative: worst-of-K (max) — chooses combo with best WORST case
            worst_best = min(combo_vals, key=lambda c: max(combo_vals[c]))
            # mean best
            mean_best = min(combo_vals, key=lambda c: np.mean(combo_vals[c]))

            # verify each choice in a real build (unified-ish; build only that one)
            chosen_results = {}
            for label, cb in [("single", single_best), ("worst", worst_best), ("mean", mean_best)]:
                st = StatToBuild(table=_qualify_table(cb.table), columns=cb.columns,
                                 level=100, kind=args.kind)
                with connect(cfg) as conn2:
                    conn2.autocommit = True
                    vr = verify_statistics(conn2, [q], [st])
                chosen_results[label] = {
                    "combo": f"{_qualify_table(cb.table)}({','.join(cb.columns)})",
                    "real_qerr": vr.mean_qerror,
                    "improves": vr.mean_qerror < base,
                }
            rows.append({
                "qid": q.qid, "base": base,
                "single_vals": [round(v,3) for v in combo_vals[single_best]],
                "selection": chosen_results,
            })
            print(f"  qid={q.qid} base={base:.3f} "
                  f"single={chosen_results['single']['real_qerr']:.3f}({chosen_results['single']['combo']}) "
                  f"worst={chosen_results['worst']['real_qerr']:.3f} "
                  f"mean={chosen_results['mean']['real_qerr']:.3f}")

    # aggregate: how often each rule improves
    for rule in ["single", "worst", "mean"]:
        n = sum(1 for r in rows if r["selection"][rule]["improves"])
        print(f"\nrule={rule}: improves {n}/{len(rows)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"repeats": args.repeats, "rows": rows}, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
