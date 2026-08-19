# Paper Outline (v2) — research-story framing

**Title**: *Choosing What and How Much: Budgeted Selection of PostgreSQL
Extended Statistics*

> 本提纲按 **research story**（Observation → Model → Optimization → Validation →
> Limitation）组织，而非研究日志顺序。所有数值均来自本仓库实际实验
> （见 `docs/extended-statistics-selection.md` 与 `results/`）。

---

## 论文 Spine（一段话 + genre 定位）

> **This is an empirical systems/optimization paper that reframes
> extended-statistics selection as budgeted what-and-how-much allocation,
> discovers an unexpectedly sparse structure that makes the optimization
> tractable, and builds a scalable measurement/deployment pipeline to validate
> it on PostgreSQL.**

### Hierarchy：A → B → C → D（ChatGPT 建议，已采纳）
| 代号 | 角色 | 一句话 | Novelty | 风险 |
|------|------|--------|---------|------|
| **A** | **thesis（论文回答什么）** | selection × capacity = budget allocation | ★★★★（"which" 已被 Chaudhuri&Narasayya 2000 做过；"how much" 轴 + upgrade-vs-add 是新） | 需精准措辞 |
| **B** | **core empirical insight（为什么可解）** | 看似多选，实测几乎全 sparse：一个 dominant statistic 捕获全部可获 improvement | ★★★★★（最漂亮） | 证据只到"workload 上观察到"，不足以说 inherently；当 headline 要承担解释 burden |
| **C** | **enabling system（怎么做出来）** | catalog-mask 测量让 massive candidate×capacity 空间可测；MILP 只是易的部分（57k stats <5s） | ★★★☆（MILP 本身不新，catalog-mask 是真贡献） | 别把 MILP 当卖点 |
| **D** | **validation surprise** | 重叠统计 planner interference；global-disjoint 恢复精确可预测（ratio 1.000） | ★★★★ | – |

**关键定位原则**：
- A 的 "what" 部分不新 → headline 必须强调 **"how much" 轴 / resource allocation**
  （upgrade 已有统计 vs add 新统计），而非 "automatic selection"。
- B 是 insight 不是 assumption → Intro 里先展示"看似多选、实测稀疏"的 surprise，
  再引出稀疏 formulation（不是"我们假设每查询一个"）。
- C 的 novelty 在 **measurement + PG deployment semantics**，不在 MILP。
- **MILP 求解便宜（<5s），measurement 才是 pipeline bottleneck** —— 保留此观察。

---

## 三个核心 Contribution（压缩后）

1. **Selection × Capacity（重新定义问题）**
   传统工作问 "Which column combinations?"；本文问
   "**Which (table, columns, statistics_target) objects under a storage budget?**"
   实验证据：capacity 不是"小一点"——t100 丢失 3 列相关、t1000/t10000 质量不同；
   紧预算下最优策略是 **capacity 降档 (10000→1000→100)** 而非删统计
   = **graceful degradation**。命名：*capacity-aware extended statistics
   selection* / *joint selection and capacity allocation*。

2. **Sparse empirical structural finding**
   $|T_i|\le1$ 基本足够：
   - stats_CEB_single：k=1≈k=2≈k=3（差 <0.03）
   - CENSUS（69 列 / 每查询数百候选）：q184 4162→1.08，q382 378→1.12，k=1,2,3
     完全相同（L1000 与 L10000 均如此）
   - 表述成结构发现：*单表相关谓词的基数误差常由一个主导列簇驱动，因此单个
     精心选择的多元 MCV 捕获查询几乎全部可获改进*。
   - 连接到 planner：加非重叠统计无益；并发重叠统计引起 interference
     = **sparse benefit + dense interference**。

3. **（可测量的）单统计有效性 vs（不可忽略的）并发重叠交互**
   诚实框架，而非 "our model has an error"：
   > *Per-statistic effectiveness is accurately measurable, but simultaneous
   > presence of overlapping statistics introduces a second-order planner
   > interaction not captured by the sparse optimization model.*
   证据：A alone → 1.042（测量模型正确）；A+B+C+D+E → 2.13（planner 换用
   次优重叠统计）。
   **修复**：Option A 全局列不相交（$y_a+y_b\le1$ 若 $cols(a)\cap cols(b)\neq\emptyset$）
   为主模型；Option B 求解后轻量 EXPLAIN + 修复为 fallback。

