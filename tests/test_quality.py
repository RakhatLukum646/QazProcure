from datetime import datetime, timezone
import pytest
from app import db
from app.ingest import incremental_since, job_key
from app.quality import quality_report
from app.quality import publication_lag


def test_publication_lag_never_turns_negative_or_unknown_time_into_success():
    rows=[{'id':1,'published_at':'2026-02-02T15:00:00+00:00'},
          {'id':2,'published_at':'2026-02-02 15:00:00'},
          {'id':3,'published_at':'2026-02-02T10:00:00+00:00'}]
    first={1:'2026-02-02T11:00:00+00:00',2:'2026-02-02T11:00:00+00:00',
           3:'2026-02-02T12:00:00+00:00'}
    lag=publication_lag(rows,first,datetime(2026,2,1,tzinfo=timezone.utc),
                        datetime(2026,2,3,tzinfo=timezone.utc))
    assert lag['excluded_negative_lag']==1
    assert lag['negative_lag_examples']==[{'id':1,'hours':-4}]
    assert lag['excluded_unknown_timezone']==1
    assert lag['n_announcements']==1 and lag['median_hours']==2
    assert lag['sla_confirmed'] is False


def test_publication_offset_is_applied_before_epoch_and_median_is_real_median():
    rows=[{'id':1,'published_at':'2026-02-02 14:00:00'},
          {'id':2,'published_at':'2026-02-02 15:00:00'},
          {'id':3,'published_at':'2026-02-02T11:00:00+00:00'}]
    first={1:'2026-02-02T12:00:00Z',2:'2026-02-02T11:00:00Z',3:'2026-02-02T14:00:00Z'}
    lag=publication_lag(rows,first,datetime(2026,2,2,9,30,tzinfo=timezone.utc),
                        datetime(2026,2,3,tzinfo=timezone.utc),300)
    assert lag['excluded_published_before_reliable_window']==1
    assert lag['n_announcements']==2 and lag['median_hours']==2


def test_publication_lag_includes_customer_when_organizer_is_outside_scope(engine):
    with engine.begin() as conn:
        set_epoch(conn,'2026-01-01T00:00:00Z')
        conn.execute(db.runs.insert(),run('done'))
        conn.execute(db.announcements.insert(),dict(id=1,organizer_bin='999999999999',
            customer_bin='000740001307',year=2024,published_at='2026-02-02T10:00:00Z'))
        conn.execute(db.first_seen.insert(),dict(entity='TrdBuy',source_id=1,seen_at='2026-02-02T12:00:00Z'))
    lag=quality_report(engine,{'bins':['000740001307'],'years':[2024]},
                       datetime(2026,2,3,tzinfo=timezone.utc))['announcement_publication_lag']
    assert lag['n_announcements']==1 and lag['median_hours']==2


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


def test_quality_announcement_lag_is_undetermined_without_a_completed_backfill(engine):
    with engine.begin() as conn:
        conn.execute(db.announcements.insert(),{'id':1,'organizer_bin':'000740001307','year':2024,
                                                  'published_at':'2026-01-20T00:00:00Z'})
        conn.execute(db.first_seen.insert(),{'entity':'TrdBuy','source_id':1,'seen_at':'2026-01-21T06:00:00+00:00'})
    result=quality_report(engine,{'bins':['000740001307'],'years':[2024]},
                           datetime(2026,2,10,tzinfo=timezone.utc))
    assert result['announcement_publication_lag']=={'status':'undetermined',
        'reason':'No completed backfill run recorded for this scope yet, or first_seen_epoch is missing (schema not migrated); cannot yet separate reliable first-seen data from initial-backfill catch-up or pre-fix records.'}


def set_epoch(conn,started_at):
    # The engine fixture's db.init_db() already wrote a real-wall-clock epoch row; tests that
    # simulate a fictional timeline must override it to a date that fits that timeline.
    conn.execute(db.first_seen_epoch.update().values(started_at=started_at))


