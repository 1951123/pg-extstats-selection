"""End-to-end validation: build the ILP-selected stat set on the real DB and
measure the actual mean q-error, comparing it to the ILP's prediction.

This closes open-question 3: does the planner's real behaviour match the
(per-candidate, mask-measured) values the ILP optimised over?

Run:
  python3 scripts/validate_e2e.py --solution results/sparse_solution_2m.json \
      --queries st.562,st.308,st.398,st.144,st.326,st.567,st.588,st.182,st.284,st.314
"""
import sys, json, argparse, time
from pathlib import Path
sys.path.insert(0, 'src')
from extstats.parsers import parse_stats_ceb_single_dir
from extstats.db import connect
from extstats.config import DBConfig
from extstats.estimate import estimate_count_query

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--solution', required=True)
    ap.add_argument('--queries', required=True, help='comma-separated qids')
    ap.add_argument('--db', default='stats')
    ap.add_argument('--target', type=int, default=10000,
                    help='ANALYZE sample target (max per-object target is sensible)')
    args=ap.parse_args()

    sol=json.load(open(args.solution))
    wanted=set(args.queries.split(','))
    queries=parse_stats_ceb_single_dir(Path('benchmarks/stats_CEB/queries'))
    qs=[q for q in queries if q.qid in wanted]
    byid={q.qid:q for q in qs}
    # keep workload order matching phase1
    qids=[qid for qid in args.queries.split(',') if qid in byid]

    cfg=DBConfig(host='localhost',port=5432,user='postgres',dbname=args.db)
    stats=sol['selected']
    print(f"solution: {sol.get('selected_mean_qerror')} predicted, "
          f"{len(stats)} stats, {sol.get('used_bytes')}B / {sol.get('budget_bytes')}B")
    prefix='e2e_'
    with connect(cfg) as conn:
        conn.autocommit=True
        with conn.cursor() as cur:
            cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
            for (n,) in cur.fetchall():
                cur.execute(f"DROP STATISTICS IF EXISTS {n}")
        # build stats grouped by table, with per-object targets
        tables={}
        for s in stats:
            tables.setdefault(s['table'], []).append(s)
        with conn.cursor() as cur:
            for table, ss in tables.items():
                for i,s in enumerate(ss):
                    name=f"{prefix}m_{table}_{i}"
                    cols=', '.join(s['columns'])
                    cur.execute(f"DROP STATISTICS IF EXISTS {name}")
                    cur.execute(f"CREATE STATISTICS {name} (mcv) ON {cols} FROM {table}")
                    if s['level']>0:
                        cur.execute(f"ALTER STATISTICS {name} SET STATISTICS {s['level']}")
                cur.execute(f"SET default_statistics_target={args.target}")
                cur.execute(f"ANALYZE {table}")
        # measure each query
        measured={}
        for qid in qids:
            q=byid[qid]
            r=estimate_count_query(conn,q.sql,actual=q.ground_truth)
            measured[qid]=r.qerror if r.qerror is not None else float('nan')
        # cleanup
        with conn.cursor() as cur:
            cur.execute(f"SELECT stxname FROM pg_statistic_ext WHERE stxname LIKE '{prefix}%'")
            for (n,) in cur.fetchall():
                cur.execute(f"DROP STATISTICS IF EXISTS {n}")
            for table in tables:
                cur.execute(f"ANALYZE {table}")

    import statistics
    vals=[measured[q] for q in qids]
    measured_mean=statistics.mean(vals)
    print(f"\n=== E2E results (built {len(stats)} stats, target={args.target}) ===")
    print(f"{'qid':>8} {'measured':>10}")
    for q in qids:
        print(f"{q:>8} {measured[q]:>10.3f}")
    print(f"\nmeasured mean q-error : {measured_mean:.4f}")
    print(f"ILP predicted mean     : {sol.get('selected_mean_qerror'):.4f}")
    print(f"ratio (meas/pred)      : {measured_mean/sol.get('selected_mean_qerror'):.3f}")

if __name__=='__main__':
    main()