---

## 章节结构（正式论文顺序，非研究日志）

### 1. Introduction
- 提出问题：extended stats 改善相关谓词基数估计，但物理表示有可调 **capacity**
  (`statistics_target`)；已有 selection 只考虑 "which" 忽略 "how much"。
- 三个 empirical observations：capacity matters / sparse structure / planner interference。
- 我们：capacity-aware budgeted MILP + 利用稀疏结构获得可扩展优化问题。
- Contribution bullets（见上，5 条，main.tex 已写）。

### 2. Background & Related Work
- **A. Cardinality estimation**：histogram/MCV (Ioannidis)、query feedback
  (Chen&Roussopoulos)、learned CE (Kipf 2019)。我们是"部署"而非"估计"。
- **B. Automatic statistics management**：Chaudhuri&Narasayya 2000 做了
  "which" 层 → 明确 A 的 novelty 在 "how much" 轴/upgrade-vs-add，
  = selection → budget allocation。
- **C. Physical design / index selection**：Chaudhuri 1997/2005 budget-constrained
  structure selection (MILP/greedy)。我们的差异 = capacity 轴 + 发现式稀疏线性。
- **D. PostgreSQL extended statistics**：MCV/ndistinct/deps、per-object
  stxstattarget、单关系限制、ANALYZE 采样耦合；我们的模型 substrate。
- **E. Workload-driven tuning**：自动调优/knob/index advisor 共享框架；我们
  的差异 = 优化"存储预算下的预测质量(mean qerror)"而非 latency，且端到端验证。
- 每类都回答：优化什么/测什么/为何解不了 what×how much（novelty 落地）。

### 3. Problem Formulation
- 记号：$s=(R,C,\ell)$，$c_s=\text{storage}(s)$，$e_{is}$=仅 s 时查询 i 的 q-error，
  $e_i^0$=基线，$\Delta_{is}=e_i^0-e_{is}$。
- sparse model：
  $$
  \max\sum_{i,s}\Delta_{is}x_{is}\quad
  \text{s.t.}\ \sum_s c_s y_s\le C,\ x_{is}\le y_s,\ \sum_s x_{is}\le1,\
  \sum_{\ell} y_{(R,C,\ell)}\le1
  $$
- **Proposition（main.tex 已有 3.1）**：per_query_cap=1 → 目标精确线性（无
  interaction 项）。表述：log-space 表述在实验验证的每查询单统计下不必要
  = 实验推动 formulation simplification。
- **Scope 澄清**：优化的是 **storage budget**（persistent size / quality）；
  ANALYZE 维护成本单独处理（PG 采样耦合到 relation 上的 max target）。

### 4. Optimization: Sparse MILP with Capacity Levels
- 变量/约束（同前）；剪枝 (skip_worse_than_baseline) + 全局去重。
- **RQ4 支撑数据**：全 Census no-prune 57,735 / 92,568 / 150,303 → ~4.7s；
  prune 34,280 / 46,392 / 80,672 → ~4.3s；均 HiGHS Optimal。

### 5. Measurement Infrastructure（已扩为完整 systems section）
- 拆成 5.1 Naive(Protocol-A 每候选 2 ANALYZE, 宽表不可行)
  / 5.2 Catalog-Mask(Protocol-M: 一次 ANALYZE 全构建 + 掩码隔离 + UPDATE..FROM 恢复)
  / 5.3 Correctness(掩码测量==单统计真实部署, 用 §7 st.144 1.04 vs 1.042 提前验证)
  / 5.4 Runtime 表(tab:measurement: naive vs Protocol-M 随 N 9x→132x, 基于实测
  ANALYZE 缩放 0→21.9s, 1000→264s)。
- **核心：measurement 才是 pipeline bottleneck，MILP 是便宜的部分**。
  这是 C(enabler) 的 systems 定位。

