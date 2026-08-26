"""Mismatch analysis: MILP optimizes *storage* (sum of size_bytes), but the
real maintenance cost is the ANALYZE rebuild, which PostgreSQL ties to the
*macimum* statistics_target on a relation (sampling effort, not storage).

For each budget's MILP solution we report:
  - storage (sum of size_bytes)  -- what the optimizer minimizes
  - per-relation max level chosen -- drives the ANALYZE sampling effort
  - an estimated ANALYZE rebuild cost vs. storage, exposing the mismatch.

No database is touched: everything is derived from the phase-1 JSON (all
measurements already complete) plus the measured ANALYZE anchors reported in
the paper (Sec 5.5 / Table tab:measurement, CENSUS climate):

  ANALYZE@t10000 with 0 stats            ~22 s   (empty base, Sec 5.1/5.4/5.5)
  ANALYZE@t10000 with N stats on table   grows approx. linearly (Sec 5.5)
  ANALYZE-at-N anchors (CENSUS):         1k~420s 3k~1013s 6k~2003s 10k~3507s

Mismatch intuition: storage is additive across (candidate, level); the ANALYZE
rebuild cost is a property of the table's *maximum* target, so two different
budget solutions with identical storage can have very different rebuild cost
if one uses an L10000 statistic and the other tops out at L1000.

Usage:
  python scripts/analyze_storage_analyze_mismatch.py \\
     --input results/phase1_census_mcv_6level.json \\
     --budgets 10000,50000,100000,250000,500000 --table-limit census
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extstats.optimize import build_problem, solve_ilp  # noqa: E402

# Measured ANALYZE anchors (CENSUS climate @ t10000, Sec 5.5 / Table tab:measurement).
# (n_stats_on_table, seconds).  Interpolated linearly for other N.
ANALYZE_ANCHORS_CENSUS_T10000 = [(0, 22.0), (1000, 420.0), (3000, 1013.0),
                                 (6000, 2003.0), (10000, 3507.0)]


def analyze_seconds_at_t10000(n_stats: int) -> float:
    if n_stats <= 0:
        return ANALYZE_ANCHORS_CENSUS_T10000[0][1]
    xs = [a[0] for a in ANALYZE_ANCHORS_CENSUS_T10000]
    ys = [a[1] for a in ANALYZE_ANCHORS_CENSUS_T10000]
    x = float(max(n_stats, 0))
    xp = np.interp(x, xs, ys)
    return float(xp)


# PostgreSQL scales ANALYZE sampling effort with the relation's *maximum*
# statistics_target (Sec 2.2 / Sec 5.5). Base CENSUS ANALYZE: ~22 s @ t10000 and
# "a few seconds" @ t100 -> sampling effort ~ proportional to target. We scale
# the empty-base (n_stats~0) portion of the estimate by target/10000; the
# per-mounted-stat increment is already reflected by the anchors.
TARGET_BASE_SAMPLE_MULT = {100: 0.01, 1000: 0.1, 10000: 1.0,
                           10: 0.001, 25: 0.0025, 50: 0.005}


def estimated_analyze_seconds(n_stats_on_table: int, max_target: int) -> float:
    """Estimated per-table ANALYZE rebuild @ its max target (seconds).

    Base (sampling) cost scales with max_target/10000; the anchor list then
    adds the cost of mounting `n_stats_on_table` statistics. If the table has
    an L10000 stat this is ~22 s base; if it tops out at L1000 the sampling base
    is ~10x smaller. total across distinct tables in the solution.
    """
    mult = TARGET_BASE_SAMPLE_MULT.get(max_target, 1.0)
    base_empty = ANALYZE_ANCHORS_CENSUS_T10000[0][1] * mult  # scaled empty base
    inc = analyze_seconds_at_t10000(n_stats_on_table) - \
        ANALYZE_ANCHORS_CENSUS_T10000[0][1]  # cost of mounting stats
    return base_empty + inc


def total_analyze_seconds(sel) -> float:
    """Sum of per-table ANALYZE rebuild costs for a solution."""
    by_table: dict[str, list[tuple[int, int]]] = {}
    for ps in sel:
        by_table.setdefault(ps.table, []).append((ps.level, ps.cost))
    total = 0.0
    for table, items in by_table.items():
        max_t = max(lev for lev, _ in items)
        n = len(items)
        total += estimated_analyze_seconds(n, max_t)
    return total


def solve_capped(phys_stats, queries_options, qerror_base, budget,
                 max_level, per_query_cap=1):
    """Solve the ILP with option levels capped at max_level (a stand-in for a
    DBA who refuses to deploy >L<max_level> to bound ANALYZE maintenance cost).
    Returns (res, n_total_stats)."""
    capped_opts = [[o for o in qo if o.level <= max_level]
                   for qo in queries_options]
    res = solve_ilp(phys_stats, capped_opts, qerror_base, budget,
                    per_query_cap=per_query_cap)
    return res


def table_max_levels(sel):
    by_table: dict[str, int] = {}
    for ps in sel:
        by_table[ps.table] = max(by_table.get(ps.table, 0), ps.level)
    return by_table


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--budgets",
                    default="10000,50000,100000,250000,500000,1000000")
    args = ap.parse_args(argv)

    phase1 = json.loads(Path(args.input).read_text())
    bench = phase1.get("bench", "?")
    phys_stats, queries_options, qerror_base = build_problem(phase1)

    print(f"bench = {bench}, n_queries = {len(qerror_base)}")
    print("== full-menu vs <<max-target capped>> at the SAME budget ==")
    print(f"{'budget':>9} {'L':>4} {'storage':>10} {'n_stats':>7} {'mean':>8} "
          f"{'maxL':>5} {'anal@t10k':>9}")
    print("-" * 78)

    for B in [int(x) for x in args.budgets.split(",") if x.strip()]:
        # full solution
        res_full = solve_ilp(phys_stats, queries_options, qerror_base, B,
                             per_query_cap=1)
        # capped at L1000
        res_cap = solve_capped(phys_stats, queries_options, qerror_base, B, 1000)
        # capped at L100
        res_cap100 = solve_capped(phys_stats, queries_options, qerror_base, B, 100)

        def _row(tag, res):
            lv = [ps.level for ps in res.selected_stats]
            ml = max(lv) if lv else 0
            an = total_analyze_seconds(res.selected_stats)
            return (f"{tag:>4} {res.total_bytes:>10,} "
                    f"{len(res.selected_stats):>7} {res.mean_qerror:>8.4f} "
                    f"{ml:>5} {an:>9.1f}")

        print(f"{B:>9,} " + _row("full", res_full))
        print(f"{'':>9} " + _row("cap1k", res_cap))
        print(f"{'':>9} " + _row("cap1h", res_cap100))
        # storage / quality / ANALYZE deltas between full and capped-L1000
        d_stor = (res_full.total_bytes - res_cap.total_bytes) / max(res_full.total_bytes, 1)
        d_qual = res_cap.mean_qerror - res_full.mean_qerror
        lv_full = [p.level for p in res_full.selected_stats]
        lv_cap = [p.level for p in res_cap.selected_stats]
        maxd = (max(lv_full) if lv_full else 0) - (max(lv_cap) if lv_cap else 0)
        print(f"{'':>9}  (cap1k: storage {d_stor*100:+.1f}%, mean {d_qual:+.4f}, "
              f"max-level Δ {maxd:+d})")
        print("-" * 78)

    print("an@t10k = est. single ANALYZE rebuild @ max-target 10000 (s)")
    print("mismatch shows when capping the max target barely reduces storage")
    print("but sharply reduces the ANALYZE rebuild cost/quality trade-off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
