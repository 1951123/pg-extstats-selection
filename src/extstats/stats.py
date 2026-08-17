"""Generate (and optionally execute) PostgreSQL extended-statistics DDL.

For each candidate combination we emit, by default, three kinds of extended
statistics where each is a separate ``CREATE STATISTICS`` object:

  - dependencies : 'd' prefix (in name), detects functional dependencies
  - ndistinct    : 'n' prefix, estimates distinct value combinations
  - mcv          : 'm' prefix, most-common-values list

A single statistic object in PostgreSQL 16 may *also* carry multiple kinds
(``CREATE STATISTICS s ON a, b, c FROM t`` with an implicit default of
``(dependencies, ndistinct, mcv)``), but we keep them separate by default so
each kind can be created/dropped/tested independently. Set ``merge_kinds=True``
to emit one object per combination holding all requested kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .candidates import CandidateSet
from .config import StatsDefaults
from .db import Connection, execute

# Prefix map for each statistic kind, used to build readable object names.
_KIND_PREFIX = {
    "dependencies": "d",
    "ndistinct": "n",
    "mcv": "m",
}


@dataclass(frozen=True)
class StatsObject:
    """A concrete CREATE STATISTICS object to be created."""

    # Qualified table the statistics live on.
    table: str
    # Sorted tuple of columns.
    columns: tuple[str, ...]
    # List of statistic kinds (subset of dependencies/ndistinct/mcv).
    kinds: tuple[str, ...]
    # Fully-qualified object name chosen for the statistic.
    name: str

    def ddl(self) -> str:
        """Return the CREATE STATISTICS statement for this object."""
        kind_clause = f" ({', '.join(self.kinds)})" if self.kinds else ""
        cols = ", ".join(self.columns)
        return f"CREATE STATISTICS {self.name} {kind_clause} ON {cols} FROM {self.table}"


def _qualify_table(table: str) -> str:
    """Qualify a bare table name if it is not already schema-qualified.

    Column keys from :mod:`extstats.predicates` are ``f"{schema}.{table}"``;
    with no schema they look like ``".climate"``. PostgreSQL treats an
    unqualified table name as being on the current search_path, so we simply
    strip a leading dot.
    """
    return table.lstrip(".") or table


def _stat_name(candidate: CandidateSet, kind: str, prefix: str) -> str:
    """Build a deterministic, human-readable statistic object name.

    Example: with prefix ``ext_`` and pair ``(a, b)`` on table ``t``,
    dependencies -> ``ext_d_t_a_b`` (attributes the table + columns).
    """
    tbl = candidate.table_unqualified
    cols = "_".join(candidate.columns)
    kind_tag = _KIND_PREFIX.get(kind, "x")
    return f"{prefix}{kind_tag}_{tbl}_{cols}"


def build_stats_objects(
    candidates: list[CandidateSet],
    defaults: StatsDefaults,
) -> list[StatsObject]:
    """Turn candidate combinations into concrete StatsObject(s).

    Each candidate yields one StatsObject per requested kind (unless
    ``defaults.merge_kinds`` were set, which this implementation does not
    currently enable — kinds are always split into separate objects).
    """
    objects: list[StatsObject] = []
    for cand in candidates:
        table = _qualify_table(cand.table)
        for kind in defaults.kinds:
            name = _stat_name(cand, kind, defaults.stats_name_prefix)
            objects.append(
                StatsObject(
                    table=table,
                    columns=cand.columns,
                    kinds=(kind,),
                    name=name,
                )
            )
    return objects


def create_statistics(
    conn: Optional[Connection],
    objects: list[StatsObject],
    defaults: StatsDefaults,
) -> None:
    """Create the given statistic objects in the database.

    Runs ``CREATE STATISTICS`` for each object. If ``defaults.dry_run`` is True
    (or ``conn`` is None), no SQL is executed — only the DDL is printed.
    """
    for obj in objects:
        ddl = obj.ddl()
        if defaults.verbose_ddl:
            print(ddl)
        if not defaults.dry_run and conn is not None:
            execute(conn, ddl)
