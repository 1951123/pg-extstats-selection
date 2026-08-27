# Reproducibility Manifest

This manifest maps every key number / figure in the paper to its **data-source
artifact (primary artifact)** and its **reproduction script + command**, for use
by reviewers / re-implementers. All primary artifacts are tracked in git (see
the `!results/...` whitelist in `.gitignore`); they are not ignored with the
rest of `results/`.

> **Maintenance convention**: any measured number cited in the paper must have
> its source JSON listed under "Data source" below and whitelisted in git. When
> adding a figure/table, register its data source and reproduction command here
> at the same time.

---

## 1. Paper figures ← data source ← reproduction script

All three figures share the same primary data artifact —
`results/phase1_ceb_single_mask_6level.json` (the 632-query stats_CEB_single
full measurement at the six-level menu {10,25,50,100,1000,10000}).

> This file **replaces** the earlier `phase1_ceb_single_mask_full_multi.json`
> (three levels {100,1000,10000}). It is verified that the six-level file is a
> **strict superset**: 632/632 queries and 616/616 candidates match exactly, and
> the shared {100,1000,10000} levels agree bit-for-bit on all 1,848
> (candidate × level) entries. Running `analyze_sparsity.py` on the six-level
> file reproduces the same coverage/sparsity metrics as the multi file
> (coverage_median=0.999, frac_ge_0_9=0.9429, frac_ge_0_99=0.7714).

> The right-hand level-mix panel of `analyze_budget_metrics.py` shows the full
> **six-level** capacity menu {10,25,50,100,1000,10000} (the script's `LEVELS`
> constant). At minimal budgets the solver chooses low levels (e.g. at 5KB all
> six physical statistics land on L10/L50), rising to L1000/L10000 as the budget
> grows — a visualisation of the "lower capacity to save cost" decision axis,
> replacing the old three-level hardcode.

| Paper figure | Reproduction script | Command |
|---|---|---|
| `figures/sparsity_cdf` (one-stat-sufficiency CDF) | `scripts/analyze_sparsity.py` | `python scripts/analyze_sparsity.py --input results/phase1_ceb_single_mask_6level.json --out results/p4_sparsity.json --fig paper/figures/sparsity_cdf` |
| `figures/e2e_predict` (E2E predicted-vs-measured scatter) | `scripts/exp_e2e_scatter.py` | `python scripts/exp_e2e_scatter.py --input results/phase1_ceb_single_mask_6level.json --db stats --table posts --target 10000 --budgets 10000,40000,100000,250000 --out results/p4_e2e_scatter.json --fig paper/figures/e2e_predict` |
| `figures/budget_quality` (budget-allocation quality) | `scripts/analyze_budget_metrics.py` | `python scripts/analyze_budget_metrics.py --input results/phase1_ceb_single_mask_6level.json --budgets 5000,10000,20000,40000,100000,250000,500000,1000000 --out results/p3_metrics.json --fig paper/figures/budget_quality` |
| `figures/census_budget_quality` (CENSUS data-capped budget-quality, Fig:census_budget_quality) | `scripts/analyze_budget_metrics.py` | `python scripts/analyze_budget_metrics.py --input results/phase1_census_mcv_6level.json --budgets 10000,25000,50000,100000,250000,500000,1000000,2000000 --out results/p3_census_metrics.json --fig paper/figures/census_budget_quality` |
| `figures/upgrade_vs_add` (capacity upgrade vs. add) | `scripts/fig_upgrade_vs_add.py` | `python scripts/fig_upgrade_vs_add.py --phase1 results/phase1_ceb_single_mask_6level.json --out paper/figures/upgrade_vs_add` |

---

## 2. Paper numbers ← data source

