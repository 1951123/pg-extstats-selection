# Extended-Statistics Selection: Problem Model & Findings

This document formalises the *selection* problem for PostgreSQL extended
statistics (`CREATE STATISTICS`) and records the empirical findings of this
project. It explains how the **capacity level** (each statistic's
`statistics_target` / storage) acts as a second decision dimension alongside
*which* statistics to build, and how the existing MILP in
`src/extstats/optimize.py` already supports it.

---

## 1. The cardinality-estimation selection problem

We are given a workload of queries `Q = {q_1, ..., q_n}` over one or more base
tables. The planner estimates each `q_i`'s result cardinality; its accuracy is
measured by the **q-error**:

$$
\operatorname{qerr}(\text{est}, \text{act}) = \frac{\max(\text{est}, \text{act})}
{\min(\text{est}, \text{act})} \ge 1.
$$

PostgreSQL's default estimate uses per-column histograms/MCVs and assumes
**column independence**. When columns are correlated this estimate can be far
off. **Extended statistics** (`CREATE STATISTICS ... (mcv|ndistinct|dependencies)`)
capture multi-column dependence to improve the estimate.

> **Scope restriction (confirmed at source level, PG 16):** extended
> statistics are only used by the planner for *restriction* (WHERE) clauses
> on a **single relation** — `statext_clauselist_selectivity` is gated by
> `find_single_rel_for_clauses`, and a join clause references two relations so
> it returns NULL. `eqjoinsel()` never consults `pg_statistic_ext`. Therefore
> **extended statistics cannot improve JOIN estimation** (official docs
> `create_statistics.sgml` also states this). Their value is limited to
> single-table selection predicates.

### Selection objective

For query `i`, let `T_i` be the set of statistics built that it uses. We want
to minimise the workload's mean q-error:

$$
\min_{ \{y_s\}, \{x_{is}\} } \quad \frac{1}{n}\sum_i \operatorname{qerr}_i\big(T_i\big)
$$

subject to a **storage budget** on the physical statistics actually created.

---

## 1.1 Benchmarks & candidate distribution

The project ships four query workloads (parsers in `src/extstats/parsers/`):

| benchmark | structure | tables | queries | DB |
|-----------|-----------|--------|---------|----|
| **Census** | single table | 1 (`climate`, 69 cols, ~2.45 M rows) | 468 | `census` |
| **JOB / IMDB** | multi-table joins | many | 113 | `imdb` |
| **stats_CEB** (join) | multi-table joins | 8 (StackExchange) | 146 | `stats` |
| **stats_CEB_single** | single-table sub-plans of the same schema | 8 (each used alone) | 632 | `stats` |

The two single-table workloads (Census and stats_CEB_single) are where extended
statistics can act (§1: only single-table selection predicates are fixable); the
join workloads serve as the negative control.

**Candidate distribution** (2- and 3-column combos from each query's selection
predicates; `generate_candidates_per_query`):

| benchmark | total candidates (per-query) | queries with any | global dedup | per-query candidates (min / med / max) |
|-----------|------------------------------|------------------|--------------|----------------------------------------|
| Census | 30856 | 467 / 468 | 19245 | 1 / **56** / 455 |
| JOB | 50 | 37 / 113 | 7 | 1 / 1 / 4 |
| stats_CEB | 609 | 108 / 146 | 79 | 1 / 4 / 39 |
| stats_CEB_single | 616 | 180 / 632 | 79 | 1 / 1 / 35 |

Census per-query candidate counts are right-skewed (median 56, 75th pct 84, 90th
pct 165, max 455) — a single Census query can have hundreds of candidate
combinations, which is why wide-table measurement required the catalog-mask
protocol (§5.4). In contrast, JOB has almost no candidates (median 1, global
dedup only 7) because its predicates are mostly single-column or join-based;
stats_CEB_single has many queries but few candidates each (median 1), and its
few candidates are highly shared (global dedup 79).

---

## 2. Two decision dimensions

A "statistic" is not atomic; each has a **capacity level** controlled by its
`statistics_target`:

| `statistics_target` | MCV entries | typical on-disk size | repair quality |
|---------------------|-------------|----------------------|----------------|
| 100  | ≤ 100 | small (~0.3–2 KB) | loses 3-column correlation |
| 1000 | ≤ 1000 | medium | good (captures 3-col) |
| 10000| ≤ 10000 | large | exact / deterministic |

Hence the full decision space is:

$$
\underbrace{\text{which statistics}}_{(table,\,columns)} \;\times\;
\underbrace{\text{which capacity level}}_{target/level}
$$

A physical statistic is identified by
$\big(\text{table}, \text{columns}, \text{level}\big)$ with a storage cost
$c_{s}$ (bytes) and a per-query effective q-error $e_{is}$.

