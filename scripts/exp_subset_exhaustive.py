#!/usr/bin/env python3
"""Exhaustive small-instance subset validation (review P0: prove one statistic
per query captures the best ARBITRARY subset).

The sparsity claim says the best SINGLE statistic per query captures ~all of
the attainable improvement. The strongest, non-circular check is to ENUMERATE
every non-empty subset of a query's candidates and measure the real joint
q-error of each subset (mask protocol: build all, keep only the subset's
payloads), then compare:

    best_single          = min over singletons of joint q-error
    best_2/3-subset      = greedy top-2/top-3 joint q-error
    best_exhaustive      = min over ALL subsets of joint q-error  (exact)

If best_exhaustive == best_single, then the sparse restriction costs nothing
even against the true (unrestricted) multi-select optimum.

We restrict to queries with a small candidate count so that 2^|S|-1 subset
enumerations are feasible.

Usage:
    source .venv/bin/activate
    python scripts/exp_subset_exhaustive.py \
        --db stats --level 10000 --max-cands 6 \
        --out results/p0_subset_exhaustive.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DEFAULT_DB, DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.measure_mask import (  # noqa: E402
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_name_level, _stat_oid, stat_size_bytes,
)
from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402


def subsets_nonempty(cands):
    for k in range(1, len(cands) + 1):
        for comb in itertools.combinations(cands, k):
            yield comb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--level", type=int, default=10000)
    ap.add_argument("--max-cands", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="results/p0_subset_exhaustive.json")
    args = ap.parse_args(argv)

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.db)
    qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
    per_cand = generate_candidates_per_query(qs, arities=(2, 3))

    # select queries with few candidates
    selected = []
    for q in qs:
        cands = per_cand.get(q.qid, [])
        if not cands or len(cands) > args.max_cands:
            continue
        selected.append((q, cands))
    if args.limit:
        selected = selected[: args.limit]
    print(f"queries with <= {args.max_cands} candidates: {len(selected)}")

    results = []
    with connect(cfg) as conn:
        conn.autocommit = True
        for (q, cands) in selected:
            tbl = cands[0].table.lstrip(".") or cands[0].table
            backup = f"_p0b_{len(results)}"
            _set_target(conn, args.level)
            r0 = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            base = r0.qerror if r0.qerror is not None else float("nan")
            names = {}
            with conn.cursor() as cur:
                for c in cands:
                    name = _stat_name_level(c, "mcv", "p0_", args.level)
                    names[c] = name
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                    cur.execute(f"CREATE STATISTICS {name} (mcv) ON "
                                f"{', '.join(c.columns)} FROM {tbl}")
                    cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {args.level}")
            _set_target(conn, args.level)
            _analyze(conn, _qualify_table(tbl))
            oids = {c: _stat_oid(conn, names[c]) for c in cands}
            _backup_payload(conn, backup, list(oids.values()), "mcv")

            # unique columns per candidate, to dedup singletons sharing columns
            singleton_err = {}
            best_subset = float("inf")
            best_subset_key = None
            best_single = float("inf")
            best_single_key = None
            count_subsets = 0
            for subset in subsets_nonempty(cands):
                keep = {oids[c] for c in subset}
                _mask_payload_all_but(conn, backup, keep, "mcv")
                _set_target(conn, args.level)
                res = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                jq = res.qerror if res.qerror is not None else float("nan")
                _restore_payload(conn, backup, "mcv")
                count_subsets += 1
                if jq != jq:
                    continue
                cols = "+".join(",".join(c.columns) for c in subset)
                if jq < best_subset:
                    best_subset = jq
                    best_subset_key = cols
                if len(subset) == 1:
                    if jq < best_single:
                        best_single = jq
                        best_single_key = cols

            # cleanup
            for (c, name) in names.items():
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            _drop_backup(conn, backup)
            _analyze(conn, _qualify_table(tbl))

            rec = {"qid": q.qid, "base": base, "n_cands": len(cands),
                   "n_subsets": count_subsets,
                   "best_single": best_single, "best_single_cols": best_single_key,
                   "best_exhaustive": best_subset, "best_exhaustive_cols": best_subset_key,
                   "exhaustive_minus_single": best_subset - best_single if best_single < float("inf") else None,
                   "ratio_lost_by_sparse":
                       (best_subset - base) / (best_single - base)
                       if best_single < float("inf") and base != best_single
                       and (best_subset - base) != 0 else None}
            results.append(rec)
            print(f"  {q.qid}: base={base:.2f} cands={len(cands)} "
                  f"best_single={best_single:.3f} best_exhaustive={best_subset:.3f} "
                  f"lost={best_subset-best_single:.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"level": args.level, "results": results}, indent=2))
    # summary
    diffs = [r["exhaustive_minus_single"] for r in results
             if r["exhaustive_minus_single"] is not None]
    print(f"\n== summary (n={len(results)}) ==")
    print(f"  exhaustive - single: median={np.median(diffs):.4f} "
          f"max={max(diffs):.4f}")
    print(f"  frac equal (<=1e-6): "
          f"{100*np.mean([d <= 1e-6 for d in diffs]):.0f}%")
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
