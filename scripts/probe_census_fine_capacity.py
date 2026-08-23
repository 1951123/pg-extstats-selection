"""Probe: does fine-grained capacity sampling re-reveal the capacity axis on
CENSUS's highest-improvement queries?

The coarse menu {100,1000,10000} collapsed on CENSUS (91.8% q-error identical).
This tests whether a FINER target grid reveals a smooth capacity->improvement
curve that the coarse menu hid (i.e. the sweet spot sits between coarse levels).

Only the top-improvement queries are tested, since those are where extended
stats matter most and thus where capacity should matter most (if anywhere).
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query, qerror

# Fine capacity grid (skips the very slow >5000 levels except a couple checkpoints)
LEVELS = [10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000]

CONNSTR = DBConfig(dbname="census")

CASES = [
    ("q.184", 13, ["iDisabl1", "iLooking", "iRspouse"],
     "SELECT COUNT(*) FROM climate WHERE dDepart >= 0 AND dDepart <= 2 AND iDisabl1 = 0 AND iEnglish = 0 AND iImmigr = 0 AND iLooking = 0 AND iMay75880 = 0 AND iRelat2 = 0 AND dRpincome >= 2 AND dRpincome <= 4 AND iRspouse = 1 AND dTravtime >= 0 AND dTravtime <= 4"),
    ("q.465", 29, ["iDisabl1", "iRownchld", "iYearsch"],
     "SELECT COUNT(*) FROM climate WHERE iCitizen = 0 AND iDisabl1 = 0 AND iFeb55 = 0 AND iImmigr = 0 AND iMobillim >= 0 AND iMobillim <= 2 AND iRownchld = 0 AND iRspouse >= 1 AND iRspouse <= 6 AND iSubfam1 = 0 AND iSubfam2 = 0 AND iYearsch >= 9 AND iYearsch <= 10"),
    ("q.62", 45, ["dTravtime", "iRspouse", "iWork89"],
     "SELECT COUNT(*) FROM climate WHERE iClass >= 0 AND iClass <= 1 AND dIncome3 = 0 AND iRelat2 >= 0 AND iRelat2 <= 1 AND iRspouse = 1 AND iRvetserv = 0 AND dTravtime = 0 AND iVietnam = 0 AND iWork89 = 0"),
    ("q.382", 902, ["iLooking", "iMeans", "iRlabor"],
     "SELECT COUNT(*) FROM climate WHERE iEnglish = 0 AND dIncome7 = 0 AND iLooking = 0 AND iMeans = 0 AND iRelat1 >= 0 AND iRelat1 <= 10 AND iRlabor >= 1 AND iRlabor <= 2 AND iSubfam2 = 0"),
    ("q.59", 141, ["dDepart", "dRearning", "dWeek89"],
     "SELECT COUNT(*) FROM climate WHERE dAncstry1 >= 3 AND dAncstry1 <= 11 AND dDepart >= 1 AND dDepart <= 3 AND iDisabl2 >= 0 AND iDisabl2 <= 2 AND dIncome3 = 0 AND dIncome4 >= 0 AND dIncome4 <= 1 AND iMeans >= 0 AND iMeans <= 1 AND dRearning = 0 AND iSchool >= 1 AND iSchool <= 2 AND dWeek89 = 1"),
    ("q.221", 1744, ["dRearning", "dWeek89", "iRelat1"],
     "SELECT COUNT(*) FROM climate WHERE dRearning = 0 AND iRelat1 >= 0 AND iRelat1 <= 1 AND dWeek89 = 2"),
]


def main():
    out = {"level_grid": LEVELS, "results": []}
    with connect(CONNSTR) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_statistics_target = 10000")
            cur.execute("ANALYZE climate")
        for label, actual, cols, sql in CASES:
            base = estimate_count_query(conn, sql, actual=actual)
            base_q = qerror(base.estimate, actual)
            row = {"qid": label, "actual": actual, "cols": cols,
                   "base_qerr": base_q, "levels": {}}
            print(f"[{label}] actual={actual} base_qerr={base_q:.1f} cols={cols}", flush=True)
            for lvl in LEVELS:
                with conn.cursor() as cur:
                    cur.execute("DROP STATISTICS IF EXISTS fc_mcv")
                    cur.execute(f"CREATE STATISTICS fc_mcv (mcv) ON {', '.join(cols)} FROM climate")
                    cur.execute(f"ALTER STATISTICS fc_mcv SET STATISTICS {lvl}")
                    cur.execute("ANALYZE climate")
                e = estimate_count_query(conn, sql, actual=actual)
                eq = round(qerror(e.estimate, actual), 3)
                row["levels"][lvl] = {"estimate": e.estimate, "qerror": eq}
                print(f"    L{lvl}: est={e.estimate} qerr={eq}", flush=True)
            with conn.cursor() as cur:
                cur.execute("DROP STATISTICS IF EXISTS fc_mcv")
            out["results"].append(row)
        with conn.cursor() as cur:
            cur.execute("ANALYZE climate")
    Path("results").mkdir(exist_ok=True)
    json.dump(out, open("results/probe_census_fine_capacity.json", "w"), indent=2)
    print("wrote results/probe_census_fine_capacity.json", flush=True)


if __name__ == "__main__":
    main()
