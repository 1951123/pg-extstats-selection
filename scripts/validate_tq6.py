"""Validate the refined ANALYZE-term cost model against measured T_q(b).

Two models are compared against the measured per-query measurement time across
all sub-batch counts b for query.3 at L=6 (nL=504 cells):

  1. ORIGINAL paper model (Cor. intra-query):
        T(b) = b*B0 + c*nL + (nL)^2*mu/b + eps*nL
     with B0=22, c=0.35, mu=0.0008, eps=0.002.
     -> term b*B0 treats each sub-batch ANALYZE as the EMPTY-table base.

  2. REFINED model (fitted): the ANALYZE term is b*Bf + cp*nL, where Bf is the
     fixed per-batch ANALYZE base WITH the batch's stats mounted and cp is the
     per-payload ANALYZE increment. Fit Bf, cp on the b=1,3 measured ANALYZE
     times, then TEST on the held-out b=7,14,28.

Used as the "train/test" sanity check (user-directed): fit on one subset,
validate on the rest, so we do not blindly report a shaky model's estimate vs
Protocol-A. Report model/measured ratios.

Usage:
  .venv/bin/python scripts/validate_tq6.py \
      --json results/tq6_model_vs_measured.json
"""
import argparse, json, math

B0, c, mu, eps = 22.0, 0.35, 0.0008, 0.002


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="results/tq6_model_vs_measured.json")
    args = ap.parse_args()

    d = json.load(open(args.json))
    nL = d["nL"]
    rows = d["rows"]
    print(f"query {d['qid']}: nL={nL}, levels={d['levels']}, rows={len(rows)}")

    # ---- fit refined ANALYZE params on b=1,3 (measured analyze times) ----
    fit_b = [r for r in rows if r["b"] in (1, 3)]
    assert len(fit_b) == 2, f"need b=1,3 for fit, got {[r['b'] for r in fit_b]}"
    (a1,) = [r for r in fit_b if r["b"] == 1]
    (a3,) = [r for r in fit_b if r["b"] == 3]
    # a1: 1 batch -> an1 = Bf + cp*nL ; a3: 3 batches -> an3 = 3*Bf + cp*nL
    an1, an3 = a1["analyze"], a3["analyze"]
    cp = (an1 - an3 / 3) / (nL - nL / 3)
    Bf = an1 - cp * nL
    print(f"FIT (b=1,3): Bf={Bf:.2f}s  cp={cp:.4f}s/payload")
    print(f"  b=1 an model={Bf+cp*nL:.1f} vs meas {an1:.1f}   "
          f"b=3 an model={3*Bf+cp*nL:.1f} vs meas {an3:.1f}")

    def orig(b):
        return b * B0 + c * nL + (nL) ** 2 * mu / b + eps * nL

    def refined(b):
        return b * Bf + cp * nL + (nL) ** 2 * mu / b + eps * nL

    print("\n   b | measured |  orig-model | orig/m | refined-model | ref/m |  role")
    for r in rows:
        b, meas = r["b"], r["measured_total_s"]
        o, rf = orig(b), refined(b)
        role = "fit" if b in (1, 3) else "test"
        print(f"{b:>3} | {meas:8.1f} | {o:10.1f} | {o/meas:5.3f} | "
              f"{rf:13.1f} | {rf/meas:5.3f} | {role}")

    # aggregate test-set accuracy (held-out b=7,14,28)
    test = [r for r in rows if r["b"] not in (1, 3)]
    if test:
        om = [orig(r["b"]) / r["measured_total_s"] for r in test]
        rm = [refined(r["b"]) / r["measured_total_s"] for r in test]
        print(f"\nTEST (b={[r['b'] for r in test]}): "
              f"orig model/meas mean={sum(om)/len(om):.3f}  "
              f"refined model/meas mean={sum(rm)/len(rm):.3f}")
        print("  (ratio 1.000 = exact; <1 = model underestimates, >1 = overestimates)")


if __name__ == "__main__":
    main()