> **PG mechanism (verified):** PostgreSQL supports *per-object* targets via
> `ALTER STATISTICS ... SET STATISTICS N` (stored in
> `pg_statistic_ext.stxstattarget`), and per-column via
> `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS`. So different statistics
> **can** be built at different capacities. However, **ANALYZE samples a
> number of rows equal to the maximum target across all statistics** (see
> `analyze.c` `targrows`), so a single high-target statistic forces a full
> table scan. The per-object target therefore controls **each statistic's
> precision and storage**, but not the ANALYZE cost difference in isolation.

---

## 3. Multiplicative approximation (multi-select) and the sparse special case

For the general **multi-select** model (a query may use several statistics),
empirically measuring every subset is infeasible, so we approximate the joint
effect multiplicatively in log space:

$$
\log e_i(T_i) \;\approx\; \log e^0_i + \sum_{s\in T_i} \log\!\Big(\frac{e_{is}}{e^0_i}\Big),
$$

where $e^0_i$ is the baseline (no extra stats) q-error and $e_{is}$ the q-error
with only statistic $s$ active. This is exact when each statistic's effect is
independent — which holds when the chosen combinations share **no columns** — so
we forbid column-overlapping selections within a query.

> **Caveat (empirically important in this project):** on Census and single-table
> stats_CEB, the planner's improvement comes from **precisely chosen single (or
> few) statistics**, not from many overlapping ones. Building *many* candidate
> statistics jointly can make the planner pick a sub-optimal combination and
> *hurt* the estimate (the "joint interference" effect).

