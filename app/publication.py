"""Complete recent-publication sweep, including customer-only announcements.

OWS has no customerBin filter on TrdBuy, so enumerate a bounded global window
and retain relevant customers/organizers locally. This avoids waiting for a lot
or contract to make a new publication discoverable.
"""
from datetime import datetime,timedelta,timezone
import uuid
from sqlalchemy import select
from . import db,settings
from .ows import ANNOUNCEMENT_FIELDS,OWSError
from .normalize import announcement


def sweep(client,bins,years,eng=db.engine,now=None,window_hours=72):
    offset=settings.OWS_PUBLICATION_UTC_OFFSET_MINUTES
    if offset is None:
        raise ValueError('Publication source timezone must be configured before sweep')
    now=now or datetime.now(timezone.utc)
    zone=timezone(timedelta(minutes=offset))
    start=now-timedelta(hours=window_hours)
    window=[d.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S') for d in (start,now)]
    run_id=str(uuid.uuid4())
    detail={'bins':bins,'years':years,'window_utc':[start.isoformat(),now.isoformat()],
            'source_window':window,'source_offset_minutes':offset,'scanned':0,'matched':0,
            'matched_ids':[],'source_total':None,'pages':0,'complete':False}
    with eng.begin() as conn:
        db.upsert(conn,db.runs,dict(id=run_id,mode='publication',started_at=db.now(),status='running',details=detail))
    query='query($filter:TrdBuyFiltersInput,$after:Int){TrdBuy(filter:$filter,limit:100,after:$after){'+ANNOUNCEMENT_FIELDS+'}}'
    after=None
    seen=set()
    status='failed'
    try:
        while True:
            body=client.query(query,{'filter':{'publishDate':window},'after':after})
            info=body.get('extensions',{}).get('pageInfo',{})
            records=body.get('data',{}).get('TrdBuy')
            if records is None and info.get('hasNextPage') is False and info.get('totalCount')==0:
                records=[]
            if not isinstance(records,list) or 'hasNextPage' not in info or not isinstance(info.get('totalCount'),int):
                raise OWSError('Publication pagination evidence is incomplete')
            if detail['source_total'] is None:
                detail['source_total']=info['totalCount']
            elif detail['source_total']!=info['totalCount']:
                raise OWSError('Publication window changed during enumeration; retry next cycle')
            ids={r['id'] for r in records}
            if seen & ids or len(ids)!=len(records):
                raise OWSError('Duplicate publication page; cannot certify enumeration')
            seen.update(ids)
            with eng.begin() as conn:
                for row in records:
                    yy=row.get('finYear')
                    yy=yy if isinstance(yy,list) else [yy]
                    if (row.get('orgBin') in bins or row.get('customerBin') in bins) and set(yy)&set(years):
                        announcement(conn,row)
                        detail['matched_ids'].append(row['id'])
            detail.update(scanned=len(seen),matched=len(detail['matched_ids']),pages=detail['pages']+1)
            if not info['hasNextPage']:
                if len(seen)!=detail['source_total']:
                    raise OWSError('Publication count mismatch')
                break
            cursor=info.get('lastId')
            if not records or cursor is None or cursor==after:
                raise OWSError('Publication cursor did not advance')
            after=cursor
        detail['complete']=True
        status='complete'
        return detail
    finally:
        with eng.begin() as conn:
            conn.execute(db.runs.update().where(db.runs.c.id==run_id).values(
                finished_at=db.now(),status=status,details=detail))
