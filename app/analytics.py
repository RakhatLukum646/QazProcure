"""Deterministic read-only analytical tools; no LLM arithmetic or arbitrary SQL."""
import calendar
import hashlib
import json
import math
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from sqlalchemy import select, func
from . import db, settings
from .schemas import AnalysisRequest
from .fair_model import city_index, joint_model

VERSION = '1.1.0'
SIGNED_STATUSES = {190, 375, 390, 455}
LIMIT = 200000


def normalize_unit(row):
    # Verified against OWS RefUnits: grams/metric tonnes -> kg, decalitres -> litres.
    conversions={'163':('166',.001),'168':('166',1000),'116':('112',10)}
    row['original_unit']=row['unit']
    row['original_price']=row['price']
    row['original_quantity']=row['quantity']
    if row['unit'] in conversions:
        row['unit'],factor=conversions[row['unit']]
        row['quantity']*=factor
        row['price']/=factor
    return row


class AnalysisError(ValueError):
    pass


def as_json(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def quantile(values, q):
    values = sorted(values)
    pos = (len(values)-1) * q
    lo = int(pos)
    return values[lo] + (values[min(lo+1,len(values)-1)]-values[lo]) * (pos-lo)


def statistics_for(rows):
    if not rows:
        return {'n_items':0,'n_contracts':0,'weighted_price':None,'median_price':None,'quantity':0,'amount_net':0}
    quantity = sum(r['quantity'] for r in rows)
    total = sum(r['price'] * r['quantity'] for r in rows)
    prices = [r['price'] for r in rows]
    median = statistics.median(prices)
    return dict(n_items=len(rows), n_contracts=len({r['contract_root'] for r in rows}),
        weighted_price=total/quantity, median_price=median, q25=quantile(prices,.25), q75=quantile(prices,.75),
        mad=statistics.median(abs(x-median) for x in prices), quantity=quantity, amount_net=total)


def snapshot(eng=db.engine):
    """Latest *signed* contract version per root, then latest item version per root.

    Deleted/terminated latest contracts suppress previous active versions. Draft
    amendments cannot hide a signed predecessor. Missing links remain counted.
    """
    c, i, p = db.contracts.c, db.items.c, db.plans.c
    with eng.connect() as conn:
        if eng.dialect.name=='sqlite':conn.exec_driver_sql('BEGIN')
        else:conn=conn.execution_options(isolation_level='REPEATABLE READ')
        all_contracts = [dict(r) for r in conn.execute(select(db.contracts).limit(LIMIT+1)).mappings()]
        if len(all_contracts) > LIMIT:
            raise AnalysisError('Dataset exceeds local analytical limit; narrow/materialize server-side view.')
        latest = {}
        for contract in all_contracts:
            if not contract['signed_at']:
                continue
            root = contract['root_id'] or contract['id']
            rank = (contract['signed_at'], contract['id'])
            if root not in latest or rank > (latest[root]['signed_at'], latest[root]['id']):
                latest[root] = contract
        valid_contract_ids = {r['id'] for r in latest.values() if not r['deleted'] and r['status'] in SIGNED_STATUSES}
        statement = select(
            i.id.label('item_id'), i.root_id.label('item_root'), i.contract_id, i.lot_id, i.plan_id,
            i.price, i.quantity, i.deleted.label('item_deleted'), i.index_date.label('item_index'),
            c.root_id.label('contract_root'), c.customer_bin, c.supplier_bin, c.signed_at, c.year,
            c.currency, c.status, c.index_date.label('contract_index'),
            p.enstru, p.unit, p.name_ru, p.name_kz, p.description, p.subject_type,
            p.deleted.label('plan_deleted'), p.index_date.label('plan_index'),
        ).select_from(db.items.join(db.contracts, i.contract_id==c.id).outerjoin(db.plans, i.plan_id==p.id)).limit(LIMIT+1)
        records = [dict(r) for r in conn.execute(statement).mappings()]
        if len(records)>LIMIT:
            raise AnalysisError('Too many items for this MVP analytical snapshot')
        locations = defaultdict(set)
        for row in conn.execute(select(db.deliveries).where(db.deliveries.c.active.is_(True))).mappings():
            if row['kato']:
                locations[row['plan_id']].add(row['kato'])
        job_rows = [dict(r) for r in conn.execute(select(db.jobs)).mappings()]
        city_locations = city_index([dict(r) for r in conn.execute(select(db.refs)).mappings()])
        from .ingest import scope
        configured_scope=scope()
        run_records=conn.execute(select(db.runs).order_by(db.runs.c.started_at.desc())).mappings()
        latest_run=next((r for r in run_records if r['status']=='complete' and r['mode'] in ('backfill','incremental')
            and sorted((r['details'] or {}).get('bins',[]))==sorted(configured_scope['bins'])
            and sorted((r['details'] or {}).get('years',[]))==sorted(configured_scope['years'])),None)
    reasons = Counter()
    dedup = {}
    for r in records:
        if r['contract_id'] not in valid_contract_ids:
            reasons['inactive_or_previous_contract'] += 1
            continue
        key = (r['contract_root'], r['item_root'])
        if key not in dedup or r['item_id'] > dedup[key]['item_id']:
            dedup[key] = r
    rows = []
    for r in dedup.values():
        if r['item_deleted']:
            reasons['deleted_item'] += 1
        elif not r['enstru'] or not r['unit'] or r['plan_deleted']:
            reasons['missing_or_deleted_plan'] += 1
        elif r['price'] is None or r['quantity'] is None or r['price'] <= 0 or r['quantity'] <= 0:
            reasons['invalid_price_or_quantity'] += 1
        elif r['currency'] not in ('KZT','398'):
            reasons['non_kzt_or_unknown_currency'] += 1
        else:
            kats = locations.get(r['plan_id'], set())
            r['kato'] = next(iter(kats)) if len(kats)==1 else None
            r.update(city_locations.get(r['kato'], {}))
            r['price'], r['quantity'] = float(r['price']), float(r['quantity'])
            r['contract_root'] = r['contract_root'] or r['contract_id']
            rows.append(normalize_unit(r))
    fingerprint = hashlib.sha256(json.dumps(sorted((r['item_id'],r['contract_index'],r['plan_index'],r['price'],r['quantity'],r['kato']) for r in rows), default=str).encode()).hexdigest()
    return rows, dict(raw_items=len(records), usable_items=len(rows), excluded=dict(reasons),
        jobs_total=len(job_rows), jobs_complete=sum(bool(j['complete']) for j in job_rows),
        backfill_partitions=[{'entity':j['entity'],'filters':j['filters'],'complete':j['complete']}
            for j in job_rows if j['entity'] in ('Plans','Contract') and 'indexDate' not in j['filters'] and 'id' not in j['filters']],
        latest_sync=dict(latest_run) if latest_run else None, snapshot_hash=fingerprint)


def matches(r, request, ignore_bins=False):
    if request.bins and not ignore_bins and r['customer_bin'] not in request.bins:
        return False
    if r['year'] not in request.years:
        return False
    if request.enstru and r['enstru'] != request.enstru:
        return False
    if request.kato and r['kato'] != request.kato:
        return False
    if request.lot_id and r['lot_id'] != request.lot_id:
        return False
    if request.text and request.text.casefold() not in ' '.join(str(r.get(k) or '') for k in ('name_ru','name_kz','description')).casefold():
        return False
    signed = (r['signed_at'] or '')[:10]
    if request.date_from and signed < str(request.date_from):
        return False
    if request.date_to and signed > str(request.date_to):
        return False
    return True


def cohort(r):
    return r['enstru'], r['unit'], r['kato'], r['subject_type']


def evidence(r, **extra):
    return dict(item_id=r['item_id'], contract_id=r['contract_id'], lot_id=r['lot_id'], plan_id=r['plan_id'],
        customer_bin=r['customer_bin'], supplier_bin=r['supplier_bin'], name_ru=r['name_ru'], name_kz=r['name_kz'],
        enstru=r['enstru'], unit=r['unit'], kato=r['kato'], year=r['year'], signed_at=r['signed_at'],
        price_net=r['price'], quantity=r['quantity'],
        original_unit=r.get('original_unit',r['unit']),original_price_net=r.get('original_price',r['price']),
        original_quantity=r.get('original_quantity',r['quantity']),
        city_code=r.get('city_code'),city_name=r.get('city_name'),city_resolution=r.get('city_resolution'),
        contract_url=f'https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{r["contract_id"]}',
        lot_url=None, **extra)


def anomaly(rows, universe, request):
    groups = defaultdict(list)
    for r in universe:
        if r['kato']:
            groups[cohort(r)].append(r)
    found, evaluated, skipped = [], 0, 0
    for r in rows:
        # Exclude whole contract family from its reference, not just the row itself.
        peers = [x for x in groups[cohort(r)] if x['contract_root'] != r['contract_root']]
        if len({x['contract_root'] for x in peers}) < settings.MIN_PEERS:
            skipped += 1
            continue
        stats = statistics_for(peers)
        delta = round((r['price']/stats['weighted_price']-1)*100, 8)
        evaluated += 1
        hit = delta > request.threshold_pct if request.direction=='above' else delta < -request.threshold_pct if request.direction=='below' else abs(delta)>request.threshold_pct
        if hit:
            found.append(evidence(r, deviation_pct=delta, reference_price=stats['weighted_price'], n_peers=stats['n_contracts'],
                peer_item_ids=[x['item_id'] for x in peers], peer_contract_ids=sorted({x['contract_id'] for x in peers})))
    found.sort(key=lambda x:abs(x['deviation_pct']),reverse=True)
    return dict(method='leave-one-contract-out quantity-weighted mean; exact ENSTRU/unit/KATO/type; net KZT',
                n_evaluated=evaluated,n_insufficient_peers=skipped,n_matches=len(found),examples=found[:request.top_k],
                limitations=['code_level_comparison','time_not_adjusted','anomaly_is_not_proof'])


def time_model(peers, target_date):
    """OLS log-price trend plus annual sine/cosine, only with sufficient history.

    This is an empirical price trend (not an official CPI inflation series).
    """
    monthly = defaultdict(list)
    for p in peers:
        monthly[p['signed_at'][:7]].append(p['price'])
    monthly = {m:v for m,v in monthly.items() if len(v)>=3}
    if len(monthly) < 24:
        return None
    import numpy as np
    origin = min(monthly)
    def features(day):
        y,m=map(int,day[:7].split('-'))
        oy,om=map(int,origin.split('-'))
        t=(y-oy)*12+m-om
        return [1,t,math.sin(2*math.pi*m/12),math.cos(2*math.pi*m/12)]
    months=sorted(monthly)
    x=np.array([features(m) for m in months],dtype=float)
    y=np.array([math.log(statistics.median(monthly[m])) for m in months])
    beta,_,rank,_=np.linalg.lstsq(x,y,rcond=None)
    if rank<4:
        return None
    predicted=x@beta
    ss=float(sum((y-y.mean())**2))
    r2=1-float(sum((y-predicted)**2))/ss if ss else 0
    if r2 < .2:
        return None
    target=float(np.array(features(target_date))@beta)
    factors={m:math.exp(target-float(np.array(features(m))@beta)) for m in months}
    if any(not .25<=v<=4 for v in factors.values()):
        return None
    return {'origin':origin,'coefficients':beta.tolist(),'months':len(months),'r2':r2,'factors':factors,
            'description':'OWS monthly median log-price trend and annual seasonality; not official CPI'}


def fair_price(rows, universe, request):
    if not request.lot_id:
        raise AnalysisError('fair_price requires lot_id or an existing lot_number')
    targets=[r for r in rows if r['lot_id']==request.lot_id]
    if not targets:
        raise AnalysisError('No usable signed contract item for this lot. Load its contract and plan first.')
    reports=[]
    for target in targets:
        if not target['kato']:
            reports.append(evidence(target,status='insufficient_location',n_peers=0))
            continue
        desc=(target['description'] or '').casefold().strip()
        matched=[r for r in universe if (r['enstru'],r['unit'],r['subject_type']) == (target['enstru'],target['unit'],target['subject_type'])
               and r['customer_bin']!=target['customer_bin']
               and r['contract_root']!=target['contract_root']
               and r['signed_at'][:10]<=target['signed_at'][:10]
               and .5<=r['quantity']/target['quantity']<=2
               and desc and (r['description'] or '').casefold().strip()==desc]
        same_city=bool(target.get('city_code'))
        peers=[r for r in matched if (r.get('city_code')==target['city_code'] if same_city else r['kato']==target['kato'])]
        n=len({r['contract_root'] for r in peers})
        if n<settings.MIN_PEERS:
            reports.append(evidence(target,status='insufficient_peers',n_peers=n))
            continue
        regional=joint_model(matched,target['signed_at'],target['city_code'],settings.MIN_PEERS) if same_city else None
        model=regional or time_model(peers,target['signed_at'])
        if model:
            adjusted=[r['price']*model['factors'].get(r['signed_at'][:7],1) for r in peers if r['signed_at'][:7] in model['factors']]
            used=[r for r in peers if r['signed_at'][:7] in model['factors']]
        else:
            # A narrow calendar quarter controls seasonality but cannot estimate inflation.
            used=[r for r in peers if r['signed_at'][:4]==target['signed_at'][:4] and (int(r['signed_at'][5:7])-1)//3==(int(target['signed_at'][5:7])-1)//3]
            adjusted=[r['price'] for r in used]
        n=len({r['contract_root'] for r in used})
        if n<settings.MIN_PEERS:
            reports.append(evidence(target,status='insufficient_time_matched_peers',n_peers=n))
            continue
        median=statistics.median(adjusted)
        reports.append(evidence(target, status='estimated' if model else 'provisional', fair_price=median,
            q25=quantile(adjusted,.25),q75=quantile(adjusted,.75),deviation_pct=(target['price']/median-1)*100,
            n_peers=n,n_peer_items=len(used),region_factor=regional['region_factor'] if regional else 1.0,
            region_reference_city=regional['region_reference_city'] if regional else target.get('city_code'),
            region_reason='joint_city_effect_calibration_same_city_peers' if regional else 'identical_verified_city' if same_city else 'identical_delivery_KATO',
            regional_model=regional,time_model=model,
            peer_item_ids=[r['item_id'] for r in used],peer_contract_ids=sorted({r['contract_id'] for r in used})))
    for report, target in zip(reports, targets):
        if target.get('target_kind') == 'planned_lot':
            report.update(target_kind='planned_lot', planned_price=target.get('planned_price'),
                          evaluation_date=target['signed_at'], signed_at=None, price_net=None, original_price_net=None,
                          deviation_pct=None, contract_url=None, contract_id=None,
                          target_source='lot_plan', quantity_source='linked_plan', planned_price_vat_basis='unknown', lot_url=None)
    return dict(method='median of other-customer contracts; exact description/unit/ENSTRU/type; verified city or exact KATO; quantity 0.5–2x; net KZT',
        n_evaluated=len(targets),n_matches=sum('fair_price' in r for r in reports),examples=reports[:request.top_k],
        limitations=['strict_description_match','time_model_requires_24_months','fair_price_is_statistical'])


def planned_lot_targets(request, eng):
    """Explicit plan targets for uncontracted lots; no invented actual contract price."""
    if request.date_from or request.date_to:
        raise AnalysisError('Плановый лот не имеет даты подписания договора: уберите фильтры дат подписания для оценки незаконтрактованного лота.')
    with eng.connect() as conn:
        signed_association=conn.execute(select(db.items.c.id).join(db.contracts,db.items.c.contract_id==db.contracts.c.id)
            .where(db.items.c.lot_id==request.lot_id,db.contracts.c.signed_at.is_not(None),db.contracts.c.signed_at!='').limit(1)).first()
        if signed_association:
            raise AnalysisError('Для лота известен подписанный договор, но его пригодная позиция отсутствует в текущей выборке. Проверьте фильтры, версии и качество данных; плановая цена не подменяет договорную.')
        lot=conn.execute(select(db.lots).where(db.lots.c.id==request.lot_id,db.lots.c.deleted.is_not(True))).mappings().first()
        if not lot:
            return []
        plans=[dict(r) for r in conn.execute(select(db.plans).join(db.lot_plans,db.plans.c.id==db.lot_plans.c.plan_id)
            .where(db.lot_plans.c.lot_id==request.lot_id,db.plans.c.deleted.is_not(True),db.plans.c.active.is_not(False))).mappings()]
        locations=defaultdict(set)
        for r in conn.execute(select(db.deliveries).where(db.deliveries.c.active.is_(True))).mappings():
            if r['kato']:locations[r['plan_id']].add(r['kato'])
        cities=city_index([dict(r) for r in conn.execute(select(db.refs)).mappings()])
    result=[]
    today=datetime.now(ZoneInfo('Asia/Qyzylorda')).date()
    evaluation_date=str(min(request.date_to or today,today))
    for p in plans:
        if not p['enstru'] or not p['unit'] or not p['quantity'] or p['quantity']<=0:
            continue
        # A shared plan does not establish the batch attributable to this lot.
        with eng.connect() as conn:
            linked_count=conn.execute(select(func.count()).select_from(db.lot_plans).where(db.lot_plans.c.plan_id==p['id'])).scalar_one()
        if linked_count!=1:
            continue
        kats=locations[p['id']]
        kato=next(iter(kats)) if len(kats)==1 else None
        row=dict(item_id=None,contract_id=None,contract_root=None,lot_id=lot['id'],plan_id=p['id'],
            customer_bin=lot['customer_bin'] or p['customer_bin'],supplier_bin=None,signed_at=evaluation_date,
            year=p['year'],enstru=p['enstru'],unit=p['unit'],kato=kato,subject_type=p['subject_type'],
            name_ru=p['name_ru'],name_kz=p['name_kz'],description=p['description'],
            quantity=float(p['quantity']),price=float(p['price'] or 0),target_kind='planned_lot')
        normalize_unit(row)
        row['planned_price']=row['price'] if row['price']>0 else None
        row.update(cities.get(kato,{}))
        if matches(row,request):result.append(row)
    return result


def quantity_anomaly(rows, request):
    target_year=max(request.years)
    # Compare identical month/day windows; leap day is clamped for previous years.
    today=datetime.now(ZoneInfo('Asia/Qyzylorda')).date()
    cutoff=(today.month,today.day) if target_year==today.year else (12,31)
    explicit_window=False
    if request.date_to and request.date_to.year==target_year:
        cutoff=(request.date_to.month,request.date_to.day)
        explicit_window=True
    groups=defaultdict(lambda:defaultdict(list))
    for r in rows:
        signed=r['signed_at'][:10]
        # Years strictly before target_year are already complete; only clamp them to the
        # cutoff when the window was explicitly requested, not for the implicit "today" default,
        # otherwise a target year still in progress wrongly discards fully elapsed prior years.
        if r['year']==target_year or explicit_window:
            end=date(r['year'],cutoff[0],min(cutoff[1],calendar.monthrange(r['year'],cutoff[0])[1]))
        else:
            end=date(r['year'],12,31)
        if signed>str(end):
            continue
        groups[(r['customer_bin'],r['enstru'],r['unit'])][r['year']].append(r)
    found=[];skipped=0;evaluated=0
    for (bin_,enstru,unit), by_year in groups.items():
        current=by_year.get(target_year,[])
        history={y:sum(r['quantity'] for r in rr) for y,rr in by_year.items() if y<target_year}
        if not current or len(history)<2:
            skipped+=1;continue
        baseline=statistics.median(history.values())
        if baseline<=0:
            skipped+=1;continue
        qty=sum(r['quantity'] for r in current)
        delta=round((qty/baseline-1)*100,8);evaluated+=1
        if delta>request.threshold_pct:
            found.append(dict(customer_bin=bin_,enstru=enstru,unit=unit,year=target_year,quantity=qty,
                baseline_quantity=baseline,baseline_by_year=history,deviation_pct=delta,
                n_contracts=len({r['contract_root'] for rr in by_year.values() for r in rr}),
                contract_ids=sorted({r['contract_id'] for rr in by_year.values() for r in rr}),
                examples=[evidence(r) for r in current[:3]]))
    found.sort(key=lambda x:x['deviation_pct'],reverse=True)
    return dict(method='quantity growth against median of at least two previous years; equal month/day window',
        cutoff_month_day='%02d-%02d'%cutoff,n_evaluated=evaluated,n_insufficient_history=skipped,n_matches=len(found),
        examples=found[:request.top_k],limitations=['contracted_not_consumed_quantity','coverage_required','growth_is_not_proof'])


def grouped(rows, request):
    groups=defaultdict(list)
    # Never average unlike products or add incomparable measurement units.
    for r in rows:
        groups[(str(r[request.group_by]),r['enstru'],r['unit'],r['kato'])].append(r)
    summaries=[dict(group=k[0],enstru=k[1],unit=k[2],kato=k[3],**statistics_for(v)) for k,v in groups.items()]
    summaries.sort(key=lambda x:x['amount_net'],reverse=True)
    return dict(method='sum(net unit price × quantity); prices and quantities grouped by ENSTRU/unit/KATO',
        n_evaluated=len(rows),n_groups=len(groups),amount_net=sum(r['price']*r['quantity'] for r in rows),
        groups=summaries[:request.top_k],examples=[evidence(r) for r in rows[:request.top_k]],limitations=['contracted_not_paid_amount'])


def analyze(request: AnalysisRequest, question='', eng=db.engine):
    if request.lot_number:
        with eng.connect() as conn:
            id_=conn.execute(select(db.lots.c.id).where(db.lots.c.number==request.lot_number)).scalar()
        if id_ is None:
            raise AnalysisError('Lot number is absent from storage')
        request=request.model_copy(update={'lot_id':id_})
    all_rows,quality=snapshot(eng)
    from .ingest import scope
    configured=scope()
    target_request=request if request.bins else request.model_copy(update={'bins':configured['bins']})
    rows=[r for r in all_rows if matches(r,target_request)]
    # Comparative reference excludes target-BIN and lot filters, but keeps explicit period/code/location/text.
    reference_request=request.model_copy(update={'bins':[],'lot_id':None,'lot_number':None})
    universe=[r for r in all_rows if matches(r,reference_request)]
    if request.intent=='price_anomaly':
        result=anomaly(rows,universe,request)
    elif request.intent=='fair_price':
        if not any(r['lot_id']==request.lot_id for r in rows):
            rows=planned_lot_targets(target_request,eng)
        result=fair_price(rows,universe,request)
    elif request.intent=='quantity_anomaly':
        result=quantity_anomaly(rows,request)
    elif request.intent in ('compare','summary'):
        result=grouped(rows,request)
    else:
        rows.sort(key=lambda x:x['signed_at'] or '',reverse=True)
        result=dict(method='filtered effective signed contract items; net KZT',n_evaluated=len(rows),n_matches=len(rows),
                    examples=[evidence(r) for r in rows[:request.top_k]],limitations=[])
    latest=quality['latest_sync']
    if request.intent in ('price_anomaly','fair_price'):
        result['limitations'].append('loaded_comparison_universe')
    target_bins=request.bins or configured['bins']
    partitions=quality.pop('backfill_partitions')
    completed={(p['entity'],p['filters'].get('subjectBiin') or p['filters'].get('customerBin'),
        (p['filters'].get('plnPointYear') or [p['filters'].get('finYear')])[0]) for p in partitions if p['complete']}
    missing=[{'entity':e,'bin':b,'year':y} for b in target_bins for y in request.years for e in ('Plans','Contract') if (e,b,y) not in completed]
    quality['missing_backfill_partitions']=missing
    if missing or not latest or latest['status']!='complete':
        result['limitations'].append('partial_ingestion')
    if quality['excluded'].get('missing_or_deleted_plan'):
        result['limitations'].append('missing_plans_excluded')
    if latest:
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(latest['finished_at'] or latest['started_at'])).total_seconds()/3600
        quality['sync_age_hours']=round(elapsed,2)
        if elapsed>24:
            result['limitations'].append('stale_data')
    result.update(id=str(uuid.uuid4()),created_at=db.now(),method_version=VERSION,request=request.model_dump(mode='json'),
                  n_selected=len(rows),quality=quality,
                  confidence='insufficient' if not result.get('n_evaluated') else 'limited' if result['limitations'] else 'descriptive')
    result=json.loads(json.dumps(result,default=as_json,ensure_ascii=False))
    with eng.begin() as conn:
        db.upsert(conn,db.audits,dict(id=result['id'],created_at=result['created_at'],question=question,request=result['request'],result=result))
    return result
