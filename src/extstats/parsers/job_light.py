"""job-light (join sub-plan) query parser.

Reads the cardinality-estimation form of JOB used by the learned-CE
community (from `End-to-End-CardEst-Benchmark`): each line is a
`SELECT COUNT(*)` join sub-plan with an embedded true cardinality::

    <sql>||<subplan_id>||<ground_truth>

The leading `SELECT COUNT(*)` form is exactly what the measurement pipeline
expects, and the trailing integer is the true join-result cardinality, so it
is stored in ``ground_truth`` for q-error evaluation.

The ``subplan_id`` repeats across the 70 top-level queries (each multi-table
combination of one query is a separate sub-plan), so we build a unique
``qid`` from the line number, mirroring the stats_CEB single-table parser.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery


def parse_job_light_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load job-light join sub-plans from `queries_dir`.

    Reads `job_light_sub_query_with_star_join.sql` (each line
    ``<sql>||<subplan_id>||<ground_truth>``). Ground truth is the LAST field.
    """
    filename = "job_light_sub_query_with_star_join.sql"
    path = queries_dir / filename
    if not path.exists():
        raise FileNotFoundError(
            f"job-light query file not found: {path} "
            f"(expected {filename!r})")

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if "||" not in line:
                raise ValueError(
                    f"job-light line {line_no}: missing '||' separator: {line!r}"
                )
            parts = line.split("||")
            sql = parts[0].strip()
            truth = int(parts[-1].strip())
            # subplan_id in parts[1] repeats; use a unique stable qid
            qid = f"jl.{line_no}"
            queries.append(
                BenchQuery(
                    bench="job_light",
                    qid=qid,
                    sql=sql,
                    ground_truth=truth,
                )
            )
    return queries


def parse_job_light_full_dir(queries_dir: Path, truth: dict[str, int] | None = None) -> list[BenchQuery]:
    """Load the full 70-query job-light workload from `job_light_queries.sql`.

    Unlike the sub-plan file, these queries ship **no** embedded ground truth
    (each line is just ``SELECT COUNT(*) ...;``). True cardinalities must be
    supplied via ``truth`` (a ``{qid: count}`` mapping, e.g. loaded from
    ``results/job_light_truth.json``), matching the convention of the other
    benchmarks after their first measurement pass.
    """
    path = queries_dir / "job_light_queries.sql"
    if not path.exists():
        raise FileNotFoundError(f"job-light query file not found: {path}")

    queries: list[BenchQuery] = []
    truth = truth or {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            sql = line.rstrip(";").strip()
            qid = f"jl.{line_no}"
            queries.append(
                BenchQuery(
                    bench="job_light",
                    qid=qid,
                    sql=sql,
                    ground_truth=truth.get(qid),
                )
            )
    return queries
