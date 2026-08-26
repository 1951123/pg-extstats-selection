# Protocol-M vs True-L: 采样失配验证实验汇总

**日期**: 2026-08-26
**目的**: 验证 Protocol-M（large-sample oracle）测得的容量-qerror 曲线，在真实部署下的保真度。
**实验脚本**: `scripts/validate_true_vs_mask.py`
**结果文件**: `results/true_vs_mask_smoke.json`, `results/true_vs_mask_pilot.json`, `results/true_vs_mask_census.json`

---

## 1. 背景：为什么做这个实验

方法学担忧（来自 ChatGPT reviewer 评估）：Protocol-M 用**一次固定 `single_col_target=10000` 的 ANALYZE** 在同一张大样本（~3M 行）上构建所有容量层级（L10..L10000）的统计对象，然后通过 mask 其他对象、EXPLAIN 来测每个层级 q-error。

但 PostgreSQL 的采样行数是 `targrows = max(单列 target 贡献, 扩展统计 target 贡献)`（`analyze.c` + `extended_stats.c`）。因此：
- **Mask** 对每个 L 用的都是**同一份 ~300 万行大样本**（因为单列 target=10000 主导采样）。
- **True 部署**（默认单列 100 + `ALTER STATISTICS SET STATISTICS L`）用 `max(300×100, 300×L)` 行的样本。

**风险**: 大样本下，低容量 L 的 MCV 可能更"精确"地捕获稀有组合 → Mask 显得低容量已足够好 → **低估真实低容量的 q-error、低估容量升级的必要性**。

> **目标**: 量化这个 mismatch 是否真实存在、影响多大、是否会破坏 (a) 论文 §6.1/§6.3 的核心容量结论、(b) MILP 的容量决策。

---

## 2. 协议定义

**Protocol-M (Mask)**: 即论文当前方法。全部层级在 `single_col_target=10000` 的一次 ANALYZE 下构建；测某层级时把其他对象的 `pg_statistic_ext_data` payload masked 为 NULL，EXPLAIN 得 q-error。

**Protocol-T (True-L, 真实部署)**: 对每个候选×层级 L：
1. `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS 100`（单列回到默认 100）
2. `CREATE STATISTICS ...`（若不存在）
3. `ALTER STATISTICS ... SET STATISTICS L`
4. 真正 `ANALYZE`（此时采样行 = `max(300×100, 300×L)`）
5. `EXPLAIN` 得真实部署的估计/实际 q-error

---

## 3. 结果一览

### 3.1 Smoke test (stackoverflow 3 个候选)
argmin 一致 **3/3**，Spearman 0.85/0.98/1.0。纯冒烟，无统计意义。

### 3.2 stats_ceb_single pilot (15 个候选，覆盖 flat/moderate/steep，多张表)

| 指标 | 值 |
|---|---|
| **argmin (最优层级) 精确一致** | **10/15 (67%)** |
| **ε-optimal (5%) 决策保真度** | **15/15 (100%)** |
| 所有 argmin 不一致候选的真实 q-error 损失 | **≤1.0%** |
| Spearman 中位 | 高（多数 >0.77，个别 <0.3 出现在 q-error≈1.0 的平缓曲线） |

**所有 15 个候选的 T@M loss% 都 ≤1.0%**（选 Mask 认为的最优层级，真实部署损失不超过 1%）。对 stats_CEB_single 这类 workload，**Protocol-M 的容量决策高度保真**。

### 3.3 CENSUS (4 个候选，含 query.184 极端选择性与 query.1 高基数) —— **关键**

| 指标 | 值 |
|---|---|
| **argmin 精确一致** | **0/4 (0%)** |
| **ε-optimal (5%) 决策保真度** | **2/4 (50%)** |
| **两个失败候选的真实 q-error 损失** | **11232% 和 17707%（≈100-180 倍）** |

逐候选（M-arg = Mask 认为的最优层级，T-arg = 真实部署最优层级，T@M = 按 Mask 选择在真实部署的 q-error，loss% = 相对真实最优的损失）：

| 候选 | qid | M-arg | T-arg | M-best | T-best | T@M | loss% | εOK |
|---|---|---|---|---|---|---|---|---|
| climate(dRearning,iSex,iTmpabsnt) | query.4 | 50 | 25 | 1.022 | 1.007 | 1.017 | 1.0% | ✅ |
| climate(iAvail,iClass,iRagechld) | query.1 | 25 | 50 | 1.830 | 1.816 | 1.832 | 0.9% | ✅ |
| climate(dRpincome,iDisabl1,iRspouse) | query.184 | **100** | **10000** | 2.6 | 2.6 | **336.2** | **11232%** | ❌ |
| climate(iDisabl1,iLooking,iRspouse) | query.184 | **50** | **10000** | 1.077 | 1.077 | **191.8** | **17707%** | ❌ |

