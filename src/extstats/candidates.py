"""Generate candidate extended-statistics column combinations.

Starting from the per-query, per-base-table predicate columns extracted by
:mod:`extstats.predicates`, we produce the set of *candidate* statistic
definitions we want to create.

Algorithm
---------
For each query:
  - for each base table with ``>= 2`` predicate columns:
    - consider all column combinations of size in ``arities`` (default 2..3),
    - each combination is a candidate extended statistic on that table.

Deduplication
-------------
Because the same ``(schema.table, column tuple)`` may appear in many queries,
we optionally deduplicate so each physical statistic is created only once.
When ``dedupe=True`` a combination is a capitalised set of its columns; the
column order does not matter for correctness (PostgreSQL treats column order
as insignificant for dependencies / ndistinct / mcv lists).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Iterator

from .predicates import predicate_columns
from .parsers.base import BenchQuery


@dataclass(frozen=True)
class CandidateSet:
    """A combination of columns on a single base table."""

    # Qualified table name, e.g. ".climate" or ".posts".
    table: str
    # Sorted tuple of base-table column names.
    columns: tuple[str, ...]

    @property
    def table_unqualified(self) -> str:
        """Return the bare table name (without schema prefix)."""
        return self.table.rpartition(".")[2]

    def __repr__(self) -> str:
        cols = ", ".join(self.columns)
        return f"<CandidateSet {self.table}({cols})>"


def _gen_combinations(
    table: str, columns: Iterable[str], arities: tuple[int, ...]
) -> Iterator[CandidateSet]:
    """Yield all CandidateSet combinations of the given sizes for one table."""
    cols = sorted(columns)
    for n in arities:
        if n > len(cols):
            continue
        for combo in combinations(cols, n):
            yield CandidateSet(table=table, columns=combo)


def generate_candidates(
    queries: list[BenchQuery],
    *,
    arities: tuple[int, ...] = (2, 3),
    dedupe: bool = True,
) -> list[CandidateSet]:
    """Build the deduplicated list of candidate statistics across `queries`.

    Parameters
    ----------
    queries : iterable of BenchQuery
        The workload to extract candidates from.
    arities : tuple of int
        Column-combination sizes to generate (default ``(2, 3)``).
    dedupe : bool
        If True, collapse identical ``(table, columns)`` combinations so each
        physical statistic is created only once (default True).

    Returns
    -------
    list of CandidateSet, sorted by (table, columns).
    """
    seen: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    candidates: list[CandidateSet] = []

    for q in queries:
        per_table = predicate_columns(q)
        for tbl, cols in per_table.items():
            for combo in _gen_combinations(tbl, cols, arities):
                if dedupe and combo.columns in seen[tbl]:
                    continue
                seen[tbl].add(combo.columns)
                candidates.append(combo)

    # Deterministic ordering: by table, then by columns.
    candidates.sort(key=lambda c: (c.table, c.columns))
    return candidates

