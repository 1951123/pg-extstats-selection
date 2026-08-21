"""Compute and persist true cardinalities for the full job-light (70-query)
workload by running each `SELECT COUNT(*)` query once on the imdb database.

The original `job_light_queries.sql` ships no ground truth (unlike the
sub-plan files), so we measure it once here and cache it to disk so subsequent
experiments reuse it like the other benchmarks' embedded truths.

Usage::

    PYTHONPATH=src python scripts/compute_job_light_truth.py --out results/job_light_truth.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402

# Load the full queries as raw SQL (no parser needed; they carry no truth yet).
_QUERY_FILE = (
    Path(__file__).resolve().parents[1]
    / "End-to-End-CardEst-Benchmark" / "workloads" / "job-light" / "job_light_queries.sql"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbname", default="imdb")
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    ap.add_argument("--out", default="results/job_light_truth.json")
    args = ap.parse_args(argv)

    lines = [l for l in _QUERY_FILE.read_text().splitlines()
             if l.strip() and not l.strip().startswith("--")]
    cfg = DBConfig(host=args.pghost, port=args.pgport,
                   user=args.pguser, dbname=args.dbname)
    payload: dict[str, int] = {}
    t0 = time.time()
    with connect(cfg) as conn:
        conn.autocommit = True
        for i, raw in enumerate(lines, 1):
            qid = f"jl.{i}"
            # Each line is already `SELECT COUNT(*) ...`; execute it directly.
            sql = raw.strip().rstrip(";").strip()
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                # psycopg dict_row: column is keyed by its label ("count")
                n = row["count"] if isinstance(row, dict) else row[0]
            payload[qid] = int(n)
            if i % 20 == 0 or i == len(lines):
                print(f"  truth {i}/{len(lines)} ({(time.time()-t0):.1f}s)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "bench": "job_light",
        "kind": "full_queries",
        "n_queries": len(payload),
        "truth": payload,
    }, indent=2))
    print(f"wrote {out}  (n={len(payload)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
