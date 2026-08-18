# extstats — PostgreSQL Extended Statistics Research Toolkit

研究 PostgreSQL 16 扩展统计（`CREATE STATISTICS`）对基数估计的影响，
覆盖三个基准：**Census**、**JOB/IMDB**、**stats_CEB**。

## 背景

PostgreSQL 的 extended statistics 仅对**基表多列选择谓词**
（`WHERE col1 <op> const AND col2 <op> const ...`）生效，**不支持连接谓词**
（`a.id = b.id`）。本工具据此从每个查询中按基表提取出现在选择谓词里的列，
生成候选扩展统计。

## 目录结构

```
extended-stats-optim/
├── benchmarks/            # 三个基准 (init 脚本 / schema / 查询)
├── src/extstats/          # Python 源码包
│   ├── config.py          # 路径、DB 连接、候选统计参数
│   ├── db.py              # psycopg 连接与执行工具
│   ├── predicates.py      # sqlglot 提取每基表的选择谓词列
│   ├── candidates.py      # 由谓词列生成候选列组合 (2..3列, 全局去重)
│   ├── stats.py           # 生成/执行 CREATE STATISTICS DDL
│   ├── measure.py         # 协议 A (逐候选 CREATE+ANALYZE+EXPLAIN)
│   ├── measure_mask.py    # 协议 M (一次 ANALYZE + catalog 掩码逐候选测量)
│   ├── optimize.py        # 多选 ILP (含容量档决策)
│   └── parsers/           # 各查询格式的解析器 (census/job/stats_ceb/single)
├── docs/                  # 理论研究文档 (见 extended-statistics-selection.md)
├── scripts/               # CLI 入口
└── results/               # 实验输出 (git 忽略)
```

> 理论形式化、实证发现与容量档(statistics_target)决策的完整说明见
> [`docs/extended-statistics-selection.md`](docs/extended-statistics-selection.md)。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 用法

激活虚拟环境后：

```bash
# 1) 干运行：打印所有候选统计的 DDL，不改数据库
python scripts/generate_candidates.py --dry-run

# 2) 只对 census 基准真正建统计
python scripts/generate_candidates.py --bench census

# 3) 全部基准 + 自定义连接
python scripts/generate_candidates.py --bench all \
    --pguser postgres --dbname imdb

# 4) 只生成两列组合 / 只建 mcv
python scripts/generate_candidates.py --arities 2 --kinds mcv
```

见 `python scripts/generate_candidates.py --help`。

## 环境变量

- `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` — 数据库连接
  （与 `benchmarks/init_*.sh` 约定一致）
