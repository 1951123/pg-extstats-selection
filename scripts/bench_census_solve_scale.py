"""Phase-2 SOLVE-scale benchmark on FULL Census (all candidates, all levels).

Measures whether build_problem + solve_ilp can construct and solve the MILP at
full (pre-prune) Census scale: n_stats = combos x levels (~57.7K), n_opt = sum
over 468 queries of (#candidate x #levels). We DO NOT prune (skip_worse_than_
baseline=False) so this is the worst-case upper bound on solver cost — pruning
can only shrink it. Per-option q-error/size are synthesized but problem SIZE and
solver time are purely structural, so this measures the true solve bottleneck.

Run:  python3 scripts/bench_census_solve_scale.py [--levels 100,1000,10000]
             [--keep_fraction 1.0] [--budget 1000000] [--cap N]
"""
import sys, time, argparse, json, random
from pathlib import Path
import numpy as np
sys.path.insert(0, 'src')
from extstats.parsers import parse_census_dir
from extstats.candidates import generate_candidates_per_query
from extstats.optimize import build_problem, solve_ilp

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--levels', default='100,1000,10000')
    ap.add_argument('--budget', type=int, default=1_000_000, help='storage budget bytes')
    ap.add_argument('--cap', type=int, default=1, help='per_query_cap (1=sparse)')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--prune', action='store_true', help='skip_worse_than_baseline (realistic)')
    ap.add_argument('--arities', default='2,3')
    args=ap.parse_args()
    levels=[int(x) for x in args.levels.split(',')]
    arities=tuple(int(x) for x in args.arities.split(','))

    queries=parse_census_dir(Path('benchmarks/Census/queries'))
    per=generate_candidates_per_query(queries, arities=arities)
    # real baselines (measured at t1000, results/census_baseline_all.json)
    base={b['qid']:b['qerr'] for b in json.load(open('results/census_baseline_all.json'))['results']}
    rng=random.Random(args.seed)

    # size model (observed from Census top-10): L100~1.5KB L1000~11KB L10000~110KB
    size_model={100:int(rng.uniform(900,2600)),1000:int(rng.uniform(9000,13000)),
                10000:int(rng.uniform(80000,140000))}

    # Build a phase1-shaped dict with full candidate space (synthesized q-errors).
    results=[]
    for q in queries:
        cands=per.get(q.qid, [])
        qb=base.get(q.qid, 1.27)
        cand_dict={}
        for c in cands:
            cols=tuple(c.columns)  # already lowers; keep as string list
            levels_dict={}
            for lv in levels:
                # model: with prob P(correlated) the combo repairs toward ~1..3,
                # else stays ~baseline (no help -> pruned in realistic mode).
                if rng.random()<0.5 and qb>1.0:
                    qerr=round(rng.uniform(1.0,min(3.0,qb)),3)
                else:
                    qerr=round(rng.uniform(qb, qb*1.5),3)
                levels_dict[str(lv)]={'qerror':qerr,'size_bytes':size_model[lv],
                                      'estimate':0,'qerror_repeats':None}
            cand_dict[f"{c.table_unqualified}({','.join(cols)})"]={
                'table':c.table_unqualified,'columns':list(cols),'levels':levels_dict}
        results.append({'qid':q.qid,'qerror_base':qb,'candidates':cand_dict})

    t0=time.time()
    phys, opts, qb = build_problem({'results':results}, skip_worse_than_baseline=args.prune)
    t_build=time.time()-t0
    n_stats=len(phys); n_opt=sum(len(o) for o in opts)
    all_level_cost={}
    ns=[f"L{lv}" for lv in levels]
    t1=time.time()
    res=solve_ilp(phys, opts, qb, args.budget, per_query_cap=args.cap)
    t_solve=time.time()-t1

    print(f"\n=== FULL Census solve-scale (arities={arities}, levels={levels}, "
          f"prune={args.prune}, cap={args.cap}, budget={args.budget}B) ===")
    print(f"queries={len(queries)}  physical stats={n_stats}  options(kept)={n_opt}  "
          f"vars={n_stats+n_opt}")
    print(f"build={t_build:.2f}s  solve={t_solve:.2f}s  total={(t_build+t_solve):.2f}s")
    print(f"selected phys_stats={len(res.selected_stats)}  used={res.total_bytes}B  "
          f"mean_qerr={res.mean_qerror:.3f}  status={res.message}")
    print(f"(prune={'ON' if args.prune else 'OFF'}: pre-prune stats={n_stats}, "
          f"but pre-prune full options = {sum(len(r['candidates'])*len(levels) for r in results)})")

if __name__=='__main__':
    main()
