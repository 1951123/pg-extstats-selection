"""JOB (Join Order Benchmark) query parser.

Format: one query per ``*.sql`` file under `queries_dir` (e.g. ``1a.sql``).
File names become query ids. The benchmark data files do not ship ground-truth
cardinalities, so ``ground_truth`` is None here.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery, iter_sql_files


def parse_job_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load JOB queries from `queries_dir` (each query in its own *.sql file)."""
    queries: list[BenchQuery] = []
    for path in iter_sql_files(queries_dir):
        sql = path.read_text(encoding="utf-8").strip()
        if not sql:
            continue
        queries.append(
            BenchQuery(
                bench="job",
                qid=path.stem,
                sql=sql,
                ground_truth=None,
            )
        )
    return queries
