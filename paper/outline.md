# Working Title

**Budget-aware Sparse Selection of Extended Statistics: When One Statistic per Query Is Enough**

> 本提纲为 VLDB 投稿初稿的结构规划。正文尚未撰写；每节列出「要点」「须填的真实
> 数据/表格(对应 results/ 文件)」「写作提示」。所有数值均来自本仓库实际实验。

---

## 0. 论文定位一览

- **问题**：PostgreSQL `CREATE STATISTICS` 在给定存储预算下，从海量 (表, 列组合, 容量档)
  候选中，选一个极小的高价值子集，使工作负载平均 q-error 最小。
- **两个决策维度**：选哪些统计 × 每个统计的容量档(`statistics_target`)。
- **核心主张**：对单表选择负载，**每查询一个主导候选**即（近）最优 —— 问题可精确退化为
  线性稀疏选择，用 ILP 在秒级求解；join 负载是负对照（扩展统计无法修 join）。
- **主要贡献**（草拟）：
  1. **形式化**：把扩展统计选择建模为预算受限的稀疏选择问题，显式纳入容量档维度
     （§3, §4）。
  2. **测量基建**：目录掩码协议(Protocol-M)让宽表上逐候选、逐容量档测量可行（~60x 降本）（§5.4）。
  3. **实证规律**：单一主导候选即近优；联合重叠统计诱导 planner 串扰（§5.5, §5.9）。
  4. **求解可行性**：全 Census 57,735 物理统计/150K 变量的 MILP 在 HiGHS 下 <5s 最优求解（§5.7）。
  5. **端到端验证**：每候选预测正确；重叠并发统计是残余风险，需列不重叠选择（§5.9）。

---

## 1. Introduction（引言）

**目标**：3 段式，从一个反直觉现象切入。

- **开头 hook**：现代 DB 靠多列相关性基估计。`CREATE STATISTICS` 存在 30+ 年，但"该建哪些、
  建多大"长期靠 DBA 手调。本文揭示：对单表负载，答案惊人地稀疏——每查询一个 3 列 MCV 往往
  就能把 q-error 从几千压到 ~1。
- **两个决策维度的困难**：
  - 组合爆炸：Census 单条查询可有多达 455 个候选列组合（全局 19,245 个）。
  - 容量档：同一 (列组合) 可在 t100/t1000/t10000 三个档，精度-存储-维护成本三方权衡；
    且 ANALYZE 采样取 max target，一个高 target 统计拖累整体。
- **本文结论预览**：单表负载可形式化为线性稀疏选择（预算受限），ILP 秒级最优；join 负载无法
  作用(负对照)。逐候选测量由目录掩码协议支撑。
- **贡献列表**：用 bullet 列出上面 5 条。

**写作提示**：把 §5.7 的"57K 物理统计 <5s"作为一个吸睛数字放引言。避免过度承诺，明确单表限定。

---

## 2. Background & Related Work（背景与相关工作）

**2.1 基数估计与多列相关**
- PG 默认逐列直方图/MCV + 独立性假设；相关列时失效。
- 扩展统计种类：MCV / ndistinct / dependencies；本文聚焦 MCV（实证中最有效）。

**2.2 PG 的机制事实（均为本项目实测/源码验证）**
- `CREATE STATISTICS ... (mcv)`；`pg_statistic_ext` / `pg_statistic_ext_data` 目录结构。
- **每对象容量档**：`ALTER STATISTICS ... SET STATISTICS N`（`stxstattarget`）。
- **关键限制**：ANALYZE 按所有统计 target 的最大值采样（`analyze.c` `targrows`）→ 单个高 target
  拖累全表 ANALYZE。§2 引用 docs §2。
- **作用域限制（源码确认）**：扩展统计仅用于单关系 restriction 子句；`eqjoinsel` 不查
  `pg_statistic_ext` → 无法修 join。§2 引用 docs §1。
- **表**：容量档成本-修复矩阵（t100/1000/10000）→ 放 docs §5.3 的数据。

**2.3 相关工作**
- 自动可选统计：Oracled/vendor 的 automatic stats（对照）
- 本领域 / 需要补的 literature（待查证填充，见 references.bib TODO）：
  - 自动创建统计（如 `automatic extended statistics` 相关论文）
  - 查询优化器 hint-based 索引/统计选择
  - 元学习 cardinality estimation（candidate 生成的角度不同：我们测真实 plannner）
  - 存储预算下的结构选择（如 index selection 用 MILP/贪心——方法学对比点，我们的差异是
    "每查询 ≤1 的稀疏性 + 全局共享 + 容量档"）
- **对比定位**：与 index-selection 类工作对比——都是预算受限的全局选择，但我们的候选是
  (列组合×容量档)，对象是统计而非索引，且实证单一主导候选使问题线性化。

---

## 3. Problem Formulation（问题形式化）

> 基于 docs §1, §2, §3, §4。

