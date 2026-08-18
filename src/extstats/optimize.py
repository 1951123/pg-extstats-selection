"""Phase-2 MILP: choose a budgeted set of extended-statistics (combo, capacity)
options that minimises the workload's average q-error.

Model (multi-select, multiplicative approximation)
--------------------------------------------------
A query may select ANY subset of its candidate statistics (powerset semantics):
multiple independent column-combinations can all be built for the same query.
Because measuring every subset empirically is infeasible, we approximate the
joint effect multiplicatively in log space:

    log e_i(T_i)  ≈  log e_i^0 + sum_{s in T_i} log(e_is / e_i^0)

This is exact when each statistic's effect on the query is independent (which
holds when the chosen combinations do not share columns). To keep the
approximation valid we forbid selecting column-overlapping statistics *within
a single query*, so the terms are independent.

Variables (all binary):
  - y_s   : create physical statistic s (table, columns, capacity)
  - x_is  : query i selects statistic s

Objective (minimise mean q-error; equivalently sum of log-q-error since the
log is monotone and the common offset is constant):

    sum_i log e_i^0            <- constant
  + sum_{i,s} w_is * x_is      <- w_is = log(e_is/e_i^0) <= 0

Constraints:
  1) storage budget  : sum_s c_s * y_s <= C
  2) select created  : x_is <= y_s                     (all queries share y_s)
  3) overlap-free    : within each query, column-overlapping stats can't both
                       be chosen: x_{i s_a} + x_{i s_b} <= 1 for overlapping
                       pairs (keeps the multiplicative approximation valid).

Because y_s is shared across queries (2) but paid once in the budget (1),
multiple queries reusing the same statistic pay its storage only once — the
"shared resource" semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


@dataclass(frozen=True)
class Option:
    """One (candidate, capacity) selection available to a query."""

    # Index into physical statistic list (which carries storage cost).
    stat_index: int
    # q-error if this query selects this option (EFFECTIVE, not log).
    qerror: float
    # capacity level
    level: int
    # informational fields
    query: str = ""
    cand: str = ""

    def log_improvement(self, qbase: float) -> float:
        """w = log(e_is / e_i^0), <= 0 when the stat helps."""
        return float(np.log(max(self.qerror, 1e-12) / max(qbase, 1e-12)))


@dataclass(frozen=True)
class PhysicalStat:
    """A unique physical statistic (table, columns, capacity) with a cost."""

    table: str
    columns: tuple[str, ...]
    level: int
    # storage cost in bytes
    cost: int
    key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "key",
            f"{self.table}|{','.join(self.columns)}|L{self.level}",
        )


@dataclass
class ILPResult:
    """Solution of the phase-2 ILP."""

    mean_qerror: float
    # per-query achieved q-error (multiplicative approximation)
    qerror_per_query: list[float]
    baseline_per_query: list[float]
    selected_stats: list[PhysicalStat]
    total_bytes: int
    # per-query chosen stat keys (list may be empty -> baseline)
    chosen: list[list[str]]
    status: int
    message: str


def build_problem(
    phase1: dict,
    *,
    skip_worse_than_baseline: bool = True,
) -> tuple[list[PhysicalStat], list[list[Option]], list[float]]:
    """Build (phys_stats, queries_options, qerror_base) from a phase-1 dict.

    Options with q-error >= baseline are dropped (they can never help under the
    multiplicative model: log(e_is/e_i0) >= 0 only increases the objective).

    Parameters
    ----------
    phase1 : loaded phase-1 JSON with key "results".
    skip_worse_than_baseline : drop dominated options (default True).
    """
    results = phase1["results"]
    qerror_base: list[float] = []
    queries_options: list[list[Option]] = []

    stat_index: dict[str, int] = {}
    phys_stats: list[PhysicalStat] = []

    for r in results:
        base = float(r["qerror_base"])
        qerror_base.append(base)
        opts: list[Option] = []
        for cand_key, cand in r.get("candidates", {}).items():
            cols = tuple(cand["columns"])
            table = cand["table"]
            for level_str, lv in cand.get("levels", {}).items():
                level = int(level_str)
                qerr = float(lv["qerror"])
                if skip_worse_than_baseline and qerr >= base:
                    continue
                stat_key = f"{table}|{','.join(cols)}|L{level}"
                if stat_key not in stat_index:
                    stat_index[stat_key] = len(phys_stats)
                    phys_stats.append(
                        PhysicalStat(
                            table=table,
                            columns=cols,
                            level=level,
                            cost=int(lv["size_bytes"]),
                        )
                    )
                opts.append(
                    Option(
                        stat_index=stat_index[stat_key],
                        qerror=qerr,
                        level=level,
                        query=str(r["qid"]),
                        cand=cand_key,
                    )
                )
        queries_options.append(opts)

    return phys_stats, queries_options, qerror_base


def _overlap_pairs(query_options: list[Option], phys_stats: list[PhysicalStat]):
    """Yield pairs of option indices (within one query) whose stats share a
    column, so at most one can be chosen (keeps multiplicative approx valid)."""
    for a in range(len(query_options)):
        cols_a = set(phys_stats[query_options[a].stat_index].columns)
        for b in range(a + 1, len(query_options)):
            cols_b = set(phys_stats[query_options[b].stat_index].columns)
            if cols_a & cols_b:
                yield a, b


def solve_ilp(
    phys_stats: list[PhysicalStat],
    queries_options: list[list[Option]],
    qerror_base: list[float],
    budget_bytes: int,
) -> ILPResult:
    """Solve the multi-select shared-resource ILP with scipy.optimize.milp.

    Variables:
      0 .. n_stats-1            : y_s (create physical stat)
      n_stats .. n_stats+n_opt  : x_is (query selects option)
    """
    n_stats = len(phys_stats)
    n_opt = sum(len(opts) for opts in queries_options)
    n_var = n_stats + n_opt
    m = len(qerror_base)

    # ---- objective: sum log e_i = const + sum w_is x_is ----
    c = np.zeros(n_var)
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        for o in opts:
            c[n_stats + gi] = o.log_improvement(qbase)
            gi += 1
    integrality = np.ones(n_var)

    # ---- constraints ----
    # 1) storage budget
    # 2) x_is <= y_s
    # 3) overlap-free within each query (x_a + x_b <= 1)
    n_overlap = 0
    for opts in queries_options:
        n_overlap += len(list(_overlap_pairs(opts, phys_stats)))
    n_con = 1 + n_opt + n_overlap

    A = lil_matrix((n_con, n_var))
    ub = np.full(n_con, np.inf)
    nrow = 0

    # 1) storage budget: sum_s c_s y_s <= C
    for s_idx, ps in enumerate(phys_stats):
        A[nrow, s_idx] = ps.cost
    ub[nrow] = budget_bytes
    nrow += 1

    # 2) x_is - y_{s} <= 0
    gi = 0
    for opts in queries_options:
        for o in opts:
            A[nrow, n_stats + gi] = 1.0
            A[nrow, o.stat_index] = -1.0
            ub[nrow] = 0.0
            gi += 1
            nrow += 1

    # 3) overlap-free within query: x_a + x_b <= 1
    gi = 0
    for opts in queries_options:
        for a, b in _overlap_pairs(opts, phys_stats):
            A[nrow, n_stats + gi + a] = 1.0
            A[nrow, n_stats + gi + b] = 1.0
            ub[nrow] = 1.0
            nrow += 1
        gi += len(opts)

    constraints = LinearConstraint(A.tocsr(), lb=np.full(n_con, -np.inf), ub=ub)
    bounds = Bounds(lb=np.zeros(n_var), ub=np.ones(n_var))

    res = milp(c=c, integrality=integrality, bounds=bounds, constraints=constraints)
    if res.x is None:
        raise RuntimeError(f"ILP failed: {res.message}")

    x = res.x
    selected = [phys_stats[s_idx] for s_idx in range(n_stats) if x[s_idx] > 0.5]
    total_bytes = int(sum(ps.cost for ps in selected))

    # decode per-query chosen stats + approximate (multiplicative) qerror
    qerr_per_query: list[float] = []
    chosen: list[list[str]] = []
    gi = 0
    for q_idx, opts in enumerate(queries_options):
        qbase = qerror_base[q_idx]
        log_t = np.log(max(qbase, 1e-12))
        sel_keys: list[str] = []
        for j, o in enumerate(opts):
            if x[n_stats + gi + j] > 0.5:
                log_t += o.log_improvement(qbase)
                sel_keys.append(phys_stats[o.stat_index].key)
        qerr_per_query.append(float(np.exp(log_t)))
        chosen.append(sel_keys)
        gi += len(opts)

    mean_qerror = float(np.mean(qerr_per_query))
    return ILPResult(
        mean_qerror=mean_qerror,
        qerror_per_query=qerr_per_query,
        baseline_per_query=list(qerror_base),
        selected_stats=selected,
        total_bytes=total_bytes,
        chosen=chosen,
        status=int(res.status),
        message=str(res.message),
    )
