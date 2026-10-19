from datetime import datetime, timezone
import pytest
from app import db
from app.ingest import incremental_since, job_key
from app.quality import quality_report


@pytest.fixture
def engine(tmp_path):
    eng=db.make_engine('sqlite:///'+str(tmp_path/'quality.db'))
    db.init_db(eng)
    yield eng
    eng.dispose()


def run(id_,mode='backfill',status='complete',started='2026-01-01T00:00:00+00:00',bins=None):
    return dict(id=id_,mode=mode,status=status,started_at=started,finished_at=started,
                details={'bins':bins or ['000740001307'],'years':[2024]})


def test_initial_incremental_overlaps_old_backfill_start():
    prior=run('old')
    now=datetime(2026,2,1,tzinfo=timezone.utc)
    assert incremental_since([prior],prior['details']['bins'],[2024],now).isoformat()=='2025-12-31T00:00:00+00:00'


def test_incremental_ignores_failed_and_other_scope_runs():
    prior=run('good',mode='incremental')
    failed=run('failed',mode='incremental',status='failed',started='2026-02-01T00:00:00+00:00')
    other=run('other',mode='incremental',started='2026-02-02T00:00:00+00:00',bins=['111111111111'])
    assert incremental_since([failed,other,prior],prior['details']['bins'],[2024]).isoformat()=='2025-12-31T00:00:00+00:00'


def test_quality_distinguishes_link_failures_and_success_freshness(engine):
    with engine.begin() as conn:
        conn.execute(db.contracts.insert(),{'id':1,'customer_bin':'000740001307','year':2024})
        conn.execute(db.plans.insert(),[{'id':3,'active':False,'deleted':False},{'id':4,'active':True,'deleted':False}])
        conn.execute(db.items.insert(),[{'id':i,'contract_id':1,'plan_id':plan} for i,plan in enumerate([None,2,3,4],1)])
        for entry in [run('success'),run('new-failure',status='failed',started='2026-02-01T00:00:00+00:00'),run('irrelevant',started='2026-02-02T00:00:00+00:00',bins=['111111111111'])]:
            conn.execute(db.runs.insert(),entry)
        filters={'subjectBiin':'000740001307','plnPointYear':[2024]}
        conn.execute(db.jobs.insert(),{'key':job_key('Plans',filters),'entity':'Plans','filters':filters,'complete':True,'rows':2})
    result=quality_report(engine,{'bins':['000740001307'],'years':[2024]},datetime(2026,2,3,tzinfo=timezone.utc))
    assert result['plan_links']=={'items':4,'null_plan_id':1,'missing_plan_lookup':1,'invalid_or_deleted_plan':1}
    assert result['last_successful_run']['id']=='success'
    assert result['latest_run']['id']=='new-failure'
    assert result['stale'] is True
    assert result['complete_partitions']==1 and result['total_partitions']==2
    assert 'not a publication SLA' in result['source_index_note']


def test_worker_cadence_measured_from_cycle_start(monkeypatch):
    import app.cli as cli
    calls=[]
    monkeypatch.setattr(cli,'sync',lambda **kw:calls.append(kw['mode']))
    times=iter([0,40])
    def stop(delay):
        assert delay==60
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):cli.run_worker(100,sleep=stop,monotonic=lambda:next(times))
    assert calls==['backfill','incremental']


def test_worker_retries_failures(monkeypatch):
    import app.cli as cli
    def fail(**kwargs):raise RuntimeError('source unavailable')
    monkeypatch.setattr(cli,'sync',fail)
    def stop(delay):
        assert delay==300
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):cli.run_worker(100,sleep=stop)
