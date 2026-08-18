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

## 3. Multiplicative approximation for joint selection

Empirically measuring every subset of statistics is infeasible. We approximate
the joint effect multiplicatively in log space:

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
> *hurt* the estimate (the "joint interference" effect). This means the
> independence/multiplicative model is most reliable when selections are sparse
> and non-overlapping — consistent with the overlap-free constraint.

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

> This already treats **each capacity level as a distinct physical statistic**
> (`Option.level`, `PhysicalStat.level`, cost from `size_bytes`). So "select a
> statistic AND choose its capacity" is expressed by picking *which level* of a
> `(table, columns)` combination appears in the solution — exactly the
> per-object `statistics_target` knobs of §2.

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

1. **Optimal level selection** — the ILP already chooses *which level* of a
   combo; does it need a monotonicity constraint (higher level ⇒ lower q-error,
   higher cost) or is free choice fine?
2. **Storage model accuracy** — `size_bytes` from `pg_column_size(stxdmcv)` is
   per-level; can we model the cost of a combo as a function of level and
   share/promote levels across queries cheaply?
3. **Joint-interference vs. sparsity** — under which workloads does building
   multiple stats interfere (multi-table) vs. help (single-table)? The empirical
   evidence suggests single-table prefers sparse exact selection.
