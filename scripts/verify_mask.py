"""Verify the catalog-mask idea cleanly. States:
  [1] baseline (stat exists in catalog but NO data row) -> planner ignores it
  [2] re-ANALYZE -> data built -> stat active
  [3] DELETE data row -> stat ignored again (mask WITHOUT full re-ANALYZE cost
      if we could restore; here we just show delete->ignore->re-ANALYZE restores)
"""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query

TARGET = 10000
NAME = "ext_m_mask_test"
SQL = ("SELECT COUNT(*) FROM climate WHERE iEnglish=0 AND dIncome7=0 AND iLooking=0 "
       "AND iMeans=0 AND iRelat1>=0 AND iRelat1<=10 AND iRlabor>=1 AND iRlabor<=2 "
       "AND iSubfam2=0")
TRUTH = 902
cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname="census")


def timing(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target={TARGET}")


def analyze(conn):
    with conn.cursor() as cur:
        cur.execute("ANALYZE climate")


def run(conn, label):
    timing(conn)
    r = estimate_count_query(conn, SQL, actual=TRUTH)
    qe = r.qerror if r.qerror is not None else float("nan")
    print(f"  {label:52s} est={r.estimate:>9}  qerr={qe:>8.2f}")


with connect(cfg) as conn:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP STATISTICS IF EXISTS {NAME}")

    # [1] baseline: stat object exists in catalog but no data (kind not built)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE STATISTICS {NAME} (mcv) ON dIncome7, iLooking, iMeans FROM climate")
    print("=== [1] stat object exists, NO data (masked) ===")
    run(conn, "expect ~baseline (stat ignored, no error)")

    # [2] ANALYZE -> data built
    print("=== [2] ANALYZE builds the data (unmasked) ===")
    analyze(conn)
    run(conn, "expect stat active (qerr improved)")

    # [3] mask: DELETE the data row
    print("=== [3] DELETE data row (masked again) ===")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pg_statistic_ext_data WHERE stxoid IN "
            "(SELECT oid FROM pg_statistic_ext WHERE stxname=%s)", (NAME,))
    timing(conn)
    r = estimate_count_query(conn, SQL, actual=TRUTH)
    qe = r.qerror if r.qerror is not None else float("nan")
    print(f"  {'expect stat ignored again (back to baseline)':52s} "
          f"est={r.estimate:>9}  qerr={qe:>8.2f}")

    # cleanup
    with conn.cursor() as cur:
        cur.execute(f"DROP STATISTICS IF EXISTS {NAME}")
    analyze(conn)
print("done")
