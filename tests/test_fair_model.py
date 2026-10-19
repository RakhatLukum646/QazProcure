import math
import pytest
from app.fair_model import city_index, joint_model


def test_city_walks_explicit_parent_not_prefix_or_level():
    rows=[dict(kind='RefKato',code='750000000',name_ru='г. Алматы',payload={}),
          dict(kind='RefKato',code='751110000',name_ru='Алмалинский район',payload={'parentCode':'750000000','level':2}),
          dict(kind='RefKato',code='751120000',name_ru='Другой район',payload={'parentCode':'missing'}),
          dict(kind='RefKato',code='101010000',name_ru='Семей',payload={'level':2})]
    cities=city_index(rows)
    assert cities['751110000']['city_code']=='750000000'
    assert cities['751110000']['city_path']==['751110000','750000000']
    assert '751120000' not in cities and '101010000' not in cities


def calibration():
    rows=[]
    for month in range(24):
        year=2023+month//12
        m=month%12+1
        for city,multiplier in [('A',1),('B',1.4)]:
            for _ in range(3):
                i=len(rows)+1
                rows.append(dict(item_id=i,contract_id=i,contract_root=i,city_code=city,
                    signed_at=f'{year}-{m:02}-10',price=100*multiplier*math.exp(.01*month+.08*math.sin(2*math.pi*m/12))))
    return rows


def test_joint_fit_separates_city_trend_seasonality_and_ignores_future():
    peers=calibration()
    future={**peers[-1],'item_id':999,'contract_id':999,'contract_root':999,'signed_at':'2030-01-01','price':1e12}
    model=joint_model(peers+[future],'2025-01-01','B')
    assert model['region_factor']==pytest.approx(1.4)
    assert model['region_reference_city']=='A'
    assert model['coefficients'][1]==pytest.approx(.01)
    assert model['coefficients'][2]==pytest.approx(.08)
    assert 999 not in model['calibration_item_ids']
    assert model['factor_components']['2023-01']['empirical_trend_factor']==pytest.approx(math.exp(.24))


def test_joint_fit_rejects_nonoverlapping_or_repeated_contract_history():
    peers=calibration()
    assert joint_model([r for r in peers if r['city_code']=='A' or r['signed_at']<'2024'],'2025-01-01','A') is None
    assert joint_model([{**r,'contract_root':1} for r in peers],'2025-01-01','A') is None


def test_fair_price_rolls_up_only_verified_same_city_and_preserves_insufficient_evidence():
    from app.analytics import fair_price
    from app.schemas import AnalysisRequest
    def row(i, **extra):
        r=dict(item_id=i,contract_id=i,contract_root=i,lot_id=i,plan_id=i,customer_bin=str(i),
               supplier_bin='supplier',signed_at='2025-03-10',year=2025,enstru='code',unit='796',
               kato=str(i),city_code='CITY',city_name='г. Город',city_resolution='reference_parent_hierarchy',
               subject_type=1,name_ru='Стул',name_kz='',description='Стул черный',quantity=1,price=100)
        r.update(extra)
        return r
    target=row(30,price=140)
    peers=[row(i) for i in range(1,6)]
    peers.append(row(10,city_code='OTHER',price=1))
    report=fair_price([target],peers,AnalysisRequest(intent='fair_price',lot_id=30))['examples'][0]
    assert report['fair_price']==100 and report['n_peers']==5
    assert 10 not in report['peer_contract_ids']
    insufficient=fair_price([target],[],AnalysisRequest(intent='fair_price',lot_id=30))['examples'][0]
    assert insufficient['contract_id']==30 and insufficient['price_net']==140
    assert insufficient['contract_url'].endswith('/30') and 'fair_price' not in insufficient


def test_unsigned_target_never_exposes_plan_price_as_actual(tmp_path):
    from app import db
    from app.analytics import planned_lot_targets, fair_price
    from app.schemas import AnalysisRequest
    eng=db.make_engine('sqlite:///'+str(tmp_path/'planned.db'))
    db.init_db(eng)
    with eng.begin() as conn:
        conn.execute(db.lots.insert(),dict(id=1,customer_bin='buyer',deleted=False))
        conn.execute(db.plans.insert(),dict(id=2,customer_bin='buyer',year=2025,enstru='code',unit='796',quantity=10,price=150,active=True,deleted=False))
        conn.execute(db.lot_plans.insert(),dict(lot_id=1,plan_id=2))
    req=AnalysisRequest(intent='fair_price',lot_id=1,years=[2025])
    targets=planned_lot_targets(req,eng)
    report=fair_price(targets,[],req)['examples'][0]
    assert report['target_kind']=='planned_lot' and report['planned_price']==150
    assert report['price_net'] is None and report['contract_url'] is None and report['lot_url'] is None
    assert report['deviation_pct'] is None and report['planned_price_vat_basis']=='unknown'
    with eng.begin() as conn:
        conn.execute(db.lots.insert(),dict(id=3,customer_bin='buyer'))
        conn.execute(db.lot_plans.insert(),dict(lot_id=3,plan_id=2))
    assert planned_lot_targets(req,eng)==[]
    eng.dispose()


@pytest.mark.parametrize('dates',[{'date_from':'2025-01-01'},{'date_to':'2025-12-31'}])
def test_planned_target_refuses_signing_date_filters(tmp_path,dates):
    from app import db
    from app.analytics import planned_lot_targets, AnalysisError
    from app.schemas import AnalysisRequest
    eng=db.make_engine('sqlite:///'+str(tmp_path/'dates.db'))
    db.init_db(eng)
    with pytest.raises(AnalysisError,match='даты подписания'):
        planned_lot_targets(AnalysisRequest(intent='fair_price',lot_id=1,**dates),eng)
    eng.dispose()


@pytest.mark.parametrize('status,currency,deleted',[(190,'USD',False),(330,'KZT',False),(190,'KZT',True)])
def test_known_signed_lot_cannot_fall_back_to_plan(tmp_path,status,currency,deleted):
    from app import db
    from app.analytics import analyze, planned_lot_targets, AnalysisError
    from app.schemas import AnalysisRequest
    eng=db.make_engine('sqlite:///'+str(tmp_path/'signed.db'))
    db.init_db(eng)
    with eng.begin() as conn:
        conn.execute(db.lots.insert(),dict(id=1,customer_bin='buyer',deleted=False))
        conn.execute(db.plans.insert(),dict(id=2,customer_bin='buyer',year=2025,enstru='code',unit='796',quantity=10,price=150,active=True,deleted=False))
        conn.execute(db.lot_plans.insert(),dict(lot_id=1,plan_id=2))
        conn.execute(db.contracts.insert(),dict(id=3,root_id=3,customer_bin='buyer',signed_at='2025-03-10',year=2025,status=status,currency=currency,deleted=deleted))
        conn.execute(db.items.insert(),dict(id=4,root_id=4,contract_id=3,lot_id=1,plan_id=2,price=0,quantity=10,deleted=False))
    req=AnalysisRequest(intent='fair_price',lot_id=1,years=[2026])
    with pytest.raises(AnalysisError,match='известен подписанный договор'):
        planned_lot_targets(req,eng)
    with pytest.raises(AnalysisError,match='известен подписанный договор'):
        analyze(req,eng=eng)
    eng.dispose()