### 6. Empirical Findings（按 RQ 结构）
- **RQ1 Capacity matters?** → Yes。t100/t1000/t10000 质量 ~2.1-2.3 / ~1.2-2.1 /
  ~1.0。target 直接决定精度，不只是存储。
- **RQ2 One stat per query?** → Yes。k=1≈2≈3（stats_CEB + CENSUS 宽表）。
  **已升级为分布级证据(P4)**：全 632 查询中 base>1.2 的 35 条，top-1 coverage
  median=0.999，94.3% ≥0.9，77.1% ≥0.99；base 2-10 的 32 条中 97% 单候选即达
  下界(≤1.05)。图 fig/sparsity_cdf.pdf。B 不再是 anecdotal。
- **RQ3 MILP vs budgeted greedy?** → 诚实表述 + **已补预算内 greedy 对照(P6)**：
  充足预算下 greedy 并集 == ILP（同 5 stats, mean 1.021）；**紧预算下 MILP
  一致胜**（5KB: MILP 1.033 vs greedy 1.080；100KB: 1.016 vs 1.024），差距随
  预算越紧越大。ILP 独特价值=capacity 重分配而非列选择。表 tab:greedy。
- **RQ4 Scale?** → 无瓶颈。MILP <5s；真正瓶颈是 phase-1 测量。
- **RQ5 Prediction survives real PG?** → 拆解：
  - RQ5a 单候选可预测？Yes（单统计对照 st.144→1.042 等）。
  - RQ5b 并发重叠统计独立？No（planner cross-talk：1.32 vs 1.02）。
- **RQ6 Help where it cannot?** → No meaningful benefit（JOB 负对照）= sanity check。

### 7. End-to-End Validation（已扩为三阶段 causal story）
- 7.1 **Stage 1 Singleton (RQ5a)**：单统计部署==掩码预测（st.144 1.04 vs 1.042），
  验证 Protocol-M correctness。
- 7.2 **Stage 2 Co-installed (RQ5b)**：5 个重叠 MCV 一起部署 → 预测 1.021 vs
  实测 1.32（ratio 1.29）；被 3 条引退（st.144 1.04→2.13）。同一统计 single
  vs co-installed 对比 → 精确定位 planner 串扰是唯一失效点。
- 7.3 **Stage 3 Global-disjoint (P1)**：不相交约束 → ratio 1.000 恢复精确可预测，
  代价可量化（2.00 vs 1.02）。
- 7.4 **Causal Takeaway**：三阶段链 (i)单统计可预测 (ii)重叠并发是唯一失效点
  (iii)不相交修复。不否定方法而精确界定适用边界 + Option B fallback。
- 表 tab:p1 升级为"deployment × 三阶段"汇总表。

### 8. Discussion & Open Questions
- 为什么单一主导候选；storage vs maintenance scope；
  全局不相交 (A) vs post-check (B)；局限。

### 9. Conclusion

---

## 待补实验（按优先级）

### ~~P1 — global-disjoint MILP ablation~~ ✅ 已完成
实现：`solve_ilp(..., global_disjoint=True)` 增加全局重叠约束
（$y_a+y_b\le1$ for overlapping cols），`solve_sparse_ilp.py --global-disjoint`。
**实测结果**（stats_CEB_single top-10，`results/p1_global_disjoint.json`）：

| budget | model | stats | predicted | E2E | ratio |
|--------|-------|------:|----------:|----:|------:|
| 2 MB  | overlap allowed | 5 | 1.021 | 1.320 | 1.294 |
| 2 MB  | **global-disjoint** | 2 | 2.004 | **2.004** | **1.000** |
| 100 KB| overlap allowed | 7 | 1.632 | 1.933 | 1.184 |
| 100 KB| **global-disjoint** | 2 | 2.004 | **2.004** | **1.000** |

