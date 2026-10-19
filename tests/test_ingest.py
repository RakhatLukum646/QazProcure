import httpx
import pytest
from sqlalchemy import select,func
from app import db
from app.ows import OWSClient,OWSError
from app.ingest import ingest_pages


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
