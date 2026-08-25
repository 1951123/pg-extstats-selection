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

* **Census (USCensus 1990)** — a wide, 69-column single-table workload
  (data-capped statistics).
* **JOB / IMDB** — join-heavy workload (negative control).
* **stats_CEB** — the Cardinality Estimation Benchmark's single-table and join
  workloads (target-capped statistics).

Initialize a benchmark with the corresponding `benchmarks/init_*.sh` script
(e.g. `bash benchmarks/init_census.sh`).

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