| Number / result in the paper | Primary data-source artifact |
|---|---|
| One-stat sufficiency: at most one statistic per query (central finding) | `phase1_ceb_single_mask_6level.json` (CDF peak) |
| RQ1 capacity axis: 91.8% of candidates have target-independent q-error; 73.3% fully collapse (identical q-error and size) over {100,1000,10000} (six-level final) | `phase1_census_mcv_6level.json` (CENSUS 30,856 candidates; traced to `phase1_census_mcv_multi.json`) |
| RQ1 extended menu {10,25,50,100,1000,10000} (two-sided decision axis) | `phase1_ceb_single_mask_6level.json` (stats_CEB_single six-level) + `phase1_census_mcv_6level.json` (**CENSUS full six-level, merged from low/multi**) |
| CENSUS low-level capacity re-exposure (~29% sensitive / ~7% improve ≥20%, full six-level) | `phase1_census_mcv_6level.json` (**merged final**); traced to `phase1_census_mcv_multi.json` ({100,1000,10000}) + `phase1_census_mcv_low_t10000.json` ({10,25,50}); older `phase1_census_mcv_low.json` (single-col-50, historical) |
| §5.1 measurement cost model (ANALYZE base ~22s @t10000, T(N)=base+0.334N) | `probe_census_fine_capacity.log` / `probe_census_analyze_scale.py` output |
| §measure-runtime single-query six-level T_q(b) measurements (query.3 + st.144) — improved model (ANALYZE=b·Bf+cp·nL) fit on b=1,3/1,2, validated on held-out b=7/14/28 and b=4/7/10 to ≤3%; fixed-B0 model underestimates CENSUS by 69% and overestimates stats_CEB by 3-10× | `results/tq6_model_vs_measured.json` (CENSUS query.3, b=1,3,7,14,28) + `results/tq6_stats_ceb_model_vs_measured.json` (st.144, b=1,2,4,7,10); scripts `scripts/measure_tq6.py` (measured) + `scripts/validate_tq6.py` (train/test validation) |
| §measure-runtime measured speedup (Protocol-M optimal batch vs Protocol-A): CENSUS ≈283×, stats_CEB ≈36× (lower bounds) | `scripts/speedup_measured.py` (based on the tq6 measured JSON above) |
| stats_CEB mcv multivariate (multi) effect | `phase1_stats_ceb_mcv.json`, `phase1_stats_ceb_mcv_r3.json` (reproduction) |
| RQ3 tab:greedy2 (MILP vs G1/G2/G3, 5KB MILP=1.0270) | `results/p6_greedy_strong.json` (`--input phase1_ceb_single_mask_6level.json`, six-level recompute) |
| RQ3 tab:whathowmuch (what-only vs what+how-much) | `results/p2_what_vs_howmuch.json` (six-level recompute) |
| RQ3 tab:ablation (component ablation, 5KB full=1.0270) | `results/p5_ablation_5k.json`/`p5_ablation_100k.json` (six-level recompute) |
| tab:meanrob (mean-choice robustness, Jaccard/mean, six-level) | `results/p10_mean_robustness.json` (six-level recompute) |

### 2b. Menu-independent targeted measurements (fixed L10000 / join / E2E)

The experiments below do **not** read phase data from `phase1_*_6level.json`.
Instead they either **probe the live database directly** (`--db stats/census`,
fixed `--level 10000`) on the real tables (posts/climate), or belong to
**independent join workloads**. The properties they measure (single-candidate /
subset-joint q-error, planner OID order, Protocol-M correctness, E2E
deployment) are **mathematically independent of the number of capacity-menu
levels {10..10000}** — they use a single fixed target L10000, and therefore are
**not subject to the "3-level vs 6-level" issue** and need no rebuild after the
menu upgrade.

