# Reproducibility Manifest

本清单把论文中的每个**关键数值 / 每张图**映射到其**数据源头工件（primary artifacts）**
与**复现脚本 + 命令**，供审稿人 / 复现者使用。所有 primary 工件均已纳入 git
（见 `.gitignore` 中的 `!results/...` 白名单），不随 `results/` 整体忽略。

> **维护约定**：凡论文引用的测量数值，其源 JSON 必须出现在下方"数据源头"中并在 git
> 白名单内。新增图表时，同步在此登记数据源头与复现命令。

---

## 1. 论文图 ← 数据源头 ← 复现脚本

三张论文图的**数据源头均为同一个 primary 工件**：
`results/phase1_ceb_single_mask_6level.json`（632 查询 stats_CEB_single 正式测量，6 级
{10,25,50,100,1000,10000}）。

> 该文件**取代**了更早的 `phase1_ceb_single_mask_full_multi.json`（仅 3 级
> {100,1000,10000}）。已验证 6level 是其**严格超集**：632/632 查询、616/616
> 候选完全一致，且共享 {100,1000,10000} 上的 1,848 项 (候选×档) 逐位一致。
> 用 6level 跑 `analyze_sparsity.py` 产生的覆盖率/稀疏指标与 multi 逐位相同
> （coverage_median=0.999, frac_ge_0_9=0.9429, frac_ge_0_99=0.7714）。

> `analyze_budget_metrics.py` 的右侧 level-mix 图展示**完整 6 档**容量菜单
> {10,25,50,100,1000,10000}（脚本 `LEVELS` 常量）。在最小预算下解会选择低档
> （如 5KB 时 6 个物理统计全部落在 L10/L50），随预算升级到 L1000/L10000 ——
> 这是"降容量省钱"决策轴的可视化，取代了旧的 3 档 hardcode。

| 论文图 | 复现脚本 | 生成命令 |
|---|---|---|
| `figures/sparsity_cdf`（稀疏 regime CDF） | `scripts/analyze_sparsity.py` | `python scripts/analyze_sparsity.py --input results/phase1_ceb_single_mask_6level.json --out results/p4_sparsity.json --fig paper/figures/sparsity_cdf` |
| `figures/e2e_predict`（E2E 预算 vs 增益散点） | `scripts/exp_e2e_scatter.py` | `python scripts/exp_e2e_scatter.py --input results/phase1_ceb_single_mask_6level.json --db stats --table posts --target 10000 --budgets 10000,40000,100000,250000 --out results/p4_e2e_scatter.json --fig paper/figures/e2e_predict` |
| `figures/budget_quality`（预算分配质量） | `scripts/analyze_budget_metrics.py` | `python scripts/analyze_budget_metrics.py --input results/phase1_ceb_single_mask_6level.json --budgets 5000,10000,20000,40000,100000,250000,500000,1000000 --out results/p3_metrics.json --fig paper/figures/budget_quality` |
| `figures/census_budget_quality`（CENSUS data-capped budget-quality，Fig:census_budget_quality） | `scripts/analyze_budget_metrics.py` | `python scripts/analyze_budget_metrics.py --input results/phase1_census_mcv_6level.json --budgets 10000,25000,50000,100000,250000,500000,1000000,2000000 --out results/p3_census_metrics.json --fig paper/figures/census_budget_quality` |
| `figures/upgrade_vs_add`（容量升级 vs 新增） | `scripts/fig_upgrade_vs_add.py` | `python scripts/fig_upgrade_vs_add.py --phase1 results/phase1_ceb_single_mask_6level.json --out paper/figures/upgrade_vs_add` |

---

## 2. 论文数值 ← 数据源头

