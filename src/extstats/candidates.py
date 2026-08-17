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

Granularity
-----------
Two entry points are provided:

  - :func:`generate_candidates_per_query` groups candidates **by query**
    (dedup only *within* a query). This is the granularity of interest when
    evaluating how much a *single query's* own extended statistics improve its
    cardinality estimates.
  - :func:`generate_candidates` collapses identical ``(table, columns)``
    combinations **across the whole workload** so each physical statistic is
    created only once (useful when creating statistics centrally).

PostgreSQL treats column order within a combination as insignificant for
dependencies / ndistinct / mcv, so combinations are always stored/sorted in a
canonical (alphabetical) order.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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


def _query_candidates(
    query: BenchQuery, arities: tuple[int, ...]
) -> list[CandidateSet]:
    """Generate the (query-local) candidate set list for a single query.

    Deduplicates combinations *within* the query (a given table+columns combo
    is emitted at most once), but does NOT coordinate with other queries.
    """
    per_table = predicate_columns(query)
    seen: set[tuple[str, tuple[str, ...]]] = set()
    result: list[CandidateSet] = []
    for tbl, cols in per_table.items():
        for combo in _gen_combinations(tbl, cols, arities):
            key = (tbl, combo.columns)
            if key in seen:
                continue
            seen.add(key)
            result.append(combo)
    result.sort(key=lambda c: (c.table, c.columns))
    return result


def generate_candidates_per_query(
    queries: list[BenchQuery],
    *,
    arities: tuple[int, ...] = (2, 3),
) -> dict[str, list[CandidateSet]]:
    """Return ``{query_id: [CandidateSet, ...]}`` grouped by query.

    Candidates are deduplicated *within* each query but **not** across
    queries, so every query gets its own independent candidate list. This is
    the granularity to use for single-query extended-statistics experiments.

    The ordering of ``queries`` is preserved for the dict (insertion order),
    and each query's list is sorted deterministically.
    """
    mapping: dict[str, list[CandidateSet]] = {}
    for q in queries:
        mapping[q.qid] = _query_candidates(q, arities)
    return mapping


def generate_candidates(
    queries: list[BenchQuery],
    *,
    arities: tuple[int, ...] = (2, 3),
    dedupe: bool = True,
) -> list[CandidateSet]:
    """Build the list of candidate statistics across the whole workload.

    Parameters
    ----------
    queries : iterable of BenchQuery
        The workload to extract candidates from.
    arities : tuple of int
        Column-combination sizes to generate (default ``(2, 3)``).
    dedupe : bool
        If True (default), collapse identical ``(table, columns)``
        combinations across the workload so each physical statistic is
        created only once. If False, keep every (per-query) occurrence.

    Returns
    -------
    list of CandidateSet, sorted by (table, columns).
    """
    seen: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    candidates: list[CandidateSet] = []

    for q in queries:
        for combo in _query_candidates(q, arities):
            if dedupe and combo.columns in seen[combo.table]:
                continue
            seen[combo.table].add(combo.columns)
            candidates.append(combo)

    # Deterministic ordering: by table, then by columns.
    candidates.sort(key=lambda c: (c.table, c.columns))
    return candidates

