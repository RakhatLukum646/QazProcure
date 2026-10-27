"""Conservative city resolution and empirical joint time/location calibration."""
import math
import re
import statistics
from collections import defaultdict


def city_index(reference_rows):
    """Follow recorded parentCode only; missing ancestors never imply a city."""
    records = {str(r['code']): r for r in reference_rows if 'kato' in r['kind'].lower()}
    result = {}
    for code in records:
        seen, path, current = set(), [], code
        while current in records and current not in seen:
            seen.add(current)
            row = records[current]
            payload = row.get('payload') or {}
            path.append(current)
            name = str(row.get('name_ru') or payload.get('nameRu') or '')
            # Explicit administrative city label, not digits or a district name.
            if re.match(r'^\s*(?:г\s*\.\s*|город\s+)', name, re.I):
                result[code] = {'city_code': current, 'city_name': name,
                                'city_resolution': 'reference_parent_hierarchy', 'city_path': path}
                break
            parent = payload.get('parentCode')
            if parent is None:
                break
            current = str(parent)
    return result


def balanced_joint_model(peers, target_date, target_city, minimum_contracts=5):
    """Monthly city medians fit jointly to prevent city mix becoming a time trend.

    All inputs must already match description/product/unit/type/batch size.
    We enforce date and distinct-contract guards again at this boundary.
    """
    import numpy as np
    eligible = [r for r in peers if r.get('city_code') and r['signed_at'][:10] <= target_date[:10]]
    cities = sorted({r['city_code'] for r in eligible})
    if len(cities) < 2 or target_city not in cities:
        return None
    monthly = defaultdict(list)
    for r in eligible:
        monthly[(r['city_code'], r['signed_at'][:7])].append(r)
    monthly = {k: v for k, v in monthly.items() if len({r['contract_root'] for r in v}) >= 3}
    month_sets = {c: {m for (city, m) in monthly if city == c} for c in cities}
    # A common 24-month calibration period identifies location separately from time.
    common = set.intersection(*(month_sets[c] for c in cities))
    if len(common) < 24:
        return None
    used = [r for (c, m), rows in monthly.items() if m in common for r in rows]
    if any(len({r['contract_root'] for r in used if r['city_code'] == c}) < minimum_contracts for c in cities):
        return None
    origin = min(common)
    def components(day):
        year, month = map(int, day[:7].split('-'))
        oy, om = map(int, origin.split('-'))
        return (year-oy)*12+month-om, math.sin(2*math.pi*month/12), math.cos(2*math.pi*month/12)
    def features(day, city):
        return [1, *components(day), *[float(city == c) for c in cities[1:]]]
    keys = sorted(k for k in monthly if k[1] in common)
    x = np.array([features(m, c) for c, m in keys])
    y = np.array([math.log(statistics.median(r['price'] for r in monthly[k])) for k in keys])
    beta, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
    if rank != x.shape[1] or np.linalg.cond(x) > 1e6:
        return None
    predicted = x @ beta
    ss = float(sum((y-y.mean())**2))
    r2 = 1-float(sum((y-predicted)**2))/ss if ss else 0
    if r2 < .2:
        return None
    offsets = {cities[0]: 0.0, **{c: float(beta[4+i]) for i, c in enumerate(cities[1:])}}
    region_factor = math.exp(offsets[target_city])
    if any(not .25 <= math.exp(v) <= 4 for v in offsets.values()):
        return None
    target_t, target_s, target_c = components(target_date)
    factors, decomposition = {}, {}
    for month in sorted(common):
        t, s, c = components(month)
        trend = math.exp(float(beta[1])*(target_t-t))
        seasonal = math.exp(float(beta[2])*(target_s-s)+float(beta[3])*(target_c-c))
        factor = trend*seasonal
        if not .25 <= factor <= 4:
            return None
        factors[month] = factor
        decomposition[month] = {'empirical_trend_factor': trend, 'seasonality_factor': seasonal}
    return {'origin': origin, 'coefficients': beta.tolist(), 'months': len(common), 'r2': r2,
            'factors': factors, 'factor_components': decomposition,
            'region_factor': region_factor, 'region_reference_city': cities[0],
            'region_target_city': target_city, 'region_city_effects': offsets,
            'calibration_item_ids': sorted({r['item_id'] for r in used}),
            'calibration_contract_ids': sorted({r['contract_id'] for r in used}),
            'description': 'Joint OWS log-price empirical trend, annual seasonality and city effects; not official CPI'}


