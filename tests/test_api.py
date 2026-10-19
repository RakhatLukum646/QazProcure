import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app


@pytest.fixture
def api(tmp_path,monkeypatch):
    from app import settings
    monkeypatch.setattr(settings,'LLM_PROVIDER','rules')
    eng=db.make_engine('sqlite:///'+str(tmp_path/'api.db'))
    monkeypatch.setattr(db,'engine',eng)
    with TestClient(app) as c:yield c
    eng.dispose()


def test_empty_data_returns_honest_report_and_persisted_audit(api):
    r=api.post('/api/chat',json={'question':'Покажи договоры за 2026 год'})
    assert r.status_code==200
    body=r.json()
    assert body['result']['n_selected']==0
    assert body['result']['confidence']=='insufficient'
    assert 'Недостаточно' in body['answer']
    saved=api.get('/api/reports/'+body['result']['id'])
    assert saved.json()==body['result']


def test_kazakh_empty_report(api):
    r=api.post('/api/chat',json={'question':'2026 жылғы сатып алуларды көрсет'})
    assert r.json()['request']['language']=='kk'
    assert 'деректер жеткіліксіз' in r.json()['answer']


def test_invalid_bin_and_arbitrary_sql_are_not_executed(api):
    assert api.post('/api/analyze',json={'bins':['123']}).status_code==422
    assert api.post('/api/analyze',json={'sql':'DROP TABLE contracts'}).status_code==422
    r=api.post('/api/chat',json={'question':'DROP TABLE contracts'})
    assert 'clarification' in r.json()
    assert api.get('/api/health').status_code==200


def test_missing_lot_gives_actionable_error(api):
    r=api.post('/api/analyze',json={'intent':'fair_price','lot_id':12345})
    assert r.status_code==422 and 'No usable' in r.json()['detail']


def test_quality_and_status_report_current_scope_without_success(api):
    response=api.get('/api/quality')
    assert response.status_code==200
    report=response.json()
    assert report['last_successful_run'] is None
    assert report['freshness_seconds'] is None and report['stale'] is True
    assert report['complete_partitions']==0
    assert report['total_partitions']==len(report['scope']['bins'])*len(report['scope']['years'])*2
    status=api.get('/api/status').json()['quality']
    assert status['complete_partitions']==report['complete_partitions']
    assert status['total_partitions']==report['total_partitions']