---

## 4. 决定性案例：query.184 的极端选择性候选

论文 §6.1 以 query.184（实际基数 act=13）作为"最差查询被单个 3 列 MCV 从 4162 压到 1.08"的核心例子。
候选 `climate(iDisabl1,iLooking,iRspouse)` 的逐层级对比：

| L | **Mask** qerr | **True** qerr | Mask est | True est | Actual |
|---|---|---|---|---|---|
| 10 | 4162.08 | 4122.92 | 54107 | 53598 | 13 |
| 50 | **1.08** | **191.77** | 14 | **2493** | 13 |
| 100 | **1.08** | **126.23** | 14 | 1641 | 13 |
| 1000 | 1.08 | 6.54 | 14 | 85 | 13 |
| 10000 | 1.08 | 1.08 | 14 | 14 | 13 |

**现象**:
- **Mask** 在 L50 就报 q-error=1.08（估计 14≈真实 13）——大样本 MCV 精确命中了这个稀有组合。
- **True 部署** 在 L50 时 q-error=**191.77**（估计 2493 vs 真实 13），L100=126.23，要到 **L10000** 才真正降到 1.08。

**根因（完全印证担忧）**: 真实低容量 L 的 MCV 在 ~300×L 行小样本上构建，抓不住这个全局只出现 13 行的极端组合；而 Mask 的大样本恰好精确捕获。**因此 Mask 严重高估了低容量的效果、低估了容量升级的必要性。**

---

## 5. 影响分析：对论文与决策

### 5.1 有利方向（结论反而被强化）
真实部署下**低容量比 Mask 更差** ⇒ "需要升级容量"这一核心结论（§6.1 capacity as a decision axis、§6.3 upgrade matters）**在真实部署下更尖、更必要**，而非被削弱。

### 5.2 不利方向（数据准确性与决策）
1. **§6.1 的一个具体数字不具部署准确性**：论文称 query.184 "L100 → 1.08"，真实 L100 是 **126.23**（差 100 倍以上）。这是一个 reviewer 可直接抓的**数据准确性问题**。
2. **MILP 依赖 Mask 的 e_is，会低估容量需求**：对极端选择性候选，Mask 报"L50 最优"，MILP 可能选 L50，但真实部署该层级 q-error=191（灾难）。**这是决策层面的真实风险**。
3. **影响面**：仅出现在**极端选择性 / 稀有组合**候选（CENSUS query.184 这类 act 极小的场景）。stats_CEB_single 的 15 个候选（含 moderate/steep，但无 act≈13 这种极端）全部 ε 保真——说明问题**不普遍，但存在于最脆弱、最可能被 reviewer 攻击的场景**。

---

## 6. 结论与待决问题

**结论**:
1. Protocol-M 是**高保真 oracle**，但对**极端选择性/稀有组合**候选存在系统性偏差：**低估低容量 q-error、低估容量升级必要性**。
2. 该偏差**直接影响论文 §6.1 中 query.184 的具体数字**，且**会让 MILP 在极端选择性候选上选择过低的容量**。
3. 不推翻容量轴大方向（反而强化升级必要性），但**数据准确性与决策可靠性需处理**。

**待决问题（需 ChatGPT/导师决策）**:
1. 是否/如何**修正 or 重新定位 §6.1 中 query.184 的数字**（例如：换成 True-L 测得的曲线，或明确标注为 large-sample oracle 界）。
2. 论文如何处理 oracle 低估容量需求的系统性偏差——是否需要**保守化处理**（例如极端选择性候选上倾向更高 target）？
3. 是否扩大 pilot 到 30-50 候选 + 加 E2E（MILP 选出的统计 → verify.py 真实部署）决策保真度验证，把 True-L 写成论文的一个 validation 章节。

---

## 7. 复现命令行

```bash
# stats_ceb_single pilot (15 cands)
timeout 1500 .venv/bin/python scripts/validate_true_vs_mask.py \
  --bench stats_ceb_single --phase1 results/phase1_ceb_single_mask_6level.json \
  --levels 10,25,50,100,1000,10000 --out results/true_vs_mask_pilot.json

# CENSUS critical (4 cands)
timeout 1500 .venv/bin/python scripts/validate_true_vs_mask.py \
  --bench census --phase1 results/phase1_census_mcv_6level.json \
  --cands "climate(iDisabl1,iLooking,iRspouse);climate(dRpincome,iDisabl1,iRspouse);climate(iAvail,iClass,iRagechld);climate(dRearning,iSex,iTmpabsnt)" \
  --levels 10,25,50,100,1000,10000 --out results/true_vs_mask_census.json
```