def test_quality_measures_announcement_publication_lag_from_first_seen(engine):
    with engine.begin() as conn:
        set_epoch(conn,'2026-01-01T00:00:00+00:00')
        conn.execute(db.runs.insert(),run('backfill-done',started='2026-01-15T00:00:00+00:00'))
        # Announcements are archived under the OWS entity label 'TrdBuy', scoped by
        # organizer_bin (customer_bin is often null for announcements).
        conn.execute(db.announcements.insert(),[
            {'id':1,'organizer_bin':'000740001307','year':2024,'published_at':'2026-01-20T00:00:00Z'},
            # Published outside the 30-day lag window: a one-time backfill necessarily fetches
            # this long after its old publish date, which must not inflate the steady-state lag.
            {'id':2,'organizer_bin':'000740001307','year':2024,'published_at':'2024-01-01T00:00:00Z'},
            # Published inside the 30-day window but before the backfill finished: part of the
            # initial catch-up sweep, not steady incremental cadence -- must be excluded even
            # though it is recently published and already has a first_seen row.
            {'id':3,'organizer_bin':'000740001307','year':2024,'published_at':'2026-01-12T00:00:00Z'},
            # Published inside the window, after the backfill, but no first_seen row has been
            # written for it yet -- must be excluded (not counted as a near-zero lag). It may
            # well already be sitting in announcements/raw_objects; this says nothing about
            # whether it was ingested, only that its first-seen time isn't known yet.
            {'id':4,'organizer_bin':'000740001307','year':2024,'published_at':'2026-01-22T00:00:00Z'}])
        # first_seen.seen_at is what the lag is computed from -- raw_objects.fetched_at is NOT
        # used because an unchanged-content refetch (see tests/test_db.py) would overwrite it.
        conn.execute(db.first_seen.insert(),[
            {'entity':'TrdBuy','source_id':1,'seen_at':'2026-01-21T06:00:00+00:00'},
            {'entity':'TrdBuy','source_id':2,'seen_at':'2026-01-21T06:00:00+00:00'},
            {'entity':'TrdBuy','source_id':3,'seen_at':'2026-01-13T12:00:00+00:00'}])
        # A later, unrelated re-fetch of announcement 1's unchanged content: raw_objects gets
        # touched, but must not affect the lag (which reads first_seen, not raw_objects).
        conn.execute(db.raw.insert(),{'key':'TrdBuy:1:h','entity':'TrdBuy','source_id':1,
                                       'hash':'h','payload':{},'fetched_at':'2026-02-09T00:00:00+00:00'})
    result=quality_report(engine,{'bins':['000740001307'],'years':[2024]},
                           datetime(2026,2,10,tzinfo=timezone.utc))
    lag=result['announcement_publication_lag']
    assert lag['n_announcements']==1
    assert lag['median_hours']==pytest.approx(30.0)
    assert lag['max_hours']==pytest.approx(30.0)
    assert lag['over_24h']==1
    assert lag['excluded_published_before_reliable_window']==1
    assert lag['excluded_no_reliable_first_seen_time']==1
    assert 'first_seen.seen_at' in result['announcement_publication_lag_note']


def test_quality_excludes_announcements_published_before_the_first_seen_epoch(engine):
    # Reproduces the exact scenario an external review flagged: an announcement published
    # and ingested long before the first_seen fix shipped, then merely refetched (unchanged
    # content) afterward. Its first_seen row therefore records the refetch time, not a real
    # first sighting, and must not be read as a ~300-hour SLA violation.
    with engine.begin() as conn:
        set_epoch(conn,'2026-01-20T00:00:00+00:00')  # fix deployed well after this announcement existed
        conn.execute(db.runs.insert(),run('backfill-done',started='2026-01-05T00:00:00+00:00'))
        conn.execute(db.announcements.insert(),
            {'id':1,'organizer_bin':'000740001307','year':2024,'published_at':'2026-01-15T00:00:00Z'})
        # Genuinely ingested within an hour of publication, long before the epoch -- but the
        # first_seen row could only be created by a refetch after the epoch, so it must not
        # be trusted even though it happens to be close to the true value here.
        conn.execute(db.first_seen.insert(),
            {'entity':'TrdBuy','source_id':1,'seen_at':'2026-01-23T00:00:00+00:00'})
    result=quality_report(engine,{'bins':['000740001307'],'years':[2024]},
                           datetime(2026,2,10,tzinfo=timezone.utc))
    lag=result['announcement_publication_lag']
    assert lag['n_announcements']==0
    assert lag['excluded_published_before_reliable_window']==1


def test_quality_separates_old_and_peer_jobs_from_core_backlog(engine):
    entries=[('Lots',{'customerBin':'000740001307'}),
             ('Plans',{'id':[999]}),
             ('Contract',{'trdBuyId':[1]})]
    with engine.begin() as conn:
        for entity,filters in entries:
            conn.execute(db.jobs.insert(),dict(key=job_key(entity,filters),entity=entity,
                                               filters=filters,complete=False,rows=0))
    result=quality_report(engine,{'bins':['000740001307'],'years':[2024]})
    assert result['backlog']['incomplete_jobs']==0
    assert result['backlog']['historical_incomplete_jobs']==1
    assert result['backlog']['link_lookup_incomplete_jobs']==1
    assert result['backlog']['peer_incomplete_jobs']==1


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
