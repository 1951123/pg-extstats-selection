# Supplementary Material

"One-Stat Sufficiency Guided Budgeted Selection of PostgreSQL Extended Statistics"

This supplement holds extended content that was condensed out of the 12-page
main paper for space. Each section below is referenced from the main text;
numbers are exact and reproduce from the same data artifacts registered in
[`docs/reproducibility.md`](../docs/reproducibility.md).

---

## A. Sampling determinism and the fixed large target (full detail)

The main text (§5.4) states that we fix the per-column `statistics_target` at
10000; this section records the full measurement behind that choice.

Because `ANALYZE` is sampling-based we verified that repeated runs do not
drift. Across **six fresh `ANALYZE`s** at three targets, determinism is a
**step function of `statistics_target`**:

- **target 100**: the estimate shows 1–2% run-to-run variation — enough to make
  *relative* q-error comparisons unreliable.
- **target 1000 / 10000**: exactly deterministic (coefficient of variation 0).

Target also sets *accuracy*. The single-MCV estimate of `st.144` rises from
**4,405 (target 1000)** to **11,894 (target 10000)**, the correct value being
≈12,391. At low target, MCVs over high-cardinality combinations can even be
built **empty** (a `NULL` payload) and contribute nothing.

We therefore fix the per-column `statistics_target` at 10000 rather than
PostgreSQL's default of 100, for three reasons:

1. **Determinism** — target 100 leaves a 1–2% run-to-run drift that would make
   relative q-error comparisons unreliable; target ≥1000 is exactly
   reproducible (CV 0).
2. **Completeness** — at low targets high-cardinality statistics can be built
   empty (a `NULL` payload), risking a degraded baseline.
3. **Attribution** — with a maximally precise single-column baseline, any
   improvement from an extended statistic is a *pure* effect of the correlation
   it captures, not of patching a coarse per-column estimate.

All per-candidate q-errors are measured against this *same* baseline, so
capacity is the only axis that changes between levels. This makes the
single-column `ANALYZE` expensive on a wide table (a CENSUS `ANALYZE` at target
10000 costs ≈22 s) — precisely what forces the catalog-mask protocol (Protocol-M).

> Reproduction: `probe_census_analyze_scale.py`, `probe_census_fine_capacity.log`,
> `results/phase1_ceb_single_mask_6level.json`.

---

## B. Coverage by design (deleted from Discussion, §8.1 in an earlier draft)

The two evaluated workloads are not an arbitrary sample; they are the two
endpoints of the `capacity` axis this paper is about. On that axis:

- **stats_CEB_single's** high-cardinality, target-capped statistics sit at the
  **ceiling** — raising the target is what buys accuracy.
- **CENSUS's** low-cardinality, data-capped columns sit at the **floor** — their
  MCV lists fill at a small target, so the useful move is often to *lower*
  capacity.

Every real deployment of extended statistics lies between these two extremes;
establishing the method at both ends therefore bounds its behaviour over the
entire capacity axis. The controllable many-cluster synthetic sweeps
(§6.2, Table 4 `tab:sweep`) probe the one orthogonal dimension where the sparse
assumption itself breaks. Coverage is by design, not by convenience.

---

## C. Extended open questions (full list)

The main paper keeps three open questions; the full set we considered is below.

1. **Level monotonicity** — enforcing that a higher capacity never yields a
   worse q-error could prune options and speed the solve.
2. **Maintenance-aware selection** — jointly modeling `ANALYZE` cost (a function
   of the *max* target) would replace the storage-only budget with an
   end-to-end maintenance budget.
3. **Upgrade vs. add** — our data show a smooth capacity-only curve on
   stats_CEB_single and a large tail on CENSUS, but a principled
   "upgrade-vs-add" decision boundary (e.g. a marginal-value heuristic: quality
   gained per storage unit) is open.
4. **When does one-stat sufficiency fail?** — identifying workloads with
   genuinely multiple independent correlated clusters (the regime needing the
   multi-select model) is the most important open question.
