"""Read-only operational coverage report for the configured warehouse scope."""
from datetime import datetime, timezone
from sqlalchemy import select, func, or_
from . import db
from .ingest import scope, job_key


def quality_report(eng=db.engine, configured=None, now=None):
    configured=configured or scope()
    bins,years=configured['bins'],configured['years']
    now=now or datetime.now(timezone.utc)
    with eng.connect() as conn:
        jobs=[dict(r) for r in conn.execute(select(db.jobs)).mappings()]
        runs=[dict(r) for r in conn.execute(select(db.runs).order_by(db.runs.c.started_at.desc())).mappings()]
        relevant=[r for r in runs if r['mode'] in ('backfill','incremental')
                  and sorted(r['details'].get('bins',[]))==sorted(bins)
                  and sorted(r['details'].get('years',[]))==sorted(years)]
        success=next((r for r in relevant if r['status']=='complete'),None)
        base=db.items.join(db.contracts,db.items.c.contract_id==db.contracts.c.id).outerjoin(db.plans,db.items.c.plan_id==db.plans.c.id)
        selected=[db.contracts.c.customer_bin.in_(bins),db.contracts.c.year.in_(years)]
        def count(*conditions):
            return conn.execute(select(func.count()).select_from(base).where(*selected,*conditions)).scalar_one()
        links={'items':count(),'null_plan_id':count(db.items.c.plan_id.is_(None)),
               'missing_plan_lookup':count(db.items.c.plan_id.is_not(None),db.plans.c.id.is_(None)),
               'invalid_or_deleted_plan':count(db.plans.c.id.is_not(None),or_(db.plans.c.deleted.is_(True),db.plans.c.active.is_(False)))}
        latest={}
        for table in (db.plans,db.lots,db.contracts,db.items):
            latest[table.name]=conn.execute(select(func.max(table.c.index_date))).scalar_one()
    by_key={j['key']:j for j in jobs}
    partitions=[]
    for bin_ in bins:
        for year in years:
            for entity,filters in [('Plans',{'subjectBiin':bin_,'plnPointYear':[year]}),('Contract',{'customerBin':bin_,'finYear':year})]:
                job=by_key.get(job_key(entity,filters))
                partitions.append({'entity':entity,'bin':bin_,'year':year,'complete':bool(job and job['complete']),
                                   'rows':job['rows'] if job else 0,'error':job['error'] if job else None})
    age=None
    if success:
        stamp=datetime.fromisoformat(success['finished_at'] or success['started_at'])
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        age=max(0,(now-stamp).total_seconds())
    index_lag={}
    for entity,value in latest.items():
        try:
            stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            index_lag[entity]=max(0,(now-stamp).total_seconds())
        except (ValueError,AttributeError):
            index_lag[entity]=None
    return {'scope':{'bins':bins,'years':years},'plan_links':links,
            'partitions':partitions,'complete_partitions':sum(p['complete'] for p in partitions),
            'total_partitions':len(partitions),'partition_definition':'Configured BIN/year Plans and Contract backfill partitions; lot/link hydration is tracked separately.',
            'last_successful_run':success,'freshness_seconds':age,'stale':age is None or age>86400,
            'latest_run':relevant[0] if relevant else None,
            'backlog':{'incomplete_jobs':sum(not j['complete'] for j in jobs),'error_jobs':sum(bool(j['error']) for j in jobs)},
            'job_errors':[{'key':j['key'],'entity':j['entity'],'error':j['error']} for j in jobs if j['error']],
            'latest_source_index_dates':latest,
            'source_index_lag_seconds':index_lag,
            'source_index_note':'Database-wide source index dates and lag describe indexed source records, not a publication SLA or proof that all recent publications are available.'}
