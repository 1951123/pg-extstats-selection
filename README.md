# pg-extstats-selection

Budgeted, capacity-aware selection of PostgreSQL extended statistics.

This repository contains the full research toolkit for the paper
**_One-Stat Sufficiency Guided Budgeted Selection of PostgreSQL Extended
Statistics_**: source code, reproducibility scripts, and the primary measured
data artifacts.

## Overview

PostgreSQL's query optimizer assumes columns are independent; when columns are
correlated this can misestimate cardinalities by orders of magnitude.
*Extended statistics* (`CREATE STATISTICS`) capture multivariate correlation,
but deciding *which* column combinations to build and *how precisely* (each
statistic's `statistics_target`) is left to the DBA. This project automates
that choice as a *budgeted allocation* problem.

The work rests on three findings/components:

1. **One-stat sufficiency.** On real single-table workloads, most queries need
   only *one* well-chosen multivariate statistic to capture nearly all
   attainable estimation gain; additional statistics yield negligible marginal
   benefit while risking planner interference.
2. **Catalog-mask measurement (Protocol-M).** A scalable protocol that builds
   many candidate statistics in one `ANALYZE` and isolates each by masking the
   others' payloads in `pg_statistic_ext_data`, making per-candidate,
   per-capacity measurement feasible on wide tables.
3. **Budgeted integer program.** Jointly selects *which* column combinations
   (what) and *which* per-statistic sampling capacity (how much) under a shared
   storage budget; validated end-to-end on PostgreSQL 16, including the
   discovery and mitigation of planner interference among co-installed
   overlapping statistics.

## Repository layout

```
├── benchmarks/            # Three benchmarks (init scripts / schema / queries)
├── src/extstats/          # Python source package
│   ├── config.py          # Paths, DB connection, candidate-stat parameter
│   ├── db.py              # psycopg connection & execution helpers
│   ├── predicates.py      # sqlglot: extract per-table selection-predicate columns
│   ├── candidates.py      # generate candidate column combos (2..3 cols, globally dedup)
│   ├── stats.py           # generate/execute CREATE STATISTICS DDL
│   ├── measure.py         # Protocol-A (per-candidate CREATE+ANALYZE+EXPLAIN)
│   ├── measure_mask.py    # Protocol-M (one ANALYZE + catalog-mask measurement)
│   ├── optimize.py        # multi-select MILP (incl. capacity-level decisions)
│   ├── verify.py          # phase-2 verifier: build a chosen set, measure true q-error
│   └── parsers/           # query parsers (census/job/stats_ceb/single)
├── docs/                  # documentation
│   ├── extended-statistics-selection.md   # theory, findings, capacity decisions
│   └── reproducibility.md                # number ↔ artifact ↔ script manifest
├── scripts/               # CLI entry points & reproducibility scripts
└── results/               # experiment output (mostly git-ignored)
```

## Benchmarks

Three benchmark suites cover the two single-table regimes the method targets,
plus a join-heavy negative control. Each is loaded by the corresponding
`benchmarks/init_*.sh` script from the CSV/raw files vendored under
`benchmarks/<name>/data/`. **Data availability:** all raw data are third-party
public datasets; the vendored files are verbatim copies (details per benchmark
below), and every query workload is deterministically reproduced from the
query files in `benchmarks/<name>/queries/`. Principal numbers map to artifacts
in [`docs/reproducibility.md`](docs/reproducibility.md).

### 1. Census — `benchmarks/Census` (data-capped, wide single-table)

- **Data set:** *USCensus1990* (a discretized one-percent sample of the 1990
  U.S. Census), downloaded from the UCI Machine Learning Repository:
  <https://archive.ics.uci.edu/static/public/116/us+census+data+1990.zip>.
  The 69-column `climate` table is built from `USCensus1990.data.txt`
  (placed in `benchmarks/Census/data/`; the archive's `.readme`, `.attributes`,
  and `.html` documentation ships with the zip and need not be committed).
- **Workload:** 468 `SELECT COUNT(*) ... WHERE` queries over the single
  `climate` table, following the census single-table workload in
  [BayesCard](https://github.com/wuziniu/BayesCard/tree/master)
  (`benchmarks/Census/queries/query.sql`, one query per line, ground-truth
  cardinality appended after `||`).
- **Role:** the *data-capped* regime (categorical columns whose MCV lists fill
  at low cardinality), stressing wide-table measurement scale.

### 2. JOB / IMDB — `benchmarks/JOB` (join-heavy negative control)

- **Data set:** the public *Internet Movie Database* (IMDb) import used by the
  [Join Order Benchmark (JOB)](https://github.com/gregrahn/join-order-benchmark)
  (Leis et al., *How good are query optimizers, really?*, PVLDB 9(3), 2015),
  downloaded from:
  <https://bonsai.cedardb.com/job/imdb.tgz>.
  Schemas and CSVs are under `benchmarks/JOB/`
  (`schema.sql`, `fkindexes.sql`, `data/*.csv`, `schematext.sql`).
- **Workload:** the canonical JOB query set (`benchmarks/JOB/queries/`).
- **Role:** the join-heavy *negative control*: a single-relation MCV cannot
  repair cross-table join selectivity, so the method should correctly do little.

### 3. stats_CEB / stats_CEB_single — `benchmarks/stats_CEB` (target-capped)

- **Data set:** *StackExchange* public data exports (Posts, Users, Votes,
  Badges, Comments, PostHistory, PostLinks, Tags), as packaged by the
  [Cardinality Estimation Benchmark (CEB)](https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark),
  downloaded from its `datasets/stats_simplified` directory:
  <https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark/tree/master/datasets/stats_simplified>.
  CSVs and schema are under `benchmarks/stats_CEB/` (`data/*.csv`, `stats.sql`),
  with the benchmark's "no secondary indexes" convention kept to keep
  join-order tests fair.
- **Workload:** the CEB single-table sub-plans
  (`stats_CEB_single`, 632 queries) and the CEB join workload
  (`stats_CEB`, 146 joins), both from the CEB workloads directory
  <https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark/tree/master/workloads/stats_CEB>
  and parsed from `benchmarks/stats_CEB/queries/stats_CEB_single_table.sql`
  and `benchmarks/stats_CEB/queries/stats_CEB.sql`.
- **Role:** the *target-capped* regime (high-cardinality statistics whose MCV
  lists are still filling), the paper's primary workload for the capacity axis.
- **job-light** (the IMDB join sub-plan workload referenced in the paper) is the
  same CEB benchmark's IMDB-derived join workload, from
  <https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark/tree/master/workloads/job-light>.

### Data files: what is in the repo vs. what you must download

Only the repo-authored **queries** and **schemas** are committed. The **raw
data payloads** (CSV / census text / tarballs) and the **bundled third-party
docs** (e.g. the Census `*.readme` / `*.attributes` / `*.html` files that ship
inside the dataset archive) are *not* committed --- they are large third-party
files, git-ignored. You must download them into `benchmarks/<name>/data/` from
the URLs above before running the `init_*.sh` scripts. The tree below marks
committed files with ✓ and download-required files with ⬇.

```
benchmarks/
├── Census/                          # USCensus1990 (UCI), data-capped
│   ├── queries/query.sql            # ✓ 468 single-table queries (+ground truth)
│   └── data/
│       ├── USCensus1990.data.txt    # ⬇ from us+census+data+1990.zip
│       ├── USCensus1990raw.data.txt # ⬇ from us+census+data+1990.zip
│       ├── us+census+data+1990.zip  # ⬇ (optional, keep archive)
│       └── *.readme / *.attributes /
│           *.html / *.mapping.sql   # ⬇ bundled docs (from us+census+data+1990.zip)
├── JOB/                             # IMDb (JOB), join-heavy negative control
│   ├── queries/*.sql                # ✓ the 113 canonical JOB queries
│   ├── queries/job_light/           # ✓ job-light (CEB IMDB join sub-plans,
│   │                                #   referenced in the paper):
│   │                                #   job_light_queries.sql,
│   │                                #   job_light_sub_query_with_star_join.sql
│   ├── schema.sql / fkindexes.sql   # ✓ schema + indexes
│   └── data/
│       ├── *.csv                    # ⬇ 21 CSVs from imdb.tgz (aka_name.csv,
│       │                            #   cast_info.csv, title.csv, ... )
│       ├── imdb.tgz                 # ⬇ (optional, keep archive)
│       └── schematext.sql           # ✓ (schema reference)
└── stats_CEB/                       # StackExchange (CEB), target-capped
    ├── queries/stats_CEB.sql            # ✓ 146 CEB join queries
    ├── queries/stats_CEB_single_table.sql  # ✓ 632 CEB single-table queries
    └── data/
        ├── stats.sql                # ✓ schema (committed)
        └── *.csv                    # ⬇ 8 CSVs from CEB datasets/stats_simplified
                                     #   (posts.csv, users.csv, votes.csv,
                                     #   badges.csv, comments.csv, postHistory.csv,
                                     #   postLinks.csv, tags.csv)
```

After placing the ⬇ files, each benchmark is initialized idempotently with its
`benchmarks/init_*.sh` script, e.g. `bash benchmarks/init_census.sh`.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires a running PostgreSQL (tested on PostgreSQL 16.14) and the benchmark
databases loaded via the `init_*.sh` scripts.

## Usage

With the virtual environment active:

```bash
# 1) Dry run: print all candidate-statistic DDL without touching the DB
python scripts/generate_candidates.py --dry-run

# 2) Actually build statistics for a single benchmark
python scripts/generate_candidates.py --bench census

# 3) Custom connection
python scripts/generate_candidates.py --bench all \
    --pguser postgres --dbname imdb

# 4) Restrict to 2-column combinations / MCV kind only
python scripts/generate_candidates.py --arities 2 --kinds mcv
```

See `python scripts/generate_candidates.py --help` for all options.

### Reproducing the paper's figures and tables

See [`docs/reproducibility.md`](docs/reproducibility.md), which maps every
number and figure in the paper to its data source (`results/*.json`, whitelisted
in `.gitignore`) and the exact command that regenerates it.

## Environment variables

* `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` — database connection
  (consistent with the `benchmarks/init_*.sh` conventions).
