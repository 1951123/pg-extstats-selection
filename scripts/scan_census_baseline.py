"""Quick Census baseline q-error scan at deterministic t10000 (single-col)."""
import sys, time, statistics
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig
from extstats.parsers import parse_census_dir
from extstats.estimate import estimate_count_query

queries = parse_census_dir(Path("benchmarks/Census/queries"))
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")

with connect(cfg) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET default_statistics_target=10000")
        t = time.time()
        cur.execute("ANALYZE climate")
        print(f"initial ANALYZE @t10000: {time.time()-t:.1f}s")
    res = []
    t0 = time.time()
    for i, q in enumerate(queries):
        r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        res.append(r.qerror if r.qerror is not None else float("nan"))
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(queries)} ({time.time()-t0:.1f}s)")
    qe = [v for v in res if v == v]
    print(f"\n=== Census baseline (t10000 single-col) q-error over {len(qe)} queries ===")
    print(f"  mean={statistics.mean(qe):.2f}  median={statistics.median(qe):.2f}")
    n1 = sum(1 for v in qe if v <= 1.001)
    n2 = sum(1 for v in qe if v <= 2)
    n5 = sum(1 for v in qe if v <= 5)
    n10 = sum(1 for v in qe if v > 10)
    n50 = sum(1 for v in qe if v > 50)
    n100 = sum(1 for v in qe if v > 100)
    print(f"  qerr<=1: {n1} ({n1/len(qe)*100:.0f}%)")
    print(f"  qerr<=2: {n2} ({n2/len(qe)*100:.0f}%)")
    print(f"  qerr<=5: {n5} ({n5/len(qe)*100:.0f}%)")
    print(f"  qerr>10: {n10} ({n10/len(qe)*100:.0f}%)")
    print(f"  qerr>50: {n50}")
    print(f"  qerr>100: {n100}")
    ranked = sorted(zip(queries, qe), key=lambda x: -x[1])[:12]
    print("\n  worst 12 queries (baseline qerr):")
    for q, v in ranked:
        print(f"    {q.qid:>10} qerr={v:>10.1f}")
