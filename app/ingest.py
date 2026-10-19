"""Resumable, idempotent page ingestion. A page and its cursor commit together."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from . import db, settings
from .ows import OWSClient, OWSError, page_query
from .normalize import NORMALIZERS


def scope():
    return json.loads((settings.ROOT / 'config/scope.json').read_text(encoding='utf-8'))


def job_key(entity, filters):
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:32]
    return f'{entity}:{digest}'


def incremental_since(runs, bins, years, now=None):
    """Overlap the last successful scan; bootstrap from backfill start, never finish."""
    now=now or datetime.now(timezone.utc)
    relevant=[r for r in runs if r['status']=='complete' and r['mode'] in ('incremental','backfill')
              and sorted(r['details'].get('bins',[]))==sorted(bins)
              and sorted(r['details'].get('years',[]))==sorted(years)]
    incremental=[r for r in relevant if r['mode']=='incremental']
    previous=max(incremental or relevant,key=lambda r:r['started_at'],default=None)
    return datetime.fromisoformat(previous['started_at'])-timedelta(days=1) if previous else now-timedelta(days=2)


def ingest_pages(client, entity, filters, eng=db.engine, max_pages=None, reset=False):
    key = job_key(entity, filters)
    with eng.begin() as conn:
        old = conn.execute(select(db.jobs).where(db.jobs.c.key == key)).mappings().first()
        if reset or old is None:
            state = dict(key=key, entity=entity, filters=filters, cursor=None, rows=0, complete=False, updated_at=db.now(), error=None)
            db.upsert(conn, db.jobs, state)
        else:
            state = dict(old)
    if state['complete']:
        return state
    pages = 0
    while max_pages is None or pages < max_pages:
        try:
            api_filters={k:v for k,v in filters.items() if not k.startswith('_')}
            variables=dict(filter=api_filters, limit=settings.PAGE_SIZE, after=state['cursor'])
            if filters.get('_enstru'):variables['units']={'refEnstruCode':filters['_enstru']}
            body = client.query(page_query(entity,units=bool(filters.get('_enstru'))), variables)
            records = body['data'].get(entity)
            info = body.get('extensions', {}).get('pageInfo', {})
            if records is None and info.get('hasNextPage') is False and (
                info.get('totalCount') == 0 or
                (state['cursor'] is not None and isinstance(info.get('totalCount'), int) and state['rows'] >= info['totalCount'])
            ):
                records = []
            if not isinstance(records, list):
                raise OWSError(f'{entity}: expected a list, refusing to advance cursor')
            if 'hasNextPage' not in info:
                raise OWSError('Pagination metadata missing; refusing to mark coverage complete')
            more = bool(info['hasNextPage'])
            cursor = info.get('lastId')
            if more and (not records or cursor is None or cursor == state['cursor']):
                raise OWSError('Pagination cursor did not advance')
            with eng.begin() as conn:
                for obj in records:
                    NORMALIZERS[entity](conn, obj)
                state.update(cursor=cursor, rows=state['rows'] + len(records), complete=not more, updated_at=db.now(), error=None)
                db.upsert(conn, db.jobs, state)
            pages += 1
            if not more:
                break
        except Exception as exc:
            with eng.begin() as conn:
                conn.execute(db.jobs.update().where(db.jobs.c.key == key).values(error=str(exc)[:1000], updated_at=db.now()))
            raise
    return state


def hydrate_links(client, eng=db.engine, max_batches=None):
    with eng.connect() as conn:
        missing_plans = list(conn.execute(select(db.items.c.plan_id).outerjoin(db.plans, db.items.c.plan_id == db.plans.c.id)
            .where(db.plans.c.id.is_(None), db.items.c.plan_id.is_not(None)).distinct()).scalars())
        missing_lots = list(conn.execute(select(db.items.c.lot_id).outerjoin(db.lots, db.items.c.lot_id == db.lots.c.id)
            .where(db.lots.c.id.is_(None), db.items.c.lot_id.is_not(None)).distinct()).scalars())
    outcomes = []
    for entity, ids in [('Plans', missing_plans), ('Lots', missing_lots)]:
        batches = 0
        for offset in range(0, len(ids), 30):
            if max_batches is not None and batches >= max_batches:
                break
            outcomes.append(ingest_pages(client, entity, {'id': ids[offset:offset+30]}, eng=eng, reset=True))
            batches += 1
    return len(missing_plans), len(missing_lots), outcomes


def _sync(mode='backfill', bins=None, years=None, max_pages=None, max_link_batches=None, enstru=None, eng=db.engine):
    db.init_db(eng)
    configured = scope()
    bins = bins if bins is not None else configured['bins']
    years = years if years is not None else configured['years']
    run_id = str(uuid.uuid4())
    start = db.now()
    with eng.begin() as conn:
        # Advance the incremental watermark only after a fully successful run.
        past=conn.execute(select(db.runs).where(db.runs.c.status == 'complete')
                                .order_by(db.runs.c.started_at.desc())).mappings().all()
        since = incremental_since(past,bins,years)
        db.upsert(conn, db.runs, dict(id=run_id, mode=mode, started_at=start, status='running', details={'bins':bins,'years':years}))
    client = None
    status = 'failed'
    details = {'bins': bins, 'years': years, 'jobs': [], 'warnings': []}
    try:
        client = OWSClient()
        for bin_ in ([None] if mode=='peers' else bins):
            for year in years:
                tasks=[('Contract',{'finYear':year,'_enstru':[enstru]})] if mode=='peers' else [('Plans', {'subjectBiin':bin_, 'plnPointYear':[year]}), ('Contract', {'customerBin':bin_, 'finYear':year})]
                for entity, base in tasks:
                    if mode == 'incremental':
                        base['indexDate'] = [since.strftime('%Y-%m-%d %H:%M:%S'), datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')]
                    result = ingest_pages(client, entity, base, eng, max_pages)
                    details['jobs'].append({'entity':entity,'bin':bin_,'year':year,'rows':result['rows'],'complete':result['complete']})
                    print(json.dumps(details['jobs'][-1]), flush=True)
            if mode=='peers':continue
            # Lots has no year filter. Resolve through plan IDs in the selected years.
            with eng.connect() as conn:
                plan_ids=list(conn.execute(select(db.plans.c.id).where(db.plans.c.customer_bin==bin_,db.plans.c.year.in_(years)).order_by(db.plans.c.id)).scalars())
            for offset in range(0,len(plan_ids),100):
                filters = {'customerBin':bin_,'pointList':plan_ids[offset:offset+100]}
                if mode == 'incremental':
                    filters['indexDate'] = [since.strftime('%Y-%m-%d %H:%M:%S'), datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')]
                result = ingest_pages(client, 'Lots', filters, eng, max_pages)
                details['jobs'].append({'entity':'Lots','bin':bin_,'rows':result['rows'],'complete':result['complete']})
        missing_plans, missing_lots, hydrated = hydrate_links(client, eng, max_link_batches)
        details['link_requests'] = {'plans':missing_plans,'lots':missing_lots}
        if max_link_batches is not None:
            details['warnings'].append('Link hydration was bounded; some references may remain unresolved.')
        complete = all(j['complete'] for j in details['jobs']) and all(j['complete'] for j in hydrated) and max_link_batches is None
        status = 'complete' if complete else 'partial'
    except Exception as exc:
        details['error'] = str(exc)[:1000]
        status = 'failed'
        raise
    finally:
        if client:client.close()
        with eng.begin() as conn:
            conn.execute(db.runs.update().where(db.runs.c.id == run_id).values(finished_at=db.now(), status=status, details=details))
    return {'run_id':run_id,'status':status,**details}


def sync(*args, **kwargs):
    """Single writer across worker processes; database lock for PostgreSQL."""
    eng=kwargs.get('eng',db.engine)
    if eng.dialect.name=='postgresql':
        from sqlalchemy import text
        with eng.connect() as conn:
            if not conn.execute(text('SELECT pg_try_advisory_lock(78144103)')).scalar():
                raise RuntimeError('Another ingestion worker is running')
            try:return _sync(*args,**kwargs)
            finally:conn.execute(text('SELECT pg_advisory_unlock(78144103)'))
    from filelock import FileLock
    with FileLock(str(settings.DATA/'ingestion.lock'),timeout=0):
        return _sync(*args,**kwargs)
