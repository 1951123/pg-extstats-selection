"""Census query parser.

Format (one query per line in `query.sql`)::

    SELECT COUNT(*) FROM climate WHERE ...||398593
    SELECT COUNT(*) FROM climate WHERE ...||1169696

Each line is ``<sql>||<ground truth cardinality>``. The ground truth is the
true number of rows matching the WHERE predicates.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery


def parse_census_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load Census queries from `queries_dir` (must contain `query.sql`)."""
    path = queries_dir / "query.sql"
    if not path.exists():
        raise FileNotFoundError(f"Census query file not found: {path}")

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            # Split on the LAST `||` so the SQL body (which never contains `||`)
            # is kept intact and the trailing integer is the ground truth.
            if "||" not in line:
                raise ValueError(f"Census query line {line_no}: missing '||' separator: {line!r}")
            sql, _, truth = line.rpartition("||")
            sql = sql.strip()
            truth = int(truth.strip())
            queries.append(
                BenchQuery(
                    bench="census",
                    qid=f"query.{line_no}",
                    sql=sql,
                    ground_truth=truth,
                )
            )
    return queries