| 论文中的数值 / 结论 | 数据源头 primary 工件 |
|---|---|
| 稀疏 regime：每查询至多一个统计量（中心发现） | `phase1_ceb_single_mask_6level.json`（CDF 峰值） |
| RQ1 容量轴：91.8% 候选 q-error 与 target 无关；73.3% 完全塌陷；4.4% 改善（{100,1000,10000}） | `phase1_census_mcv_multi.json`（CENSUS 30,856 候选） |
| RQ1 容量轴扩展菜单 {10,25,50,100,1000,10000}（两向决策轴） | `phase1_ceb_single_mask_6level.json`（stats_CEB_single 6 级）+ `phase1_census_mcv_6level.json`（**CENSUS 完整 6 级，由 low/multi 合并**） |
| CENSUS 低档容量再暴露（~29% 敏感 / ~7% 改善≥20%，完整 6 级核算） | `phase1_census_mcv_6level.json`（**合并终值**）；溯源文件 `phase1_census_mcv_multi.json`（{100,1000,10000}）+ `phase1_census_mcv_low_t10000.json`（{10,25,50}）；旧值 `phase1_census_mcv_low.json`（single-col-50 历史） |
| §5.1 测量成本模型（ANALYZE base ~22s @t10000，T(N)=base+0.334N） | `probe_census_fine_capacity.log` / `probe_census_analyze_scale.py` 输出 |
| §measure-runtime 单查询 6 级 T_q(b) 实测（query.3 + st.144）——改进模型（ANALYZE=b·Bf+cp·nL）训练集 b=1,3/1,2 拟合、测试集 b=7/14/28 与 b=4/7/10 验证 ≤3%；固定 B0 模型 CENSUS 低估 69%、stats_CEB 高估 3-10× | `results/tq6_model_vs_measured.json`（CENSUS query.3, b=1,3,7,14,28）+ `results/tq6_stats_ceb_model_vs_measured.json`（st.144, b=1,2,4,7,10）；脚本 `scripts/measure_tq6.py`（实测）+ `scripts/validate_tq6.py`（train/test 验证） |
| §measure-runtime 实测加速比（Protocol-M 最优 batch 实测 vs Protocol-A）：CENSUS ≈283×、stats_CEB ≈36×（下界） | `scripts/speedup_measured.py`（基于上面的 tq6 实测 JSON） |
| stats_CEB mcv 多变量（multi）效果 | `phase1_stats_ceb_mcv.json`、`phase1_stats_ceb_mcv_r3.json`（复现） |

---

## 3. primary 工件清单（已纳入 git）

| 文件 | 大小 | 内容 / 用途 |
|---|---|---|
| `results/phase1_ceb_single_mask_6level.json` | 860K | **三张论文图的数据源头** + RQ1 容量轴 {10..10000} 完整。取代 `full_multi` |
| `results/phase1_census_mcv_multi.json` | ~21M | CENSUS {100,1000,10000} 正式基线（30,856 候选），single-col-10000。**总耗时约 21h**（中断过一次：`phase1_census_mcv_multi.log` 开头 `[resume] loaded 140 done queries`，其 `53857s` 仅含续跑段 328 查询；第一段 140 查询 ~6.2h 无独立日志，为按 ~160s/q 估算） |
| `results/phase1_census_mcv_low.json` | ~20M | CENSUS 低档探测（single-col-50，历史参考） |
| `results/phase1_census_mcv_low_t10000.json` | ~21M | **CENSUS 正式重跑完成**（single-col-10000 + {10,25,50}）：完整 468 查询，实测墙钟 75{,}512s ≈ 21.0h（`phase1_census_low_t10000.log`，无中断） |
| `results/phase1_census_mcv_6level.json` | ~41M | **CENSUS 完整 6 级轴 {10,25,50,100,1000,10000}**（468 查询，30,856 候选，每候选 6 档全测量）——由 `scripts/merge_phase1.py` 合并 low/multi 两档，逐位一致（qid 顺序、候选集完全一致，无缺失档）。**实测总墙钟 ≈42h**（低 3 级 21h + 高 3 级 ~21h）。下游 RQ1 / budget 分析以它为数据源头 |
| `results/phase1_stats_ceb_mcv.json` | 452K | stats_CEB mcv 测量 |
| `results/phase1_stats_ceb_mcv_r3.json` | 736K | stats_CEB mcv 复现 |
| `results/phase1_ceb_single_6level.log` | 42 行 | 6 级运行日志（配置/进度） |
| `results/phase1_census_low.log` | 51 行 | CENSUS 低档运行日志 |
| `results/phase1_census_low_t10000.log` | 小 | **正式重跑日志**（进行中，先提交，重跑完成后更新） |
| `results/phase1_census_mcv_multi.log` | 178 行 | CENSUS 正式基线日志 |
| `results/probe_census_fine_capacity.log` | 103 行 | 细粒度容量轴探测日志 |

---

## 4. primary 工件的重生成命令

> 这些是远程 PostgreSQL 16.14 上的昂贵测量（数小时），因此把完成的结果 JSON **直接纳入
> git**，而不是要求审稿人重跑。命令仅供按需重生成 / 交叉验证。

- **stats_CEB_single 全工作量 6 级**（632 查询，single-col-10000）：
  ```bash
  env PYTHONPATH=src .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench stats_ceb_single --kind mcv --arities 2,3 \
    --target-levels 10,25,50,100,1000,10000 --single-col-target 10000 \
    --out results/phase1_ceb_single_mask_6level.json
  ```
  抽取 `full_multi` 版本（三图数据源头）可加 `--limit 632` / 指定 qids 集合。

