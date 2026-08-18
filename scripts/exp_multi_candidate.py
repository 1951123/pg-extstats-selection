"""Experiment: does selecting MULTIPLE non-overlapping candidates per query beat
the single best candidate? (the "middle ground" between single-best and joint).

For each query we:
  1. create ALL candidate stats for its table at a given target (ONE ANALYZE),
  2. greedily pick a non-overlapping set of candidates ordered by per-candidate
     q-error (skipping any that share a column with an already-picked one),
  3. measure the JOINT q-error when keeping the picked subset's MCVs and
     NULL-ing all others (mask protocol, keep = set of oids).

This is done for the top-1, top-2 and top-3 non-overlapping picks so we can see
whether more candidates help (they should, when single-best cannot reach 1.0).

Usage
-----
    source .venv/bin/activate
    python scripts/exp_multi_candidate.py \
        --bench stats_ceb_single --kind mcv --target-levels 100,1000,10000 \
        --qids st.562 --out results/exp_multi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.config import DEFAULT_DB, DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.measure_mask import (  # noqa: E402
    _analyze,
    _backup_payload,
    _drop_backup,
    _mask_payload_all_but,
    _restore_payload,
    _set_target,
    _stat_name_level,
    _stat_oid,
    stat_size_bytes,
)
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
    parse_stats_ceb_single_dir,
)

_PARSERS = {
    "census": parse_census_dir,
    "job": parse_job_dir,
    "stats_ceb": parse_stats_ceb_dir,
    "stats_ceb_single": parse_stats_ceb_single_dir,
}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB",
               "stats_ceb_single": "stats_CEB"}
_SINGLE_TABLE = {"census": "climate"}


def greedy_nonoverlap(cands, qerr_of):
    """Pick candidates in order of increasing q-error, skipping column-overlaps."""
    order = sorted(cands, key=lambda c: qerr_of[c])
    picked, used_cols = [], set()
    for c in order:
        cols = set(c.columns)
        if cols & used_cols:
            continue
        picked.append(c)
        used_cols |= cols
    return picked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=list(_PARSERS), default="stats_ceb_single")
    ap.add_argument("--kind", default="mcv")
    ap.add_argument("--arities", default="2,3")
    ap.add_argument("--target-levels", default="100,1000,10000")
    ap.add_argument("--qids", default="", help="comma-separated qids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="results/exp_multi_candidate.json")
    ap.add_argument("--dbname", default=None)
    args = ap.parse_args(argv)

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    levels = tuple(int(x) for x in args.target_levels.split(",") if x.strip())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    dbname = args.dbname or DEFAULT_DB[args.bench]
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=dbname)
    forced_table = _SINGLE_TABLE.get(args.bench)

    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    if args.qids:
        want = {x.strip() for x in args.qids.split(",") if x.strip()}
        queries = [q for q in queries if q.qid in want]
    if args.limit and not args.qids:
        queries = queries[: args.limit]

    per_cand = generate_candidates_per_query(queries, arities=arities)

    results = []
    with connect(cfg) as conn:
        conn.autocommit = True
        base_store = {}
        # deterministic single-col baseline at max level
        _set_target(conn, max(levels))
        # ANALYZE all tables involved once at max level
        tables = {forced_table} if forced_table else set()
        for cands in per_cand.values():
            for c in cands:
                tbl = forced_table or c.table.lstrip(".") or c.table
                tables.add(tbl)
        for tbl in tables:
            _analyze(conn, _qual(tbl))
        for qi, q in enumerate(queries):
            cands = per_cand.get(q.qid, [])
            if not cands:
                continue
            tbl = forced_table or cands[0].table.lstrip(".") or cands[0].table
            backup = f"_exp_backup_{qi}"
            # baseline
            _set_target(conn, max(levels))
            r0 = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            base = r0.qerror if r0.qerror is not None else float("nan")
            # create + build all candidate objects (multi-level)
            names = {}
            for c in cands:
                for lvl in levels:
                    name = _stat_name_level(c, args.kind, "ext_", lvl)
                    names[(c, lvl)] = name
                    with conn.cursor() as cur:
                        cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                        cur.execute(f"CREATE STATISTICS {name} ({args.kind}) ON "
                                    f"{', '.join(c.columns)} FROM {tbl}")
                        cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {int(lvl)}")
            _set_target(conn, max(levels))
            _analyze(conn, _qual(tbl))

            oids = {(c, lvl): _stat_oid(conn, names[(c, lvl)])
                    for c in cands for lvl in levels}
            _backup_payload(conn, backup, list(oids.values()), args.kind)

            rec = {"qid": q.qid, "base": base, "levels": {}}
            for lvl in levels:
                # per-candidate single qerror at this level
                qerr1 = {}
                for c in cands:
                    keep = {oids[(c, lvl)]}
                    _mask_payload_all_but(conn, backup, keep, args.kind)
                    _set_target(conn, max(levels))
                    res = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                    qerr1[c] = res.qerror if res.qerror is not None else float("nan")
                    _restore_payload(conn, backup, args.kind)
                # greedy non-overlap ordered by single-qerror
                picked = greedy_nonoverlap(cands, qerr1)
                sizes = [stat_size_bytes(conn, names[(c, lvl)], args.kind) for c in picked]
                lv = {"single_best": min(qerr1.values()),
                      "picked": [f'{c.table_unqualified}({",".join(c.columns)})'
                                 for c in picked],
                      "picked_sizes": sizes,
                      "k": {}}
                # measure joint for top1..top3 (cumulative non-overlap picks)
                for k in (1, 2, 3):
                    subset = picked[:k]
                    if not subset:
                        continue
                    keep = {oids[(c, lvl)] for c in subset}
                    _mask_payload_all_but(conn, backup, keep, args.kind)
                    _set_target(conn, max(levels))
                    res = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                    jq = res.qerror if res.qerror is not None else float("nan")
                    lv["k"][str(k)] = {"joint_qerror": jq,
                                       "size_bytes": sum(
                                           stat_size_bytes(conn, names[(c, lvl)], args.kind)
                                           for c in subset)}
                    _restore_payload(conn, backup, args.kind)
                rec["levels"][str(lvl)] = lv
            results.append(rec)
            # cleanup this query's stats
            for (c, lvl), name in names.items():
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            _drop_backup(conn, backup)
            _analyze(conn, _qual(tbl))
            print(f"  {q.qid}: base={base:.2f} " +
                  " ".join(f"L{lvl}:1={rec['levels'][str(lvl)]['single_best']:.2f} "
                           f"2={rec['levels'][str(lvl)]['k'].get('2',{}).get('joint_qerror',float('nan')):.2f} "
                           f"3={rec['levels'][str(lvl)]['k'].get('3',{}).get('joint_qerror',float('nan')):.2f}"
                           for lvl in levels))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": results}, indent=2))
    print(f"wrote {out}")
    return 0


def _qual(tbl: str) -> str:
    return tbl.lstrip(".") or tbl


if __name__ == "__main__":
    raise SystemExit(main())
