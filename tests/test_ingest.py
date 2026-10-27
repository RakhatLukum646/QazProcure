import httpx
import pytest
from sqlalchemy import select,func
from app import db
from app.ows import OWSClient,OWSError
from app.ingest import ingest_pages, hydrate_kato_parents


@pytest.fixture
def engine(tmp_path):
    e=db.make_engine('sqlite:///'+str(tmp_path/'ingest.db'));db.init_db(e)
    yield e
    e.dispose()


def client(responses):
    it=iter(responses)
    def handler(request):
        assert str(request.url)=='https://ows.goszakup.gov.kz/v3/graphql'
        return httpx.Response(200,json=next(it))
    return OWSClient(token='test',transport=httpx.MockTransport(handler))


def test_null_zero_result_is_empty(engine):
    c=client([{'data':{'Plans':None},'extensions':{'pageInfo':{'totalCount':0,'hasNextPage':False,'lastId':0}}}])
    assert ingest_pages(c,'Plans',{'id':[1]},engine)['complete'] is True
    c.close()


def test_graphql_errors_do_not_advance_cursor(engine):
    c=client([{'errors':[{'message':'bad field'}],'data':{'Plans':[]}}])
    with pytest.raises(OWSError):ingest_pages(c,'Plans',{},engine)
    with engine.connect() as conn:
        state=conn.execute(select(db.jobs)).mappings().one()
        assert state['cursor'] is None and not state['complete']
    c.close()


def test_resume_committed_cursor(engine):
    pages=[{'data':{'Plans':[{'id':2}]},'extensions':{'pageInfo':{'hasNextPage':True,'lastId':2}}},
           {'data':{'Plans':[{'id':1}]},'extensions':{'pageInfo':{'hasNextPage':False,'lastId':1}}}]
    c=client(pages)
    first=ingest_pages(c,'Plans',{},engine,max_pages=1)
    assert first['rows']==1 and first['cursor']==2 and not first['complete']
    second=ingest_pages(c,'Plans',{},engine)
    assert second['rows']==2 and second['complete']
    with engine.connect() as conn:assert conn.execute(select(func.count()).select_from(db.plans)).scalar()==2
    c.close()


def test_page_failure_rolls_back_rows_and_cursor(engine):
    c=client([{'data':{'Plans':[{'id':2},{}]},'extensions':{'pageInfo':{'hasNextPage':False,'lastId':1}}}])
    # Empty objects are ignored by plan normalizer; use a malformed numeric id to fail SQL.
    c.close()
    c=client([{'data':{'Plans':[{'id':2},{'id':[3]}]},'extensions':{'pageInfo':{'hasNextPage':False,'lastId':1}}}])
    with pytest.raises(Exception):ingest_pages(c,'Plans',{},engine)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.plans)).scalar()==0
        assert conn.execute(select(db.jobs.c.cursor)).scalar() is None
    c.close()


def test_nonempty_null_is_failure(engine):
    c=client([{'data':{'Plans':None},'extensions':{'pageInfo':{'totalCount':10,'hasNextPage':False}}}])
    with pytest.raises(OWSError):ingest_pages(c,'Plans',{},engine)
    c.close()


def test_terminal_null_page_after_exact_page_size(engine):
    c=client([{'data':{'Plans':[{'id':1}]},'extensions':{'pageInfo':{'totalCount':1,'hasNextPage':True,'lastId':1}}},
              {'data':{'Plans':None},'extensions':{'pageInfo':{'totalCount':1,'hasNextPage':False,'lastId':0}}}])
    state=ingest_pages(c,'Plans',{},engine)
    assert state['complete'] and state['rows']==1
    c.close()


def test_missing_kato_ancestors_come_from_source_not_code_prefix(engine):
    with engine.begin() as conn:
        db.upsert(conn, db.plans, dict(id=1))
        db.upsert(conn, db.deliveries, dict(id=1, plan_id=1, kato='711210000', active=True))
        db.upsert(conn, db.refs, dict(kind='RefKato', code='711210000', name_ru='район Есиль',
                                      payload={'code':'711210000','parentCode':'710000000'}))

    class KatoClient:
        def kato(self, code):
            assert code == '710000000'
            return {'code':code, 'name_ru':'г.Астана', 'name_kz':'Астана қ.',
                    'parent_code':'000000000', 'level_':1}

    assert hydrate_kato_parents(KatoClient(), engine) == {'fetched':1,'source_absent':0}
    with engine.connect() as conn:
        parent=conn.execute(select(db.refs).where(db.refs.c.kind=='RefKato',db.refs.c.code=='710000000')).mappings().one()
    assert parent['payload']['parentCode']=='000000000'


def test_incremental_fetches_changed_lot_without_new_plan_chunk(engine, monkeypatch):
    from app import ingest
    with engine.begin() as conn:
        db.upsert(conn, db.plans, dict(id=1, customer_bin='000740001307', year=2025))

    seen=[]
    class Source:
        def query(self, query, variables):
            if 'TrdBuy(filter:' in query:
                return {'data':{'TrdBuy':[]},'extensions':{'pageInfo':{
                    'totalCount':0,'hasNextPage':False,'lastId':0}}}
            entity=next(name for name in ('Plans','Contract','Lots') if f'{{ {name}(' in query)
            filters=variables['filter']
            seen.append((entity,filters))
            records=[{'id':42,'customerBin':'000740001307','indexDate':'2026-10-19T11:00:00'}] if entity=='Lots' else []
            return {'data':{entity:records},'extensions':{'pageInfo':{
                'totalCount':len(records),'hasNextPage':False,'lastId':42 if records else 0}}}
        def close(self):pass

    monkeypatch.setattr(ingest,'OWSClient',Source)
    monkeypatch.setattr(ingest,'hydrate_kato_parents',lambda client,eng: {'fetched':0,'source_absent':0})
    result=ingest._sync(mode='incremental',bins=['000740001307'],years=[2025],eng=engine)
    assert result['status']=='complete'
    lots=[f for entity,f in seen if entity=='Lots']
    assert len(lots)==1 and lots[0]['customerBin']=='000740001307'
    assert 'indexDate' in lots[0] and 'pointList' not in lots[0]
    from datetime import datetime,timezone,timedelta
    assert datetime.strptime(lots[0]['indexDate'][1],'%Y-%m-%d %H:%M:%S').replace(
        tzinfo=timezone.utc)>datetime.now(timezone.utc)+timedelta(hours=23)
    with engine.connect() as conn:
        assert conn.execute(select(db.lots.c.id)).scalar_one()==42