def joint_model(peers,target_date,target_city,minimum_contracts=5):
    """Weighted unbalanced city/month panel; no requirement for identical calendars.

    Exact comparable product/description/batch filtering belongs to the caller.
    Singleton months remain observations, weighted by independent families (cap 5).
    At least 24 months, seasonal coverage, connected city overlap and full rank are
    required. Forecast errors are exposed; this is an empirical benchmark, not CPI.
    """
    import numpy as np
    eligible=[r for r in peers if r.get('city_code') and r['signed_at'][:10]<=target_date[:10]
              and r['price']>0]
    cells=defaultdict(dict)
    for r in eligible:
        cells[(r['city_code'],r['signed_at'][:7])].setdefault(r['contract_root'],[]).append(r)
    city_months=defaultdict(set)
    for c,m in cells:city_months[c].add(m)
    cities=sorted(c for c,months in city_months.items() if len(months)>=12 and
        len({r['contract_root'] for r in eligible if r['city_code']==c})>=max(12,minimum_contracts))
    if target_city not in cities or len(cities)<2:return None
    # Keep only the overlap-connected component containing the target city.
    connected={target_city}
    while True:
        expanded=connected|{c for c in cities if any(len(city_months[c]&city_months[d])>=6 for d in connected)}
        if expanded==connected:break
        connected=expanded
    cities=sorted(connected)
    keys=sorted((c,m) for c,m in cells if c in connected)
    months=sorted({m for c,m in keys})
    if len(cities)<2 or len(months)<24 or len({m[5:] for m in months})<12:return None
    origin=months[0]
    def components(day):
        y,m=map(int,day[:7].split('-'));oy,om=map(int,origin.split('-'))
        return ((y-oy)*12+m-om)/12,math.sin(2*math.pi*m/12),math.cos(2*math.pi*m/12)
    def features(day,city):return [1,*components(day),*[float(city==c) for c in cities[1:]]]
    x=np.array([features(m,c) for c,m in keys])
    y=np.array([math.log(statistics.median(statistics.median(r['price'] for r in family)
        for family in cells[k].values())) for k in keys])
    weights=np.array([min(5,len(cells[k])) for k in keys],dtype=float)
    wx=x*np.sqrt(weights)[:,None];wy=y*np.sqrt(weights)
    beta,_,rank,_=np.linalg.lstsq(wx,wy,rcond=None)
    if rank!=x.shape[1] or np.linalg.cond(wx)>1e6:return None
    ss=float(sum(weights*(y-np.average(y,weights=weights))**2))
    r2=1-float(sum(weights*(y-x@beta)**2))/ss if ss else 0
    if r2<.2:return None
    offsets={cities[0]:0.,**{c:float(beta[4+i]) for i,c in enumerate(cities[1:])}}
    if any(not .25<=math.exp(v)<=4 for v in offsets.values()):return None
    tt,ts,tc=components(target_date)
    factors={};parts={}
    for m in months:
        t,s,c=components(m)
        trend=math.exp(float(beta[1])*(tt-t));season=math.exp(float(beta[2])*(ts-s)+float(beta[3])*(tc-c))
        if not .25<=trend*season<=4:return None
        factors[m]=trend*season
        parts[m]={'empirical_trend_factor':trend,'seasonality_factor':season}
    used=[r for k in keys for family in cells[k].values() for r in family]
    # Temporal validation is diagnostic, never hidden by the training R².
    cutoff=months[-6];train=np.array([m<cutoff for c,m in keys])
    bt,_,rt,_=np.linalg.lstsq(wx[train],wy[train],rcond=None)
    validation={'method':'last six observed months held out','available':bool(rt==x.shape[1])}
    if validation['available']:
        errors=np.abs(np.exp(x[~train]@bt-y[~train])-1)
        validation.update(cells=int(sum(~train)),median_absolute_percentage_error=float(np.median(errors)*100),
                          p90_absolute_percentage_error=float(np.quantile(errors,.9)*100))
    return dict(origin=origin,coefficients=beta.tolist(),months=len(months),r2=r2,
        factors=factors,factor_components=parts,region_factor=math.exp(offsets[target_city]),
        region_reference_city=cities[0],region_target_city=target_city,region_city_effects=offsets,
        calibration_item_ids=sorted({r['item_id'] for r in used}),
        calibration_contract_ids=sorted({r['contract_id'] for r in used}),
        temporal_validation=validation,model_kind='weighted_unbalanced_city_month',
        description='Exact-comparable OWS monthly family medians; weighted city effects, empirical annual trend and seasonality, not official CPI')
