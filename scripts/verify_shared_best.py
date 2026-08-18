"""Workload-shared verification: build each query's BEST candidate (from the
mask phase1) ALL AT ONCE, then EXPLAIN every query to check that the shared
co-presence does NOT interfere (i.e. each query still reaches ~its best qerr).

If every query stays near its per-candidate best, then "one best stat per
query, shared across the workload" is a clean, low-conflict solution.
"""
import sys, json
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query
from extstats.parsers import parse_census_dir
from extstats.measure_mask import _set_target, _analyze

TARGET = 10000
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")
queries = parse_census_dir(Path("benchmarks/Census/queries"))
byid = {q.qid: q for q in queries}

# best candidate per query from mask phase1
p1 = json.load(open("results/phase1_census_mask_top10.json"))
best = {}
for r in p1["results"]:
    best_v, best_k = min((v["levels"]["10000"]["qerror"], k)
                         for k, v in r["candidates"].items())
    cols = tuple(best_k.split("(")[1].rstrip(")").split(","))
    best[r["qid"]] = (cols, r["qerror_base"], best_v)

print("building each query's BEST candidate, all at once...")
names = {}
with connect(cfg) as conn:
    conn.autocommit = True
    _set_target(conn, TARGET)
    # baseline per query
    base = {}
    for qid, q in byid.items():
        if qid in best:
            r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
            base[qid] = r.qerror
    # create all best candidates
    i = 0
    for qid, (cols, _, _) in best.items():
        name = f"ext_best_{i}"
        names[qid] = name
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            cur.execute(f"CREATE STATISTICS {name} (mcv) ON {', '.join(cols)} FROM climate")
        i += 1
    _analyze(conn, "climate")
    print(f"\n{'qid':>10} {'baseline':>9} {'best(single)':>12} {'SHARED(joint)':>14}  preserved?")
    for qid, (cols, _, best_v) in best.items():
        q = byid[qid]
        r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        qerr = r.qerror if r.qerror is not None else float("nan")
        preserved = "YES" if qerr <= 2.0 else "NO !!"
        print(f"{qid:>10} {base[qid]:>9.1f} {best_v:>12.1f} {qerr:>14.2f}  {preserved}")
    # cleanup
    with conn.cursor() as cur:
        cur.execute("DROP STATISTICS IF EXISTS ext_best_0, ext_best_1, ext_best_2, "
                    "ext_best_3, ext_best_4, ext_best_5, ext_best_6, ext_best_7, "
                    "ext_best_8, ext_best_9")
    _analyze(conn, "climate")
print("done; DB restored")
