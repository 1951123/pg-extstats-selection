"""End-to-end validation of the CATALOG-MASK protocol (v2, PG-internal restore).

Build all candidate stats for query.382 in ONE ANALYZE, then use
pg_statistic_ext_data.stxdmcv=NULL as a mask to measure each candidate's
INDEPENDENT effect WITHOUT further ANALYZE.

Restore is done PURELY inside PostgreSQL via a backup table + UPDATE ... FROM
(same-type assignment), avoiding the pg_mcv_list cast problem in the driver.

Protocol A (each) cost = 2 ANALYZEs per candidate. This = 1 ANALYZE build +
per-candidate (mask UPDATE + EXPLAIN + restore UPDATE), all catalog-only.
"""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query
from extstats.measure import stat_name

TARGET = 10000
SQL = ("SELECT COUNT(*) FROM climate WHERE iEnglish=0 AND dIncome7=0 AND iLooking=0 "
       "AND iMeans=0 AND iRelat1>=0 AND iRelat1<=10 AND iRlabor>=1 AND iRlabor<=2 "
       "AND iSubfam2=0")
TRUTH = 902
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")
BACKUP_TBL = "tmp_mcv_backup"

queries = parse_census_dir(Path("benchmarks/Census/queries"))
q = [x for x in queries if x.qid == "query.382"][0]
pc = generate_candidates_per_query([q], arities=(2, 3))
cands = pc["query.382"][:6]


def timing(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target={TARGET}")


def analyze(conn):
    with conn.cursor() as cur:
        cur.execute("ANALYZE climate")


def qe(conn):
    timing(conn)
    r = estimate_count_query(conn, SQL, actual=TRUTH)
    return r.qerror


def stat_oid(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT oid FROM pg_statistic_ext WHERE stxname=%s", (name,))
        row = cur.fetchone()
        return row if isinstance(row, dict) else None
    return None


def oids_for(conn):
    oidmap = {}
    for c in cands:
        name = stat_name(c, "mcv", "ext_")
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_statistic_ext WHERE stxname=%s", (name,))
            row = cur.fetchone()
            oidmap[c] = row["oid"] if isinstance(row, dict) else row[0]
    return oidmap


def backup_all(conn, oidmap):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TBL}")
        cur.execute(f"CREATE TEMP TABLE {BACKUP_TBL} (stxoid oid, stxdmcv pg_mcv_list)")
        for c, oid in oidmap.items():
            cur.execute(
                f"INSERT INTO {BACKUP_TBL} SELECT stxoid, stxdmcv FROM "
                f"pg_statistic_ext_data WHERE stxoid=%s", (oid,))


def mask_many(conn, oidmap, exclude=None):
    with conn.cursor() as cur:
        for c, oid in oidmap.items():
            if c is not exclude:
                cur.execute(
                    "UPDATE pg_statistic_ext_data SET stxdmcv=NULL WHERE stxoid=%s",
                    (oid,))


def restore_all(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE pg_statistic_ext_data d SET stxdmcv=b.stxdmcv "
            f"FROM {BACKUP_TBL} b WHERE d.stxoid=b.stxoid")


with connect(cfg) as conn:
    conn.autocommit = True
    timing(conn)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TBL}")
        for c in cands:
            cur.execute(f"DROP STATISTICS IF EXISTS {stat_name(c,'mcv','ext_')}")
    analyze(conn)

    # ONE joint build: create all + single ANALYZE
    for c in cands:
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {stat_name(c,'mcv','ext_')} (mcv) ON "
                        f"{', '.join(c.columns)} FROM climate")
    analyze(conn)
    print(f"built {len(cands)} stats in ONE ANALYZE")

    oidmap = oids_for(conn)
    print(f"JOINT (all {len(cands)} active):     qerr={qe(conn):.2f}")

    # backup all mcv
    backup_all(conn, oidmap)

    print("\nper-candidate (mask others, NO ANALYZE):")
    for target_c in cands:
        mask_many(conn, oidmap, exclude=target_c)
        qv = qe(conn)
        name = f"{target_c.table_unqualified}({','.join(target_c.columns)})"
        print(f"  {name:44s} qerr={qv:8.2f}")
        restore_all(conn)

    # cleanup
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TBL}")
        for c in cands:
            cur.execute(f"DROP STATISTICS IF EXISTS {stat_name(c,'mcv','ext_')}")
    analyze(conn)
print("done; DB restored")
