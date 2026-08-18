#!/usr/bin/env python3
"""Generate candidate extended statistics for one (or all) benchmarks.

Usage examples
--------------
Activate the venv first::

    source .venv/bin/activate

Dry-run (print DDL without executing) for all benchmarks::

    python scripts/generate_candidates.py --dry-run

Actually create statistics for the census benchmark::

    python scripts/generate_candidates.py --bench census

Create for all benchmarks with database overrides::

    python scripts/generate_candidates.py --bench all \\
        --pguser myuser --dbname imdb

Options
-------
--bench NAME     census | job | stats_ceb | all        (default: all)
--arities N[,N]  combination sizes, e.g. 2,3            (default: 2,3)
--kinds K[,K]    dependencies|ndistinct|mcv, comma sep (default: all three)
--no-dedupe      create per-query stats even if duplicated
--dry-run        print DDL only; do not touch the database
--prefix PREFIX  statistic name prefix                 (default: ext_)
--dbname NAME    database name (overrides benchmark default)
--pguser USER / --pghost HOST / --pgport PORT / (env PGPASSWORD)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extstats.candidates import generate_candidates  # noqa: E402
from extstats.config import (  # noqa: E402
    DEFAULT_DB,
    BENCH_NAMES,
    DBConfig,
    StatsDefaults,
)
from extstats.db import connect  # noqa: E402
from extstats.parsers import (  # noqa: E402
    parse_census_dir,
    parse_job_dir,
    parse_stats_ceb_dir,
    parse_stats_ceb_single_dir,
)
from extstats.stats import build_stats_objects, create_statistics  # noqa: E402

_PARSERS = {
    "census": parse_census_dir,
    "job": parse_job_dir,
    "stats_ceb": parse_stats_ceb_dir,
    "stats_ceb_single": parse_stats_ceb_single_dir,
}

# Map our lowercase bench keys to the actual on-disk directory names.
_BENCH_DIRS = {
    "census": "Census",
    "job": "JOB",
    "stats_ceb": "stats_CEB",
    "stats_ceb_single": "stats_CEB",
}


def _parse_arities(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x.strip())


def _parse_kinds(text: str) -> tuple[str, ...]:
    raw = [k.strip() for k in text.split(",") if k.strip()]
    return tuple(raw)


def load_queries(bench: str, bench_root: Path):
    """Load queries for one benchmark using its parser."""
    queries_dir = bench_root / _BENCH_DIRS[bench] / "queries"
    parser = _PARSERS[bench]
    return parser(queries_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate candidate extended statistics for benchmarking.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--bench", choices=list(BENCH_NAMES) + ["all"], default="all")
    ap.add_argument("--arities", default="2,3", help="combination sizes, comma separated")
    ap.add_argument(
        "--kinds",
        default="dependencies,ndistinct,mcv",
        help="statistic kinds, comma separated",
    )
    ap.add_argument("--no-dedupe", action="store_true", help="do not deduplicate candidates")
    ap.add_argument("--dry-run", action="store_true", help="only print DDL, do not execute")
    ap.add_argument("--prefix", default="ext_", help="statistic object name prefix")
    ap.add_argument("--dbname", default=None, help="database name override")
    ap.add_argument("--pguser", default="postgres")
    ap.add_argument("--pghost", default="localhost")
    ap.add_argument("--pgport", type=int, default=5432)
    ap.add_argument("--no-verbose-ddl", action="store_true", help="do not print generated DDL")
    args = ap.parse_args(argv)

    bench_dir = Path(__file__).resolve().parents[1] / "benchmarks"
    arities = _parse_arities(args.arities)
    kinds = _parse_kinds(args.kinds)

    defaults = StatsDefaults(
        arities=arities,
        kinds=kinds,
        dedupe=not args.no_dedupe,
        dry_run=args.dry_run,
        stats_name_prefix=args.prefix,
        verbose_ddl=not args.no_verbose_ddl,
    )

    benches = BENCH_NAMES if args.bench == "all" else [args.bench]

    for bench in benches:
        # Database for this benchmark.
        dbname = args.dbname or DEFAULT_DB[bench]
        cfg = DBConfig(
            host=args.pghost,
            port=args.pgport,
            user=args.pguser,
            dbname=dbname,
        )

        queries = load_queries(bench, bench_dir)
        candidates = generate_candidates(queries, arities=arities, dedupe=defaults.dedupe)
        objects = build_stats_objects(candidates, defaults)

        print(f"\n=== benchmark: {bench} (db={dbname}) ===")
        print(f"loaded {len(queries)} queries, {len(candidates)} candidate combinations, "
              f"{len(objects)} statistic objects")

        if objects:
            if defaults.dry_run:
                print(f"-- dry run; not creating statistics on {dbname}")
                create_statistics(None, objects, defaults)
            else:
                with connect(cfg) as conn:
                    create_statistics(conn, objects, defaults)
        else:
            print("  (no candidates)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
