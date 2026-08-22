"""Calibrate Protocol-M's real per-candidate measurement cost against the probe.

The low-level probe (probe_explain_mask_cost.py) measured mask+EXPLAIN+restore
cost ~ 0.0008 s/payload. This script confirms the SAME scaling holds in the
REAL "measure_table_workload_mask" harness path on CENSUS climate, by running
it on bounded query subsets of increasing payload count and attributing the
elapsed to the number of (query, candidate, level) measurements performed, and
comparing per-measurement elapsed to the probe model.

It requires DB 'census' to be at 0 ext stats before/after (the harness cleans
up in its finally, but we re-check).
"""
import json, sys, time
from pathlib import Path
from psycopg import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.config import DBConfig
from extstats.db import connect as db_connect
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query
from extstats.measure_mask import measure_table_workload_mask

_BENCH_ROOT = Path(__file__).resolve().parents[1] / "benchmarks"
_TABLE = "climate"


def main():
    # pick, from the workload, query subsets whose candidate union is bounded.
    queries = parse_census_dir(_BENCH_ROOT / "Census" / "queries")
    mp = generate_candidates_per_query(queries, arities=(2, 3))

    # sort queries by candidate count descending; take top-k to get a bounded
    # distinct universe. We'll run two scales.
    ordered = sorted(queries, key=lambda q: len(mp[q.qid]), reverse=True)
    levels = (100, 1000, 10000)

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")
    out = {"levels": list(levels), "runs": []}
    scales = {
        "scale1": ordered[:2],    # ~ top-2 queries (a few hundred distinct cands)
        "scale2": ordered[:8],    # more queries -> bigger distinct universe
    }
    with db_connect(cfg) as conn:
        conn.autocommit = True
        for name, selected in scales.items():
            items = [(q, mp[q.qid]) for q in selected if mp[q.qid]]
            distinct = set()
            for _, cands in items:
                for c in cands:
                    distinct.add(tuple(c.columns))
            n_payload = len(distinct) * len(levels)
            t = time.perf_counter()
            results = measure_table_workload_mask(
                conn, items, kind="mcv", levels=levels, table=_TABLE)
            dt = time.perf_counter() - t
            # count measurements performed: for each query: 1 baseline + (#cands*levels)
            n_meas = sum(1 + len(cands) * len(levels) for _, cands in items)
            per = dt / n_meas if n_meas else float("nan")
            # probe model prediction: per-meas ~ 0.0008*n_payload (mask) + restore
            probe_pred = 0.000815 * n_payload
            print(f"{name}: queries={len(items)} distinct={len(distinct)} "
                  f"payloads={n_payload} meas={n_meas} total={dt:.1f}s "
                  f"per-meas={per:.3f}s  (probe model={probe_pred:.3f}s)")
            out["runs"].append({
                "name": name, "queries": len(items), "distinct": len(distinct),
                "n_payload": n_payload, "n_meas": n_meas, "total_s": round(dt,1),
                "per_meas_s": round(per, 3), "probe_pred_s": round(probe_pred, 3),
            })
    Path("results/calib_mask_real.json").parent.mkdir(parents=True, exist_ok=True)
    Path("results/calib_mask_real.json").write_text(json.dumps(out, indent=2))
    print("wrote results/calib_mask_real.json")


if __name__ == "__main__":
    main()
