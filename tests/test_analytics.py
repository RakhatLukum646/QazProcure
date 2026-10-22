from datetime import date, datetime
import pytest
from app import db
from app.normalize import contract, plan
import app.analytics as analytics_mod
from app.analytics import (anomaly, fair_price, quantity_anomaly, statistics_for,
                           snapshot, grouped, analyze, AnalysisError)
from app.schemas import AnalysisRequest


def row(id_, price=100, quantity=1, year=2025, **kw):
    r=dict(item_id=id_,item_root=id_,contract_id=id_,contract_root=id_,lot_id=id_,plan_id=id_,
           customer_bin=f'{id_:012}',supplier_bin='123456789012',signed_at=f'{year}-03-10',year=year,
           enstru='123456.000.000001',unit='796',kato='750000000',subject_type=1,
           name_ru='Стул',name_kz='Орындық',description='Офисный стул черный',price=price,quantity=quantity)
    r.update(kw);return r


@pytest.fixture
def engine(tmp_path):
    eng=db.make_engine('sqlite:///'+str(tmp_path/'test.db'))
    db.init_db(eng)
    yield eng
    eng.dispose()


def test_weighted_mean_not_mean_of_prices():
    stats=statistics_for([row(1,100,9),row(2,200,1)])
    assert stats['weighted_price']==110
    assert stats['amount_net']==1100
    assert stats['median_price']==150


def test_anomaly_excludes_entire_contract_and_uses_strict_threshold():
    peers=[row(i,100) for i in range(1,6)]
    target=row(20,131)
    sibling=row(21,100000,contract_root=20)
    req=AnalysisRequest(intent='price_anomaly',threshold_pct=30,direction='above')
    result=anomaly([target],peers+[target,sibling],req)
    assert result['n_matches']==1
    e=result['examples'][0]
    assert e['reference_price']==100 and e['n_peers']==5
    assert 20 not in e['peer_contract_ids'] and 21 not in e['peer_contract_ids']
    assert e['deviation_pct']==pytest.approx(31)
    assert anomaly([row(20,130)],peers,req)['n_matches']==0


def test_anomaly_never_mixes_units_or_locations():
    target=row(20,200)
    peers=[row(i,100,unit='166') for i in range(1,7)]+[row(i+10,100,kato='710000000') for i in range(1,7)]
    result=anomaly([target],peers,AnalysisRequest(intent='price_anomaly'))
    assert result['n_evaluated']==0 and result['n_insufficient_peers']==1


def test_fair_price_requires_other_customers_same_description_and_place():
    target=row(30,140)
    peers=[row(i,100) for i in range(1,6)]
    bad=[row(40,1,customer_bin=target['customer_bin']),row(41,1,description='Другой товар'),row(42,1,kato='710000000')]
    req=AnalysisRequest(intent='fair_price',lot_id=30,years=[2025])
    report=fair_price([target],peers+bad,req)['examples'][0]
    assert report['fair_price']==100 and report['n_peers']==5
    assert report['status']=='provisional' and report['time_model'] is None
    assert report['region_factor']==1


def test_fair_price_no_future_leakage():
    target=row(30,140,signed_at='2025-03-01')
    peers=[row(i,100,signed_at='2025-03-31') for i in range(1,8)]
    r=fair_price([target],peers,AnalysisRequest(intent='fair_price',lot_id=30))
    assert r['n_matches']==0


def test_quantity_missing_year_is_not_zero():
    req=AnalysisRequest(intent='quantity_anomaly',years=[2023,2024,2025])
    rows=[row(1,quantity=10,year=2024,customer_bin='000740001307'),row(2,quantity=100,year=2025,customer_bin='000740001307')]
    assert quantity_anomaly(rows,req)['n_matches']==0
    rows.append(row(3,quantity=10,year=2023,customer_bin='000740001307'))
    e=quantity_anomaly(rows,req)['examples'][0]
    assert e['baseline_quantity']==10 and e['deviation_pct']==900


def test_quantity_anomaly_keeps_full_history_for_completed_past_years(monkeypatch):
    # Target year is "today's" year (in progress), so it gets clamped to the
    # same-day-of-year window. Prior years are already complete and must use
    # their full annual quantity even if their one contract was signed later
    # in the calendar year than today's cutoff date.
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 19, tzinfo=tz)
    monkeypatch.setattr(analytics_mod, 'datetime', FakeDateTime)
    req=AnalysisRequest(intent='quantity_anomaly',years=[2024,2025,2026],threshold_pct=20,direction='above')
    rows=[row(1,quantity=1513,year=2024,signed_at='2024-12-09',customer_bin='980440001034'),
          row(2,quantity=1458,year=2025,signed_at='2025-11-18',customer_bin='980440001034'),
          row(3,quantity=1916,year=2026,signed_at='2026-01-12',customer_bin='980440001034')]
    result=quantity_anomaly(rows,req)
    assert result['n_matches']==1
    e=result['examples'][0]
    assert e['baseline_quantity']==1485.5
    assert e['deviation_pct']==pytest.approx(28.98014137,abs=1e-6)


def test_quantity_anomaly_explicit_date_to_still_windows_history(monkeypatch):
    # When the caller explicitly asks for an equal sub-year window (date_to),
    # that window should still apply uniformly to past years too.
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 19, tzinfo=tz)
    monkeypatch.setattr(analytics_mod, 'datetime', FakeDateTime)
    req=AnalysisRequest(intent='quantity_anomaly',years=[2024,2025,2026],threshold_pct=20,direction='above',
                         date_to=date(2026,10,19))
    rows=[row(1,quantity=1513,year=2024,signed_at='2024-12-09',customer_bin='980440001034'),
          row(2,quantity=1458,year=2025,signed_at='2025-11-18',customer_bin='980440001034'),
          row(3,quantity=1916,year=2026,signed_at='2026-01-12',customer_bin='980440001034')]
    result=quantity_anomaly(rows,req)
    assert result['n_matches']==0
    assert result['n_insufficient_history']==1


