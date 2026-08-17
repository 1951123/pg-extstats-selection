"""stats_CEB query parser.

Format (one query per line in `stats_CEB.sql`)::

    79851||SELECT COUNT(*) FROM badges as b, users as u WHERE ...

Each line is ``<ground_truth>||<sql>``. The leading integer is the **true
cardinality** of the query (verified against the loaded `stats` database on
PostgreSQL 16), so it is both used as the query id and stored in
``ground_truth``.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery


def parse_stats_ceb_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load stats_CEB queries from `queries_dir` (must contain `stats_CEB.sql`).

    The ``||``-prefix is the query's true cardinality:
      - it becomes the ``qid`` (a short identifier), and
      - it is stored as ``ground_truth`` for q-error evaluation.
    """
    path = queries_dir / "stats_CEB.sql"
    if not path.exists():
        raise FileNotFoundError(f"stats_CEB query file not found: {path}")

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if "||" not in line:
                raise ValueError(
                    f"stats_CEB query line {line_no}: missing '||' separator: {line!r}"
                )
            prefix, _, sql = line.partition("||")
            prefix = prefix.strip()
            sql = sql.strip()
            queries.append(
                BenchQuery(
                    bench="stats_ceb",
                    qid=prefix,
                    sql=sql,
                    ground_truth=int(prefix),
                )
            )
    return queries