> **关键发现（闭环 Contribution 3）**：global-disjoint 解在两个预算点都
> **精确可预测（ratio 1.000）**，因为统计两两列不相交 → planner 无串扰、
> §3 独立性假设结构性成立。代价是**可量化的名义质量损失**（预测 2.00 vs 1.02）
> ——不相交约束迫使求解器放弃"重叠但高价值"的主导候选。
> 这精确定位了两件事：(i) 每候选测量模型正确（disjoint ratio=1.000）；
> (ii) overlap 模型的残余误差**全部**是并发重叠统计的 planner 干扰
> （ratio 1.18–1.29）。**Option A 给出可信赖部署，代价可量化**——这正是 §7 要呈现的 trade-off。

### ~~P2 — 632-query stats_CEB_single 全集~~ ✅ 已完成
完整 phase-1 掩码测量（79 组合 × 3 档 × 8 表，共 126s）+
8 个预算求解（`results/p2_capacity_allocation.json`）：

| budget | stats | mean | L100 | L1000 | L10000 |
|--------|------:|-----:|-----:|------:|-------:|
| 5KB  | 3 | 1.033 | 1 | 1 | 1 |
| 10KB | 5 | 1.026 | 3 | 1 | 1 |
| 40KB | 11 | 1.019 | 8 | 2 | 1 |
| 100KB| 17 | 1.016 | 8 | 7 | 2 |
| 250KB| 14 | 1.004 | 4 | 6 | 4 |
| 1MB  | 42 | 1.001 | 12 | 15 | 15 |

> **验证 capacity allocation**：高容量统计数随预算增长（L10000 1→2→4→7→15；
> L1000 1→…→15）——solver 升级已选组合的 target，而非只加覆盖。
> 教科书案例 `posts(AnswerCount,ViewCount)`：L100→L1000→L10000 随预算升档。
> 8 个预算间 25 次容量档变化与增删组合并存 → capacity 是活跃的第二决策维度。
> **诚实 caveat**：全 workload mean 改善小（1.033→1.001, +5.7-8.6%），因
> 632 查询中位数基线=1.0、仅 34 个 >1.5。stats_CEB_single 是 *capacity-
> allocation* 验证载体；大尾修复是 Census 的角色（§5.1）。这精确界定了主张。

### ~~P3 — 预算曲线 + level 分布 + 多指标~~ ✅ 已完成
`scripts/analyze_budget_metrics.py`（8 budgets）+ 图 `paper/figures/budget_quality.pdf`
（左: 预算-质量多指标曲线 mean/median/P90, 右: L100/L1000/L10000 堆叠分布）。
`results/p3_metrics_full.json` + `results/p3_tail_census.json`。

**stats_CEB_single 全 632 查询多指标**（基线 mean=1.095, median=1.000, max=4.7）：
| 预算 | mean | median | P90 | max | L100/L1000/L10000 |
|-----|-----:|-------:|----:|----:|-------------------|
| 5KB | 1.033 | 1.000 | 1.003 | 2.2 | 1/1/1 |
| 100KB | 1.016 | 1.000 | 1.002 | 2.2 | 8/7/2 |
| 250KB | 1.004 | 1.000 | 1.002 | 1.4 | 4/6/4 |
| 1MB | 1.001 | 1.000 | 1.000 | 1.1 | 12/15/15 |
> 印证 reviewer 直觉——**mean 由离群值主导**（median 恒=1.000）；ILP 的真实作用是
> **tail repair**（max 4.7→1.1）。