def test_grouped_quantities_do_not_mix_units():
    r=grouped([row(1,unit='796'),row(2,unit='166')],AnalysisRequest(intent='compare'))
    assert r['n_groups']==2


def test_lot_id_takes_priority_over_a_stray_lot_number(engine):
    # The LLM planner sometimes fills both lot_id and lot_number for a purely
    # numeric lot reference (e.g. because the question said "лот номер"). A
    # stray lot_number that doesn't match any real lots.number must not override
    # an already-correct lot_id.
    with engine.begin() as conn:
        conn.execute(db.lots.insert().values(id=555,number='999-ОЛ-ОК1',customer_bin='123456789012',deleted=False))
    req=AnalysisRequest(intent='search',years=[2025],lot_id=555,lot_number='555')
    result=analyze(req,eng=engine)
    assert result['n_evaluated']==0  # no contract items seeded, but it must not raise


def test_numeric_lot_number_falls_back_to_lot_id(engine):
    # Some planner outputs put a purely numeric lot reference into lot_number
    # instead of lot_id. If it doesn't match any lots.number string, it should
    # be tried as a lot_id before giving up with "lot not found".
    with engine.begin() as conn:
        conn.execute(db.lots.insert().values(id=777,number='73050442-ЗЦП1',customer_bin='123456789012',deleted=False))
    req=AnalysisRequest(intent='search',years=[2025],lot_number='777')
    result=analyze(req,eng=engine)
    assert result['n_evaluated']==0  # resolved without raising AnalysisError


def test_unresolvable_lot_number_still_raises(engine):
    with pytest.raises(AnalysisError):
        analyze(AnalysisRequest(intent='search',years=[2025],lot_number='no-such-lot'),eng=engine)


def make_contract(id_,root=1,signed='2025-03-10',deleted=0,status=190,price=100):
    return dict(id=id_,rootId=root,signDate=signed,finYear=2025,customerBin='000740001307',supplierBiin='123456789012',
                refContractStatusId=status,refCurrencyCode='KZT',deleted=deleted,indexDate=signed,
                ContractUnits=[dict(id=id_*10,rootId=10,plnPointId=1,lotId=1,itemPrice=price,quantity=2,deleted=0)])


def seed_plan(conn):
    plan(conn,dict(id=1,rootrecordId=1,subjectBiin='000740001307',plnPointYear=2025,nameRu='Стул',
         refEnstruCode='123',refUnitsCode='796',isActive=1,isDeleted=0,PlansKato=[dict(id=1,refKatoCode='750000000',isActive=1)]))


def test_latest_signed_amendment_no_double_count(engine):
    with engine.begin() as conn:
        seed_plan(conn)
        contract(conn,make_contract(1))
        contract(conn,make_contract(2,signed='2025-04-01',price=120))
        contract(conn,make_contract(3,signed=None,price=999))
    rows,q=snapshot(engine)
    assert len(rows)==1 and rows[0]['price']==120


def test_deleted_latest_contract_suppresses_previous(engine):
    with engine.begin() as conn:
        seed_plan(conn);contract(conn,make_contract(1));contract(conn,make_contract(2,signed='2025-04-01',deleted=1))
    assert snapshot(engine)[0]==[]


def test_idempotent_ingestion_and_report_audit(engine):
    from sqlalchemy import select,func
    with engine.begin() as conn:
        seed_plan(conn);contract(conn,make_contract(1));contract(conn,make_contract(1))
    result=analyze(AnalysisRequest(years=[2025]),eng=engine)
    assert result['n_selected']==1
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.items)).scalar()==1
        saved=conn.execute(select(db.audits.c.result)).scalar()
        assert saved['quality']['snapshot_hash']==result['quality']['snapshot_hash']


def test_default_targets_stay_in_configured_bins_after_peer_ingestion(engine):
    with engine.begin() as conn:
        seed_plan(conn)
        contract(conn,make_contract(1))
        outside=make_contract(2,root=2)
        outside['customerBin']='123456789012'
        outside['ContractUnits'][0]['rootId']=20
        contract(conn,outside)
    default=analyze(AnalysisRequest(years=[2025]),eng=engine)
    explicit=analyze(AnalysisRequest(years=[2025],bins=['123456789012']),eng=engine)
    assert default['n_selected']==1
    assert explicit['n_selected']==1
    assert default['examples'][0]['customer_bin']=='000740001307'
    assert explicit['examples'][0]['customer_bin']=='123456789012'


def test_invalid_zero_quantity_excluded(engine):
    c=make_contract(1);c['ContractUnits'][0]['quantity']=0
    with engine.begin() as conn:seed_plan(conn);contract(conn,c)
    rows,q=snapshot(engine)
    assert not rows and q['excluded']['invalid_price_or_quantity']==1


def test_unit_conversion_preserves_amount():
    from app.analytics import normalize_unit
    r=normalize_unit(row(1,price=500000,quantity=2,unit='168'))
    assert r['unit']=='166' and r['quantity']==2000 and r['price']==500
    assert r['price']*r['quantity']==r['original_price']*r['original_quantity']
