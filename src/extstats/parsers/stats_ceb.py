"""stats_CEB query parser.

Format (one query per line in `stats_CEB.sql`)::

    79851||SELECT COUNT(*) FROM badges as b, users as u WHERE ...

Each line is ``<queryID>||<sql>``. Unlike Census, stats_CEB does NOT include
ground-truth cardinality inline in the file, so ``ground_truth`` stays None.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery


def parse_stats_ceb_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load stats_CEB queries from `queries_dir` (must contain `stats_CEB.sql`)."""
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
            qid, _, sql = line.partition("||")
            qid = qid.strip()
            sql = sql.strip()
            queries.append(
                BenchQuery(
                    bench="stats_ceb",
                    qid=qid,
                    sql=sql,
                    ground_truth=None,
                )
            )
    return queries