| Number / result in the paper | Independent data artifact | Measurement method |
|---|---|---|
| RQ2 sparsity: top-1 coverage median 1.000, 100% >0.9, 93% (k=2==k=1), 18,177-subset exhaustive optimum = single candidate | `exp_multi_full.json` (top-1/2/3 joint measurement, `levels={10000}`) + `p0_subset_exhaustive.json`/`p0_subset_exhaustive10.json` (`level=10000`) | direct `--db stats --level 10000` catalog-mask measurement; scripts `exp_subset_exhaustive.py`, `analyze_sparsity_corrected.py` |
| tab:switch (planner OID order: both==A-only to last) | `p0_planner_switch.json` (`level=10000`, posts) | direct `--db stats --level 10000` EXPLAIN probe; script `exp_planner_switch.py` |
| tab:p1 (E2E three stages: singleton/overlap/disjoint pred/ratio) | `p1_global_disjoint.json` (posts deployment) | real deployment + re-measure; script (global-disjoint MILP ablation) |
| fig:e2epredict (464 points: 234 disjoint mean ratio 1.0000 / 230 overlap mean 1.048) | `p4_e2e_scatter.json` (posts, `--target 10000`) | direct `--db stats --table posts --target 10000` budget-sweep deployment; script `exp_e2e_scatter.py` |
| §measure-correct (singleton vs masked bit-identical) | `p5_mask_scale.json` (`level=10000`) | direct `--db stats --level 10000`; script `exp_catalog_mask_scale.py` |
| RQ6 join negative control (median 12.3 / mean 33,853 / max 4,177,428, only 1.022× improvement) | `phase1_stats_ceb_mcv.json` (`bench=stats_ceb`, 146 queries) | independent join workload measurement |
| Table:bench job-light row (join negative control) | `phase1_job_light_full_cand5.json`, `phase1_job_light_cand27.json` | job-light join workload |
| RQ1 extreme-selectivity CENSUS cases (query.184 4,162→1.08 @L100, etc.) | `phase1_census_mcv_6level.json` (or `phase1_census_mask_top10.json`) | six-level phase (data-capped cases) |

### 2c. Sec.7 / Sec.8 deployment, maintenance-mismatch and fidelity tables

| Table / number in the paper | Primary data-source artifact | Reproduction script + command |
|---|---|---|
| §7 tab:rq5 (fidelity-ratio by deployment type on stats-CEB-single: singleton 1.0000/0/75, overlap 1.040/5/75, disjoint 0.9999/0/41) | `results/p_option_b_phaseB_full.json` (real-deployment ratios) + `results/p4_e2e_scatter.json` | `scripts/exp_option_b_phaseB.py`; `scripts/exp_e2e_scatter.py --input results/phase1_ceb_single_mask_6level.json --db stats --table posts --target 10000 --budgets 10000,40000,100000,250000 --out results/p4_e2e_scatter.json` |
| §7 tab:rq6 (Census end-to-end target 10⁴: overlap/disjoint/repaired gmean-max-tail at 40/100/250KB) | `results/p_census_e2e_memlight.json` | `scripts/exp_census_e2e_memlight.py --input results/phase1_census_mcv_6level.json --budgets 40000,100000,250000 --db census --target 10000 --max-iters 3 --out results/p_census_e2e_memlight.json` |
| §8 tab:disc-mismatch (storage vs ANALYZE maintenance mismatch, CENSUS + stats-CEB-single) | derived from `phase1_census_mcv_6level.json` / `phase1_ceb_single_mask_6level.json` via `analyze_storage_analyze_mismatch.py` (stdout; no separate JSON emitted) | `scripts/analyze_storage_analyze_mismatch.py --input results/phase1_census_mcv_6level.json --budgets 25000,40000,100000,250000,500000` (CENSUS); `--input results/phase1_ceb_single_mask_6level.json --budgets 10000,40000,100000,200000` (stats-CEB-single) |
| §8 tab:fidelity-lambda (fidelity ratio ρ vs expected sample count λ, rare combo 13/2.46M) | `results/transition_query184.json` (single candidate) + `results/transition_multicand.json` (multi-candidate) | `scripts/exp_transition_query184.py`; `scripts/exp_transition_multicand.py` (need live `census` DB) |
| §8 fidelity robust side (high-occurrence posts combo L25–L10000 zero gaps) | `results/p4_e2e_scatter.json` (singleton deployments) | `scripts/exp_e2e_scatter.py` (see above) |

