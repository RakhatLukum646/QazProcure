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
