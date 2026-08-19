#!/usr/bin/env python3
"""Experiment 5: catalog-mask scalability + correctness.

Upgrades Protocol-M from a "clever trick" to a systems contribution by
measuring, end-to-end:

  Part A (correctness, on the real `posts` table)
    For several (candidate, query) pairs, the q-error is measured three ways
    and compared:
      * singleton    : build ONLY that statistic, ANALYZE, EXPLAIN
      * masked       : build ALL candidate statistics for the table in ONE
                       ANALYZE, NULL the other payloads (Protocol-M), EXPLAIN
      Since a mask must reproduce the singleton effect exactly, these must
      agree; any residual is Protocol-M's measurement error.

  Part B (scalability, on a controlled synthetic wide table)
    Measures:
      * A0        : ANALYZE time with 0 statistics (Protocol-A per-candidate
                    cost is 2*A0 + EXPLAIN, since each candidate is isolated)
      * A(N)      : ANALYZE time with N statistics co-built (Protocol-M's ONE
                    ANALYZE cost)
      * EXPLAIN   : per-candidate estimate cost
      * MASK      : per-candidate catalog-mask UPDATE overhead
    Then derives, per N:
      * Protocol-A total = N * (2*A0 + EXPLAIN)
      * Protocol-M total = A(N) + N * (EXPLAIN + MASK)
      * speedup / catalog-overhead fraction
    Sweeping N = 1, 10, 100, 500, 1000, 3000, 5000.

Usage:
    source .venv/bin/activate
    python scripts/exp_catalog_mask_scale.py \\
        --db stats --out results/p5_mask_scale.json \\
        --fig paper/figures/mask_scale
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.config import DBConfig  # noqa: E402
from extstats.db import connect  # noqa: E402
from extstats.estimate import estimate_count_query  # noqa: E402

_TARGET = 1000
_N_STEPS = [1, 10, 100, 500, 1000, 3000, 5000]


def _cols(n=40):
    return [f"c{k}" for k in range(n)]


def build_wide(conn, table="syn_wide"):
    colnames = _cols()
    parts = [f"((i * {(k + 3)}) % {7 + (k % 7)}) AS {c}"
             for k, c in enumerate(colnames)]
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} AS SELECT {', '.join(parts)} "
                    f"FROM generate_series(0, 199999) AS i")
        cur.execute("ANALYZE " + table)


def time_analyze(conn, table):
    t = time.time()
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {table}")
    return time.time() - t


def time_explain(conn, n=50):
    sql = ("SELECT COUNT(*) FROM syn_wide WHERE "
           + " AND ".join(f"c{k}=5" for k in range(3)))
    t = time.time()
    for _ in range(n):
        estimate_count_query(conn, sql, actual=None)
    return (time.time() - t) / n


def time_mask(conn, n=30):
    # catalog-delta cost of a Protocol-M mask update on one candidate
    with conn.cursor() as cur:
        cur.execute("CREATE STATISTICS mask_probe (mcv) ON c0, c1 FROM syn_wide")
        cur.execute("ANALYZE syn_wide")
        cur.execute("DROP TABLE IF EXISTS _mask_bak")
        cur.execute("CREATE TABLE _mask_bak (stxoid oid PRIMARY KEY, "
                    "payload pg_mcv_list)")
        cur.execute("INSERT INTO _mask_bak SELECT stxoid, stxdmcv "
                    "FROM pg_statistic_ext_data "
                    "WHERE stxoid IN (SELECT oid FROM pg_statistic_ext "
                    "WHERE stxname='mask_probe')")
    t = time.time()
    for _ in range(n):
        with conn.cursor() as cur:
            cur.execute("UPDATE pg_statistic_ext_data SET stxdmcv = NULL "
                        "WHERE stxoid IN (SELECT oid FROM pg_statistic_ext "
                        "WHERE stxname='mask_probe')")
            cur.execute("UPDATE pg_statistic_ext_data SET stxdmcv = b.payload "
                        "FROM _mask_bak b WHERE pg_statistic_ext_data.stxoid = b.stxoid")
    dt = (time.time() - t) / n
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS _mask_bak")
        cur.execute("DROP STATISTICS IF EXISTS mask_probe")
        cur.execute("ANALYZE syn_wide")
    return dt


# ---------------------------------------------------------------------------
# Part A: correctness (mask vs singleton) on the real `posts` table
# ---------------------------------------------------------------------------
def part_a(conn, out):
    """Direct A/B: masked q-error vs singleton q-error for shared siblings.

    For each target (combo, level) we measure the query's q-error twice:
      singleton : only the target stat physically exists (CREATE+ANALYZE)
      masked    : target + 2 overlapping siblings co-built in one ANALYZE,
                  then the siblings' payloads are NULLed (only target kept)
    A correct Protocol-M must give identical q-error in both cases.
    """
    from extstats.parsers import parse_stats_ceb_single_dir
    from extstats.measure_mask import (_analyze, _backup_payload, _drop_backup,
                                       _mask_payload_all_but, _restore_payload,
                                       _set_target)
    from extstats.stats import _qualify_table
    qs = parse_stats_ceb_single_dir(Path("benchmarks/stats_CEB/queries"))
    byid = {q.qid: q for q in qs}

    p1f = Path("results/phase1_ceb_single_mask_full_multi.json")
    if not p1f.exists():
        out["part_a"] = {"error": "phase-1 input not found"}
        return []
    phase1 = json.loads(p1f.read_text())
    # find up to 3 posts queries with >=3 candidate combos (enough siblings)
    targets = []
    for r in phase1["results"]:
        if targets and len(targets) >= 3:
            break
        pcands = [c for c in r.get("candidates", {}).values()
                  if c.get("table") == "posts"]
        if len(pcands) < 3:
            continue
        # best combo by L10000 q-error
        best = min(pcands, key=lambda c: min(v["qerror"]
                   for v in c.get("levels", {}).values()))
        best_cols = tuple(best["columns"])
        best_level = int(min(best["levels"], key=lambda ls: best["levels"][ls]["qerror"]))
        siblings = [tuple(c["columns"]) for c in pcands[:3]
                    if tuple(c["columns"]) != best_cols]
        targets.append((r["qid"], best_cols, best_level, siblings))

    prefix = "p5a_"
    def _clean(tables):
        with conn.cursor() as cur:
            cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
            for r in cur.fetchall():
                cur.execute(f"DROP STATISTICS IF EXISTS {r['stxname']}")
            for t in tables:
                cur.execute(f"ANALYZE {_qualify_table(t)}")

    rows = []
    for qid, cols, level, siblings in targets:
        q = byid.get(qid)
        if q is None:
            continue
        table = ("posts")
        # singleton
        _clean([table])
        _set_target(conn, level)
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {prefix}sing (mcv) ON "
                        f"{', '.join(cols)} FROM {table}")
            cur.execute(f"ANALYZE {table}")
        rs = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        q_sing = rs.qerror if rs.qerror is not None else float("nan")
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {prefix}sing")
            cur.execute(f"ANALYZE {table}")
        # masked: target + siblings co-built, mask all but target
        all_names = [f"{prefix}m{i}" for i in range(1 + len(siblings))]
        oids = []
        _clean([table])
        _set_target(conn, max([level] + [1000] * len(siblings)))
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {all_names[0]} (mcv) ON "
                        f"{', '.join(cols)} FROM {table}")
            cur.execute(f"ALTER STATISTICS {all_names[0]} SET STATISTICS {level}")
            for j, sc in enumerate(siblings, start=1):
                cur.execute(f"CREATE STATISTICS {all_names[j]} (mcv) ON "
                            f"{', '.join(sc)} FROM {table}")
            cur.execute(f"ANALYZE {table}")
            for nm in all_names:
                cur.execute("SELECT oid FROM pg_statistic_ext WHERE stxname=%s", (nm,))
                oids.append(cur.fetchone()["oid"])
        backup = f"{prefix}bak"
        _backup_payload(conn, backup, oids, "mcv")
        _mask_payload_all_but(conn, backup, {oids[0]}, "mcv")
        rm = estimate_count_query(conn, q.sql, actual=q.ground_truth)
        q_mask = rm.qerror if rm.qerror is not None else float("nan")
        _restore_payload(conn, backup, "mcv")
        _drop_backup(conn, backup)
        _clean([table])
        rows.append({"qid": qid, "cols": list(cols), "level": level,
                     "singleton": q_sing, "masked": q_mask,
                     "abs_diff": abs(q_sing - q_mask),
                     "rel_diff": abs(q_sing - q_mask) / q_sing if q_sing else None})
        print(f"  q{qid} [{'+'.join(cols)}@L{level}]  singleton={q_sing:.4f} "
              f"masked={q_mask:.4f}  |diff|={abs(q_sing-q_mask):.4f}")
    out["part_a"] = {"note": "masked (co-built siblings, others NULLed) vs "
                             "singleton (only this stat) q-error; equality = "
                             "Protocol-M correctness",
                     "pairs": rows}
    return rows


# ---------------------------------------------------------------------------
# Part B: scalability on synthetic wide table
# ---------------------------------------------------------------------------
def part_b(conn, out):
    table = "syn_wide"
    build_wide(conn, table)
    colnames = _cols()
    combos = list(itertools.combinations(colnames, 2))
    # first 5000 distinct combos: 2-col then 3-col
    combos3 = list(itertools.combinations(colnames, 3))
    order = list(combos[:len(colnames)]) + \
            [c for c in itertools.chain(combos, combos3)][: _N_STEPS[-1]]

    with conn.cursor() as cur:
        cur.execute(f"SET default_statistics_target={_TARGET}")

    # A0 = ANALYZE with 0 stats
    A0 = time_analyze(conn, table)
    EXPLAIN = time_explain(conn)
    MASK = time_mask(conn)

    # measure A(N) for increasing N (build cumulatively, one ANALYZE each)
    a_curve = {}
    built = 0
    names = []
    A0 = time_analyze(conn, table)  # re-measure cleanly after mask probe
    for N in _N_STEPS:
        # build more stats up to N
        with conn.cursor() as cur:
            for i in range(built, N):
                cols = order[i]
                cur.execute(f"CREATE STATISTICS w_{i} (mcv) ON "
                            f"{', '.join(cols)} FROM {table}")
                names.append(f"w_{i}")
        built = N
        an = time_analyze(conn, table)
        a_curve[str(N)] = {"A_masked_analyze": an,
                           "A0": A0, "explain": EXPLAIN, "mask": MASK}
        # protocol totals
        proto_a = N * (2 * A0 + EXPLAIN)
        proto_m = an + N * (EXPLAIN + MASK)
        a_curve[str(N)]["protocol_A_total"] = proto_a
        a_curve[str(N)]["protocol_M_total"] = proto_m
        a_curve[str(N)]["speedup"] = proto_a / proto_m if proto_m > 0 else None
        a_curve[str(N)]["catalog_overhead_frac"] = N * MASK / proto_m if proto_m > 0 else None
        print(f"  N={N:>5}  A(N)={an:7.2f}s  P-A={proto_a:9.0f}s  "
              f"P-M={proto_m:8.1f}s  speedup={proto_a/proto_m:7.1f}x")

    # cleanup
    with conn.cursor() as cur:
        for n_ in names:
            cur.execute(f"DROP STATISTICS IF EXISTS {n_}")
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    out["part_b"] = {"target": _TARGET, "A0": A0, "explain": EXPLAIN,
                     "mask": MASK, "N": a_curve}
    return a_curve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="stats")
    ap.add_argument("--out", default="results/p5_mask_scale.json")
    ap.add_argument("--fig", default="paper/figures/mask_scale")
    ap.add_argument("--skip-b", action="store_true")
    args = ap.parse_args(argv)

    cfg = DBConfig(host="localhost", port=5432, user="postgres", password="postgres",
                   dbname=args.db)
    out = {}
    with connect(cfg) as conn:
        conn.autocommit = True
        print("=== Part A: mask correctness (singleton q-errors on posts) ===")
        part_a(conn, out)
        if not args.skip_b:
            print("\n=== Part B: catalog-mask scalability (synthetic wide table) ===")
            part_b(conn, out)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
