"""Live planner holdout scoring. No fallback and no dropped failures."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=settings.ROOT / 'config/acceptance_holdout.json')
    parser.add_argument('--output', type=Path, default=settings.DATA / 'acceptance_latest.json')
    parser.add_argument('--workers', type=int, choices=(2, 3), default=2)
    parser.add_argument('--no-resume', action='store_true')
    parser.add_argument('--verify-facts', action='store_true')
    args = parser.parse_args()
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
