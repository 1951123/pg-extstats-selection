"""Probe: confirm posts capacity leverage persists on 4/5-column combinations.

Route-1 decision probe (2026-08-22). The 3-col post `posts(AnswerCount,
FavoriteCount,ViewCount)` shows strong capacity leverage (L100 qerr ~4.6 ->
L10000 ~1.04). This probe tests whether adding a 4th/5th low-cardinality column
keeps (or extends) that leverage, by building 4/5-col MCV candidates over
moderate-selectivity equality points and measuring q-error at L100/L1000/L10000.

Method: direct CREATE STATISTICS (mcv) -> ANALYZE -> EXPLAIN per level.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query, qerror

_TABLE = "posts"
_LEVELS = [100, 1000, 10000]

# (column-values, mcv-columns) — moderate-selectivity equality points.
_COMBO_4 = [
    # (AnswerCount, FavoriteCount, Score, ViewCount), mcv over all 4
    ((0, 1, 1, 36),   ["AnswerCount","FavoriteCount","Score","ViewCount"]),
    ((0, 1, 2, 55),   ["AnswerCount","FavoriteCount","Score","ViewCount"]),
    ((1, 1, 1, 66),   ["AnswerCount","FavoriteCount","Score","ViewCount"]),
    ((0, 1, 0, 16),   ["AnswerCount","FavoriteCount","Score","ViewCount"]),
]
_COMBO_5 = [
    # (AnswerCount, FavoriteCount, Score, CommentCount, ViewCount)
    ((0, 1, 1, 2, 36),["AnswerCount","FavoriteCount","Score","CommentCount","ViewCount"]),
    ((1, 1, 1, 0, 66),["AnswerCount","FavoriteCount","Score","CommentCount","ViewCount"]),
    ((0, 1, 2, 1, 55),["AnswerCount","FavoriteCount","Score","CommentCount","ViewCount"]),
]
# Also test a 4-col WITHOUT ViewCount (all low-card cols) to isolate whether
# the high-card ViewCount is the driver.
_COMBO_LOW4 = [
    ((0, 1, 1, 2), ["AnswerCount","FavoriteCount","Score","CommentCount"]),
    ((1, 1, 1, 0), ["AnswerCount","FavoriteCount","Score","CommentCount"]),
]


def _pred(cols, vals):
    colnames = [c.lower() for c in cols]
    return " AND ".join(f"{cn}={v}" for cn, v in zip(colnames, vals))


def measure(conn, cols, pred_sql, actual):
    name = "probe_pc_mcv"
    out = {}
    for lvl in _LEVELS:
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            cur.execute(f"CREATE STATISTICS {name}(mcv) ON {', '.join(cols)} "
                        f"FROM {_TABLE}")
            cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {lvl}")
            cur.execute(f"ANALYZE {_TABLE}")
        est = estimate_count_query(conn, pred_sql).estimate
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
        out[lvl] = {"estimate": est, "qerror": qerror(est, actual),
                    "size_bytes": sz, "mcv_items": n_mcv}
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
    return out


def main():
    cfg = DBConfig(dbname="stats")
    out = {"bench": "stats", "table": _TABLE, "levels": _LEVELS, "results": []}
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_statistics_target = 10000")
        # baseline: no MCV, per-col at 10000
        with conn.cursor() as cur:
            cur.execute(f"ANALYZE {_TABLE}")
        for group, cases in [("4col", _COMBO_4), ("5col", _COMBO_5), ("4col_low", _COMBO_LOW4)]:
            for vals, cols in cases:
                pred_sql = f"SELECT count(*) FROM {_TABLE} WHERE {_pred(cols, vals)}"
                with conn.cursor() as cur:
                    cur.execute(pred_sql); row = cur.fetchone()
                    actual = row["count"] if isinstance(row, dict) else row[0]
                base = qerror(estimate_count_query(conn, pred_sql).estimate, actual)
                res = measure(conn, cols, pred_sql, actual)
                out["results"].append({"group": group, "where": _pred(cols, vals),
                                       "cols": cols, "actual": actual,
                                       "base_qerr": base, "levels": res})
                lev = "  ".join(f"L{l}={res[l]['qerror']:.2f}({res[l]['estimate']})"
                                for l in _LEVELS)
                print(f"[{group}] WHERE {_pred(cols, vals)}: actual={actual} "
                      f"base={base:.2f} | {lev}")
            with conn.cursor() as cur:
                cur.execute(f"ANALYZE {_TABLE}")
    Path("results").mkdir(exist_ok=True)
    json.dump(out, open("results/probe_posts_capacity.json", "w"), indent=2)
    print("\nwrote results/probe_posts_capacity.json")


if __name__ == "__main__":
    main()