---

## 3. Primary-artifact list (tracked in git)

| File | Size | Content / purpose |
|---|---|---|
| `results/phase1_ceb_single_mask_6level.json` | 860K | **Data source for the paper's figures** + full RQ1 capacity axis {10..10000}. Replaces `full_multi` |
| `results/phase1_census_mcv_multi.json` | ~21M | CENSUS {100,1000,10000} official baseline (30,856 candidates), single-col-10000. **≈21h total** (interrupted once: `phase1_census_mcv_multi.log` header `[resume] loaded 140 done queries`; its `53857s` covers only the resumed 328-query segment; the first 140-query segment ≈6.2h has no separate log, estimated at ~160s/query) |
| `results/phase1_census_mcv_low.json` | ~20M | CENSUS low-level probe (single-col-50, historical) |
| `results/phase1_census_mcv_low_t10000.json` | ~21M | **CENSUS official rerun complete** (single-col-10000 + {10,25,50}): full 468 queries, measured wall-clock 75{,}512s ≈21.0h (`phase1_census_low_t10000.log`, no interruption) |
| `results/phase1_census_mcv_6level.json` | ~41M | **CENSUS full six-level axis {10,25,50,100,1000,10000}** (468 queries, 30,856 candidates, all six levels fully measured) — merged from the low/multi files by `scripts/merge_phase1.py`, bit-consistent (qid order, candidate set identical, no missing levels). **Measured total wall-clock ≈42h** (low three levels 21h + high three levels ~21h). Downstream RQ1 / budget analyses use this as their source |
| `results/phase1_stats_ceb_mcv.json` | 452K | stats_CEB mcv measurement |
| `results/phase1_stats_ceb_mcv_r3.json` | 736K | stats_CEB mcv reproduction |
| `results/phase1_ceb_single_6level.log` | 42 lines | six-level run log (config/progress) |
| `results/phase1_census_low.log` | 51 lines | CENSUS low-level run log |
| `results/phase1_census_low_t10000.log` | small | **official rerun log** (in progress at commit; update after rerun completes) |
| `results/phase1_census_mcv_multi.log` | 178 lines | CENSUS official baseline log |
| `results/probe_census_fine_capacity.log` | 103 lines | fine-grained capacity-axis probe log |
| `results/p4_e2e_scatter.json` | 103K | §7 tab:rq5 + fig:e2epredict data source (464 real-deployment points on `posts`, target 10000) |
| `results/p_option_b_phaseB_full.json` | 13K | §7 tab:rq5 fidelity-ratio distribution by deployment type (Option-A/B on stats-CEB-single) |
| `results/p_census_e2e_memlight.json` | 11K | §7 tab:rq6 Census end-to-end (real gmean/P90/max q-error, tail, storage, deploy time per budget×method; memory-light run) |
| `results/transition_query184.json` | small | §8 tab:fidelity-lambda single-candidate λ-fidelity curve (CENSUS query.184 rare combo) |
| `results/transition_multicand.json` | small | §8 tab:fidelity-lambda multi-candidate confirmation curve |
| `results/transition_query184.png`, `results/transition_multicand.png` | — | plots of the above (optional) |

---

## 4. Regeneration commands for primary artifacts

> These are expensive measurements on a remote PostgreSQL 16.14 (several hours
> each), so the completed result JSONs are tracked **directly in git** rather
> than requiring reviewers to rerun them. The commands are provided for
> on-demand regeneration / cross-validation.

- **stats_CEB_single full-workload six-level** (632 queries, single-col-10000):
  ```bash
  env PYTHONPATH=src .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench stats_ceb_single --kind mcv --arities 2,3 \
    --target-levels 10,25,50,100,1000,10000 --single-col-target 10000 \
    --out results/phase1_ceb_single_mask_6level.json
  ```
  To extract a `full_multi` variant (the figure data source) add `--limit 632`
  / a specific set of qids.