**3.1 记号**
- workload 查询 $q_i$，真实基数 vs 估计 → **q-error** $qerr(est,act)=\max/min\ge1$。
- 基线 $e_i^0$（无扩展统计），统计 $s=(table, cols, level)$，成本 $c_s$（字节），
  有效 q-error $e_{is}$（查询 $q_i$ 只用统计 $s$ 时）。

**3.2 目标**
- $$ \min_{\{y_s\},\{x_{is}\}} \frac1n\sum_i qerr_i(T_i) \quad\text{s.t.}\quad
   \sum_s c_s y_s \le C $$
- 语义：统计可被多查询**共享**（建一次，付一次存储——`x_{is}\le y_s`）。

**3.3 多选近似 vs 稀疏特例**
- 通用多选（一查询用多个统计）需乘性近似（log 空间）；列重叠时近似可能失效
  （joint interference）。
- **稀疏特例（本文模型）**：每查询至多一个统计（`per_query_cap=1`）→ 目标**精确线性**：
  $$ \min \sum_i \Big(e_i^0 - \sum_s (\underbrace{e_i^0-e_{is}}_{\Delta_{is}\ge0}) x_{is}\Big), \quad
     \sum_s x_{is}\le1 $$
- 说明为何线性：无重叠前提在"每查询单选"下自动成立。

**写作提示**：这里要把 docs 的推导整理成投稿级数学；补一个"为什么线性"的小注(容量档互斥).

---

## 4. Optimization: Sparse MILP with Capacity Levels（优化）

> 基于 src/extstats/optimize.py 与 docs §4, §5.7。

**4.1 变量与目标**
- 二元变量 $y_s$（建统计）、$x_{is}$（查询 $i$ 用统计 $s$）。
- 目标 = $\sum_{i,s} \log(e_{is}/e_i^0)\, x_{is}$（稀疏时等价于线性改善）。

**4.2 约束**
1. 存储预算 $\sum_s c_s y_s\le C$。
2. 存在可被用 $x_{is}\le y_s$（跨查询共享）。
3. (多选模式) 查询内列重叠互斥；稀疏模式被 $\sum_s x_{is}\le1$ 取代。
4. **容量档互斥**：同一 (table,cols) 的多档 $y$ 至多一个（物理上一个对象只有一个 target）。
5. 每查询单选 $\sum_s x_{is}\le1$（可选 per_query_cap）。

**4.3 剪枝（可扩展性关键）**
- `skip_worse_than_baseline`：q-error ≥ 基线 的选项被丢弃。
- 全局去重：`n_stats` 是去重后的 (表,列,档) 宇宙，非逐查询和。
- **实测规模**（docs §5.7 / results/bench 输出）：
  | mode | phys stats | options | binary vars | solve |
  |------|-----------:|--------:|------------:|------:|
  | no-prune (worst) | 57,735 | 92,568 | 150,303 | 4.74 s |
  | prune ON | 34,280 | 46,392 | 80,672 | 4.29 s |
- 结论：全 Census（468 查询）在 HiGHS 下 <5s 最优。solve 不是瓶颈；phase-1 测量才是。

**写作提示**：放一张预算-质量曲线图（见 §5.6 表），fig 建议：横轴预算(log)，纵轴 mean q-error。

---

## 5. Measurement Infrastructure（测量基建）

> 基于 docs §5.4、src/extstats/measure_mask.py。

**5.1 逐候选测量的两难**：宽表 ANALYZE 慢（Census t10000 ~22s/次；1000 扩展统计 ~264s）。
协议-A（每候选 CREATE+ANALYZE+EXPLAIN）在宽表不可行。

**5.2 目录掩码协议 (Protocol-M)**
- 一次 ANALYZE 构建全部候选；逐候选测量时把其他统计的 `pg_statistic_ext_data` 负载置 NULL，
  planner 忽略 NULL 负载统计 → 得到单个候选的真实 q-error。
- 多容量档：每候选每档一个对象，`ALTER STATISTICS SET STATISTICS`，一次 ANALYZE(采样取 max)。
- 恢复：临时备份表 + `UPDATE ... FROM`（`pg_mcv_list` 无 bytea cast）。
- 成本：~60x 低于协议-A。
- **数**(可补充一张归一化成本对比小表)。

**写作提示**：这是方法学贡献，画一张协议对比图（Protocol-A vs M 的步骤示意）。

---

## 6. Empirical Findings（实证发现）

> 这是论文核心证据章。每节对应 docs 一个 §，并列出要放的真实表格。

**6.1 Setup（实验设置）**：PG 16.14；4 workload（表 §1.1 的基准表）；two targets（t10000 确定性，
t1000 默认）；q-error 定义。

**6.2 Single-table selection predicates are fixable（docs §5.1）**
- Census：query.184 基线 4162 → 单个 3 列 MCV → **1.08**（独立真实 ANALYZE 验证：54107→14 行）。
  其余高基线同：q465/62/61/382 全部 → ~1.0-1.4。
