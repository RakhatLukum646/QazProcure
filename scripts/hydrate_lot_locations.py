"""Enrich stored lots from OWS's explicit delivery KATO list, without rewriting plans."""
from sqlalchemy import select
from app import db
from app.ows import OWSClient,OWSError
from app.ingest import hydrate_kato_parents
from app.normalize import archive


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enstru',help='Limit enrichment to lots explicitly linked to this product code')
    args=parser.parse_args()
    db.init_db()
    with db.engine.connect() as conn:
        query=select(db.lots.c.id)
        if args.enstru:
            query=query.join(db.lot_plans,db.lots.c.id==db.lot_plans.c.lot_id).join(
                db.plans,db.plans.c.id==db.lot_plans.c.plan_id).where(db.plans.c.enstru==args.enstru).distinct()
        ids=list(conn.execute(query.order_by(db.lots.c.id)).scalars())
    client=OWSClient()
    try:
        for offset in range(0,len(ids),90):
            batch=ids[offset:offset+90]
            body=client.query('query($f:LotsFiltersInput){Lots(filter:$f,limit:100){id pointList plnPointKatoList isDeleted}}',{'f':{'id':batch}})
            records=body['data'].get('Lots') or []
            if body.get('extensions',{}).get('pageInfo',{}).get('hasNextPage') is not False:
                raise OWSError('Location batch exceeds complete page')
            with db.engine.begin() as conn:
                for row in records:
                    if row['id'] not in batch:raise OWSError('Unrequested lot')
                    archive(conn,'LotDeliveryEvidence',row)
                    conn.execute(db.lot_delivery_codes.delete().where(db.lot_delivery_codes.c.lot_id==row['id']))
                    if row.get('isDeleted'):continue
                    for code in set(row.get('plnPointKatoList') or []):
                        if code and str(code).isdigit():
                            db.upsert(conn,db.lot_delivery_codes,dict(lot_id=row['id'],kato=str(code)))
            print(f'{min(offset+90,len(ids))}/{len(ids)}',flush=True)
        print(hydrate_kato_parents(client),flush=True)
    finally:client.close()


if __name__=='__main__':main()
