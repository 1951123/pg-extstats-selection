"""Census: find which high-baseline-error queries get real improvement.

Because the Census benchmark is a SINGLE table (climate, 2.4M rows, 69 cols),
Protocol-A's per-candidate ANALYZE (each ~22s at target=10000) is far too slow
for hundreds of candidates. Instead we measure, for each query that already has
a HIGH baseline q-error (worst offenders -> strongest column correlation), the
JOINT effect of creating ALL of its candidate extended statistics at once with
a SINGLE ANALYZE. This gives an upper bound on how much extended stats can help
that query, cheaply.

Mode "joint":  create all query candidates -> one ANALYZE -> EXPLAIN -> qerr.
Mode "each":   for a small subset, measure each candidate individually (slow,
               only for the single most-promising query to confirm mechanism).

Usage
-----
    source .venv/bin/activate
    python scripts/scan_census_improvement.py \
        --mode joint --min-qerr 10 --arities 2 --out results/census_joint.json
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
from extstats.estimate import estimate_count_query  # noqa: E402
from extstats.parsers import parse_census_dir  # noqa: E402
from extstats.candidates import generate_candidates_per_query  # noqa: E402
from extstats.measure import stat_name  # noqa: E402
from extstats.stats import _qualify_table  # noqa: E402

TARGET = 10000
KIND = "mcv"


def analyze(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="joint", choices=["joint", "each"])
    ap.add_argument("--min-qerr", type=float, default=10.0,
                    help="only queries with baseline q-error above this")
    ap.add_argument("--arities", default="2")
    ap.add_argument("--limit", type=int, default=0, help="max queries to scan")
    ap.add_argument("--each-limit", type=int, default=0,
                    help="each-mode: max candidate stats to try per query (0=all)")
    ap.add_argument("--qids", default="",
                    help="comma-separated qids to scan (overrides min-qerr selection)")
    ap.add_argument("--out", default="results/census_joint.json")
    args = ap.parse_args(argv)

    arities = tuple(int(x) for x in args.arities.split(",") if x.strip())
    queries = parse_census_dir(Path(__file__).resolve().parents[1]
                               / "benchmarks" / "Census" / "queries")
    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")

    per_query_cands = generate_candidates_per_query(queries, arities=arities)
    table = "climate"

    results = []
    with connect(cfg) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET default_statistics_target={TARGET}")
        # deterministic single-col baseline
        analyze(conn, table)
        # gather per-query baseline for all, pick high-qerr
        rows = []
        for q in queries:
            r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            rows.append((q, r.qerror if r.qerror is not None else float("nan")))
        high = [(q, qr) for q, qr in rows if qr >= args.min_qerr]
        high.sort(key=lambda x: -x[1])
        if args.qids:
            want = {x.strip() for x in args.qids.split(",") if x.strip()}
            high = [(q, qr) for q, qr in rows if q.qid in want]
            high.sort(key=lambda x: -x[1])
        if args.limit:
            high = high[: args.limit]
        print(f"=== census improvement scan (mode={args.mode}, "
              f"min_qerr>={args.min_qerr}, candidates arity={arities}) ===")
        print(f"high-baseline queries: {len(high)} "
              f"(of {len(rows)} total)")

        for q, base in high:
            cands = per_query_cands.get(q.qid, [])
            rec = {
                "qid": q.qid,
                "actual": q.ground_truth,
                "qerror_base": base,
                "n_candidates": len(cands),
            }
            if args.mode == "each" and cands:
                trial_cands = cands[: args.each_limit] if args.each_limit else cands
                rec["each"] = []
                for c in trial_cands:
                    name = stat_name(c, KIND, "ext_")
                    tbl = _qualify_table(c.table)
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                f"CREATE STATISTICS {name} ({KIND}) ON "
                                f"{', '.join(c.columns)} FROM {tbl}")
                        analyze(conn, table)
                        r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                        qerr = r.qerror if r.qerror is not None else float("nan")
                        rec["each"].append({
                            "stat": f"{c.table_unqualified}({','.join(c.columns)})",
                            "qerror": qerr,
                            "improve_pct": (1 - qerr / base) * 100 if qerr == qerr else float("nan"),
                        })
                    finally:
                        with conn.cursor() as cur:
                            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                        analyze(conn, table)
                best = min((e["qerror"] for e in rec["each"] if e["qerror"] == e["qerror"]),
                           default=float("nan"))
                rec["qerror_joint"] = best
                rec["best_each"] = min(
                    (e for e in rec["each"] if e["qerror"] == e["qerror"]),
                    key=lambda e: e["qerror"], default=None)
                rec["improve_pct"] = (1 - best / base) * 100 if best == best else float("nan")
                if args.each_limit:
                    for e in rec["each"]:
                        ep = e.get("improve_pct", float("nan"))
                        es = f"{ep:+.1f}%" if ep == ep else " n/a"
                        print(f"      {e['stat']:40s} qerr={e['qerror']:>9.1f} ({es})")
            elif args.mode == "joint" and cands:
                # create ALL candidate stats, one ANALYZE, one EXPLAIN
                created = []
                try:
                    for c in cands:
                        name = stat_name(c, KIND, "ext_")
                        tbl = _qualify_table(c.table)
                        with conn.cursor() as cur:
                            cur.execute(
                                f"CREATE STATISTICS {name} ({KIND}) ON "
                                f"{', '.join(c.columns)} FROM {tbl}")
                        created.append(name)
                    analyze(conn, table)
                    r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
                    rec["qerror_joint"] = r.qerror if r.qerror is not None else float("nan")
                    if rec["qerror_joint"] == rec["qerror_joint"]:
                        rec["improve_pct"] = (1 - rec["qerror_joint"] / base) * 100
                    else:
                        rec["improve_pct"] = float("nan")
                finally:
                    for name in created:
                        with conn.cursor() as cur:
                            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                    analyze(conn, table)
            else:
                rec["qerror_joint"] = float("nan")
                rec["improve_pct"] = float("nan")
            results.append(rec)
            ip = rec.get("improve_pct", float("nan"))
            ip_s = f"{ip:+.1f}%" if ip == ip else "  n/a"
            print(f"  {q.qid:>10} base={base:>9.1f} cands={len(cands):>2} "
                  f"best={rec['qerror_joint']:>9.1f} ({ip_s})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "target": TARGET, "mode": args.mode, "min_qerr": args.min_qerr,
        "arities": list(arities), "results": results,
    }, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
