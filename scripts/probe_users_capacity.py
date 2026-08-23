"""Probe: does the capacity axis show q-error leverage on `users` high-cardinality
column combinations (analogous to the posts AnswerCount/FavoriteCount/ViewCount
example)?

Route C decision probe (2026-08-22): the current 632-query set exercises NO
high-cardinality combos on users (nothing touching reputation/views/upvotes/
downvotes), which is why users showed ~zero capacity leverage. This probe builds
synthetic equality queries over those columns and measures whether raising the
MCV capacity (statistics_target 100 -> 1000 -> 10000) lowers q-error.

Method: direct CREATE STATISTICS (mcv) -> ANALYZE -> EXPLAIN per level (only a
handful of candidates, so no need for the full catalog-mask machinery). Ground
truth = actual COUNT(*).
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import explain_json, top_estimate, qerror

_TABLE = "users"
_LEVELS = [100, 1000, 10000]

# High-cardinality "selective" points (unique row targets) over the 3 columns.
_POINTS = [
    (25123, 1245, 582),
    (22625, 4069, 2496),
    (18283, 3781, 1014),
    (18187, 1660, 230),
    (16108, 1673, 63),
    (14082, 3320, 4235),
]


def explain_est(conn, sql):
    plan = explain_json(conn, sql)
    return top_estimate(plan)


def measure(conn, cols, pred_sql, actual):
    """Build MCV over cols, ANALYZE, EXPLAIN at each level. Returns per-level
    {estimate, qerror, size_bytes, mcv_entries}."""
    name = f"probe_mcv_{'_'.join(cols)}"
    results = {}
    for lvl in _LEVELS:
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            cur.execute(
                f"CREATE STATISTICS {name} (mcv) ON {', '.join(cols)} FROM {_TABLE}")
            cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {lvl}")
            cur.execute(f"ANALYZE {_TABLE}")
        est = explain_est(conn, pred_sql)
        # size of the mcv payload + number of mcv tuples
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_column_size(stxdmcv) AS sz, stxdmcv AS mcv "
                "FROM pg_statistic_ext "
                "JOIN pg_statistic_ext_data ON pg_statistic_ext.oid=pg_statistic_ext_data.stxoid "
                "WHERE stxname=%s", (name,))
            row = cur.fetchone()
            sz = (row["sz"] if isinstance(row, dict) else row[0]) or 0
            mcvs = row["mcv"] if isinstance(row, dict) else row[1]
            n_mcv = len(mcvs) if mcvs else 0
        results[lvl] = {
            "estimate": est, "qerror": qerror(est, actual),
            "size_bytes": sz, "mcv_entries": n_mcv,
        }
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
    return results


def main():
    cfg = DBConfig(dbname="stats")
    out = {"bench": "stats", "table": _TABLE, "levels": _LEVELS, "results": []}
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_statistics_target = 10000")
        for rep, views, upv in _POINTS:
            cols = ["reputation", "views", "upvotes"]
            where = f"reputation={rep} AND views={views} AND upvotes={upv}"
            pred_sql = f"SELECT count(*) FROM {_TABLE} WHERE {where}"
            with conn.cursor() as cur:
                cur.execute(pred_sql); row = cur.fetchone()
                actual = row["count"] if isinstance(row, dict) else row[0]
            res = measure(conn, cols, pred_sql, actual)
            out["results"].append({
                "qid": f"probe({where})", "actual": actual,
                "cols": cols, "levels": res,
            })
            print(f"WHERE {where}: actual={actual}")
            for l, v in res.items():
                print(f"   L{l}: est={v['estimate']} qerror={v['qerror']:.3f} "
                      f"size={v['size_bytes']}B mcv={v['mcv_entries']}")
            # restore
            with conn.cursor() as cur:
                cur.execute(f"ANALYZE {_TABLE}")
    Path("results").mkdir(exist_ok=True)
    json.dump(out, open("results/probe_users_capacity.json", "w"), indent=2)
    print("\nwrote results/probe_users_capacity.json")


if __name__ == "__main__":
    main()
