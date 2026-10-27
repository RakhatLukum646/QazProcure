import json
import threading
import time

from app import acceptance
from app.schemas import AnalysisRequest


def dataset(tmp_path, cases=None):
    path = tmp_path / 'holdout.json'
    path.write_text(json.dumps(cases or [dict(id=str(i), question=str(i),
        expected={'intent': 'search', 'years': [2025]}) for i in range(5)]))
    return path


def test_missing_malformed_and_errors_remain_in_denominator(tmp_path):
    path = dataset(tmp_path)
    def planner(question, **kwargs):
        if question == '0':
            raise TimeoutError('secret API key must never be persisted')
        if question == '1':
            return {'intent': 'invalid'}
        return AnalysisRequest(years=[2025])
    result = acceptance.run(path, tmp_path / 'out.json', planner=planner)
    assert result['cases'] == 5
    assert result['total_fields'] == 10
    assert result['correct_fields'] == 6
    assert result['correct_cases'] == 3
    assert result['call_failures'] == 2
    assert 'secret' not in json.dumps(result)
    assert not acceptance.score_case({'id': 'x', 'question': 'q', 'expected': {'text': None}}, {})['exact']


def test_resume_identity_and_bounded_concurrency(tmp_path, monkeypatch):
    path = dataset(tmp_path)
    output = tmp_path / 'out.json'
    lock = threading.Lock()
    counts = {'active': 0, 'peak': 0, 'calls': 0}
    def planner(question, **kwargs):
        assert kwargs['provider'] == 'gemini'
        with lock:
            counts['active'] += 1
            counts['calls'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        time.sleep(.02)
        with lock:
            counts['active'] -= 1
        return AnalysisRequest(years=[2025])
    acceptance.run(path, output, planner=planner)
    assert counts['peak'] == 2
    resumed = acceptance.run(path, output, planner=planner)
    assert counts['calls'] == 5
    assert all(d['resumed'] for d in resumed['details'])
    monkeypatch.setattr(acceptance.settings, 'GEMINI_MODEL', 'different-model')
    acceptance.run(path, output, planner=planner)
    assert counts['calls'] == 10
    path.write_text(path.read_text() + ' ')
    acceptance.run(path, output, planner=planner)
    assert counts['calls'] == 15


def test_independent_fact_arithmetic_catches_wrong_amount(monkeypatch):
    from app import analytics
    rows = [dict(item_id=1, year=2025, customer_bin='000740001307',
                 price=12.5, quantity=4, contract_id=8)]
    monkeypatch.setattr(analytics, 'snapshot', lambda eng: (rows, {'snapshot_hash': 'hash'}))
    def wrong_analysis(request, **kwargs):
        return dict(n_selected=1, amount_net=49, quality={'snapshot_hash': 'hash'}, id='run',
                    examples=[dict(contract_id=8, contract_url='https://goszakup.gov.kz/ru/egzcontract/cpublic/show/8')])
    monkeypatch.setattr(analytics, 'analyze', wrong_analysis)
    result = acceptance.verify_facts(object())
    assert result['status'] == 'failed'
    assert len(result['checks']) == 3
    assert all(c['expected_amount'] == '50.0' and not c['fields']['amount'] for c in result['checks'])


def test_factual_score_requires_visible_source_fact_and_tracks_denominators():
    case = {'id': 'ru-real', 'question': 'Покажи договоры',
            'request': {'intent': 'search', 'language': 'ru'},
            'facts': [{'path': 'result.n_selected', 'value': 2,
                       'source': 'independent SQL count', 'answer_text': 'N = 2'}]}
    response = {'request': {'intent': 'search', 'language': 'ru'},
                'result': {'n_selected': 2, 'id': 'report'}, 'answer': 'N = 2'}
    good = acceptance.score_factual_case(case, response)
    assert good['exact'] and good['fact_correct'] == 1
    hidden = acceptance.score_factual_case(case, {**response, 'answer': 'No number shown'})
    assert not hidden['exact'] and hidden['fact_correct'] == 0
    failed = acceptance.score_factual_case(case, error='TimeoutError')
    totals = acceptance.summarize_factual([good, hidden, failed])
    assert totals['overall']['cases'] == 3
    assert totals['overall']['fact_total'] == 3
    assert totals['overall']['fact_correct'] == 1
    assert totals['overall']['failures'] == 1
    assert totals['by_language']['ru']['fact_total'] == 3
    forbidden = acceptance.score_factual_case({**case, 'answer_forbids': ['выдуманный БИН 123456789012']},
                                              {**response, 'answer': 'N = 2; выдуманный БИН 123456789012'})
    assert not forbidden['exact'] and forbidden['forbidden_correct'] == 0


def test_factual_run_on_frozen_database_keeps_errors_in_denominator(tmp_path, monkeypatch):
    from app import analytics, db
    cases = [dict(id='ru-1', question='один', request={'intent': 'search', 'language': 'ru'},
                  facts=[dict(path='result.n_selected', value=2, source='independent raw count',
                              answer_text='N = 2')]),
             dict(id='ru-2', question='два', request={'intent': 'search', 'language': 'ru'},
                  facts=[dict(path='result.n_selected', value=2, source='independent raw count',
                              answer_text='N = 2')])]
    source = tmp_path / 'facts.json'
    source.write_text(json.dumps(cases, ensure_ascii=False), encoding='utf-8')
    engine = db.make_engine('sqlite:///' + str(tmp_path / 'source.db'))
    db.init_db(engine)
    monkeypatch.setattr(analytics, 'analyze', lambda request, question, eng:
                        {'request': request.model_dump(mode='json'), 'n_selected': 2,
                         'quality': {'snapshot_hash': 'fixed'}, 'id': 'report'})
    monkeypatch.setattr('app.agent.render', lambda result: 'N = 2')
    from threading import Barrier
    rendezvous=Barrier(2)
    def planner(question, **kwargs):
        rendezvous.wait(timeout=5)
        if question == 'два':
            raise TimeoutError('private URL with token')
        return AnalysisRequest(intent='search', language='ru')
    try:
        report = acceptance.run_factual(source, tmp_path / 'result.json', eng=engine, planner=planner,workers=3)
    finally:
        engine.dispose()
    assert report['complete']
    assert report['workers']==3 and report['expected_cases']==2
    assert report['details'][0]['answer']=='N = 2'
    assert report['code_sha256']['acceptance.py']
    assert report['scores']['overall']['fact_total'] == 2
    assert report['scores']['overall']['fact_correct'] == 1
    assert report['scores']['overall']['failures'] == 1
    assert 'private URL' not in json.dumps(report)


def test_visibility_matches_values_across_number_and_date_formats():
    answer = 'N = 3; сумма 689 760.00 KZT; қол қойылған күні 2026-01-12; БСН 980440001034'
    assert acceptance._visible_value(answer, 689760)
    assert acceptance._visible_value(answer, 3)
    assert acceptance._visible_value(answer, '2026-01-12 14:53:32')
    assert acceptance._visible_value(answer, '980440001034')
    assert not acceptance._visible_value(answer, 689761)


def test_factual_report_rejects_concurrent_writer_before_starting(tmp_path):
    from filelock import FileLock, Timeout
    import pytest
    output=tmp_path/'report.json'
    with FileLock(str(output)+'.lock'):
        with pytest.raises(Timeout):
            acceptance.run_factual(tmp_path/'not-needed.json',output)
