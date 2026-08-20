#!/usr/bin/env python3
"""R2: candidate-width sensitivity --- is the 2/3-column restriction
artificially manufacturing sparsity?

The critique: candidates are 2-/3-column subsets of a query's selection
predicates, so sparsity ("one dominant statistic per query") might be an
artifact of never considering wider statistics. This experiment relaxes the
width: for the same queries we additionally generate 4-column and 5-column MCV
candidates (mask-measured, identical protocol) and ask whether the best
single-candidate q-error materially improves.

If widening to 4/5 columns changes nothing, the 2/3-column scope and the sparse
claim are validated. If it improves some query, that is a stated limitation.

Run:
    python scripts/exp_candidate_width.py --db stats --level 10000 \
        --qids st.144,st.588,st.326,st.308,st.562,st.182,st.284,st.314,st.398 \
        --out results/p2_candidate_width.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.measure_mask import (  # noqa: E402
    _analyze, _set_target,
)
from extstats.parsers import parse_stats_ceb_single_dir  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402

WIDTHS = {"23": (2, 3), "234": (2, 3, 4), "2345": (2, 3, 4, 5)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="stats")
    ap.add_argument("--level", type=int, default=10000)
    ap.add_argument("--qids", required=True)
    ap.add_argument("--bench", default="stats_ceb_single",
                    choices=["stats_ceb_single", "census"])
    ap.add_argument("--out", default="results/p2_candidate_width.json")
    args = ap.parse_args(argv)

    qids = [q.strip() for q in args.qids.split(",") if q.strip()]
    if args.bench == "census":
        from extstats.parsers import parse_census_dir
        qs = parse_census_dir(Path("benchmarks/Census/queries"))
    else:
        qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
    sel = [q for q in qs if q.qid in qids]

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=args.db,
                   password="postgres")
    prefix = "cw_"

    # per-width best single candidate q-error per query, measured via a
    # min-over-singletons mask sweep
    from extstats.measure_mask import (
        _backup_payload, _drop_backup, _mask_payload_all_but, _restore_payload,
        _stat_name_level, _stat_oid,
    )
    from extstats.estimate import estimate_count_query

    results = []
    with connect(cfg) as conn:
        conn.autocommit = True
        for Wname, arities in WIDTHS.items():
            print(f"\n=== widths {arities} ===")
            per_cand = generate_candidates_per_query(sel, arities=arities)
            for q in sel:
                cands = per_cand.get(q.qid, [])
                if not cands:
                    continue
                tbl = cands[0].table.lstrip(".") or cands[0].table
                _set_target(conn, args.level)
                r0 = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                base = r0.qerror if r0.qerror is not None else float("nan")
                # build all candidates for this query's table
                names = {}
                with conn.cursor() as cur:
                    for c in cands:
                        name = _stat_name_level(c, "mcv", prefix, args.level)
                        names[c] = name
                        cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                        cur.execute(f"CREATE STATISTICS {name} (mcv) ON "
                                    f"{', '.join(c.columns)} FROM {tbl}")
                        cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {args.level}")
                _set_target(conn, args.level)
                _analyze(conn, _qualify_table(tbl))
                oids = {c: _stat_oid(conn, names[c]) for c in cands}
                bkey = f"_cwb_{q.qid.replace('.', '_')}"
                _backup_payload(conn, bkey, list(oids.values()), "mcv")
                # min over singletons
                best = float("inf"); best_cols = None
                n_cand = len(cands); n4 = 0; n5 = 0
                for c in cands:
                    _mask_payload_all_but(conn, bkey, {oids[c]}, "mcv")
                    _set_target(conn, args.level)
                    res = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                    jq = res.qerror if res.qerror is not None else float("nan")
                    _restore_payload(conn, bkey, "mcv")
                    if jq == jq and jq < best:
                        best = jq; best_cols = c.columns
                    if len(c.columns) == 4:
                        n4 += 1
                    elif len(c.columns) == 5:
                        n5 += 1
                # cleanup
                for name in names.values():
                    with conn.cursor() as cur:
                        cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                _drop_backup(conn, bkey)
                _analyze(conn, _qualify_table(tbl))
                results.append({"qid": q.qid, "widths": Wname,
                                "base": base, "best": best,
                                "best_cols": list(best_cols) if best_cols else None,
                                "n_cand": n_cand, "n4": n4, "n5": n5})
                print(f"  {q.qid} widths={Wname} n_cand={n_cand} "
                      f"base={base:.3f} best={best:.3f} cols={best_cols}")

    # ---- aggregation: best q-error by width, per query ----
    byq = {}
    for r in results:
        byq.setdefault(r["qid"], {})[r["widths"]] = r

    summary = {}
    for q in byq:
        row = {}
        for w in ("23", "234", "2345"):
            if w in byq[q]:
                row[w] = round(byq[q][w]["best"], 4)
        summary[q] = row
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"level": args.level, "per_width_best": summary,
         "results": results}, indent=2))
    print("\n=== best single q-error by width (per query) ===")
    for q, row in summary.items():
        print(f"  {q}: " + ", ".join(f"{w}={v}" for w, v in row.items()))
    print(f"[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
