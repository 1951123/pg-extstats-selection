"""Temporary: compare capacity-axis sensitivity between the two CENSUS runs
on the COMMON query subset (first 120 queries).

  multi  = results/phase1_census_mcv_multi.json      {100,1000,10000} single-col 10000
  low    = results/phase1_census_mcv_low_t10000.json {10,25,50}       single-col 10000

For each (query, candidate) with a full level dict, classify capacity sensitivity:
  - invariant : all levels have identical q-error (capacity axis flat)
  - collapsed : at least one level has a NULL/empty payload (contributes 0), so
                the candidate is 'collapsed' at that level
  - sensitive : q-error varies by > tol across levels
Also report:
  - median max->min q-error ratio per query (how much capacity can help/hurt)
  - fraction of candidates whose best level improves over their worst by > 1%
"""
import json, statistics, sys

def load(p):
    with open(p) as f:
        return json.load(f)

def analyze(path, levels, label):
    d = load(path)
    print(f"\n===== {label}: levels={levels} ({d['n_queries']} queries) =====")
    cand_total = 0
    inv = 0          # all levels identical qerror
    collapsed = 0    # has empty/NULL payload at some level
    sensitive = 0    # varies > tol
    improves = 0     # best < worst * 0.99 (capacity helps)
    hurts = 0        # best level worsens w.r.t. another (capacity hurts)
    ratios = []
    cand_improve_20 = 0  # best < worst*0.80
    n_null_levels = 0
    for q in d['results']:
        for cname, c in q.get('candidates', {}).items():
            lv = c.get('levels', {})
            # only consider candidates measured at all requested levels
            if set(lv.keys()) != set(str(x) for x in levels):
                continue
            cand_total += 1
            qs = []
            has_null = False
            for L in levels:
                v = lv.get(str(L), {})
                # qerror None / estimate None => empty payload => collapsed
                if v.get('qerror') is None or v.get('estimate') is None or v.get('qerror', 1) != v.get('qerror', 1):
                    has_null = True; n_null_levels += 1
                    qs.append(float('inf'))
                else:
                    qs.append(v['qerror'])
            if has_null:
                collapsed += 1
            # sensitivity among finite values
            finite = [x for x in qs if x != float('inf')]
            if len(finite) >= 2:
                rng = (max(finite) - min(finite)) / min(finite)
                ratios.append(max(finite) / min(finite))
                if rng <= 1e-6:
                    inv += 1
                elif rng >= 0.01:
                    sensitive += 1
                if max(finite) / min(finite) >= 1.20:
                    cand_improve_20 += 1
                if min(finite) / max(finite) < 0.99:
                    improves += 1
                if max(finite) / min(finite) > 1.01 and min(finite) == finite[0] is False:
                    pass
    print(f"  candidates (full levels): {cand_total}")
    print(f"  invariant (all qerror identical): {inv} ({100*inv/cand_total:.1f}%)")
    print(f"  collapsed (NULL/empty payload somewhere): {collapsed} ({100*collapsed/cand_total:.1f}%)")
    print(f"  sensitive (>1% qerror range): {sensitive} ({100*sensitive/cand_total:.1f}%)")
    if ratios:
        print(f"  median max/min qerror ratio: {statistics.median(ratios):.4f}")
        print(f"  best<worst*0.8 (capacity helps >=20%): {cand_improve_20} ({100*cand_improve_20/cand_total:.1f}%)")
    return {'cand_total': cand_total, 'inv': inv, 'collapsed': collapsed,
            'sensitive': sensitive, 'n_null_levels': n_null_levels}

if __name__ == '__main__':
    multi = load('results/phase1_census_mcv_multi.json')
    low   = load('results/phase1_census_mcv_low_t10000.json')
    levels_multi = multi['target_levels']
    levels_low   = low['target_levels']
    analyze('results/phase1_census_mcv_multi.json', levels_multi,
            f'multi {levels_multi}')
    analyze('results/phase1_census_mcv_low_t10000.json', levels_low,
            f'low   {levels_low} (partial {low["n_queries"]}/468)')
