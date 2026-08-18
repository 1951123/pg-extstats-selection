"""Measure ANALYZE time with N extended statistics on climate.

Builds N distinct 2-column MCV stats (lazy), then times ANALYZE climate at
t10000, then drops all + re-analyze to restore. Used to scale the
"#stats -> ANALYZE time" curve (we only had 0/1/56 data points).
"""
import sys, time, itertools
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig

N = 1000
TARGET = 10000
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")

# 69 columns of climate (from information_schema)
with connect(cfg) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='climate' ORDER BY ordinal_position")
        cols = [r["column_name"] for r in cur.fetchall()]
print(f"climate columns: {len(cols)}")

# first N distinct 2-col combos
combos = list(itertools.combinations(cols, 2))[:N]
print(f"building {len(combos)} 2-col mcv stats")

with connect(cfg) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target={TARGET}")
    # create N stats (lazy)
    t0 = time.time()
    names = []
    for i, (a, b) in enumerate(combos):
        name = f"ext_scale_{i}"
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {name} (mcv) ON {a}, {b} FROM climate")
        names.append(name)
    t1 = time.time()
    print(f"CREATE {N} stats: {t1-t0:.2f}s")

    # time ANALYZE
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("ANALYZE climate")
    t1 = time.time()
    print(f"ANALYZE climate with {N} ext stats @t10000: {t1-t0:.2f}s")

    # cleanup
    t0 = time.time()
    for name in names:
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
    with conn.cursor() as cur:
        cur.execute("ANALYZE climate")
    t1 = time.time()
    print(f"cleanup (drop {N} + re-ANALYZE): {t1-t0:.2f}s")
print(f"restored; check no residual: ", end="")
with connect(cfg) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_statistic_ext WHERE stxname LIKE 'ext_scale_%'")
        print(cur.fetchone()[0])