- **CENSUS {100,1000,10000} official baseline**:
  ```bash
  env PYTHONPATH=src nohup .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench census --kind mcv --arities 2,3 \
    --target-levels 100,1000,10000 --single-col-target 10000 \
    --cands-per-batch 55 --checkpoint-every 20 \
    --out results/phase1_census_mcv_multi.json
  ```

- **CENSUS {10,25,50} official rerun** (single-col-10000, complete):
  ```bash
  env PYTHONPATH=src nohup .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench census --kind mcv --arities 2,3 \
    --target-levels 10,25,50 --single-col-target 10000 \
    --cands-per-batch 55 --checkpoint-every 20 --resume \
    --out results/phase1_census_mcv_low_t10000.json
  ```

- **CENSUS full six-level axis (merge low/multi, repeatable)**:
  ```bash
  .venv/bin/python -u scripts/merge_phase1.py \
    --low results/phase1_census_mcv_low_t10000.json \
    --high results/phase1_census_mcv_multi.json \
    --out results/phase1_census_mcv_6level.json
  ```
  Verified: 468/468 queries, identical candidate sets, all six levels present
  with no gaps.

- **Sec.7 Census end-to-end deployment (tab:rq6)** — memory-light; real
  PostgreSQL must be up with the `census` database loaded:
  ```bash
  source .venv/bin/activate
  python scripts/exp_census_e2e_memlight.py \
    --input results/phase1_census_mcv_6level.json \
    --budgets 40000,100000,250000 --db census --target 10000 --max-iters 3 \
    --out results/p_census_e2e_memlight.json
  ```

- **Sec.7 Option-A/B phase-B fidelity (tab:rq5)**:
  ```bash
  python scripts/exp_option_b_phaseB.py     # -> results/p_option_b_phaseB_full.json
  ```

- **Sec.8 storage-vs-ANALYZE mismatch (tab:disc-mismatch)** — pure derivation
  from phase-1 (no DB), output printed to stdout:
  ```bash
  python scripts/analyze_storage_analyze_mismatch.py \
    --input results/phase1_census_mcv_6level.json \
    --budgets 25000,40000,100000,250000,500000
  python scripts/analyze_storage_analyze_mismatch.py \
    --input results/phase1_ceb_single_mask_6level.json \
    --budgets 10000,40000,100000,200000
  ```

- **Sec.8 fidelity-vs-lambda curves (tab:fidelity-lambda)** — real `census` DB:
  ```bash
  python scripts/exp_transition_query184.py    # -> results/transition_query184.{json,png}
  python scripts/exp_transition_multicand.py   # -> results/transition_multicand.{json,png}
  ```


---

## 5. Git include / ignore policy

- `results/` is ignored by default; only the **primary artifacts** explicitly
  whitelisted with `!` in `.gitignore` are tracked.
- **Inclusion criteria**: directly depends on a paper figure/number, costs hours
  to rerun, and is measured data that cannot be regenerated from scripts alone.
- **Stays ignored**: `smoke_*`, temporary probes, regenerable intermediates,
  `phase1_*_t100/t1000/top10*.json`, etc.
- ⚠️ **gitignore pitfall (already hit)**: do not append `# comment` at the end
  of a negation (`!`) line — it breaks that rule's parsing and the file stays
  silently ignored. Put the comment on a preceding line instead. After editing,
  verify with `git check-ignore -v <file>`.

## 6. To-do / change log

