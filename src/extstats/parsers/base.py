"""Benchmark query loader interface + shared query representation.

Each benchmark ships its queries in a slightly different textual format
(Census embeds ground-truth after ``||``; stats_CEB prefixes a query id; JOB
uses one file per query). A parser normalises these into a common
`BenchQuery` object so downstream code (candidate generation, plan inspection,
q-error comparison) is benchmark-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol


@dataclass(frozen=True)
class BenchQuery:
    """One parsed query from a benchmark."""

    # Benchmark name: "census", "job", "stats_ceb".
    bench: str
    # Short identifier, e.g. "1a", "stats_CEB_7", "query.1".
    qid: str
    # The raw SQL text (without the ground-truth suffix).
    sql: str
    # Ground-truth result cardinality; None if the benchmark doesn't store it.
    ground_truth: Optional[int] = None


class QueryParser(Protocol):
    """Protocol implemented by each benchmark's loader."""

    def __call__(self, queries_dir: Path) -> list[BenchQuery]:
        """Load and normalise every query in `queries_dir`."""
        ...


def iter_sql_files(queries_dir: Path) -> list[Path]:
    """Return sorted *.sql paths directly under `queries_dir`."""
    return sorted(queries_dir.glob("*.sql"))
