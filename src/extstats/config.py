"""Global configuration: paths, DB connection, and candidate-generation defaults.

All values can be overridden by environment variables (see `env_or`).
Database connection defaults match the benchmarks' init_*.sh scripts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

# src/extstats/config.py  ->  repo root is two levels up
REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
RESULTS_DIR = REPO_ROOT / "results"

# Names of the three benchmark subdirectories under benchmarks/.
BENCH_NAMES = ("census", "job", "stats_ceb")


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DBConfig:
    """PostgreSQL connection parameters (mirror benchmarks' env conventions)."""

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = "postgres"
    # Default database per benchmark; can be overridden in a full config.
    dbname: str = "postgres"

    @classmethod
    def from_env(cls) -> "DBConfig":
        return cls(
            host=os.environ.get("PGHOST", "localhost"),
            port=int(os.environ.get("PGPORT", "5432")),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
        )


# Default database name for each benchmark (matches init_*.sh).
DEFAULT_DB = {
    "census": "census",
    "job": "imdb",
    "stats_ceb": "stats",
}


# ---------------------------------------------------------------------------
# Candidate extended statistics generation defaults
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatsDefaults:
    """Defaults for candidate extended-statistics generation.

    PG16 supports three statistic kinds for column combinations:
      - dependencies : detect functional dependencies between columns
      - ndistinct    : estimate the number of distinct combinations
      - mcv          : most-common-values list across the combination
    """

    # Column-combination arities to consider, e.g. (2, 3) => pairs and triples.
    # NOTE: ndistinct requires *at least* two columns; arity 1 is skipped.
    arities: tuple[int, ...] = (2, 3)

    # Default statistic kinds created for each candidate combination.
    kinds: tuple[str, ...] = ("dependencies", "ndistinct", "mcv")

    # List of extra statistics to create even when a query touches a single
    # column (useful for expression stats, kept here for future extension).
    single_col_kinds: tuple[str, ...] = ()

    # If True, deduplicate identical (schema.table, column set) combinations
    # across all queries so each physical statistic is created only once.
    dedupe: bool = True

    # Optional prefix so created statistics can be identified / dropped later.
    # Set to "" to leave PostgreSQL to choose a name.
    stats_name_prefix: str = "ext_"

    # If True, actually run CREATE STATISTICS; otherwise only print the DDL
    # (useful for a dry run / review before touching the database).
    dry_run: bool = False

    # Print the generated DDL even when executing.
    verbose_ddl: bool = True


# ---------------------------------------------------------------------------
# Small environment helper
# ---------------------------------------------------------------------------

def env_or(name: str, default: str) -> str:
    """Return environment variable `name` or `default`."""
    return os.environ.get(name, default)
