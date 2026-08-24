"""Compute the Protocol-M vs Protocol-A speedup from MEASURED single-query data.

AVOIDS the model-accuracy problem entirely: the Protocol-M (sub-batched) term
uses the REAL measured optimal-batch total time, not any model fit. Protocol-A
is the per-candidate cycle (each cell CREATE->ANALYZE->EXPLAIN->DROP->ANALYZE,
i.e. 2 ANALYZEs + EXPLAIN + DDL), whose per-candidate ANALYZE is bounded by the
table's ANALYZE base Bf, measured from the b=1,2/3 fit. Using Bf keeps
Protocol-A (hence the speedup) a LOWER BOUND on the true advantage.

Primary x-axis is the sub-batch SIZE s (candidates per batch) = nL/b, matching
Tab.~:measurement and the query-invariant optimum s*=sqrt(B0/(mu L^2)). The
sweep measured batch COUNTS b; we present both (s=nL/b).

Inputs (results of scripts/measure_tq6.py):
  CENSUS query.3  : results/tq6_model_vs_measured.json          (b=1,3,7,14,28)
  stats_CEB st.144: results/tq6_stats_ceb_model_vs_measured.json (b=1,2,4,7,10)
"""
import argparse, json

EXPLAIN = 0.002
DDL = 0.002  # rough per CREATE/DROP cost (negligible vs ANALYZE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", default="results/tq6_model_vs_measured.json")
    ap.add_argument("--statsceb", default="results/tq6_stats_ceb_model_vs_measured.json")
    args = ap.parse_args()

    def load(path):
        d = json.load(open(path))
        rows = d["rows"]
        return d, rows

    for name, path in [("CENSUS query.3", args.census),
                       ("stats_CEB st.144", args.statsceb)]:
        d, rows = load(path)
        nL = d["nL"]
        # Bf = per-batch ANALYZE base from two lowest-b points (an=b*Bf+cp*nL)
        # so Bf = (an_hi - an_lo) / (b_hi - b_lo)
        rr = sorted(rows, key=lambda r: r["b"])
        lo = rr[0]
        hi = next(r for r in rr if r["b"] in (2, 3))  # b=2 (or b=3)
        Bf = (hi["analyze"] - lo["analyze"]) / (hi["b"] - lo["b"])
        # measured optimum by TOTAL time
        best = min(rows, key=lambda r: r["measured_total_s"])
        print(f"\n=== {name} (nL={nL}, Bf≈{Bf:.1f}s) ===")
        print("  b |  s=nL/b | measured_total | analyze | mask")
        for r in sorted(rows, key=lambda x: x["b"]):
            print(f" {r['b']:>2} | {nL/r['b']:6.1f} | {r['measured_total_s']:9.1f} | "
                  f"{r['analyze']:7.1f} | {r['mask']:6.1f}")
        s_best = nL / best["b"]
        protoM = best["measured_total_s"]
        protoA = nL * (2 * Bf + EXPLAIN + DDL)
        print(f"  Protocol-M MEASURED optimal: b={best['b']} (s={s_best:.0f} cands/batch) -> {protoM:.1f}s (REAL)")
        print(f"  Protocol-A (per-cand ANALYZE=Bf={Bf:.1f}s): {nL} cells x 2 = {protoA:.0f}s = {protoA/3600:.2f}h")
        print(f"  SPEEDUP (Protocol-A / Protocol-M-MEASURED): {protoA/protoM:.0f}x  (lower bound)")
        # query-invariant s* for reference
        import math
        print(f"  (reference) s* = sqrt(B0/(mu L^2)) with B0=22,mu=0.0008,L=6 = {math.sqrt(22/(0.0008*36)):.0f} cands/batch")


if __name__ == "__main__":
    main()
