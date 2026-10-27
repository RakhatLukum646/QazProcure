"""Recompute the stored regional example from normalized source rows, independently of fair_model."""
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
import numpy as np
from app.analytics import snapshot
from app import db


def main():
    path=Path('submission/regional_core_example.json')
    result=json.loads(path.read_text(encoding='utf-8'))['examples'][0]
    rows,quality=snapshot()
    target=next(r for r in rows if r['item_id']==result['item_id'])
    model=result['regional_model']
    cities=sorted(model['region_city_effects'])
    peers=[r for r in rows if r.get('city_code') in cities and
        (r['enstru'],r['unit'],r['subject_type'])==(target['enstru'],target['unit'],target['subject_type']) and
        r['customer_bin']!=target['customer_bin'] and r['contract_root']!=target['contract_root'] and
        r['signed_at'][:10]<=target['signed_at'][:10] and
        .5<=r['quantity']/target['quantity']<=2 and
        (r['description'] or '').casefold().strip()==target['description'].casefold().strip()]
    assert {r['item_id'] for r in peers}==set(result['peer_item_ids'])
    cells=defaultdict(lambda:defaultdict(list))
    for row in peers:cells[row['city_code'],row['signed_at'][:7]][row['contract_root']].append(row['price'])
    origin=min(m for c,m in cells)
    def features(day,city):
        year,month=map(int,day[:7].split('-'));oy,om=map(int,origin.split('-'))
        return np.array([1,((year-oy)*12+month-om)/12,
            math.sin(2*math.pi*month/12),math.cos(2*math.pi*month/12),
            *[int(city==c) for c in cities[1:]]])
    keys=sorted(cells)
    x=np.array([features(m,c) for c,m in keys])
    y=np.array([math.log(statistics.median(statistics.median(v) for v in cells[k].values())) for k in keys])
    w=np.array([min(5,len(cells[k])) for k in keys])
    # Normal equations provide a separate numerical route from lstsq in production.
    beta=np.linalg.solve(x.T@(w[:,None]*x),x.T@(w*y))
    assert np.allclose(beta,model['coefficients'],rtol=1e-8,atol=1e-8)
    target_x=features(target['signed_at'],target['city_code'])
    prices={r['item_id']:r['price']*math.exp(float((target_x-features(r['signed_at'],r['city_code']))@beta)) for r in peers}
    for adjustment in result['adjustments']:
        assert math.isclose(prices[adjustment['item_id']],adjustment['adjusted_price'],rel_tol=1e-8)
    recomputed=statistics.median(prices.values())
    assert math.isclose(recomputed,result['fair_price'],rel_tol=1e-8)
    report=dict(checked_at=db.now(),passed=True,lot_id=target['lot_id'],
        n_contracts=len({r['contract_root'] for r in peers}),fair_price=recomputed,
        source_snapshot=quality['snapshot_hash'],example_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        method='Independent peer predicates and normal-equation fit; all individual adjusted prices checked. Uses shared normalized source snapshot; not an independent audit of OWS truth.')
    Path('submission/regional_independent_check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
