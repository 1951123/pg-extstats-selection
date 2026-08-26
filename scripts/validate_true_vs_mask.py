"""True-L (real target-specific deployment) vs Mask-L (Protocol-M) fidelity pilot.

Motivation
----------
Protocol-M builds every capacity level (L10..L10000) of a candidate in ONE
ANALYZE under a fixed single-column target of 10000, so every level (including
low ones) is constructed on a ~3M-row sample. A *real* deployment of "
SET STATISTICS L" builds that level on the sample its own target implies
(~300 x L rows). This script quantifies the resulting fidelity: it re-measures
a sampled set of candidates with a *true* target-specific ANALYZE and compares
value / decision fidelity to Protocol-M's numbers already recorded in the
phase1-Mask JSON.

True-L protocol (per candidate x level L):
    1. RESET default_statistics_target          (single-column baseline stays 100)
    2. CREATE STATISTICS s (mcv) ON <cols> FROM <table>
    3. ALTER STATISTICS s SET STATISTICS <L>    (extended object's own target)
    4. ANALYZE <table>                           (sample = max(300*100, 300*L))
    5. EXPLAIN the owning query; record q-error / est / act / size
    6. DROP STATISTICS s; ANALYZE <table>        (restore)
This is a controlled oracle-vs-deployment validation; it does NOT modify
Protocol-M.

Usage
-----
    source .venv/bin/activate
    python scripts/validate_true_vs_mask.py \\
        --bench stats_ceb_single \\
        --phase1 results/phase1_ceb_single_mask_6level.json \\
        --cands 'postHistory(CreationDate,PostHistoryTypeId),posts(AnswerCount,FavoriteCount)' \\
        --levels 10,25,50,100,1000,10000 \\
        --repeats-cands 'posts(AnswerCount,FavoriteCount,ViewCount)|3' \\
        --out results/true_vs_mask_pilot.json

The first run without --cands lists candidate keys available in --phase1 so you
can pick a stratified sample (flat / moderate / steep).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from extstats.config import DEFAULT_DB, DBConfig  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.candidates import CandidateSet  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.measure import stat_name, stat_size_bytes  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir, parse_stats_ceb_single_dir,
)
from extstats.stats import _qualify_table  # noqa: E402
from extstats.measure import stat_name  # noqa: E402

_PARSERS = {
    "census": parse_census_dir,
    "stats_ceb_single": parse_stats_ceb_single_dir,
}
_BENCH_DIRS = {"census": "Census", "stats_ceb_single": "stats_CEB"}
_KIND_DATA_COL = {"dependencies": "stxddependencies",
                  "ndistinct": "stxdndistinct", "mcv": "stxdmcv"}


def _analyze(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")


def _reset_target(conn):
    with conn.cursor() as cur:
        cur.execute("RESET default_statistics_target")


def _set_target(conn, level):
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target = {int(level)}")


def true_level(conn, table, cols, kind, level, sql, actual, stat_prefix="tv_"):
    """Measure the TRUE q-error for (candidate,level) via a real deployment.

    Single-column statistics stay at the session default (100); only the
    extended statistic's own target is set to ``level``, so ANALYZE samples at
    max(300*100, 300*level) -- the true deployment semantics.
    Returns dict with estimate/actual/qerror/size_bytes.
    """
    name = stat_name(CandidateSet(table, tuple(cols)), kind, stat_prefix)
    tbl = table.lstrip(".") or table
    try:
        _reset_target(conn)  # single-column baseline default 100
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {name} ({kind}) ON "
                        f"{', '.join(cols)} FROM {tbl}")
            cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {int(level)}")
        _analyze(conn, tbl)
        # measure with the session default (single-col 100), i.e. real planner
        _reset_target(conn)
        res = estimate_count_query(conn, sql, actual=actual)
        size = stat_size_bytes(conn, name, kind)
        return {
            "estimate": res.estimate,
            "actual": actual,
            "qerror": res.qerror if res.qerror is not None else float("nan"),
            "size_bytes": int(size) if size else 0,
            "level": int(level),
        }
    finally:
        with conn.cursor() as cur:
            try:
                cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            except Exception:
                pass
        _reset_target(conn)
        _analyze(conn, tbl)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", choices=list(_PARSERS), default="stats_ceb_single")
    ap.add_argument("--phase1", required=True, help="phase1 Mask-L JSON")
    ap.add_argument("--cands", default="", help="comma-separated candidate keys")
    ap.add_argument("--levels", default="10,25,50,100,1000,10000")
    ap.add_argument("--repeats-cands", default="",
                    help="'key|N;key|N' candidates to repeat True-L N times")
    ap.add_argument("--out", default="results/true_vs_mask_pilot.json")
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    args = ap.parse_args(argv)

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    repeats = {}
    for item in args.repeats_cands.split(";"):
        if "|" in item:
            k, n = item.rsplit("|", 1)
            repeats[k.strip()] = int(n)

    # ---- load phase1 (Mask-L) + queries (for SQL / ground truth) ----
    phase1 = json.loads(Path(args.phase1).read_text())
    bench_root = Path(__file__).resolve().parents[1] / "benchmarks"
    queries = _PARSERS[args.bench](bench_root / _BENCH_DIRS[args.bench] / "queries")
    by_qid = {q.qid: q for q in queries}
    results = phase1["results"]

    # Build candidate index: cand_key -> list of (qid) owning it
    cand_owner: dict[str, str] = {}
    for r in results:
        qid = r["qid"]
        if not r.get("candidates"):
            continue
        for ck in r["candidates"].keys():
            cand_owner.setdefault(ck, qid)

    if args.cands:
        # candidate keys contain commas, so separate them with ';'
        want = [x.strip() for x in args.cands.split(";") if x.strip()]
    else:
        # list available candidates (grouped by owning qid) so user can pick
        print("No --cands given; available candidates (key -> owning qid):")
        for ck, qid in sorted(cand_owner.items()):
            print(f"  {ck!r}  qid={qid}")
        print("\nPick a stratified set (flat/moderate/steep) and pass via --cands.")
        return 0

    dbname = args.pguser and DEFAULT_DB.get(args.bench, "postgres")
    cfg = DBConfig(host=args.pghost, port=args.pgport,
                   user=args.pguser, dbname=DEFAULT_DB[args.bench])

    out_rows = []
    with connect(cfg) as conn:
        # keep autocommit so DDL takes effect immediately
        conn.autocommit = True
        for ck in sorted(want):
            if ck not in cand_owner:
                print(f"[skip] unknown candidate key {ck!r}")
                continue
            qid = cand_owner[ck]
            q = by_qid.get(qid)
            if q is None:
                print(f"[skip] no query for qid {qid} ({ck!r})")
                continue
            # pull candidate metadata (table/columns) from phase1
            meta = None
            for r in results:
                if r["qid"] == qid and ck in (r.get("candidates") or {}):
                    meta = r["candidates"][ck]
                    break
            if meta is None:
                print(f"[skip] no candidate meta for {ck!r} in phase1")
                continue
            table = meta["table"]
            cols = list(meta["columns"])
            mask_levels = meta.get("levels", {})
            row = {"candidate": ck, "qid": qid, "table": table,
                   "columns": cols, "mask": mask_levels, "true": {}}
            for L in levels:
                nrep = repeats.get(ck, 1)
                trues = [true_level(conn, table, cols, "mcv", L,
                                   q.sql, q.ground_truth)
                         for _ in range(nrep)]
                agg = trues[0]
                if nrep > 1:
                    qs = [t["qerror"] for t in trues]
                    agg = {**trues[0],
                           "qerror": float(np.mean(qs)),
                           "qerror_repeats": qs,
                           "repeats": nrep}
                row["true"][str(L)] = agg
            out_rows.append(row)
            print(f"done {ck!r} qid={qid}")

    # ---- analysis: argmin agreement + spearman ----
    analysis = {"argmin_agreement": None, "spearman_by_level": {},
                "per_candidate": []}
    n_agree = 0
    per_cand_summary = []
    for row in out_rows:
        mask = row["mask"]; true = row["true"]
        def argmin(d):
            vals = {int(L): v["qerror"] for L, v in d.items()
                    if isinstance(v, dict) and "qerror" in v}
            vals = {L: v for L, v in vals.items() if v == v}
            return min(vals, key=vals.get) if vals else None
        Lm = argmin(mask); Lt = argmin(true)
        agree = (Lm == Lt)
        if agree:
            n_agree += 1
        per_cand_summary.append({"candidate": row["candidate"],
                                 "argmin_mask": Lm, "argmin_true": Lt,
                                 "agree": bool(agree)})
        # spearman over levels where both have q-error
        xs, ys = [], []
        for L in levels:
            if str(L) in mask and str(L) in true:
                m = mask[str(L)].get("qerror"); t = true[str(L)].get("qerror")
                if m == m and t == t:
                    xs.append(m); ys.append(t)
        rho = float(_spearman(xs, ys)) if len(xs) >= 3 else None
        per_cand_summary[-1]["spearman_mask_vs_true"] = rho
    # aggregate: spearman of argmin order? simpler: candidate-level agreement
    if out_rows:
        analysis["argmin_agreement"] = n_agree / len(out_rows)
    analysis["per_candidate"] = per_cand_summary

    result = {"bench": args.bench, "phase1": args.phase1,
              "single_col_target_deployed": 100,
              "candidates": out_rows, "analysis": analysis}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nwrote {out}")
    print("argmin (best-level) agreement:", analysis["argmin_agreement"],
          f"({n_agree}/{len(out_rows)})")
    for s in per_cand_summary:
        print(f"  {s['candidate']}: argmin Mask={s['argmin_mask']} "
              f"True={s['argmin_true']} agree={s['agree']} "
              f"spearman={s['spearman_mask_vs_true']}")
    return 0


def _spearman(xs, ys):
    """Spearman rank correlation (1 = monotonic same order, -1 = inverse)."""
    rx = {v: i for i, v in enumerate(sorted(set(xs)))}
    ry = {v: i for i, v in enumerate(sorted(set(ys)))}
    dx = [rx[v] for v in xs]; dy = [ry[v] for v in ys]
    n = len(xs)
    mx = sum(dx)/n; my = sum(dy)/n
    cov = sum((a-mx)*(b-my) for a, b in zip(dx, dy))
    vx = sum((a-mx)**2 for a in dx); vy = sum((b-my)**2 for b in dy)
    if vx == 0 or vy == 0:
        return float("nan")
    return cov / (vx**0.5 * vy**0.5)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
