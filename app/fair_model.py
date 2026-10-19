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


def joint_model(peers, target_date, target_city, minimum_contracts=5):
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
