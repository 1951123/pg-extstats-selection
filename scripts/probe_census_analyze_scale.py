"""Probe: time a SINGLE ANALYZE on Census `climate` at t10000 vs number of
extended statistics, to decide between per-query vs workload-wide one-ANALYZE
measurement. Creates K distinct 2/3-col MCV stats, ANALYZE once, times it, then
drops them (all within a dedicated run; table restored after)."""
import sys, time, itertools
from pathlib import Path
from psycopg import connect
from extstats.config import DBConfig

DB = DBConfig(host="localhost", port=5432, user="postgres", password="postgres",
              dbname="census")
TABLE = "climate"
KIND = "mcv"

# column names on climate (69 cols). Read from a real stat or from pg_attribute.
def climate_cols(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT a.attname FROM pg_attribute a
                       JOIN pg_class c ON c.oid=a.attrelid
                       WHERE c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
                       ORDER BY a.attnum""", (TABLE,))
        return [r[0] if not isinstance(r, dict) else r["attname"] for r in cur.fetchall()]

def combos(cols, kvals):
    out = []
    seen = set()
    # 2-col then 3-col, deterministic
    for r in kvals:
        for c in itertools.combinations(cols, r):
            key = tuple(sorted(c))
            if key not in seen:
                seen.add(key); out.append(c)
    return out

def main():
    with connect(host=DB.host, port=DB.port, user=DB.user, password=DB.password,
                 dbname=DB.dbname, autocommit=True) as conn:
        conn.execute("SET default_statistics_target = 100")
        cols = climate_cols(conn)
        allc = combos(cols, (2, 3))
        print(f"climate cols={len(cols)} available combos={len(allc)}")
        for K in map(int, sys.argv[1:]):
            cset = allc[:K]
            names = []
            t0 = time.time()
            with conn.cursor() as cur:
                for i, c in enumerate(cset):
                    nm = f"probe_k{K}_{i}"
                    cur.execute(f"CREATE STATISTICS probe_{K}_{i} (mcv) ON {', '.join(c)} FROM {TABLE}")
                    names.append(f"probe_{K}_{i}")
            t_create = time.time() - t0
            t0 = time.time()
            conn.execute("SET default_statistics_target = 10000")
            conn.execute(f"ANALYZE {TABLE}")
            t_analyze = time.time() - t0
            print(f"K={K}: create={t_create:.1f}s analyze(t10000)={t_analyze:.1f}s "
                  f"(cumulative {len(names)} stats on table)")
            with conn.cursor() as cur:
                cur.execute("DROP STATISTICS IF EXISTS " + ", ".join(f"probe_{K}_{i}" for i in range(len(cset))))
        # restore
        conn.execute("SET default_statistics_target = 10000")
        t0 = time.time(); conn.execute(f"ANALYZE {TABLE}"); 
        print(f"restore ANALYZE after drop: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
