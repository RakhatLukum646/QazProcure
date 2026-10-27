from datetime import datetime,timezone
import pytest
from sqlalchemy import select
from app import db,settings
from app.publication import sweep
from app.ows import OWSError


def test_global_publication_scan_includes_customer_only_and_records_completeness(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'OWS_PUBLICATION_UTC_OFFSET_MINUTES',300)
    eng=db.make_engine('sqlite:///'+str(tmp_path/'p.db'));db.init_db(eng)
    class Source:
        def query(self,q,v):
            assert v['filter']['publishDate'][1]=='2026-10-23 15:00:00'
            return {'data':{'TrdBuy':[{'id':1,'orgBin':'other','customerBin':'buyer','finYear':[2026],
                'publishDate':'2026-10-23 14:00:00'},{'id':2,'orgBin':'other','finYear':[2026]}]},
                'extensions':{'pageInfo':{'totalCount':2,'hasNextPage':False,'lastId':1}}}
    try:
        result=sweep(Source(),['buyer'],[2026],eng,datetime(2026,10,23,10,tzinfo=timezone.utc))
        assert result['complete'] and result['matched_ids']==[1] and result['scanned']==2
        with eng.connect() as conn:
            assert conn.execute(select(db.announcements.c.id)).scalars().all()==[1]
            assert conn.execute(select(db.runs.c.status)).scalar()=='complete'
    finally:eng.dispose()


def test_publication_count_mismatch_is_failed_not_complete(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'OWS_PUBLICATION_UTC_OFFSET_MINUTES',300)
    eng=db.make_engine('sqlite:///'+str(tmp_path/'p.db'));db.init_db(eng)
    class Source:
        def query(self,q,v):
            return {'data':{'TrdBuy':[]},'extensions':{'pageInfo':{'totalCount':2,'hasNextPage':False}}}
    try:
        with pytest.raises(OWSError,match='count mismatch'):sweep(Source(),['buyer'],[2026],eng)
        with eng.connect() as conn:assert conn.execute(select(db.runs.c.status)).scalar()=='failed'
    finally:eng.dispose()
