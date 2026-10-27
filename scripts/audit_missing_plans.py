"""Read-only OWS check of every missing plan ID in the configured scope.

No warehouse writes, no assertion that a zero-result ID was physically deleted.
The output includes requested IDs and page metadata so source absence is auditable.
"""
import argparse
import hashlib
import json
from pathlib import Path
from sqlalchemy import select
from app import db
from app.ingest import scope
from app.ows import OWSClient, page_query


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('submission/missing_plans_audit.json'))
    args=parser.parse_args()
    configured=scope()
    with db.engine.connect() as conn:
        rows=conn.execute(select(db.items.c.id,db.items.c.plan_id).select_from(
            db.items.join(db.contracts,db.items.c.contract_id==db.contracts.c.id)
            .outerjoin(db.plans,db.items.c.plan_id==db.plans.c.id)).where(
                db.contracts.c.customer_bin.in_(configured['bins']),
                db.contracts.c.year.in_(configured['years']),db.items.c.plan_id.is_not(None),
                db.plans.c.id.is_(None))).all()
    ids=sorted({r.plan_id for r in rows})
    report={'started_at':db.now(),'scope':configured,'missing_items_at_start':len(rows),
            'missing_distinct_plan_ids':len(ids),'complete':False,'batches':[],
            'interpretation':'not_returned means absent from this authenticated OWS query at this time, not proof of deletion'}
    client=OWSClient()
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for offset in range(0,len(ids),30):
            batch=ids[offset:offset+30]
            entry={'requested_ids':batch,'checked_at':db.now(),'returned_ids':[], 'pages':[]}
            after=None
            try:
                while True:
                    body=client.query(page_query('Plans'),{'filter':{'id':batch},'limit':100,'after':after})
                    info=body.get('extensions',{}).get('pageInfo',{})
                    records=body['data'].get('Plans')
                    if records is None and info.get('hasNextPage') is False and info.get('totalCount')==0:
                        records=[]
                    if not isinstance(records,list) or 'hasNextPage' not in info:
                        raise ValueError('Incomplete pagination evidence')
                    returned=[int(r['id']) for r in records]
                    if not set(returned)<=set(batch):
                        raise ValueError('OWS returned an unrequested ID')
                    entry['returned_ids'].extend(returned)
                    entry['pages'].append({'metadata':info,'response_sha256':hashlib.sha256(
                        json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
                    if not info['hasNextPage']:
                        break
                    next_id=info.get('lastId')
                    if next_id is None or next_id==after or not records:
                        raise ValueError('Pagination did not advance')
                    after=next_id
                entry['not_returned_ids']=sorted(set(batch)-set(entry['returned_ids']))
                entry['complete']=True
            except Exception as exc:
                entry.update(complete=False,error=type(exc).__name__)
            report['batches'].append(entry)
            report['updated_at']=db.now()
            save()
            print(f'Checked {min(offset+30,len(ids))}/{len(ids)} plan IDs',flush=True)
        report['complete']=all(b['complete'] for b in report['batches'])
        report['returned_ids']=sorted({i for b in report['batches'] for i in b['returned_ids']})
        report['not_returned_count']=sum(len(b.get('not_returned_ids',[])) for b in report['batches'])
        report['unverified_count']=sum(len(b['requested_ids']) for b in report['batches'] if not b['complete'])
        save()
    finally:
        client.close()


if __name__=='__main__':
    main()