**Sparse special case (this project's final model).** Empirical results (below)
showed that per query a **single dominant candidate** already captures the
relevant correlation, and adding a second non-overlapping candidate gives almost
no extra benefit. We therefore specialise to **at most one statistic per query**
(`per_query_cap = 1`). In that case the objective is **exactly linear** — no
multiplicative approximation / log transform is needed:

$$
e_i(T_i) = e^0_i - \sum_{s} \underbrace{(e^0_i - e_{is})}_{\Delta_{is} \ge 0}\cdot x_{is}
\ \longmapsto\ \min \sum_i \Big(e^0_i - \textstyle\sum_s \Delta_{is} x_{is}\Big),
\qquad \sum_s x_{is}\le 1.
$$

---

## 4. MILP (existing in `src/extstats/optimize.py`)

Variables (binary):

- $y_s$: create physical statistic $s = (\text{table}, \text{cols}, \text{level})$,
- $x_{is}$: query $i$ selects statistic $s$.

Objective (constant baseline + improvements):

$$
\min \;\; \sum_{i,s} x_{is}\cdot \log\!\Big(\frac{e_{is}}{e^0_i}\Big),
\qquad \log(\cdot)\le 0.
$$

Constraints:

1. Storage budget: $\displaystyle \sum_s c_s\, y_s \le C$.
2. A statistic must exist to be used: $x_{is} \le y_s$ (share `y_s` across queries → pay storage once).
3. Overlap-free within a query: $x_{i a}+x_{i b} \le 1$ for column-overlapping `a,b`.
4. **Column-combo capacity exclusivity**: at most one level per `(table, columns)` — $\sum_{\text{levels } l} y_{(\text{cols}, l)} \le 1$. A physical object has a single `statistics_target`, so different capacity levels of the *same* combination are mutually exclusive (they can't all be created).
5. **Sparse per-query cap** (optional, `per_query_cap=1`): $\sum_s x_{is} \le 1$ — each query picks at most one candidate (stronger than overlap-free, so constraint 3 is skipped). With `per_query_cap=1` the objective is linear (§3).

> This already treats **each capacity level as a distinct physical statistic**
> (`Option.level`, `PhysicalStat.level`, cost from `size_bytes`). So "select a
> statistic AND choose its capacity" is expressed by picking *which level* of a
> `(table, columns)` combination appears in the solution — exactly the
> per-object `statistics_target` knobs of §2 (subject to constraint 4).

---

## 5. Empirical findings (PG 16, t=10000 deterministic and t=1000 default)

### 5.1 Single-table selection predicates are *fixable* (~to 1)

- **Census** (single 69-col table): every high-q-error query we examined has a
  single well-chosen 3-column MCV bringing q-error from hundreds (e.g. 4162) to
  ~1.0. Verified by an independent real `ANALYZE` (q184: 54107 → 14 rows).
- **stats_CEB single-table sub-plans** (same StackExchange schema, no joins):
  top queries (base ~4.6) go to ~1.0–1.06 with a 3-col MCV.
- **But** the multi-table (join) stats_CEB workload is *not* fixable by
  extended statistics: its error is dominated by JOIN estimation, which
  extended statistics cannot touch (§1).

### 5.2 "Best single candidate" per query, shared across the workload

- Building each query's best candidate **all at once** on Census did **not**
  interfere: every query kept its ~1.0 repair ("preserved=YES", n=10).
- Best candidates across different queries used **disjoint 3-col combos**; high
  sharing degree on stats_CEB single-table (many queries benefit from the same
  `(AnswerCount, FavoriteCount, ViewCount)` etc.).

### 5.5 One dominant candidate is (nearly) optimal; multi-candidate adds nothing

Measured with `exp_multi_candidate.py` (stats_CEB single-table top-10, all on
`posts`): for each query we greedily picked non-overlapping candidates ranked by
single q-error and measured the JOINT q-error keeping the top-1/2/3 picks:

- **k=2 and k=3 joint q-error ≈ k=1** (within 0.02–0.03); no query materially
  improved by adding a 2nd/3rd non-overlapping candidate.
- **Why:** single-table selection error is driven by **one dominant correlated
  column cluster** (here `AnswerCount/FavoriteCount/ViewCount/PostTypeId`). One
  best 3-col MCV captures it; other non-overlapping combos have no independent
  second cluster to contribute.
- The `t=1000` ceiling (~2.0) is a **capacity-precision** limit (MCV entries),
  not a coverage limit — raising `target` to 10000 fixes it; adding candidates
  does not.

This justifies `per_query_cap=1`: **sparse (one dominant candidate per query) is
optimal/near-optimal; joint is harmful (planner interference from non-dominant
stats); the "middle" (several non-overlapping) adds almost nothing.**

### 5.6 Sparse ILP under a storage budget: global, non-greedy

`scripts/solve_sparse_ilp.py` + `scripts/compare_greedy_vs_ilp.py` on
stats_CEB single-table top-10 (per-query dominant candidates):

| budget | stats | used | mean q-error | improve |
|--------|-------|------|--------------|---------|
| 20 KB  | 5 | 19.6 KB | 1.864 | +56.7% |
| 40 KB  | 5 | 28.6 KB | 1.821 | +57.6% |
| 100 KB | 7 | 96.5 KB | 1.632 | +62.0% |
| 2 MB   | 5 | 360 KB | 1.021  | +76.3% |

- The ILP is **global (not per-query greedy)**: every statistic is an option to
  every relevant query, statistics are shared across queries (built once), and
  the budget is allocated to the highest-value (stat, capacity) choices.
- Under a tight budget the ILP **degrades capacity levels** (L10000 → L100/1000)
  rather than dropping coverage, giving a smooth budget–quality curve; greedy
  would insist on each query's personal best regardless of budget.

### 5.3 Capacity-level trade-off (measured on Census `climate`, 69 cols)

| `target` | ANALYZE (0 ext) | ANALYZE (1000 ext) | repair (best, single-table) |
|----------|-----------------|---------------------|-----------------------------|
| 100  | 0.28 s | 2.03 s | ~2.1–2.3 |
| 1000 | 3.97 s | ~31 s  | ~1.2–2.1 |
| 10000| 21.85 s| 264 s  | ~1.0 |

- `target=1000` is the maintenance/repair sweet spot: captures 3-column
  correlation (unlike 100) at ~1/5.5–1/8.5 the ANALYZE cost of 10000.
- The per-level storage distribution (small for many combos, large for a few
  high-cardinality combos) is exactly the `c_s` used in the ILP budget.

### 5.4 Measurement feasibility on wide tables

`Protocol-A` (per-candidate CREATE+ANALYZE+EXPLAIN) is infeasible on wide
tables (Census ANALYZE ~20s each). The **catalog-mask protocol (Protocol-M)**
builds all candidates in ONE ANALYZE and measures each candidate independently
by NULL-ing the others' `pg_statistic_ext_data` payload (planner ignores
NULL-payload stats). Restore is done PG-internally via a temp backup table +
`UPDATE ... FROM` (the `pg_mcv_list` type has no bytea cast). This gives
per-candidate, per-level q-errors + sizes needed by the ILP at ~60x lower cost.

---

## 6. Open questions

1. **Optimal level selection** — with `per_query_cap=1` the ILP chooses the
   (stat, level) jointly; a monotonicity constraint (higher level ⇒ lower
   q-error, higher cost) is not required for correctness but could speed solve
   time. Is free choice fine in practice? (Current evidence: yes.)
2. **Storage model accuracy** — `size_bytes` from `pg_column_size(stxdmcv)` is
   per-level; can we model the cost of a combo as a smooth function of level and
   share/promote levels across queries cheaply?
3. **End-to-end validation** — the real-PG verification of a selected sparse
   stat set (does the ILP's predicted mean q-error match the measured one on
   the actual database?) is the remaining closed-loop step.
4. **Scalability / larger workloads** — how do ILP solve time and phase-1 cost
   scale to the full 468-query Census or 632-query stats_CEB-single, and does
   the "one dominant candidate" finding hold at that scale?
