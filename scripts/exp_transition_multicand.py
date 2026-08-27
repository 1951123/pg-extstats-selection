#!/usr/bin/env python3
"""Reproducible fidelity-vs-lambda transition across multiple candidates.

Task4b (formalised from the exploratory tmp_transition_multicand.py): confirm
that the divergence-to-convergence fidelity curve of
exp_transition_query184.py is mechanistic -- i.e. a property of the
combination-to-sampling path -- and not a single-candidate artifact, by
repeating the lambda sweep across several distinct OPT candidates of the same
rare query.184 on CENSUS `climate`.

Primary artifact produced:
    results/transition_multicand.json   (+ results/transition_multicand.png)

Usage:
    source .venv/bin/activate
    python scripts/exp_transition_multicand.py
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

N = 2_458_285
SQL = ("SELECT COUNT(*) FROM climate WHERE dDepart>=0 AND dDepart<=2 "
       "AND iDisabl1=0 AND iEnglish=0 AND iImmigr=0 AND iLooking=0 "
       "AND iMay75880=0 AND iRelat2=0 AND dRpincome>=2 AND dRpincome<=4 "
       "AND iRspouse=1 AND dTravtime>=0 AND dTravtime<=4")
ACTUAL = 13
# OPT candidate -> known phase1 Mask q-error (constant for L>=50).
CANDS = {
    "climate(iDisabl1,iRspouse)":
        (["iDisabl1", "iRspouse"], 1.3076923076923077),
    "climate(dTravtime,iDisabl1,iRspouse)":
        (["dTravtime", "iDisabl1", "iRspouse"], 1.0769230769230769),
    "climate(iDisabl1,iEnglish,iRspouse)":
        (["iDisabl1", "iEnglish", "iRspouse"], 1.6153846153846154),
}
DEFAULT_LEVELS = [30, 100, 200, 400, 800, 1600, 3200, 6400, 10000]


def sample_rows(L: int) -> int:
    return max(300 * 100, 300 * L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dbname", default=DEFAULT_DB["census"])
    ap.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)),
                    help="comma-separated target levels to sweep")
    ap.add_argument("--out", default="results/transition_multicand.json",
                    help="output JSON path (data)")
    ap.add_argument("--fig", default="results/transition_multicand.png",
                    help="output PNG path (optional plot); empty to skip")
    args = ap.parse_args(argv)

    LEVELS = [int(x) for x in args.levels.split(",") if x.strip()]

    conn = psycopg.connect(host="localhost", port=5432, user="postgres",
                           password="postgres", dbname=args.dbname,
                           autocommit=True)
    out = {}
    for cand, (cols, mask_qerr) in CANDS.items():
        name = stat_name(CandidateSet("climate", tuple(cols)), "mcv", "t4_")
        with conn.cursor() as cur:
            cur.execute(f"DROP STATISTICS IF EXISTS {name}")
        pts = []
        print(f"\n=== {cand}  mask_qerr={mask_qerr:.3f} ===")
        print(f"{'L':>6}{'lambda':>8}{'sample':>10}{'TrueQ':>9}{'ratio':>8}")
        for L in LEVELS:
            sample = sample_rows(L)
            lam = (ACTUAL / N) * sample
            # TRUE-L deployment: create the stat, set its target, ANALYZE.
            _reset_target(conn)
            with conn.cursor() as cur:
                cur.execute(f"CREATE STATISTICS {name} (mcv) "
                            f"ON {', '.join(cols)} FROM climate")
                cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {L}")
            with conn.cursor() as cur:
                cur.execute("ANALYZE climate")
            _reset_target(conn)
            res = estimate_count_query(conn, SQL, actual=ACTUAL)
            ratio = res.qerror / mask_qerr if res.qerror else None
            pts.append({"L": L, "lambda": lam, "sample": sample,
                        "true_qerr": res.qerror, "mask_qerr": mask_qerr,
                        "ratio": ratio, "estimate": res.estimate})
            print(f"{L:>6}{lam:>8.2f}{sample:>10}{res.qerror:>9.2f}"
                  f"{'' if ratio is None else round(ratio, 2):>8}")
            with conn.cursor() as cur:
                cur.execute(f"DROP STATISTICS IF EXISTS {name}")
            _reset_target(conn)
            with conn.cursor() as cur:
                cur.execute("ANALYZE climate")
        out[cand] = pts

    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nwrote {args.out}")

    if args.fig:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            plt.figure(figsize=(6.5, 4.5))
            for cand, pts in out.items():
                xs = [p["lambda"] for p in pts]
                ys = [p["ratio"] if p["ratio"] is not None
                      else float("nan") for p in pts]
                plt.plot(xs, ys, "o-",
                         label=cand.split("(")[1].rstrip(")"))
            plt.axhline(1.0, color="gray", ls="--")
            plt.axvline(1.0, color="red", ls=":")
            plt.xscale("log")
            plt.yscale("log")
            plt.xlabel("expected sample count lambda (= p_combo * sample_rows(L))")
            plt.ylabel("qerr_True / qerr_Mask")
            plt.title("Protocol-M fidelity transition across query.184 OPT candidates")
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
