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
| `figures/upgrade_vs_add`（容量升级 vs 新增） | `scripts/fig_upgrade_vs_add.py` | `python scripts/fig_upgrade_vs_add.py --phase1 results/phase1_ceb_single_mask_6level.json --out paper/figures/upgrade_vs_add` |

---

## 2. 论文数值 ← 数据源头

| 论文中的数值 / 结论 | 数据源头 primary 工件 |
|---|---|
| 稀疏 regime：每查询至多一个统计量（中心发现） | `phase1_ceb_single_mask_6level.json`（CDF 峰值） |
| RQ1 容量轴：91.8% 候选 q-error 与 target 无关；73.3% 完全塌陷；4.4% 改善（{100,1000,10000}） | `phase1_census_mcv_multi.json`（CENSUS 30,856 候选） |
| RQ1 容量轴扩展菜单 {10,25,50,100,1000,10000}（两向决策轴） | `phase1_ceb_single_mask_6level.json`（stats_CEB_single 6 级）+ `phase1_census_mcv_multi.json`（{100,1000,10000}） |
| CENSUS 低档容量再暴露（~30% 敏感 / 22% 改善，正式数字以重跑为准） | `phase1_census_mcv_low_t10000.json`（**正式重跑，完成后以它为准**）；旧值 `phase1_census_mcv_low.json`（single-col-50 历史） |
| §5.1 测量成本模型（ANALYZE base ~22s @t10000，T(N)=base+0.334N） | `probe_census_fine_capacity.log` / `probe_census_analyze_scale.py` 输出 |
| stats_CEB mcv 多变量（multi）效果 | `phase1_stats_ceb_mcv.json`、`phase1_stats_ceb_mcv_r3.json`（复现） |

---

## 3. primary 工件清单（已纳入 git）

| 文件 | 大小 | 内容 / 用途 |
|---|---|---|
| `results/phase1_ceb_single_mask_6level.json` | 860K | **三张论文图的数据源头** + RQ1 容量轴 {10..10000} 完整。取代 `full_multi` |
| `results/phase1_census_mcv_multi.json` | ~21M | CENSUS {100,1000,10000} 正式基线（30,856 候选），single-col-10000。**总耗时约 21h**（中断过一次：`phase1_census_mcv_multi.log` 开头 `[resume] loaded 140 done queries`，其 `53857s` 仅含续跑段 328 查询；第一段 140 查询 ~6.2h 无独立日志，为按 ~160s/q 估算） |
| `results/phase1_census_mcv_low.json` | ~20M | CENSUS 低档探测（single-col-50，历史参考） |
| `results/phase1_census_mcv_low_t10000.json` | （运行中） | **CENSUS 正式重跑**（single-col-10000 + {10,25,50}），完成后更新终值 |
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

- **CENSUS {10,25,50} 正式重跑**（single-col-10000，运行中）：
  ```bash
  env PYTHONPATH=src nohup .venv/bin/python -u scripts/measure_phase1_subbatch.py \
    --bench census --kind mcv --arities 2,3 \
    --target-levels 10,25,50 --single-col-target 10000 \
    --cands-per-batch 55 --checkpoint-every 20 --resume \
    --out results/phase1_census_mcv_low_t10000.json
  ```

---

## 5. git 纳入 / 忽略策略

- `results/` 整体默认忽略；仅 `.gitignore` 中显式 `!` 白名单的 **primary 工件** 会被跟踪。
- **纳入标准**：论文图表 / 数值直接依赖、重跑成本数小时、无法从脚本再生成的原始测量。
- **保持忽略**：`smoke_*`、临时探测、可再生中间量、`phase1_*_t100/t1000/top10*.json` 等。
- ⚠️ **gitignore 坑（已踩）**：否定（`!`）行尾部不要加 `# 注释` — 会破坏该规则的解析，
  文件会静默保持忽略。注释放到前一行的注释块中。改完用 `git check-ignore -v <file>` 验证。

## 6. 待办

- [ ] `phase1_census_mcv_low_t10000.json` **正式重跑完成后**：确认终值、纳入 git，并替换
      §2.2 / RQ1 中"~30% 敏感 / 22% 改善"的**初步**数字为 single-col-10000 终值。
- [ ] **等 CENSUS 完整 6 级测量完成后，重做 `tab:measurement` 和 §measure-runtime 的成本数字**。
      原因：容量菜单从 3 级 {100,1000,10000} 扩到 6 级 {10,25,50,100,1000,10000} 后，
      每个候选的测量次数 (L) 翻倍，ANALYZE 项 vs mask 项的比例、最优子批大小 $m^{*}$、
      以及总墙钟时间都随之变化。**当前论文中的 CENSUS 成本数字（≈20h / ~12× / workload-wide
      87h）基于旧 3 级数据，已回退为定性/待填表述**，待
      `phase1_census_mcv_low_t10000.json`（低 3 级）+ `phase1_census_mcv_multi.json`（高 3 级）
      合并成完整 6 级实测后填入真实值。
- [ ] stats_CEB_single 的 workload-wide/per-query 时长（≈0.12h / ≈1.29h）也是模型外推，
      其 6level 数据已完成（632 查询），可复核是否随 6 级化更新。
- [ ] 若要做增量容量合并，可新增 `merge_phase1.py`（方法 B，尚未开始）。
