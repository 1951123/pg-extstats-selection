"""Split-batch phase-1 measurement (Census), per the intra-query Corollary.

Variant of ``measure_phase1_mask.py`` that applies Cor.~\ref{cor:intraquery}:
queries with more than ``--cands-per-batch`` candidates are split into sub-
batches of that many candidates; each sub-batch is ANALYZEd + masked separately.
This reduces the mask term ~1/b (measured: query.335 455 cands 181s -> 63s at
7 batches) at the price of one extra ANALYZE base per sub-batch.

Default ``--cands-per-batch`` = optimal m* = sqrt(B0/mu) ~ 61 for L=1 @ target
1000. Pass --cands-per-batch 0 for true per-query (b=1, no split) to A/B compare.

Usage (smoke test, ~10-15 min):
  python scripts/measure_phase1_subbatch.py --bench census --kind mcv \
      --target 1000 --limit 30 --cands-per-batch 61 --out results/smoke_subbatch.json
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DEFAULT_DB, DBConfig, RECOMMENDED_STATS_TARGET
from extstats.parsers import (
    parse_census_dir, parse_job_dir, parse_job_light_dir, parse_job_light_full_dir,
    parse_stats_ceb_dir, parse_stats_ceb_single_dir,
)
from extstats.candidates import generate_candidates_per_query
from extstats.db import connect
from extstats.measure_mask import measure_query_mask

_PARSERS = {
    "census": parse_census_dir, "job": parse_job_dir,
    "job_light": parse_job_light_dir, "job_light_full": parse_job_light_full_dir,
    "stats_ceb": parse_stats_ceb_dir, "stats_ceb_single": parse_stats_ceb_single_dir,
}
_BENCH_DIRS = {"census": "Census", "job": "JOB", "stats_ceb": "stats_CEB",
               "stats_ceb_single": "stats_CEB"}
_SINGLE_TABLE = {"census": "climate"}
_JOB_LIGHT_QUERIES_DIR = Path(__file__).resolve().parents[1] / "benchmarks/JOB/queries"
_JOB_LIGHT_TRUTH = Path(__file__).resolve().parents[1] / "benchmarks/JOB/data/imdb_truth.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=list(_PARSERS) + ["all"], default="census")
    ap.add_argument("--kind", default="mcv", choices=["dependencies", "ndistinct", "mcv"])
    ap.add_argument("--arities", default="2,3")
    ap.add_argument("--target", type=int, default=1000,
                    help="single default_statistics_target (L=1). Ignored if "
                         "--target-levels given.")
    ap.add_argument("--target-levels", default=None,
                    help="comma-separated capacity levels to sweep, e.g. "
                         "100,1000,10000 (one stat object per (candidate,level), "
                         "multi-object one-ANALYZE per sub-batch). Overrides --target.")
    ap.add_argument("--cands-per-batch", type=int, default=61,
                    help="max candidates per ANALYZE+mask sub-batch (0 = no split, "
                         "true per-query b=1). For L-level sweep, an optimal m*="
                         "sqrt(B0/mu) gives ~L*sqrt(B0/mu)/L cands; use 55 for L=3@t10000.")
    ap.add_argument("--limit", type=int, default=0, help="only first N queries")
    ap.add_argument("--qids", default="")
    ap.add_argument("--resume", default=None,
                    help="path to an existing output JSON to resume from; "
                         "already-measured qids are skipped (enables checkpointing + continue)")
    ap.add_argument("--checkpoint-every", type=int, default=20,
                    help="write the partial output JSON every N queries (default 20)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dbname", default=None)
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    args = ap.parse_args(argv)

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    if args.target_levels:
        level_list = tuple(int(x) for x in args.target_levels.split(",") if x.strip())
        target = None
    else:
        level_list = (args.target,)
        target = args.target
    split = args.cands_per_batch  # >0 -> split big queries into sub-batches

    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    benches = list(_PARSERS) if args.bench == "all" else [args.bench]

    for bench in benches:
        dbname = args.dbname or DEFAULT_DB[bench]
        cfg = DBConfig(host=args.pghost, port=args.pgport, user=args.pguser, dbname=dbname)
        forced_table = _SINGLE_TABLE.get(bench)
        if bench == "job_light":
            queries = _PARSERS[bench](_JOB_LIGHT_QUERIES_DIR)
        elif bench == "job_light_full":
            truth = {}
            if _JOB_LIGHT_TRUTH.exists():
                truth = json.loads(_JOB_LIGHT_TRUTH.read_text())["truth"]
            queries = parse_job_light_full_dir(_JOB_LIGHT_QUERIES_DIR, truth=truth)
            dbname = args.dbname or "imdb"
            cfg = DBConfig(host=args.pghost, port=args.pgport, user=args.pguser, dbname=dbname)
        else:
            queries = _PARSERS[bench](bench_root / _BENCH_DIRS[bench] / "queries")
        if args.qids:
            want = {x.strip() for x in args.qids.split(",") if x.strip()}
            queries = [q for q in queries if q.qid in want]
        elif args.limit:
            queries = queries[: args.limit]

        per_query_cands = generate_candidates_per_query(queries, arities=arities)
        total_cands = sum(len(v) for v in per_query_cands.values())
        n_subb = sum(max(1, (len(v) + split - 1) // split) if split else 1
                     for v in per_query_cands.values() if v)
        out = Path(args.out or f"results/phase1_{bench}_{args.kind}_subbatch.json")
        out.parent.mkdir(parents=True, exist_ok=True)

        # -- resume support: load existing results, compute done qids --
        resume_results: list[dict] = []
        done_qids: set[str] = set()
        if args.resume:
            rp = Path(args.resume)
            if rp.exists():
                prev = json.loads(rp.read_text())
                resume_results = prev.get("results", [])
                done_qids = {r["qid"] for r in resume_results if isinstance(r, dict)}
                print(f"[resume] loaded {len(resume_results)} done queries from {rp}"
                      f" ; skipping them")

        def _ckpt() -> None:
            out.write_text(json.dumps({
                "bench": bench, "kind": args.kind,
                "target_levels": list(level_list),
                "cands_per_batch": split, "scope": "intra-query sub-batched (Cor.)",
                "n_queries": len(results) + len(resume_results),
                "n_sub_batches": n_subb,
                "results": resume_results + results,
            }, indent=2))

        print(f"=== phase1[subbatch] {bench} (db={dbname}, kind={args.kind}, "
              f"levels={list(level_list)}, cands/batch={split or 'b=1'}) ===")
        print(f"queries={len(queries)}  total_candidates={total_cands}  "
              f"sub-batches={n_subb}  out={out}")

        results: list[dict] = []
        t_start = datetime.now()
        measured = 0
        with connect(cfg) as conn:
            conn.autocommit = True
            sub_idx = 0
            for qi, q in enumerate(queries):
                if q.qid in done_qids:
                    print(f"  [skip] {q.qid}")
                    continue
                cands = per_query_cands.get(q.qid, [])
                # partition into sub-batches (each its own ANALYZE+mask).
                # BALANCED split: distribute n cands into b=ceil(n/split) batches
                # of sizes floor(n/b) or ceil(n/b), so no tiny residual batch pays
                # a full ANALYZE base for 1-2 candidates (kills orphan sub-batches).
                batches = []
                if not cands or not split:
                    batches = [cands]  # b=1 (whole query at once), or empty
                elif len(cands) <= split:
                    batches = [cands]
                else:
                    b = (len(cands) + split - 1) // split
                    base, rem = divmod(len(cands), b)
                    idx = 0
                    for j in range(b):
                        sz = base + (1 if j < rem else 0)
                        batches.append(cands[idx:idx + sz])
                        idx += sz
                # measure each sub-batch, merge -> one per-query record
                merged: dict = {}
                first_mes = None
                for batch in batches:
                    bt = f"_ext_subb_{sub_idx}"; sub_idx += 1
                    mes = measure_query_mask(
                        conn, q, batch, kind=args.kind, target=target,
                        target_levels=level_list if len(level_list) > 1 else None,
                        table=forced_table, backup_table=bt)
                    if first_mes is None:
                        first_mes = mes
                    merged.update(
                        {k: dict(v) for k, v in mes.candidates.items()})
                results.append({
                    "qid": first_mes.qid,
                    "actual": first_mes.actual,
                    "qerror_base": first_mes.qerror_base,
                    "estimate_base": first_mes.estimate_base,
                    "target_levels": list(level_list),
                    "n_subbatches": len(batches),
                    "candidates": merged,
                })
                measured += 1
                if measured % 20 == 0 or measured == len(queries) - len(done_qids):
                    el = (datetime.now()-t_start).total_seconds()
                    tot = len([x for x in queries if x.qid not in done_qids])
                    print(f"  [{q.qid}] done {measured}/{tot} "
                          f"({el:.0f}s, {el/max(measured,1):.1f}s/q, "
                          f"{len(results)+len(resume_results)} total)")
                if measured % args.checkpoint_every == 0:
                    _ckpt()
                    print(f"  [checkpoint] wrote {out} ({len(results)+len(resume_results)} queries)", flush=True)

        _ckpt()
        print(f"reported {measured}/{len(queries)}; wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
