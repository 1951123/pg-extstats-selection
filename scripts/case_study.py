"""Case-study verifier for the strongest "column-correlation" extended stats.

For each fixable query we measure the REAL PostgreSQL q-error (at the
deterministic target=10000) under:
  (a) baseline (no extended stats),
  (b) each candidate statistic individually.

This shows exactly which statistic actually helps, confirming the phase-1
predictions and demonstrating that real improvement comes only from a
*correlated multi-column* stat, never from single-column/double-counted ones.

Usage
-----
    source .venv/bin/activate
    python scripts/case_study.py \
        --phase1 results/phase1_stats_ceb_mcv_det_40.json \
        --qids 232039659,6672465,593169291 \
        --out results/case_study.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.parsers import parse_stats_ceb_dir  # noqa: E402
from extstats.verify import StatToBuild, verify_statistics  # noqa: E402

TARGET = 10000  # deterministic protocol


def build_stat(tbl: str, cols: tuple[str, ...]) -> StatToBuild:
    return StatToBuild(table=tbl, columns=cols, level=TARGET, kind="mcv")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase1", default="results/phase1_stats_ceb_mcv_det_40.json")
    ap.add_argument("--qids", required=True,
                    help="comma-separated qids to case-study")
    ap.add_argument("--out", default="results/case_study.json")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.phase1).read_text())
    res = {r["qid"]: r for r in phase1["results"]}
    qby = {q.qid: q for q in parse_stats_ceb_dir(
        Path(__file__).resolve().parents[1] / "benchmarks" / "stats_CEB" / "queries")}
    qids = [x.strip() for x in args.qids.split(",") if x.strip()]

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="stats")
    results = {}
    with connect(cfg) as conn:
        conn.autocommit = True
        for qid in qids:
            r = res[qid]
            q = qby[qid]
            # baseline
            vr_base = verify_statistics(conn, [q], [], target=TARGET)
            base = vr_base.mean_qerror
            per_stat = []
            for name, cand in r["candidates"].items():
                # candidate name: "table(col,col)"
                tbl, colspart = name.split("(", 1)
                cols = tuple(colspart.rstrip(")").split(","))
                st = build_stat(tbl, cols)
                vr = verify_statistics(conn, [q], [st], target=TARGET)
                pred = cand["levels"]["10000"]["qerror"]
                per_stat.append({
                    "stat": name,
                    "size_bytes": cand["levels"]["10000"]["size_bytes"],
                    "predicted_qerror": pred,
                    "real_qerror": vr.mean_qerror,
                    "improve_vs_base_pct": (1 - vr.mean_qerror / base) * 100
                    if base else float("nan"),
                })
            results[qid] = {
                "sql": q.sql,
                "base_qerror": base,
                "stats": per_stat,
            }
            print(f"--- qid={qid}  real_base={base:.2f}")
            for s in per_stat:
                print(f"    {s['stat']:45s} pred={s['predicted_qerror']:>9.1f} "
                      f"real={s['real_qerror']:>9.1f} "
                      f"({s['improve_vs_base_pct']:+.1f}%)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"target": TARGET, "results": results}, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