- stats_CEB_single：top 查询 ~4.6 → 1.0-1.06。
- **表**：post docs §5.1 的"每条查询最佳候选"表（qid/base/最佳候选/improve%）→
  src: results/phase1_census_mask_top10.json。
- **图建议**：q-error 直方图 before/after。

**6.3 One dominant candidate per query; multi-candidate adds nothing（docs §5.5）**
- 每查询贪婪取非重叠 top-1/2/3，联合实测 q-error 无差别（<0.03）。
- 在宽 Census 上复现：query.184(165候选)/query.382(56候选) k=1,2,3 完全相同（L1000/L10000）。
- **表**：docs §5.5 的 k=1/2/3 表（stats_CEB_single + Census 两行）。
- 含义：证明稀疏假设不是小候选空间的伪影。

**6.4 Sparse MILP under a storage budget（docs §5.6）**
- stats_CEB_single top10，预算曲线：20KB→1.864(+56.7%) / 40KB→1.821 / 100KB→1.632 / 2MB→1.021。
- 主张：ILP 是全局非贪心——紧预算下降级容量档而非牺牲覆盖。
- **表/图**：docs §5.6 表 → 一定改成 matplotlib 图（预算 vs q-error），这是论文主图之一。

**6.5 Join workloads are the negative control（docs §1.1, §5.8D）**
- JOB：37/113≈33% 有候选，去重后仅 7 个唯一组合；误差由 join 驱动，扩展统计无法作用。
- stats_CEB(join)：146 查询，候选 med 4。
- 主张：模型在 join 负载上无操作空间，这是**预期空结果**。

**6.6 Capacity-level trade-off（docs §5.3）**
- 表：t100/1000/10000 的 ANALYZE 成本（0/1000 ext）与修复质量 → docs §5.3。
- 结论：t1000 是维护/修复甜点。

---

## 7. End-to-End Validation（端到端验证）

> 基于 docs §5.9、results/、scripts/validate_e2e.py。

**7.1 实验**：把 ILP 2MB 解(5 统计)真正建到 PG，实测。
- 预测 mean 1.021 vs 实测 1.32（ratio 1.29）。
- 逐查询表（docs §5.9）：5 个命中、3 个退化（st.144/588/284）。

**7.2 归因**：单统计对照——只建退化查询的同一统计时，实测 1.042/1.029/1.025，与掩码预测一致。
→ **每候选预测本身正确**；退化源于并发**重叠** MCV 的 planner 串扰（5 个选中统计两两重叠，
都在 {AnswerCount,FavoriteCount,ViewCount,PostTypeId} 上）。

**7.3 对模型的意义**：
- 端到端可预测 ⇔ 并发统计列不重叠（Census §5.2 全 "preserved=YES"）。
- 开放精化：求解器优先列不重叠的解，或求解后做轻量 E2E 干扰检查（删除冲突统计）。
- 这是 honest/sound 的负面结果——论文的可信度所在。

---

## 8. Discussion & Open Questions（讨论与开放问题）

> 基于 docs §6。

- **为什么单一主导候选**：单表选择误差由"一个主导相关列簇"驱动；一个 3 列 MCV 抓住它。
- 容量档单调性约束、存储模型平滑化、全 workload 的 phase-1 测量成本是真正的可扩展瓶颈。
- 局限：实验聚焦 MCV；Census/stats_CEB_single 是单表负载；真实系统上统计对象数上限、
  多表 join 负载需其它方法。

---

## 9. Conclusion（结论）

- 复述四个主张（形式化/测量基建/稀疏实证/可解性）+ 端到端诚实评估。

---

## 附录 / 待办（填充时对照）

**每节要放的真实数据源（results/ 文件）→ 论文表/图：**
| 论文位置 | 数据源文件 | 输出形式 |
|---------|-----------|---------|
| §1.1 基准表 | parsers + bench 概览 | 表 |
| §2.2 容量矩阵 | docs §5.3（结果实测） | 表 |
| §4.3 规模表 | bench_census_solve_scale.py 输出 | 表 |
| §6.2 修复表 | phase1_census_mask_top10.json | 表 |
| §6.3 k=1/2/3 | exp_multi_top10.json + /tmp/census_multicand.json | 表 |
| §6.4 预算曲线 | phase1_ceb_single_mask_top10_multi.json + solve_sparse 输出 | 图(matplotlib) |
| §7 E2E | validate_e2e.py 输出 | 表 |

**未填数据/需补：**
- 完整 workload 的 phase-1（全 468 Census / 632 stats_CEB_single）仍是开放项——论文可标
  "extended evaluation" 未来工作，或先用 top-10/top-27 作为初步结果。
- 与 index-selection 类基线的定量对比（可选加分项）。

**写作顺序建议**：§6(最能"卖"的实证) → §3/§4(形式化+优化) → §2(背景) → §5(基建) → §7(E2E) →
§1(引言,最后写) → §8/§9。引言最后写以贴合最终内容。
