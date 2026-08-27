#!/usr/bin/env python3
"""Reproducible fidelity vs. expected-sample-count (lambda) transition curve.

Task4 (formalised from the exploratory tmp_transition_query184.py): for a single
rare-driver candidate on CENSUS `climate`, sweep the statistics target L as a
continuous knob so that the expected sample count

    lambda(L) = p_combo * sample_rows(L),   sample_rows = max(300*100, 300*L)

sweeps from ~0.1 to ~16, and measure the TRUE-L q-error at each L. Because the
Catalog-Mask oracle q-error is constant (=MASK_QERR) for L>=50 on this candidate,
the ratio  rho = qerr_True / qerr_Mask  over L gives the divergence-to-
convergence fidelity curve, used to support the discussion in the paper's
Sec. 8 (Deployment Fidelity, tab:fidelity-lambda).

Primary artifact produced:
    results/transition_query184.json   (+ results/transition_query184.png)

Requires a live PostgreSQL 16 `census` database (see src/extstats/config.py
DEFAULT_DB) with the table loaded and NO extended statistics left behind (the
script drops its own stats after every point).

Usage:
    source .venv/bin/activate
    python scripts/exp_transition_query184.py
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from extstats.config import DEFAULT_DB
from extstats.candidates import CandidateSet
from extstats.measure import stat_name, _set_target, _reset_target
from extstats.estimate import estimate_count_query
from extstats.parsers import parse_census_dir

# Target candidate + query on CENSUS `climate` (the rare driver of query.184).
CAND = "climate(iDisabl1,iLooking,iRspouse)"
QID = "query.184"
TABLE = "climate"
COLS = ["iDisabl1", "iLooking", "iRspouse"]
N = 2_458_285  # table row count
SQL = ("SELECT COUNT(*) FROM climate WHERE dDepart>=0 AND dDepart<=2 "
       "AND iDisabl1=0 AND iEnglish=0 AND iImmigr=0 AND iLooking=0 "
       "AND iMay75880=0 AND iRelat2=0 AND dRpincome>=2 AND dRpincome<=4 "
       "AND iRspouse=1 AND dTravtime>=0 AND dTravtime<=4")
ACTUAL = 13
# phase1 Mask@L50..L10000 q-error for this candidate (large-sample oracle).
MASK_QERR = 1.0769230769230769

# L grid -> lambda sweeps ~0.1 .. 16 on this candidate.
DEFAULT_LEVELS = [30, 60, 100, 200, 400, 800, 1600, 3200, 6400, 10000]


def sample_rows(L: int) -> int:
    return max(300 * 100, 300 * L)


def _make_stat_name(prefix: str) -> str:
    return stat_name(CandidateSet(TABLE, tuple(COLS)), "mcv", prefix)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbname", default=DEFAULT_DB["census"])
    ap.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)),
                    help="comma-separated target levels to sweep")
    ap.add_argument("--out", default="results/transition_query184.json",
                    help="output JSON path (data) ")
    ap.add_argument("--fig", default="results/transition_query184.png",
                    help="output PNG path (optional plot); empty to skip")
    args = ap.parse_args(argv)

    LEVELS = [int(x) for x in args.levels.split(",") if x.strip()]

    conn = psycopg.connect(host="localhost", port=5432, user="postgres",
                           password="postgres", dbname=args.dbname,
                           autocommit=True)
    name = _make_stat_name("trans_")
    with conn.cursor() as cur:
        cur.execute(f"DROP STATISTICS IF EXISTS {name}")

    rows = []
    for L in LEVELS:
        sample = sample_rows(L)
        lam = (ACTUAL / N) * sample
        # TRUE-L deployment: create the stat, set its target, ANALYZE.
        _reset_target(conn)
        with conn.cursor() as cur:
            cur.execute(f"CREATE STATISTICS {name} (mcv) "
                        f"ON {', '.join(COLS)} FROM {TABLE}")
            cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {L}")
        with conn.cursor() as cur:
            cur.execute(f"ANALYZE {TABLE}")
        _reset_target(conn)
        res = estimate_count_query(conn, SQL, actual=ACTUAL)
        ratio = res.qerror / MASK_QERR if res.qerror else None
        rows.append({"L": L, "sample": sample, "lambda": lam,
                     "true_qerr": res.qerror, "mask_qerr": MASK_QERR,
                     "ratio": ratio, "estimate": res.estimate, "actual": ACTUAL})
        print(f"L={L:>5} lam={lam:>6.2f} sample={sample:>9} "
              f"TrueQ={res.qerror:>9.2f} "
              f"ratio={'' if ratio is None else round(ratio, 2)}")
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
        _reset_target(conn)
        with conn.cursor() as cur:
            cur.execute(f"ANALYZE {TABLE}")

    Path(args.out).write_text(json.dumps(
        {"candidate": CAND, "qid": QID, "N": N, "actual": ACTUAL,
         "mask_qerr": MASK_QERR, "points": rows}, indent=2))
    print(f"\nwrote {args.out}")

    # ASCII transition summary (log-ish lambda buckets).
    print("\n=== ratio vs lambda (transition) ===")
    print(f"{'lambda':>8}{'L':>6}{'ratio':>8}{'TrueQ':>9}")
    for r in rows:
        print(f"{r['lambda']:>8.2f}{r['L']:>6}"
              f"{'' if r['ratio'] is None else round(r['ratio'], 2):>8}"
              f"{r['true_qerr']:>9.2f}")

    if args.fig:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            xs = [r["lambda"] for r in rows]
            ys = [r["ratio"] if r["ratio"] is not None else float("nan")
                  for r in rows]
            plt.figure(figsize=(6, 4))
            plt.plot(xs, ys, "o-")
            plt.axhline(1.0, color="gray", ls="--", label="ratio=1 (faithful)")
            plt.axvline(1.0, color="red", ls=":", label="lambda=1")
            plt.xscale("log")
            plt.xlabel("expected sample count lambda = p_combo * sample_rows(L)")
            plt.ylabel("qerr_True / qerr_Mask")
            plt.title("Protocol-M fidelity vs expected sample count (query.184)")
            plt.legend()
            plt.tight_layout()
            plt.savefig(args.fig, dpi=150)
            print(f"saved {args.fig}")
        except Exception as e:  # pragma: no cover - plotting is optional
            print("plot skipped:", e)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
