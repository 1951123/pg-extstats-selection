"""stats_CEB single-table (sub-plan) query parser.

Each line in ``stats_CEB_single_table.sql`` looks like::

    SELECT COUNT(*) FROM badges as b;||0||79851
    SELECT COUNT(*) FROM users as u WHERE u.UpVotes>=0;||0||40325

i.e. ``<sql>||<subplan_id>||<ground_truth>``. The SQL is a SINGLE-table
selection-predicate query (no joins), extracted from per-table sub-plans of the
multi-table stats_CEB workload. Because they contain only selection predicates
(no join conditions), extended statistics — which cannot be used for join
estimation — are fully applicable here, making this a good 'Census-like' single
table benchmark with the same schema as the join workload.
"""

from __future__ import annotations

from pathlib import Path

from .base import BenchQuery


def parse_stats_ceb_single_dir(queries_dir: Path) -> list[BenchQuery]:
    """Load single-table stats_CEB queries from ``stats_CEB_single_table.sql``.

    Each line is ``<sql>||<subplan_id>||<ground_truth>``. We take the trailing
    integer as the true cardinality, and build a unique ``qid`` from the
    line/sql hash to avoid collisions with the subplan id (which repeats).
    """
    path = queries_dir / "stats_CEB_single_table.sql"
    if not path.exists():
        raise FileNotFoundError(
            f"stats_CEB single-table query file not found: {path}")

    queries: list[BenchQuery] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if "||" not in line:
                raise ValueError(
                    f"stats_CEB single-table line {line_no}: missing '||': {line!r}"
                )
            # <sql> || <subplan_id> || <ground_truth>
            parts = line.split("||")
            sql = parts[0].strip()
            # ground truth is the LAST field
            truth = int(parts[-1].strip())
            # use the single-table prefix + line number for a unique stable qid
            qid = f"st.{line_no}"
            queries.append(
                BenchQuery(
                    bench="stats_ceb_single",
                    qid=qid,
                    sql=sql,
                    ground_truth=truth,
                )
            )
    return queries
