"""Live planner holdout scoring. No fallback and no dropped failures."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from . import settings
from .agent import llm_plan
from .schemas import AnalysisRequest

LABEL = ('Independently authored development holdout; not third-party certification. '
         'Scores the deployed planner including its explicit-constraint validation.')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def case_key(case, dataset_hash, model):
    payload = ['gemini', model, dataset_hash, case['id'], case['question']]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def score_case(case, actual=None, error=None):
    # A missing/null value must not pass even if a future expected value is null.
    actual = actual if isinstance(actual, dict) else {}
    flags = {k: error is None and k in actual and actual[k] == value
             for k, value in case['expected'].items()}
    return dict(id=case['id'], question=case['question'], expected=case['expected'],
                actual=actual, fields=flags, correct_fields=sum(flags.values()),
                total_fields=len(flags), exact=all(flags.values()), error=error)


def summarize(details):
    correct = sum(d['correct_fields'] for d in details)
    total = sum(d['total_fields'] for d in details)
    exact = sum(d['exact'] for d in details)
    return dict(cases=len(details), correct_cases=exact, correct_fields=correct,
                total_fields=total, field_accuracy=correct / total if total else 0,
                exact_case_accuracy=exact / len(details) if details else 0,
                call_failures=sum(d['error'] not in (None, 'Pending') for d in details),
                error_types=dict(Counter(d['error'] for d in details if d['error'] not in (None, 'Pending'))),
                failed_case_ids=[d['id'] for d in details if not d['exact']])


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run(dataset, output, *, workers=2, resume=True, planner=None):
    if workers not in (2, 3):
        raise ValueError('Concurrency must be 2 or 3')
    raw = Path(dataset).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    cases = json.loads(raw.decode('utf-8-sig'))
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Holdout must contain unique nonempty case IDs')
    for case in cases:
        if not isinstance(case['question'], str) or not case['expected']:
            raise ValueError('Each case requires a question and expected fields')
        if set(case['expected']) - set(AnalysisRequest.model_fields):
            raise ValueError('Unknown expected request field')
    model = settings.GEMINI_MODEL
    cache = {}
    if resume and Path(output).exists():
        try:
            old = json.loads(Path(output).read_text(encoding='utf-8'))
            if (old.get('dataset_sha256'), old.get('provider'), old.get('model')) == (digest, 'gemini', model):
                cache = {d['key']: d for d in old.get('details', [])}
        except (ValueError, KeyError, TypeError):
            cache = {}
    report = dict(label=LABEL, dataset=str(Path(dataset).resolve()), dataset_sha256=digest,
                  provider='gemini', model=model, started_at=stamp(), updated_at=stamp(),
                  complete=False, concurrency=workers, timeout='existing llm_plan HTTP timeout (90 seconds)',
                  details=[])
    results = {}

    def evaluate(case):
        key = case_key(case, digest, model)
        cached = cache.get(key)
        if cached and cached.get('question') == case['question']:
            result = score_case(case, cached.get('actual'), cached.get('error'))
            result.update(key=key, evaluated_at=cached.get('evaluated_at'), resumed=True)
            return result
        try:
            request = (planner or llm_plan)(case['question'], provider='gemini')
            # Validate substitutes too, so malformed output is always a failed case.
            validated = AnalysisRequest.model_validate(request).model_dump(mode='json')
            actual = ({k: validated[k] for k in request} if isinstance(request, dict)
                      else validated)
            result = score_case(case, actual)
        except Exception as exc:
            # Exception messages may contain credential-bearing URLs or response bodies.
            result = score_case(case, error=type(exc).__name__)
        result.update(key=key, evaluated_at=stamp(), resumed=False)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(evaluate, case): case for case in cases}
        for future in as_completed(pending):
            case = pending[future]
            results[case['id']] = future.result()
            report['details'] = [results[c['id']] for c in cases if c['id'] in results]
            report.update(updated_at=stamp(), completed_cases=len(results))
            # Denominators include pending cases as incorrect until completion.
            report.update(summarize([results.get(c['id'], score_case(c, error='Pending')) for c in cases]))
            save_report(output, report)
    report.update(complete=True, updated_at=stamp())
    save_report(output, report)
    return report


def verify_facts(eng=None, *, _frozen=False):
    """Independent arithmetic/filter checks on the effective local snapshot, not OWS truth."""
    from . import analytics, db
    eng = eng or db.engine
    if not _frozen and getattr(getattr(eng, 'dialect', None), 'name', None) == 'sqlite':
        import sqlite3
        from tempfile import TemporaryDirectory
        # SQLite online backup takes a consistent copy while ingestion keeps running.
        with TemporaryDirectory(prefix='procurement-acceptance-') as directory:
            path = Path(directory) / 'snapshot.db'
            with eng.connect() as source:
                target = sqlite3.connect(str(path))
                try:
                    source.connection.driver_connection.backup(target)
                finally:
                    target.close()
            frozen = db.make_engine('sqlite:///' + str(path))
            try:
                result = verify_facts(frozen, _frozen=True)
                result['verification_storage'] = 'consistent SQLite online backup'
                return result
            finally:
                frozen.dispose()
    rows, quality = analytics.snapshot(eng)
    if not rows:
        return dict(status='insufficient', scope='local effective snapshot only', checks=[])
    # Three deterministic real requests; expected values do not call matches/grouped/statistics_for.
    first = sorted(rows, key=lambda row: row['item_id'])[0]
    specs = [dict(years=sorted({r['year'] for r in rows})),
             dict(years=[first['year']]),
             dict(years=[first['year']], bins=[first['customer_bin']])]
    checks = []
    for filters in specs:
        request = AnalysisRequest(intent='summary', **filters)
        selected = [r for r in rows if r['year'] in filters['years'] and
                    (not filters.get('bins') or r['customer_bin'] in filters['bins'])]
        amount = sum((Decimal(str(r['price'])) * Decimal(str(r['quantity'])) for r in selected), Decimal(0))
        actual = analytics.analyze(request, eng=eng)
        sources = sorted({r['contract_id'] for r in selected})
        flags = dict(count=actual['n_selected'] == len(selected),
                     amount=abs(Decimal(str(actual['amount_net'])) - amount) <= max(Decimal('.01'), abs(amount)*Decimal('1e-12')),
                     snapshot=actual['quality']['snapshot_hash'] == quality['snapshot_hash'],
                     sources=all(e['contract_id'] in sources and e['contract_url'] ==
                                 f"https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{e['contract_id']}"
                                 for e in actual['examples']))
        checks.append(dict(request=request.model_dump(mode='json'), expected_count=len(selected),
                           expected_amount=str(amount), actual_count=actual['n_selected'],
                           actual_amount=actual['amount_net'], fields=flags, passed=all(flags.values()),
                           source_contract_ids=sources, result_id=actual['id']))
    return dict(status='passed' if all(c['passed'] for c in checks) else 'failed',
                scope='Independent count/amount/source checks against the same effective local snapshot; '
                      'does not independently validate ingestion or remote source truth.',
                snapshot_sha256=quality['snapshot_hash'], checks=checks)


def _field(document, path):
    value = document
    for part in path.split('.'):
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return False, None
    return True, value


def _fact_equal(actual, expected):
    if isinstance(expected, (float, int)) and not isinstance(expected, bool):
        try:
            return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal('0.01')
        except (ValueError, TypeError, ArithmeticError):
            return False
    return actual == expected


def _visible_value(answer, value):
    """Match a factual value in prose despite thousands/decimal formatting."""
    if value is None:
        return True  # A missing optional machine field is not a positive prose claim.
    if isinstance(value, bool):
        return str(value).lower() in answer.lower()
    if isinstance(value, (int, float, Decimal)):
        expected = Decimal(str(value))
        pattern = r'(?<![\w.])[-+]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\w.])|(?<![\w.])[-+]?\d+(?:[.,]\d+)?(?![\w.])'
        for match in re.finditer(pattern, answer):
            token = match.group().strip().rstrip('.,')
            token = token.replace(' ', '').replace('\u00a0', '').replace('\u202f', '')
            if ',' in token and '.' in token:
                token = token.replace(',', '') if token.rfind('.') > token.rfind(',') else token.replace('.', '').replace(',', '.')
            elif ',' in token:
                token = token.replace(',', '.') if len(token.split(',')[-1]) != 3 else token.replace(',', '')
            try:
                if abs(Decimal(token) - expected) <= Decimal('0.005'):
                    return True
            except ArithmeticError:
                pass
        return False
    rendered = str(value)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T].*)?', rendered):
        day = rendered[:10]
        return day in answer or f'{day[8:10]}.{day[5:7]}.{day[:4]}' in answer
    status_phrases = {
        'insufficient_peers': ('Недостаточно сопоставимых договоров', 'Салыстырмалы шарттар жеткіліксіз'),
        'insufficient_location': ('Не определено место поставки', 'Жеткізу орны анықталмады'),
        'insufficient_time_matched_peers': ('Недостаточно аналогов за сопоставимый период', 'Салыстырмалы кезеңдегі баламалар жеткіліксіз'),
        'provisional': ('Предварительная оценка', 'алдын ала баға'),
        'estimated': ('Статистическая оценка', 'Статистикалық баға'),
    }
    if rendered in status_phrases:
        return any(phrase.casefold() in answer.casefold() for phrase in status_phrases[rendered])
    return rendered.casefold() in answer.casefold()


def score_factual_case(case, response=None, error=None):
    """Score authored source facts against a complete user-visible answer."""
    response = response if isinstance(response, dict) else {}
    requested = response.get('request') or {}
    route_fields = {key: requested.get(key) == value for key, value in case['request'].items()}
    route_ok = error is None and all(route_fields.values())
    answer = response.get('answer') or ''
    facts = []
    for fact in case['facts']:
        path = fact.get('path') or fact['field']
        exists, value = _field(response, path)
        shown = _visible_value(answer, fact['value'])
        ok = error is None and exists and _fact_equal(value, fact['value']) and shown
        facts.append(dict(path=path, expected=fact['value'], actual=value if exists else None,
                          source=fact.get('source'), answer_text=fact.get('answer_text'),
                          present_in_answer=shown, correct=ok))
    visible = [dict(text=text, correct=error is None and text in answer)
               for text in case.get('answer_contains', [])]
    forbidden = [dict(text=text, absent=error is None and text not in answer)
                 for text in case.get('answer_forbids', [])]
    expected_outcome = case.get('expected_outcome')
    outcome = None
    outcome_correct = None
    if expected_outcome:
        result = response.get('result') or {}
        if error:
            outcome = 'error'
        elif requested.get('intent') == 'fair_price':
            examples = result.get('examples') or []
            outcome = 'numeric' if any(e.get('fair_price') is not None for e in examples) else 'insufficient'
        else:
            outcome = 'numeric' if result.get('n_evaluated', 0) else 'insufficient'
        outcome_correct = outcome == expected_outcome
    return dict(id=case['id'], language=case['request']['language'],
                intent=case['request']['intent'], question=case['question'],
                route_fields=route_fields, route_correct=route_ok, facts=facts,
                visible_checks=visible, forbidden_checks=forbidden,
                fact_correct=sum(f['correct'] for f in facts),
                fact_total=len(facts), visible_correct=sum(v['correct'] for v in visible),
                visible_total=len(visible), forbidden_correct=sum(v['absent'] for v in forbidden),
                forbidden_total=len(forbidden), expected_outcome=expected_outcome,
                actual_outcome=outcome, outcome_correct=outcome_correct,
                exact=route_ok and all(f['correct'] for f in facts)
                and all(v['correct'] for v in visible) and all(v['absent'] for v in forbidden)
                and outcome_correct is not False, error=error,
                report_id=response.get('result', {}).get('id'))


def summarize_factual(details):
    def counts(rows):
        cases = len(rows)
        facts = sum(d['fact_total'] for d in rows)
        correct = sum(d['fact_correct'] for d in rows)
        visible = sum(d['visible_total'] for d in rows)
        visible_correct = sum(d['visible_correct'] for d in rows)
        forbidden = sum(d['forbidden_total'] for d in rows)
        forbidden_correct = sum(d['forbidden_correct'] for d in rows)
        return dict(cases=cases, exact_cases=sum(d['exact'] for d in rows),
                    route_correct=sum(d['route_correct'] for d in rows),
                    fact_correct=correct, fact_total=facts,
                    fact_accuracy=correct / facts if facts else 0,
                    visible_correct=visible_correct, visible_total=visible,
                    visible_accuracy=visible_correct / visible if visible else 0,
                    forbidden_correct=forbidden_correct, forbidden_total=forbidden,
                    outcome_correct=sum(d['outcome_correct'] is True for d in rows),
                    outcome_total=sum(d['expected_outcome'] is not None for d in rows),
                    numeric_answers=sum(d['actual_outcome'] == 'numeric' for d in rows),
                    insufficient_answers=sum(d['actual_outcome'] == 'insufficient' for d in rows),
                    failures=sum(d['error'] is not None for d in rows))
    return dict(overall=counts(details), by_language={lang: counts([d for d in details if d['language'] == lang])
                                                     for lang in ('ru', 'kk')},
                by_intent={intent: counts([d for d in details if d['intent'] == intent])
                           for intent in sorted({d['intent'] for d in details})})


def run_factual(dataset, output, *, eng=None, provider='gemini', planner=None, _frozen=False, workers=1):
    """One writer per report; parallelize model calls, serialize SQLite analysis."""
    from filelock import FileLock
    if workers not in (1,2,3):
        raise ValueError('Factual workers must be 1, 2 or 3')
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(output)+'.lock',timeout=0):
        return _run_factual(dataset,output,eng=eng,provider=provider,planner=planner,
                            _frozen=_frozen,workers=workers)


def _run_factual(dataset, output, *, eng=None, provider='gemini', planner=None, _frozen=False, workers=1):
    """Run natural questions on one immutable database snapshot; no rules fallback."""
    from . import db
    eng = eng or db.engine
    if not _frozen and eng.dialect.name == 'sqlite':
        import sqlite3
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix='procurement-factual-') as directory:
            path = Path(directory) / 'snapshot.db'
            with eng.connect() as source:
                target = sqlite3.connect(str(path))
                try:
                    source.connection.driver_connection.backup(target)
                finally:
                    target.close()
            frozen = db.make_engine('sqlite:///' + str(path))
            try:
                return _run_factual(dataset, output, eng=frozen, provider=provider,
                                   planner=planner, _frozen=True, workers=workers)
            finally:
                frozen.dispose()
    from . import analytics
    from .agent import render
    raw = Path(dataset).read_bytes()
    cases = json.loads(raw.decode('utf-8-sig'))
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Factual holdout requires unique nonempty IDs')
    for case in cases:
        AnalysisRequest.model_validate(case['request'])
        if not case.get('question') or not case.get('facts'):
            raise ValueError('Every factual case requires a question and source facts')
        for fact in case['facts']:
            if not fact.get('source') or not ('path' in fact or 'field' in fact):
                raise ValueError('Each expected fact requires a path and independent source')
        if case.get('expected_outcome') not in (None, 'numeric', 'insufficient'):
            raise ValueError('Expected outcome must be numeric or insufficient')
    model = settings.GEMINI_MODEL if provider == 'gemini' else provider
    report = dict(label='Development acceptance set on real data, with expected values separately recomputed from local raw payloads/SQL; '
                        'questions and renderer are developed against this same set, so this is not a blind holdout or certification. '
                        'Only annotated required facts are scored; unannotated extra claims and OWS source truth are not exhaustively checked.',
                  dataset=str(Path(dataset).resolve()), dataset_sha256=hashlib.sha256(raw).hexdigest(),
                  provider=provider, model=model, started_at=stamp(), complete=False, details=[],
                  expected_cases=len(cases), workers=workers,
                  code_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(Path(__file__).parent.glob('*.py'))})
    snapshot_hash = None
    def evaluate(case, planned):
        try:
            request = planned.result()
            request = AnalysisRequest.model_validate(request)
            result = analytics.analyze(request, case['question'], eng=eng)
            result_hash = result['quality']['snapshot_hash']
            if snapshot_hash is not None and result_hash != snapshot_hash:
                raise RuntimeError('Snapshot changed during factual holdout')
            response = dict(request=result['request'], result=result, answer=render(result))
            detail = score_factual_case(case, response)
            detail['answer']=response['answer']
            detail['actual_request']=response['request']
            return detail,result_hash
        except Exception as exc:
            return score_factual_case(case, error=type(exc).__name__),None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        planned=[pool.submit(planner or llm_plan,case['question'],provider=provider) for case in cases]
        for case,future in zip(cases,planned):
            detail,result_hash=evaluate(case,future)
            if snapshot_hash is None and result_hash is not None:
                snapshot_hash=result_hash
            report['details'].append(detail)
            report.update(updated_at=stamp(), snapshot_sha256=snapshot_hash,
                          scores=summarize_factual(report['details']), completed_cases=len(report['details']))
            save_report(output, report)
    report['complete'] = True
    save_report(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=settings.ROOT / 'config/acceptance_holdout.json')
    parser.add_argument('--output', type=Path, default=settings.DATA / 'acceptance_latest.json')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=2)
    parser.add_argument('--no-resume', action='store_true')
    parser.add_argument('--verify-facts', action='store_true')
    parser.add_argument('--factual-only', action='store_true', help='Run real-data final-answer factual holdout')
    parser.add_argument('--provider',choices=('gemini',),default=settings.planner_name(),help='Language model for factual acceptance; no rule fallback')
    parser.add_argument('--factual-dataset', type=Path, default=settings.ROOT / 'config/factual_holdout.json')
    parser.add_argument('--factual-output', type=Path, default=settings.DATA / 'factual_latest.json')
    args = parser.parse_args()
    if args.factual_only:
        report = run_factual(args.factual_dataset, args.factual_output,workers=args.workers,provider=args.provider)
        print(json.dumps({k: v for k, v in report.items() if k != 'details'}, ensure_ascii=False, indent=2))
        if report['scores']['overall']['fact_accuracy'] < .85:
            raise SystemExit(1)
        return
    report = run(args.dataset, args.output, workers=args.workers, resume=not args.no_resume)
    if args.verify_facts:
        try:
            report['factual_verification'] = verify_facts()
        except Exception as exc:
            report['factual_verification'] = dict(status='error', error=type(exc).__name__)
        save_report(args.output, report)
    summary = {k: v for k, v in report.items() if k not in ('details', 'factual_verification')}
    if 'factual_verification' in report:
        summary['factual_verification'] = {k: v for k, v in report['factual_verification'].items() if k != 'checks'}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if report['field_accuracy'] < .85 or report.get('factual_verification', {}).get('status') in ('failed', 'error'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