5. **Limits** — we consider MCV statistics on single-table workloads; joins need
   other techniques, there is a real limit on statistics per relation, and our
   2-/3-column candidate family understates achievable quality on harder CENSUS
   queries, where 4-/5-column candidates help — extending candidate generation
   to higher arity is future work.
6. **Deployment-mode selection** — our end-to-end comparison (Table 6
   `tab:optionB`) shows Option-A (global disjointness) and Option-B (repair)
   carve a real trade-off; a principled rule for choosing a mode per deployment
   (e.g. from the workload's quality-versus-reproducibility requirements or a
   budget-dependent crossover point) is open.

---

## D. Full sub-batch measurement sweep (Table 2 `tab:measurement`)

Per-query phase-1 cost across sub-batch sizes, on one candidate-dense query per
workload at the full six-level menu (n·L = 504 for CENSUS `query.3`; n·L = 210
for stats_CEB_single `st.144`). `b` is the number of sub-batches and `s = n·L/b`
the sub-batch *size* (candidates mounted per ANALYZE+mask build); times are real
wall-clock. The mask column tracks ≈1/b (e.g. CENSUS 186.0 → 6.3 s as `b` grows
1 → 28), while the ANALYZE column grows roughly linearly in `b` — the two
competing terms of `T_q(b)`.

Measured per-query optimum: `b=1` / `s=504` on CENSUS (402.0 s) and
`b=4` / `s=52` on stats_CEB_single (22.2 s). The sub-batched Protocol-M time
there, versus Protocol-A's per-candidate cost, gives a *measured* speedup of
≈283× / ≈36×.

| CENSUS `query.3` (n·L=504) | | | | | stats_CEB_single `st.144` (n·L=210) | | | | |
|---|---|---|---|---|---|---|---|---|---|
| sub-batch `b` | size `s` | total (s) | ANALYZE (s) | mask (s) | sub-batch `b` | size `s` | total (s) | ANALYZE (s) | mask (s) |
| **1** | **504** | **402.0** | 210.3 | 186.0 | 1 | 210 | 47.9 | 4.0 | 37.5 |
| 3 | 168 | 496.9 | 435.9 | 57.9 | 2 | 105 | 27.7 | 5.9 | 18.3 |
| 7 | 72 | 938.2 | 910.3 | 25.4 | **4** | **52** | **22.2** | 10.1 | 8.9 |
| 14 | 36 | 1646.0 | 1631.4 | 12.3 | 7 | 30 | 23.3 | 15.9 | 5.1 |
| 28 | 18 | 3398.9 | 3390.4 | 6.3 | 10 | 21 | 27.3 | 22.9 | 3.4 |

Real `ANALYZE`-at-`N` anchors on CENSUS: 1,000 stats → 420 s; 3,000 → 1,013 s;
6,000 → 2,003 s; 10,000 → 3,507 s.

> Reproduction: `scripts/measure_tq6.py`, `scripts/validate_tq6.py`,
> `results/tq6_model_vs_measured.json`, `results/tq6_stats_ceb_model_vs_measured.json`.

---

## E. Protocol-M safety and implementation notes

Protocol-M measures a candidate by masking the catalog so that only the desired
statistic's payload remains, instead of running a per-candidate `ANALYZE`. This
is safe because masking only toggles the *payloads* already produced by a single
`ANALYZE`; it never writes new statistics, only rewrites existing payloads, and
restores them afterward. The measured per-operation costs are linear in the
table's payload count:

- mask ≈ 0.0008 s per payload,
- restore ≈ 0.00003 s per payload,
- `EXPLAIN` constant ≈ 0.002 s.

So one masked measurement costs ε(N) ≈ 0.0008·N s — masking is *not* constant:
it grows with the number of statistics mounted during that measurement, a
property of the measurement *granularity*, not of Protocol-M per se. The cost
model and its optimal-batching derivation appear in §5.5 of the main text
(Eqs. 1–2 and Prop. 1).