- **CENSUS {100,1000,10000} 正式基线**：
  ```bash
  env PYTHONPATH=src nohup .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench census --kind mcv --arities 2,3 \
    --target-levels 100,1000,10000 --single-col-target 10000 \
    --cands-per-batch 55 --checkpoint-every 20 \
    --out results/phase1_census_mcv_multi.json
  ```

- **CENSUS {10,25,50} 正式重跑**（single-col-10000，已完成）：
  ```bash
  env PYTHONPATH=src nohup .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench census --kind mcv --arities 2,3 \
    --target-levels 10,25,50 --single-col-target 10000 \
    --cands-per-batch 55 --checkpoint-every 20 --resume \
    --out results/phase1_census_mcv_low_t10000.json
  ```

- **CENSUS 完整 6 级轴（合并 low/multi，可重复）**：
  ```bash
  .venv/bin/python -u scripts/merge_phase1.py \
    --low results/phase1_census_mcv_low_t10000.json \
    --high results/phase1_census_mcv_multi.json \
    --out results/phase1_census_mcv_6level.json
  ```
  校验通过：468/468 查询、候选集完全一致、6 档逐位无缺失。

---

## 5. git 纳入 / 忽略策略

- `results/` 整体默认忽略；仅 `.gitignore` 中显式 `!` 白名单的 **primary 工件** 会被跟踪。
- **纳入标准**：论文图表 / 数值直接依赖、重跑成本数小时、无法从脚本再生成的原始测量。
- **保持忽略**：`smoke_*`、临时探测、可再生中间量、`phase1_*_t100/t1000/top10*.json` 等。
- ⚠️ **gitignore 坑（已踩）**：否定（`!`）行尾部不要加 `# 注释` — 会破坏该规则的解析，
  文件会静默保持忽略。注释放到前一行的注释块中。改完用 `git check-ignore -v <file>` 验证。

## 6. 待办

- [x] **RQ3 全表已用 6 级 `phase1_ceb_single_mask_6level.json` 重建（审计发现旧 3-level 残留）**：
      系统性重算统计发现 paper 里 RQ3 的 4 张表在 5/10/20/40KB 处仍是 3-level（旧 full_multi）数字，
      与已 6 级的 `fig:budgetquality` 矛盾。已全部用 6 级重算更新：
      - `tab:greedy2`（MILP vs G1/G2/G3）：5KB MILP 1.0327→**1.0270**、best-greedy 1.0798→**1.0531**、adv 0.047→**0.026**；全 budget 表值+正文更新
      - `tab:whathowmuch`：what+how-much 列 5/10/20/40KB 更新（1.0327→1.0270 等）；what-only 列本就正确
      - `tab:ablation`：5KB 行 full/--sparse/--prune 1.0327→**1.0270**（100KB 已正确）；正文 1.033→1.057 改为 1.027→1.057
      - `tab:meanrob`：Jaccard 与 mean 全部更新（10KB 0.667→**0.818**, 40KB 0.833→**0.286** 等）；"identical" 改为 "nearly identical (3rd-4th decimal)"
      重新生成的 6 级结果 JSON：`results/p6_greedy_strong.json`、`results/p2_what_vs_howmuch.json`、
      `results/p5_ablation_{5k,100k}.json`、`results/p10_mean_robustness.json`。
      核心论点均不变（MILP 仍胜 greedy、capacity 轴仍关键、mean 选择仍稳健），仅数值随 6 级更新。
      未误改的正确部分：RQ2 coverage（独立 L10000 joint 实验，非容量菜单问题）、fig:budgetquality（本已 6 级）、RQ1/Table:bench/tab:measurement（已核对一致）。

- [x] `phase1_census_mcv_low_t10000.json` **正式重跑已完成**（468 查询，实测 21.0h）：已纳入 git，
      并与 `phase1_census_mcv_multi.json` 合并为完整 6 级轴 `phase1_census_mcv_6level.json`（已纳入 git）。
      §2.2 / RQ1 中容量数字已用完整 6 级终值更新：{100,1000,10000} 91.8% 不变 / 73.3% 塌陷；
      {10,25,50} ~29% 敏感 / ~7% 改善≥20%。
