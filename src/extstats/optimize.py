"""Phase-2 MILP: choose a budgeted set of extended-statistics (combo, capacity)
options that minimises the workload's average q-error.

Model
-----
Input (from a phase-1 results JSON):
  - queries i = 1..m, each with a baseline q-error ``qerror_base`` and a set of
    candidate options O_i. Each option k corresponds to a physical statistic
    ``s(k) = (table, columns, capacity)`` and carries a q-error ``e_ik``.
  - physical statistics s in S, each with storage cost ``c_s`` (size_bytes).
  - global storage budget C.

Decision variables (all binary):
  - y_s   : create physical statistic s
  - x_ik  : query i selects option k

Constraints:
  1) storage budget       : sum_s c_s * y_s <= C
  2) at most one option   : sum_{k in O_i} x_ik <= 1   (query i, else baseline)
  3) select only created  : x_ik <= y_{s(k)}           (share a physical stat)

Objective (minimise mean q-error):
    minimise (1/m) * sum_i t_i
where t_i = qerror_base_i + sum_{k in O_i} (e_ik - qerror_base_i) * x_ik,
which is exact because sum_k x_ik <= 1 makes at most one term nonzero.

Because y_s is shared across queries (constraint 3) but paid once in the budget
(constraint 1), multiple queries reusing the same statistic pay its storage
only once — the "shared resource" semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

# Upper bound used for the implicit binary boxes (<=1) of y variables.
_BIG = 1.0


@dataclass(frozen=True)
class Option:
    """One (candidate, capacity) selection available to a query."""

    # Index into physical statistic list (which carries storage cost).
    stat_index: int
    # q-error if this query selects this option.
    qerror: float
    # query id (informational)
    query: str = ""
    # candidate key (table(columns)) informational
    cand: str = ""
    # capacity level (informational)
    level: int = 0


@dataclass(frozen=True)
class PhysicalStat:
    """A unique physical statistic (table, columns, capacity) with a cost."""

    table: str
    columns: tuple[str, ...]
    level: int
    # storage cost in bytes
    cost: int
    # a readable key
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

    # optimal mean q-error
    mean_qerror: float
    # per-query achieved q-error
    qerror_per_query: list[float]
    # baseline per-query q-error (pre)
    baseline_per_query: list[float]
    # selected physical statistics (created)
    selected_stats: list[PhysicalStat]
    # total storage used
    total_bytes: int
    # per-query chosen option (stat key or None for baseline)
    chosen: list[Optional[str]]
    # solver status / message
    status: int
    message: str


def build_problem(
    phase1: dict,
    budget_bytes: int,
    *,
    skip_worse_than_baseline: bool = True,
) -> tuple[list[Option], list[PhysicalStat], list[list[Option]], list[float]]:
    """Build (options, phys_stats, queries_options, qerror_base) from a phase-1
    results dict.

    Parameters
    ----------
    phase1 : loaded phase-1 JSON (keys: results, and each result has
        qerror_base + candidates{... levels{...}})
    budget_bytes : storage budget (kept for reference; returned stats only)
    skip_worse_than_baseline : if True, drop options with qerror >= baseline
        (they can never be chosen in the optimum, shrinking the problem).
    """
    results = phase1["results"]
    qerror_base: list[float] = []
    queries_options: list[list[Option]] = []
    all_options: list[Option] = []
    # phys stat unique key -> index
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
                # Option that is worse (or equal) than baseline is dominated;
                # skip unless caller wants it.
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
                o = Option(
                    stat_index=stat_index[stat_key],
                    qerror=qerr,
                    query=str(r["qid"]),
                    cand=cand_key,
                    level=level,
                )
                opts.append(o)
                all_options.append(o)
        queries_options.append(opts)

    return all_options, phys_stats, queries_options, qerror_base


def solve_ilp(
    all_options: list[Option],
    phys_stats: list[PhysicalStat],
    queries_options: list[list[Option]],
    qerror_base: list[float],
    budget_bytes: int,
) -> ILPResult:
    """Solve the shared-resource selection ILP with scipy.optimize.milp.

    Variables:
      0 .. n_stats-1           : y_s (create physical stat)
      n_stats .. n_stats+n_opt : x_ik (query selects option)
    """
    n_stats = len(phys_stats) if phys_stats else 0
    n_opt = len(all_options)
    n_var = n_stats + n_opt

    # Map (query, option) -> x variable index.
    # all_options is in the same order as encountered while building per-query lists
    # but queries_options references Option objects; we need a position map.
    # Rebuild a global position index for options in all_options.
    opt_index = {id(o): i for i, o in enumerate(all_options)}

    # ---- objective coefficients ----
    # Minimise mean q-error: c = (1/m) * delta_ik on x vars (0 on y vars),
    # plus constant sum(qerror_base)/m which milp handles via offset? milp has
    # no integer offset; we add it back in the result.
    m = len(qerror_base)
    c = np.zeros(n_var)
    for q_idx, opts in enumerate(queries_options):
        base = qerror_base[q_idx]
        for o in opts:
            vi = opt_index[id(o)]
            c[n_stats + vi] = (o.qerror - base) / m
    integrality = np.ones(n_var)  # all binary

    # ---- constraints ----
    n_con = 1 + m + n_opt  # budget + query-cardinality + select-only-created
    A = lil_matrix((n_con, n_var))
    lb = np.full(n_con, -np.inf)
    ub = np.full(n_con, np.inf)
    row = 0

    # 1) storage budget: sum c_s y_s <= C
    for s_idx, ps in enumerate(phys_stats):
        A[row, s_idx] = ps.cost
    ub[row] = budget_bytes
    lb[row] = -np.inf
    row += 1

    # 2) per-query at most one option: sum x_ik <= 1
    for q_idx, opts in enumerate(queries_options):
        for o in opts:
            A[row, n_stats + opt_index[id(o)]] = 1.0
        ub[row] = 1.0
        lb[row] = -np.inf
        row += 1

    # 3) x_ik <= y_{s(k)}  =>  x_ik - y_{s(k)} <= 0
    for o in all_options:
        vi = n_stats + opt_index[id(o)]
        A[row, vi] = 1.0
        A[row, o.stat_index] = -1.0
        ub[row] = 0.0
        lb[row] = -np.inf
        row += 1

    constraints = LinearConstraint(
        A.tocsr(), lb=np.array(lb), ub=np.array(ub)
    )
    bounds = Bounds(lb=np.zeros(n_var), ub=np.ones(n_var))

    res = milp(
        c=c,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
    )

    # ---- decode ----
    if res.x is None:
        raise RuntimeError(f"ILP failed: {res.message}")

    x = res.x
    selected = [
        phys_stats[s_idx]
        for s_idx in range(n_stats)
        if x[s_idx] > 0.5
    ]
    total_bytes = int(sum(ps.cost for ps in selected))

    qerr_per_query: list[float] = []
    chosen: list[Optional[str]] = []
    mean = 0.0
    for q_idx, opts in enumerate(queries_options):
        base = qerror_base[q_idx]
        t = base
        chosen_key: Optional[str] = None
        for o in opts:
            if x[n_stats + opt_index[id(o)]] > 0.5:
                t = o.qerror
                chosen_key = phys_stats[o.stat_index].key
                break
        qerr_per_query.append(t)
        mean += t
        chosen.append(chosen_key)
    mean /= m

    return ILPResult(
        mean_qerror=mean,
        qerror_per_query=qerr_per_query,
        baseline_per_query=list(qerror_base),
        selected_stats=selected,
        total_bytes=total_bytes,
        chosen=chosen,
        status=int(res.status),
        message=str(res.message),
    )
