"""Measure joint-mode timing for query.382's 56 candidates: create-all, one
ANALYZE, EXPLAIN, then how long to drop+reanalyze to restore."""
import sys, time
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query
from extstats.measure import stat_name
from extstats.estimate import estimate_count_query

TARGET = 10000
queries = parse_census_dir(Path("benchmarks/Census/queries"))
sel = [q for q in queries if q.qid == "query.382"]
pc = generate_candidates_per_query(sel, arities=(2, 3))
cands = pc["query.382"]
q = sel[0]
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")


def analyze(conn):
    with conn.cursor() as cur:
        cur.execute("ANALYZE climate")


def t(label, dt):
    print(f"  {label:38s} {dt:8.3f} s")


with connect(cfg) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target={TARGET}")

    # baseline (single-col, t10000)
    t0 = time.time()
    analyze(conn)
    t1 = time.time()
    r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
    t2 = time.time()
    print(f"n_candidates = {len(cands)}")
    print(f"--- baseline (no ext stats) ---")
    t("ANALYZE (build single-col)", t1 - t0)
    t("EXPLAIN", t2 - t1)
    print(f"    qerr_base={r.qerror}")

    # create all 56 candidates
    t0 = time.time()
    for c in cands:
        name = stat_name(c, "mcv", "ext_")
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {name} (mcv) ON "
                        f"{', '.join(c.columns)} FROM climate")
    t1 = time.time()
    print(f"--- joint: create all {len(cands)} ---")
    t("CREATE ALL (56 stats)", t1 - t0)

    # one ANALYZE
    t0 = time.time()
    analyze(conn)
    t1 = time.time()
    print(f"--- joint ANALYZE (56 ext + 69 single) ---")
    t("ANALYZE", t1 - t0)

    # EXPLAIN
    t0 = time.time()
    r = estimate_count_query(conn, q.sql, actual=q.ground_truth)
    t1 = time.time()
    t("EXPLAIN", t1 - t0)
    print(f"    qerr_joint={r.qerror}")

    # drop + reanalyze restore
    t0 = time.time()
    for c in cands:
        name = stat_name(c, "mcv", "ext_")
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
    t1 = time.time()
    t("DROP ALL (56 stats)", t1 - t0)
    t0 = time.time()
    analyze(conn)
    t1 = time.time()
    t("reANALYZE (restore)", t1 - t0)

    print(f"\nTOTAL joint cycle: {t2-(time.time()- (time.time()-t2)):.3f}s" if False else "")
print("done; DB restored")