- [x] **`tab:measurement` 已重构为实测表**：展示 CENSUS `query.3`（84 候选项，nL=504）与
      `stats_CEB_single` `st.144`（35 候选项，nL=210）在六个 niveau 菜单下、不同 sub-batch
      数量 b（s=nL/b）的**实测墙钟**（total / ANALYZE / mask 分解）。数据来自
      `results/tq6_model_vs_measured.json` 与 `results/tq6_stats_ceb_model_vs_measured.json`。
      实测单查询最优：CENSUS b=1 (402.0s)、stats_CEB b=4 (22.2s)；mask 项随 b 增长按 ≈1/b 下降
      （CENSUS 186.0→6.3s），ANALYZE 项近似随 b 线性增长——支撑 Cor.~intraquery 的两项竞争。
      这取代了旧版"纯模型曲线"（B₀/s+cL+μL²s, s*=55, 24×）表，彻底消除模型估计与实测的混淆；
      harness 用 b=55 是 workloade-wide 设置（区别于单查询最优），正文表述已解耦。
- [x] **§measure-runtime 的 workload 级墙钟（42h/0.25h 与 b=55）已从论文移除（有意为之）**：
      完整 6 级 phase-1 实测 CENSUS ≈42h（低 3 级 21h + 高 3 级 ~21h）、stats_CEB ≈0.25h，
      但 **b=55 是历史遗留的 harness 粒度**（来自 3-level→6-level 演进 + 旧错误代价模型
      选出的 batch size），并非最优、难以解释。论文不再汇报 workload 级墙钟，只报
      **干净的实测单查询最优**（CENSUS query.3 b=1=402s、stats_CEB st.144 b=4=22.2s）
      与实测加速比 283×/36×；intro 改为强调"完整 6 级 per-candidate q-error 表可单趟获得"
      而非 42h。本文件保留 b=55 与实测时长为**技术运行记录**（复现真实性）。
- [x] **§measure-runtime 的 CENSUS 实际墙钟时长已填入**（见上一条完成项；已从论文移除，
      仅留作复现记录）。
- [ ] **模型 vs 实测的系统性低估，已用完整 6 级实测确认**：
      代价模型估算 L=6 CENSUS ≈22-33h，但实测完整 6 级 ≈42h（低 3 级 21h + 高 3 级 ~21h）——
      模型系统性低估 ~1.3-1.9×（归因同前：ANALYZE 实际基成本 > 22s、每子批隐式固定开销
      CREATE/DROP/restore/连接未被捕获）。**结论**：42h 实测与早期"÷0.58 校准 ≈ 38-40h"
      的估计基本吻合。论文不再汇报 42h（见上一条完成项），但此低估结论仍然成立——
      它正是驱动改进模型（per-dataset Bf）的关键依据，已在 §measure-runtime 用
      单查询 T_q(b) 实测验证（CENSUS/ stats_CEB held-out ≤3%）。
- [ ] **Protocol-A 对比也是 ANALYZE-only、系统性低估 Protocol-A**（强化 Protocol-M 动机）：
      `exp_catalog_mask_scale.py` 中 Protocol-A 定义为 $N(2B_0 + \text{EXPLAIN})$，
      **只含 ANALYZE，忽略每候选的 CREATE/DROP STATISTICS DDL 开销**。对 CENSUS
      6 级下 N=30,856×6=185,136 物理统计即 185,136 次 CREATE + 185,136 次 DROP 未计。
      仅 ANALYZE 项 Protocol-A baseline ≈2{,}263h（30,856×6 物理统计 × 2×22s），
      实测 6 级 sub-batched ≈42h → 加速比 $2263/42 \approx 54\times$ 仍是下界；
      计入逐候选 DDL 后 Protocol-A 只会更慢、Protocol-M 优势只会更大（不对称低估：
      只压低 Protocol-A，不压 Protocol-M）。**建议**：另起一次不冲突的小 probe 实测
      单次 CREATE/DROP STATISTICS 开销，以量化这个增量并决定是否写进论文
      （当前为定性论证，正文用"over an order of magnitude"，未硬编码 DDL 增量数字）。
- [x] **`merge_phase1.py` 已实现**（方法 B）：合并 CENSUS low（{10,25,50}）与 multi
      （{100,1000,10000}）为完整 6 级轴 `phase1_census_mcv_6level.json`（已纳入 git），
      校验 468/468 查询、候选集一致、6 档无缺失。
- [x] **stats_CEB_single 的运行时叙事已按实测修正**：原"workload-wide 最优 (~0.12h vs
      ~1.29h per-query)"是 3 级模型外推，且与实际运行（sub-batched, b=55）不符。已改为：
      两个 workload 均用 sub-batched Protocol-M（b=55），stats_CEB_single 6 级实测
      ≈0.25h（632 查询, 日志 phase1_ceb_single_6level.log: 893s），CENSUS ≈42h。
      workload-wide 仅保留为闭合式识别出的理论最优 scope，不再声称"实际用它"。
      `≈188×`（stats_CEB sub-batched vs naive per-candidate）经实测 0.25h vs 45h 核算 ≈182× 一致。
