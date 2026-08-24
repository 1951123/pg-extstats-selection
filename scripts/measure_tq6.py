"""Measure the FULL T_q(b) cost curve for a single query at SIX levels.

Answers: "how large is the gap between the cost model T_q(b)=b*B0 + c*nL +
(nL)^2*mu/b and the REAL measured per-query phase-1 time, at the current
6-level menu {10,25,50,100,1000,10000}?"

The paper's existing single-query validation (query.335) used L=1. Here we do
the same structural validation at L=6 (the menu the paper now uses), on a
moderate-candidate query so the whole convex curve (b below and above the
optimum b*=nL*sqrt(mu/B0)) is measurable.

For each sub-batch count b, we:
  - split the query's candidates into b balanced sub-batches (as in
    measure_phase1_subbatch.py);
  - for each sub-batch, CREATE (cand x level) stats, ONE ANALYZE (timed), then
    for every (cand, level) cell time a real mask--EXPLAIN--restore cycle;
  - sum ANALYZE + mask cycles across sub-batches for the total measured T_q(b).

Model (Cor.~4 intra-query): T_q(b) = b*B0 + c*nL + (nL)^2*mu/b
with B0=22s (ANALYZE base), c~0.35 s/stat, mu~0.0008 s/payload; n=#candidates,
L=#levels. --bench selects parser/db/table (census -> climate/census;
stats_ceb_single -> posts/stats) so we can test whether the fitted parameters
transfer across datasets.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/measure_tq6.py \
      --bench census --qid query.3 --levels 10,25,50,100,1000,10000 \
      --batches 1,3,7,14,28 --n-cycles 1 --out results/tq6_model_vs_measured.json
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from psycopg import connect

from extstats.config import DBConfig, DEFAULT_DB
from extstats.db import connect as db_connect
from extstats.parsers import (
    parse_census_dir, parse_stats_ceb_single_dir,
)
from extstats.candidates import generate_candidates_per_query, CandidateSet
from extstats.measure_mask import (
    _analyze, _backup_payload, _drop_backup, _mask_payload_all_but,
    _restore_payload, _set_target, _stat_oid, _stat_name_level,
)
from extstats.stats import _qualify_table

_BENCH = {
    "census": (parse_census_dir, "Census", "climate"),
    "stats_ceb_single": (parse_stats_ceb_single_dir, "stats_CEB", "posts"),
}
_KIND = "mcv"


def balanced_split(cands, n_parts):
    """Split cands into n_parts balanced (floor/ceil) batches, as the harness."""
    if n_parts <= 1 or len(cands) <= n_parts:
        return [cands]
    b = n_parts
    base, rem = divmod(len(cands), b)
    out, idx = [], 0
    for j in range(b):
        sz = base + (1 if j < rem else 0)
        out.append(cands[idx:idx + sz]); idx += sz
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", choices=sorted(_BENCH), default="census")
    ap.add_argument("--qid", default="query.3")
    ap.add_argument("--levels", default="10,25,50,100,1000,10000")
    ap.add_argument("--batches", default="1,3,7,14,28")
    ap.add_argument("--single-col-target", type=int, default=10000)
    ap.add_argument("--n-cycles", type=int, default=1)
    ap.add_argument("--out", default="results/tq6_model_vs_measured.json")
    args = ap.parse_args()

    levels = tuple(int(x) for x in args.levels.split(",") if x.strip())
    L = len(levels)
    bvals = [int(x) for x in args.batches.split(",") if x.strip()]
    B0, c, mu, eps = 22.0, 0.35, 0.0008, 0.002

    parser, bench_dir, default_table = _BENCH[args.bench]
    queries = parser(Path(__file__).resolve().parents[1] / "benchmarks" / bench_dir / "queries")
    mp = generate_candidates_per_query(queries, arities=(2, 3))
    q = next(x for x in queries if x.qid == args.qid)
    cands = mp[args.qid]
    n = len(cands)
    nL = n * L
    # table = the (single) table the query's candidates live on; fall back to default
    tables = {c.table for c in cands}
    raw = sorted(tables)[0] if len(tables) == 1 else default_table
    tbl = _qualify_table(raw)  # -> public.<table>
    qid_safe = args.qid.replace(".", "_")
    dbname = DEFAULT_DB[args.bench]
    print(f"== [{args.bench}] query {args.qid}: n={n} cands, L={L} -> nL={nL} cells, table={tbl}, db={dbname} ==")
    print(f"   model B0={B0} c={c} mu={mu};  b* = nL*sqrt(mu/B0) = {nL*(mu/B0)**0.5:.1f}")

    qual = q.sql  # measured via estimate_count_query against ground truth

    cfg = DBConfig(host="localhost", port=5432, user="postgres", dbname=dbname)
    rows = []
    with db_connect(cfg) as conn:
        conn.autocommit = True
        for b in bvals:
            _set_target(conn, args.single_col_target)
            batches = balanced_split(cands, b)
            an_t = 0.0; mask_t = 0.0; exp_t = 0.0; res_t = 0.0; build_t = 0.0
            created_names = []
            bk = 0
            for batch in batches:
                # ---- create (cand x level) objects for this sub-batch ----
                t0 = time.perf_counter()
                names = []
                for cand in batch:
                    for lvl in levels:
                        nm = _stat_name_level(cand, _KIND, "ext_", lvl)
                        names.append(nm)
                        with conn.cursor() as cur:
                            cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
                            cur.execute(
                                f"CREATE STATISTICS {nm} ({_KIND}) "
                                f"ON {', '.join(cand.columns)} FROM {tbl}")
                            if L > 1:
                                cur.execute(
                                    f"ALTER STATISTICS {nm} SET STATISTICS {int(lvl)}")
                created_names += names
                build_t += time.perf_counter() - t0

                # ---- ONE ANALYZE builds all mounted objects ----
                _set_target(conn, args.single_col_target)
                t0 = time.perf_counter(); _analyze(conn, tbl); an_t += time.perf_counter() - t0

                # ---- backup + per-cell mask--EXPLAIN--restore cycles ----
                oids = [_stat_oid(conn, nm) for nm in names]
                backup_table = f"_tq6_bk_{qid_safe}_{bk}"; bk += 1
                _drop_backup(conn, backup_table)
                _backup_payload(conn, backup_table, oids, _KIND)
                ncell = len(batch) * L
                for _ in range(args.n_cycles):
                    for i, oid in enumerate(oids):
                        keep = {oid}
                        t0 = time.perf_counter()
                        _mask_payload_all_but(conn, backup_table, keep, _KIND)
                        mask_t += time.perf_counter() - t0
                        t0 = time.perf_counter()
                        with conn.cursor() as cur:
                            cur.execute("EXPLAIN (FORMAT JSON) " + qual); cur.fetchone()
                        exp_t += time.perf_counter() - t0
                        t0 = time.perf_counter()
                        _restore_payload(conn, backup_table, _KIND)
                        res_t += time.perf_counter() - t0
                _drop_backup(conn, backup_table)

            # measured total for this b (all phases, the true wall cost)
            tot = build_t + an_t + mask_t + exp_t + res_t

            # model
            model = b * B0 + c * nL + (nL) ** 2 * mu / b + eps * nL

            rows.append({
                "b": b, "n_subbatches": len(batches),
                "build": round(build_t, 1), "analyze": round(an_t, 1),
                "mask": round(mask_t, 1), "explain": round(exp_t, 1),
                "restore": round(res_t, 1),
                "measured_total_s": round(tot, 1),
                "model_total_s": round(model, 1),
                "ratio_model_measured": round(model / tot, 3),
            })
            print(f"  b={b:>2}: measure={tot:6.1f}s "
                  f"(build {build_t:5.1f} | AN {an_t:5.1f} | mask {mask_t:6.1f} | "
                  f"exp {exp_t:4.1f} | res {res_t:4.1f})   model={model:6.1f}s  "
                  f"model/meas={model/tot:.3f}")

            # cleanup (drop stats + re-analyze) so each b starts clean
            for nm in created_names:
                with conn.cursor() as cur:
                    cur.execute(f"DROP STATISTICS IF EXISTS {nm}")
            _drop_backup(conn, f"_tq6_bk_{qid_safe}")
            _analyze(conn, tbl)

    out = {
        "qid": args.qid, "levels": list(levels), "n_candidates": n, "nL": nL,
        "model_params": {"B0": B0, "c": c, "mu": mu, "eps": eps},
        "batches": bvals, "rows": rows,
    }
    op = Path(args.out)
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {op}")


if __name__ == "__main__":
    main()
