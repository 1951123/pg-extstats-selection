"""Extract per-base-table predicate columns from a query using sqlglot.

Extended statistics in PostgreSQL apply only to *base-table column combines
for selection predicates* — they are not used for join predicates. Therefore,
for a given query we:

  1. parse the SQL into an AST (sqlglot, dialect=postgres),
  2. resolve the FROM clause so we know each alias -> (schema, table),
  3. walk the WHERE predicates and, for every base-table column referenced by
     a *selection* condition, record (schema.table, column),
  4. group the referenced columns by base table.

Join predicates (``a.id = b.id``) reference columns of two tables but are
excluded: we only keep columns that appear in a **selection** predicate
(``col <op> <literal>``, ``col IN (...)``, ``col IS NULL``, ...) against a
constant, not equality between two relation columns.

Returns, per query: ``{qualified_table: frozenset[column]}``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp

# Operators that indicate a *selection* predicate with a constant value.
# (IS NULL / IS NOT NULL are represented as `Is` with a null-literal operand.)
_SELECTION_OPS = {
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.In,
    exp.Like, exp.ILike, exp.Between, exp.Is, exp.Contains,
}


@dataclass
class TableRef:
    """A resolved relation in the FROM clause."""

    schema: Optional[str]
    table: str
    alias: Optional[str]
    # Canonical key for grouping: schema-qualified table name with schema=None
    # treated as the empty string.
    key: str = field(init=False)

    def __post_init__(self) -> None:
        self.key = f"{self.schema or ''}.{self.table}"


def _resolve_column(column: exp.Column, aliases: dict[str, TableRef]) -> Optional[TableRef]:
    """Map a sqlglot Column to its base table via the FROM aliases.

    If the column is unqualified it cannot be reliably attributed to one of
    several joined tables, so we return None (caller may still record it for
    single-table queries).
    """
    if column.table is None:
        return None
    ref = aliases.get(column.table) or aliases.get(f"({column.table})")
    return ref


def table_aliases(query: BenchQuery) -> dict[str, TableRef]:
    """Parse the FROM clause and return alias -> TableRef mapping."""
    ast = sqlglot.parse_one(query.sql, read="postgres")
    aliases: dict[str, TableRef] = {}

    for table in ast.find_all(exp.Table):
        alias = table.alias or table.name
        ref = TableRef(
            schema=table.db or table.catalog,
            table=table.name,
            alias=alias,
        )
        aliases[alias] = ref
        # Also register the bare table name as an alias (e.g. `users` w/o alias).
        aliases.setdefault(table.name, ref)
    return aliases


def predicate_columns(query: BenchQuery) -> dict[str, set[str]]:
    """Return {qualified_table: set(column)} of base-table columns constrained
    by selection predicates in the query's WHERE clause.

    Only *selection* predicates are considered (a single column compared
    against a constant / literal). Join predicates (two columns compared to
    each other) are excluded, matching PostgreSQL's extended-statistics
    semantics.

    When a query references a single base table (as in Census), unqualified
    columns with no ``alias.`` prefix are attributed to that one table. With
    multiple tables, unqualified columns are ambiguous and are skipped.
    """
    ast = sqlglot.parse_one(query.sql, read="postgres")
    aliases = table_aliases(query)
    where = ast.find(exp.Where)
    result: dict[str, set[str]] = defaultdict(set)

    if where is None:
        return dict(result)

    # If exactly one distinct base table appears, unqualified columns belong to it.
    unique_tables = {ref.key for ref in aliases.values()}
    single_table = unique_tables.pop() if len(unique_tables) == 1 else None

    for node in where.walk():
        if not isinstance(node, tuple(_SELECTION_OPS)):
            continue
        cols = [c for c in node.find_all(exp.Column)]
        # Resolve each column to a base table key (or None if unqualified).
        keys = {(_resolve_column(c, aliases).key if _resolve_column(c, aliases) else None)
                for c in cols}
        keys.discard(None)

        if len(keys) == 1:
            target = keys.pop()
        elif len(keys) == 0 and single_table is not None:
            # Single-table query, unqualified columns -> lone table.
            target = single_table
        else:
            # Multi-table + ambiguous, or a join predicate between two tables.
            continue

        for c in cols:
            result[target].add(c.name)
    return dict(result)

