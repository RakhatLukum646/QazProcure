import hashlib
import html
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from . import db


def clean(value):
    text = html.unescape(str(value or ''))
    text = re.sub(r'<[^>]*>', ' ', text)
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def number(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def archive(conn, entity, obj):
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    db.upsert(conn, db.raw, dict(key=f'{entity}:{obj["id"]}:{digest}', entity=entity,
              source_id=obj['id'], hash=digest, payload=obj, fetched_at=db.now()))


def references(conn, obj):
    for kind, value in obj.items():
        if kind.startswith('Ref') and isinstance(value, dict):
            code = value.get('code') or value.get('id')
            if code is not None:
                db.upsert(conn, db.refs, dict(kind=kind, code=str(code), name_ru=value.get('nameRu') or value.get('name'), name_kz=value.get('nameKz'), payload=value))


def plan(conn, p):
    if not p or not p.get('id'):
        return
    archive(conn, 'Plans', p)
    references(conn, p)
    db.upsert(conn, db.plans, dict(id=p['id'], root_id=p.get('rootrecordId') or p['id'],
        customer_bin=p.get('subjectBiin'), year=p.get('plnPointYear'), name_ru=clean(p.get('nameRu')),
        name_kz=clean(p.get('nameKz')), description=clean(' '.join(p.get(k) or '' for k in ('descRu','extraDescRu'))),
        enstru=p.get('refEnstruCode'), unit=p.get('refUnitsCode'), quantity=number(p.get('count')),
        price=number(p.get('price')), amount=number(p.get('amount')), subject_type=p.get('refSubjectTypeId'),
        status=p.get('refPlnPointStatusId'), active=bool(p.get('isActive')), deleted=bool(p.get('isDeleted')), index_date=p.get('indexDate')))
    # Replace child collection atomically; removed delivery locations must not linger.
    conn.execute(db.deliveries.delete().where(db.deliveries.c.plan_id == p['id']))
    for d in p.get('PlansKato') or []:
        references(conn, d)
        db.upsert(conn, db.deliveries, dict(id=d['id'], plan_id=p['id'], kato=d.get('refKatoCode'),
            place=clean(d.get('fullDeliveryPlaceNameRu')), quantity=number(d.get('count')),
            active=not bool(d.get('isDeleted')) and d.get('isActive') != 0))


def announcement(conn, a):
    if not a:
        return None
    archive(conn, 'TrdBuy', a)
    references(conn, a)
    db.upsert(conn, db.announcements, dict(id=a['id'], number=a.get('numberAnno'), name_ru=clean(a.get('nameRu')),
        name_kz=clean(a.get('nameKz')), organizer_bin=a.get('orgBin'), customer_bin=a.get('customerBin'),
        published_at=a.get('publishDate'), year=(a.get('finYear')[0] if len(a.get('finYear')) == 1 else None) if isinstance(a.get('finYear'), list) else a.get('finYear'), status=a.get('refBuyStatusId'), index_date=a.get('indexDate')))
    return a['id']


def lot(conn, l):
    archive(conn, 'Lots', l)
    references(conn, l)
    aid = announcement(conn, l.get('TrdBuy'))
    db.upsert(conn, db.lots, dict(id=l['id'], number=l.get('lotNumber'), announcement_id=aid,
        customer_bin=l.get('customerBin'), name_ru=clean(l.get('nameRu')), name_kz=clean(l.get('nameKz')),
        description=clean(l.get('descriptionRu')), quantity=number(l.get('count')), amount=number(l.get('amount')),
        status=l.get('refLotStatusId'), deleted=bool(l.get('isDeleted')), index_date=l.get('indexDate')))
    conn.execute(db.lot_plans.delete().where(db.lot_plans.c.lot_id == l['id']))
    for p in l.get('Plans') or []:
        plan(conn, p)
        db.upsert(conn, db.lot_plans, dict(lot_id=l['id'], plan_id=p['id']))


def contract(conn, c):
    archive(conn, 'Contract', c)
    references(conn, c)
    for role in ('Customer', 'Supplier'):
        o = c.get(role)
        if o:
            db.upsert(conn, db.organizations, dict(id=o['pid'], bin=o.get('bin'), name_ru=clean(o.get('nameRu')), name_kz=clean(o.get('nameKz'))))
    aid = announcement(conn, c.get('TrdBuy'))
    db.upsert(conn, db.contracts, dict(id=c['id'], root_id=c.get('rootId') or c['id'], parent_id=c.get('parentId'),
        number=c.get('contractNumberSys') or c.get('contractNumber'), customer_bin=c.get('customerBin'), supplier_bin=c.get('supplierBiin'),
        signed_at=c.get('signDate'), year=c.get('finYear'), status=c.get('refContractStatusId'), currency=c.get('refCurrencyCode'),
        with_vat=bool(c.get('withNds')), amount=number(c.get('contractSum')), deleted=bool(c.get('deleted')),
        announcement_id=aid, index_date=c.get('indexDate')))
    # Mark missing former children as removed instead of leaving stale analytical rows.
    conn.execute(db.items.update().where(db.items.c.contract_id == c['id']).values(deleted=True))
    for i in c.get('ContractUnits') or []:
        plan(conn, i.get('Plans'))
        db.upsert(conn, db.items, dict(id=i['id'], contract_id=c['id'], root_id=i.get('rootId') or i['id'],
            lot_id=i.get('lotId') or None, plan_id=i.get('plnPointId') or None,
            price=number(i.get('itemPrice')), price_with_vat=number(i.get('itemPriceWnds')),
            quantity=number(i.get('quantity')), deleted=bool(i.get('deleted') or i.get('crDeleted')), index_date=i.get('indexDate')))


NORMALIZERS = {'Contract': contract, 'Plans': plan, 'Lots': lot}