- [x] **Reproducibility hardening for Sec.7/Sec.8 filled tables**: formalised
      the λ-fidelity experiments (`tmp_transition_query184.py` /
      `tmp_transition_multicand.py` → tracked `exp_transition_query184.py` /
      `exp_transition_multicand.py`), whitelisted their outputs and the §7
      end-to-end data (`p4_e2e_scatter.json`, `p_option_b_phaseB_full.json`,
      `p_census_e2e_memlight.json`) in `.gitignore`, and registered all four
      new tables (§7 tab:rq5/tab:rq6, §8 tab:disc-mismatch/tab:fidelity-lambda)
      in §2c Data-source + §3 artifact list + §4 regeneration commands.
      Obsolete `tmp_transition_*.py` removed.

- [x] **All RQ3 tables rebuilt from the six-level `phase1_ceb_single_mask_6level.json`**
      (audit found old three-level leftovers). A systematic recomputation found
      that the paper's four RQ3 tables still carried three-level (old full_multi)
      numbers at 5/10/20/40KB, contradicting the already-six-level
      `fig:budgetquality`. All were recomputed and updated to six-level:
      - `tab:greedy2` (MILP vs G1/G2/G3): 5KB MILP 1.0327→**1.0270**, best-greedy
        1.0798→**1.0531**, adv 0.047→**0.026**; whole-table + body text updated
      - `tab:whathowmuch`: what+how-much column 5/10/20/40KB updated
        (1.0327→1.0270 etc.); the what-only column was already correct
      - `tab:ablation`: 5KB row full/--sparse/--prune 1.0327→**1.0270**
        (100KB was already correct); body 1.033→1.057 changed to 1.027→1.057
      - `tab:meanrob`: Jaccard and means all updated (10KB 0.667→**0.818**,
        40KB 0.833→**0.286**, etc.); "identical" → "nearly identical
        (3rd-4th decimal)"
      Regenerated six-level result JSONs: `results/p6_greedy_strong.json`,
      `results/p2_what_vs_howmuch.json`,
      `results/p5_ablation_{5k,100k}.json`, `results/p10_mean_robustness.json`.
      The core conclusions are unchanged (MILP still beats greedy, the capacity
      axis is still key, mean choice is still robust); only the numbers were
      updated to six-level. Parts verified NOT to change: RQ2 coverage
      (independent L10000 joint experiment, not a capacity-menu issue),
      fig:budgetquality (already six-level), RQ1 / Table:bench / tab:measurement
      (already verified consistent).

- [x] **`phase1_census_mcv_low_t10000.json` official rerun complete** (468
      queries, measured 21.0h): tracked in git, and merged with
      `phase1_census_mcv_multi.json` into the full six-level axis
      `phase1_census_mcv_6level.json` (tracked). §2.2 / RQ1 capacity numbers
      updated to full six-level finals: {100,1000,10000} 91.8% unchanged /
      73.3% collapsed; {10,25,50} ~29% sensitive / ~7% improve ≥20%.

- [x] **`tab:measurement` rebuilt as a measured table**: shows CENSUS `query.3`
      (84 candidates, nL=504) and `stats_CEB_single` `st.144` (35 candidates,
      nL=210) at the six-level menu under different sub-batch counts b
      (s=nL/b), with **measured wall-clock** (total / ANALYZE / mask
      breakdown). Data from `results/tq6_model_vs_measured.json` and
      `results/tq6_stats_ceb_model_vs_measured.json`. Measured single-query
      optima: CENSUS b=1 (402.0s), stats_CEB b=4 (22.2s); the mask term falls
      ≈1/b as b grows (CENSUS 186.0→6.3s), the ANALYZE term grows roughly
      linearly in b — the two competing terms of Cor.~intraquery. This replaces
      the old "pure model curve" (B₀/s+cL+μL²s, s*=55, 24×) table, eliminating
      the confusion between model-estimated and measured times; the harness's
      b=55 is a workload-wide setting (distinct from the single-query optimum),
      and the body text now decouples the two.

