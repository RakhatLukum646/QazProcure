"""Read-only operational coverage report for the configured warehouse scope."""
from datetime import datetime, timezone, timedelta
import statistics
from sqlalchemy import select, func, or_
from . import db, settings
from .ingest import scope, job_key

PUBLICATION_LAG_WINDOW_DAYS = 30


def parse(value):
    # Local run/first_seen timestamps are UTC. Do not use this implicit UTC fallback
    # for source publication timestamps; publication_lag handles them explicitly.
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def publication_lag(rows, first_seen, reliable_since, now, source_offset_minutes=None):
    """Observed arrivals only; unknown source timezone is not a zero-hour delay."""
    start=max(reliable_since, now-timedelta(days=PUBLICATION_LAG_WINDOW_DAYS))
    excluded=dict(excluded_published_before_reliable_window=0,
                  excluded_no_reliable_first_seen_time=0, excluded_unknown_timezone=0,
                  excluded_invalid_timestamp=0, excluded_negative_lag=0,
                  excluded_future_publication=0, excluded_future_first_seen=0)
    values=[]
    negative=[]
    for row in rows:
        try:
            published=datetime.fromisoformat(row['published_at'].replace('Z','+00:00'))
            if published.tzinfo is None:
                if source_offset_minutes is None:
                    excluded['excluded_unknown_timezone']+=1
                    continue
                published=published.replace(tzinfo=timezone(timedelta(minutes=source_offset_minutes)))
            published=published.astimezone(timezone.utc)
            if published<now-timedelta(days=PUBLICATION_LAG_WINDOW_DAYS):
                continue
            if published<=reliable_since:
                excluded['excluded_published_before_reliable_window']+=1
                continue
            fetched=first_seen.get(row['id'])
            if not fetched:
                excluded['excluded_no_reliable_first_seen_time']+=1
                continue
            seen=parse(fetched)
            hours=(seen-published).total_seconds()/3600
            if hours<0:
                excluded['excluded_negative_lag']+=1
                if len(negative)<10:
                    negative.append({'id':row['id'],'hours':hours})
                continue
            if published>now:
                excluded['excluded_future_publication']+=1
                continue
            if seen>now:
                excluded['excluded_future_first_seen']+=1
                continue
            values.append(hours)
        except (ValueError,TypeError,AttributeError,OverflowError):
            excluded['excluded_invalid_timestamp']+=1
    return {'status':'observed_only' if values else 'undetermined', 'sla_confirmed':False,
            'coverage':'already_ingested_announcements_only',
            'source_utc_offset_minutes':source_offset_minutes,
            'window_start':start.isoformat(),'window_end':now.isoformat(),
            'n_announcements':len(values),
            'median_hours':statistics.median(values) if values else None,
            'max_hours':max(values) if values else None,
            'over_24h':sum(h>24 for h in values),
            'negative_lag_examples':negative, **excluded}


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
        # publishDate is the only field OWS reports as an actual portal-publication timestamp
        # (as opposed to indexDate, our/OWS's own reindex time); plans/lots/contracts carry
        # no equivalent, so this lag can only be measured for announcements, not the full dataset.
        # Restricted to recently-published announcements: including old ones would mix in the
        # one-time initial backfill's catch-up delay (fetched long after an old publish date by
        # construction) and make steady-state ingest latency look artificially large.
        ann_rows=[dict(r) for r in conn.execute(select(db.announcements.c.id,db.announcements.c.published_at)
            .where(or_(db.announcements.c.organizer_bin.in_(bins),db.announcements.c.customer_bin.in_(bins)),
                   db.announcements.c.year.in_(years),db.announcements.c.published_at.is_not(None))).mappings()]
        # first_seen.seen_at is written once via INSERT ... ON CONFLICT DO NOTHING and is never
        # updated afterward, unlike raw_objects.fetched_at (which a same-content refetch
        # overwrites). This is the only column that can answer "when did we first see this".
        first_seen={}
        if ann_rows:
            first_seen=dict(conn.execute(select(db.first_seen.c.source_id,db.first_seen.c.seen_at)
                .join(db.announcements,db.first_seen.c.source_id==db.announcements.c.id)
                .where(db.first_seen.c.entity=='TrdBuy',
                       or_(db.announcements.c.organizer_bin.in_(bins),db.announcements.c.customer_bin.in_(bins)),
                       db.announcements.c.year.in_(years))).all())
        # An announcement first observed during the initial backfill necessarily looks "late"
        # relative to its (possibly old) publishDate; that is a one-time catch-up cost, not a
        # steady-state ingest delay. Only count announcements first seen after the configured
        # scope's initial backfill finished, so the metric reflects ongoing incremental cadence.
        first_backfill_complete=min((r['finished_at'] for r in relevant
                                      if r['mode']=='backfill' and r['status']=='complete' and r['finished_at']),
                                     default=None)
        # first_seen only distinguishes a true first sighting from a routine refetch for
        # records that had no raw_objects row before this measurement existed (see the
        # caveat on db.first_seen). An announcement published before this epoch marker may
        # already have been sitting in raw_objects when the fix shipped, so its first
        # first_seen write could be an arbitrary later refetch, not a real first sighting.
        epoch_row=conn.execute(select(db.first_seen_epoch.c.started_at).where(db.first_seen_epoch.c.id==1)).scalar_one_or_none()
    by_key={j['key']:j for j in jobs}
    partitions=[]
    for bin_ in bins:
        for year in years:
            for entity,filters in [('Plans',{'subjectBiin':bin_,'plnPointYear':[year]}),('Contract',{'customerBin':bin_,'finYear':year})]:
                job=by_key.get(job_key(entity,filters))
                partitions.append({'entity':entity,'bin':bin_,'year':year,'complete':bool(job and job['complete']),
                                   'rows':job['rows'] if job else 0,'error':job['error'] if job else None})
    active_base_keys={job_key(p['entity'], {'subjectBiin':p['bin'],'plnPointYear':[p['year']]} if p['entity']=='Plans' else {'customerBin':p['bin'],'finYear':p['year']}) for p in partitions}
    current_jobs=[j for j in jobs if j['key'] in active_base_keys or
                  (j['entity']=='Lots' and 'pointList' in j['filters'] and j['filters'].get('customerBin') in bins)]
    peer_jobs=[j for j in jobs if j['entity'] in ('Plans','Lots','Contract') and
               (j['filters'].get('refEnstruCode') or j['filters'].get('trdBuyId') or
                ('pointList' in j['filters'] and not j['filters'].get('customerBin')))]
    obsolete_jobs=[j for j in jobs if j not in current_jobs and j not in peer_jobs and
                   not (j['entity']=='Plans' and 'id' in j['filters']) and not (j['entity']=='Lots' and 'id' in j['filters'])]
    link_jobs=[j for j in jobs if 'id' in j['filters'] and j['entity'] in ('Plans','Lots')]
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
    ann_lag=None
    if first_backfill_complete is None or epoch_row is None:
        ann_lag={'status':'undetermined','reason':'No completed backfill run recorded for this scope yet, or first_seen_epoch is missing (schema not migrated); cannot yet separate reliable first-seen data from initial-backfill catch-up or pre-fix records.'}
    else:
        # An announcement is only trustworthy for this metric if it could not possibly have
        # existed in raw_objects before BOTH (a) this scope's initial backfill finished, and
        # (b) the first_seen-tracking fix was deployed on this database -- i.e. its own
        # publishDate is after the later of the two. Gating on seen_at instead (as an earlier
        # version of this code did) does not work: a pre-existing, pre-fix record's first
        # first_seen write can land arbitrarily late (whenever it next happens to be
        # refetched), which looks identical to a genuine recent first-sighting.
        reliable_since=max(parse(first_backfill_complete),parse(epoch_row))
        ann_lag=publication_lag(ann_rows,first_seen,reliable_since,now,
                                settings.OWS_PUBLICATION_UTC_OFFSET_MINUTES)
    return {'scope':{'bins':bins,'years':years},'plan_links':links,
            'latest_publication_sweep':next((r for r in runs if r['mode']=='publication'
                and sorted((r['details'] or {}).get('bins',[]))==sorted(bins)
                and sorted((r['details'] or {}).get('years',[]))==sorted(years)),None),
            'partitions':partitions,'complete_partitions':sum(p['complete'] for p in partitions),
            'total_partitions':len(partitions),'partition_definition':'Configured BIN/year Plans and Contract backfill partitions; lot/link hydration is tracked separately.',
            'last_successful_run':success,'freshness_seconds':age,'stale':age is None or age>86400,
            'latest_run':relevant[0] if relevant else None,
            'backlog':{'incomplete_jobs':sum(not j['complete'] for j in current_jobs),
                       'error_jobs':sum(bool(j['error']) for j in current_jobs),
                       'historical_incomplete_jobs':sum(not j['complete'] for j in obsolete_jobs),
                       'peer_incomplete_jobs':sum(not j['complete'] for j in peer_jobs),
                       'link_lookup_incomplete_jobs':sum(not j['complete'] for j in link_jobs)},
            'job_errors':[{'key':j['key'],'entity':j['entity'],'error':j['error']} for j in jobs if j['error']],
            'latest_source_index_dates':latest,
            'source_index_lag_seconds':index_lag,
            'source_index_note':'Database-wide source index dates and lag describe indexed source records, not a publication SLA or proof that all recent publications are available.',
            'announcement_publication_lag':ann_lag,
            'announcement_publication_lag_note': 'Observed hours from first_seen.seen_at minus publishDate, only after backfill and first_seen_epoch within the recent window. Naive publication times are excluded unless OWS_PUBLICATION_UTC_OFFSET_MINUTES is explicitly configured after source verification. Negative delays are excluded and exposed, never clamped to zero. Unknown-timezone exclusions count all selected stored announcements because their absolute window is unknown. This is not an SLA certification: only already-ingested announcements are visible, never-ingested publications are absent, and plans/contracts/lots have no equivalent publication timestamp.'}