**Census top-10 huge-tail**（`p3_tail_census.json`）：baseline mean/median/P90/**max**
= 1071.5/332.5/2542.9/**4162.1** → selected 1.10/1.09/1.18/**1.41**。
> objective 用 mean，但 evaluation 报告全分布；headline 是 tail 崩塌（4162→1.4）。

### ~~P4 — Top-1 sparsity 分布级证据~~ ✅ 已完成
`scripts/analyze_sparsity.py` + 图 `paper/figures/sparsity_cdf.pdf`；
`results/p4_sparsity.json`。
全 632 查询，base>1.2 的 35 条可修查询：top-1 coverage **median=0.999**，
94.3% ≥0.9，77.1% ≥0.99；base 2-10 的 32 条中 **97% 单候选即达下界(≤1.05)**。
> B 从 anecdotal（q184/q382）升级为**分布级结构发现**。注意：coverage 公式
> (base-best)/(base-1) 只在 base>1.2 有意义（近 1 基线分母爆炸，需如实限定）。

### ~~P5 — Component ablation~~ ✅ 已完成
`scripts/ablate_components.py`（`results/p5_ablation.json`）。
| 配置 | 5KB | 100KB |
|------|-----|-------|
| baseline | 1.095 | 1.095 |
| full | 1.0327 | 1.0156 |
| 去 capacity(仅L10000) | **1.0569** | 1.0233 |
| 去 sparse cap(multi-select) | 1.0327 | 1.0154 |
| 去 pruning | 1.0327 | 1.0156 (2085→1178 vars) |
> **capacity 轴是必要组件**（紧预算下最重要）；sparse cap 与 pruning 零质量损失。
> 实证"每个设计选择都是 justified 的"。

### ~~P6 — Budgeted greedy vs MILP~~ ✅ 已完成
`scripts/compare_greedy_budget.py`（预算内 greedy，improvement/byte 排序）+ 表 tab:greedy。
| 预算 | MILP | budgeted greedy | 差距 |
|------|------|----------------|------|
| 5KB | 1.0327 | 1.0798 | 4.7pts |
| 20KB | 1.0237 | 1.0553 | 3.2pts |
| 100KB | 1.0156 | 1.0238 | 0.8pts |
> 充足预算 greedy 并集 == ILP（同 5 stats 1.021）；**紧预算下 MILP 一致胜，
> 差距随预算越紧越大**——实证"capacity 重分配而非列选择"是 MILP 的价值。
> 替代旧 TODO：不再只是"标注待补"，而是有真实数据。

---

## Reviewer Challenges（提前准备）

1. **为什么直接用 mean q-error？** → 补 median/P90/max；objective 仍用 mean，
   evaluation 给全分布。
2. **为什么不是 exhaustive search？** → Census 19245 去重组合 × 3 档、per-query
   med 56 / max 455，子集穷举不可行；我们选择 single-stat 测量 + 稀疏结构假设 +
   MILP。明确写："We do not attempt to measure arbitrary subsets. Instead, we
   exploit the empirically observed one-dominant-statistic structure and measure
   only singleton interventions."。
3. **capacity 是否影响 ANALYZE 成本而不仅是存储？** → 精确表述：target 控制每
   对象精度与存储，但 ANALYZE 采样由 relation 上最大 target 决定；故 budget 是
   **storage budget** 而非 maintenance-cost budget（已在 §3 scope 声明）。

---

## 论文图（建议顺序）

- **Fig 1（总览图）**：Workload → Candidate generation → (table,columns,level)
  → single-stat measurement → Sparse MILP (column choice | capacity choice)
  → Selected stats → ANALYZE → E2E EXPLAIN。旁注 "**Selection × Capacity**"。
- **Fig 2（RQ2）**：k=1/2/3 联合 q-error 几乎重合（stats_CEB_single + CENSUS）。
- **Fig 3（RQ3）**：budget(log) vs mean q-error；叠加 level 分布。
- **Fig 4（RQ4）**：solve time / problem size 尺度。
- **Fig 5（RQ5）**：predicted vs measured（标出重叠干扰退化点）。

---

## 数据源映射（RQ 绑定）

| 论文位置 | 数据源文件 | 输出 |
|---------|-----------|------|
| §1.1 基准表 | parsers + bench 概览 | 表 |
| §2/RQ1 容量矩阵 | docs §5.3（实测） | 表 |
| §4/RQ4 规模表 | bench_census_solve_scale.py 输出 | 表 |
| RQ2 k=1/2/3 | exp_multi_top10.json + /tmp/census_multicand.json | 图 |
| RQ3 预算曲线 | phase1_ceb_single_mask_top10_multi.json + solve_sparse 输出 | 图 |
| RQ5 E2E | validate_e2e.py 输出 | 表 |
| RQ6 JOB | 候选分布统计 | 表 |

## 写作顺序建议
RQ2/RQ3 图先行（最能"卖"）→ §3/§4 形式化 → §2 背景 → §5 基建 → §7 E2E →
§1 引言（最后写）→ §8/§9。