- [x] **Workload-level wall-clock (42h/0.25h and b=55) removed from the paper
      (deliberate)**: the full six-level phase-1 measured CENSUS ≈42h (low 3
      levels 21h + high 3 levels ~21h), stats_CEB ≈0.25h, but **b=55 is a
      legacy harness granularity** (from the 3-level→6-level evolution + an old
      incorrect cost-model batch size), not optimal and hard to explain. The
      paper no longer reports workload-level wall-clock, only the **clean
      measured single-query optima** (CENSUS query.3 b=1=402s, stats_CEB st.144
      b=4=22.2s) and the measured speedups 283×/36×; the intro instead
      emphasizes that "the full six-level per-candidate q-error table is
      obtainable in a single pass" rather than 42h. This file keeps b=55 and
      the measured durations as a **technical run record** (for faithful
      reproduction).

- [x] **CENSUS actual wall-clock durations filled in** (see item above; already
      removed from the paper, kept here only as a reproduction record).

- [ ] **Systematic under-estimation of the model vs measurement, confirmed on
      the full six-level measurement**: the cost model estimated L=6 CENSUS
      ≈22-33h, but the measured full six-level is ≈42h (low 3 levels 21h + high
      3 levels ~21h) — a systematic under-estimate of ~1.3-1.9× (same cause as
      before: the real ANALYZE base > 22s, plus implicit per-sub-batch fixed
      CREATE/DROP/restore/connection overhead not captured). **Conclusion**: the
      42h measurement roughly agrees with the earlier "÷0.58 calibration ≈
      38-40h" estimate. The paper no longer reports the 42h figure (see the
      completed item above), but the under-estimation conclusion stands — it is
      the key motivation driving the improved per-dataset Bf model, validated in
      §measure-runtime with the single-query T_q(b) measurements (CENSUS /
      stats_CEB held-out ≤3%).

- [ ] **Protocol-A comparison is ANALYZE-only and systematically under-estimates
      Protocol-A** (strengthening the Protocol-M motivation): in
      `exp_catalog_mask_scale.py`, Protocol-A is defined as
      $N(2B_0 + \text{EXPLAIN})$, **ANALYZE only, ignoring the per-candidate
      CREATE/DROP STATISTICS DDL overhead**. For CENSUS at six levels this is
      N=30,856×6=185,136 physical statistics = 185,136 CREATE + 185,136 DROP
      unaccounted. The ANALYZE-only Protocol-A baseline is ≈2{,}263h (30,856×6
      physical stats × 2×22s), so the measured six-level sub-batched ≈42h gives
      a speedup of $2263/42 \approx 54\times$ — still a lower bound; counting the
      per-candidate DDL only makes Protocol-A slower and Protocol-M's advantage
      larger (asymmetric under-estimate: it lowers only Protocol-A, not
      Protocol-M). **Suggestion**: run a small non-conflicting probe measuring
      the single CREATE/DROP STATISTICS overhead to quantify this increment and
      decide whether to include it in the paper (currently a qualitative
      argument; the paper uses "over an order of magnitude" without hard-coding
      the DDL-increment number).

- [x] **`merge_phase1.py` implemented (Method B)**: merges CENSUS low
      ({10,25,50}) and multi ({100,1000,10000}) into the full six-level axis
      `phase1_census_mcv_6level.json` (tracked); verified 468/468 queries,
      identical candidate sets, six levels with no gaps.

- [x] **stats_CEB_single runtime narrative corrected to measured**: the original
      "workload-wide optimal (~0.12h vs ~1.29h per-query)" was a three-level
      model extrapolation and did not match the actual run (sub-batched,
      b=55). Now: both workloads use sub-batched Protocol-M (b=55);
      stats_CEB_single six-level measured ≈0.25h (632 queries, log
      phase1_ceb_single_6level.log: 893s); CENSUS ≈42h. Workload-wide is kept
      only as the closed-form theoretical optimal scope, no longer claimed as
      "actually used". The `≈188×` (stats_CEB sub-batched vs naive
      per-candidate), checked against the measured 0.25h vs 45h, gives ≈182×,
      consistent.
