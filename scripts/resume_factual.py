"""Resume a recorded factual run on the same frozen input, preserving real results."""
import argparse
import hashlib
import json
from pathlib import Path
from filelock import FileLock
from app import db
from app.acceptance import run_factual, save_report, summarize_factual, stamp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--database-url',required=True)
    args=parser.parse_args()
    with FileLock(str(args.report)+'.lock',timeout=0):
        original=json.loads(args.report.read_text(encoding='utf-8'))
        if original['complete']:
            print('Already complete');return
        for name,digest in original['code_sha256'].items():
            assert hashlib.sha256((Path('app')/name).read_bytes()).hexdigest()==digest, 'Code changed: '+name
        dataset=Path(original['dataset'])
        assert hashlib.sha256(dataset.read_bytes()).hexdigest()==original['dataset_sha256'],'Dataset changed'
        cases=json.loads(dataset.read_text(encoding='utf-8-sig'))
        engine=db.make_engine(args.database_url)
        from app.analytics import snapshot
        _,quality=snapshot(engine)
        assert quality['snapshot_hash']==original['snapshot_sha256'],'Frozen input differs'
        details=original['details']
        part_path=args.report.with_suffix('.remaining.json')
        if part_path.exists():
            part=json.loads(part_path.read_text(encoding='utf-8'))
            assert part['code_sha256']==original['code_sha256']
            assert part['provider']==original['provider'] and part['model']==original['model']
            assert part.get('snapshot_sha256') in (None,original['snapshot_sha256'])
            by_id={d['id']:d for d in details}
            for detail in part['details']:by_id.setdefault(detail['id'],detail)
            details=[by_id[c['id']] for c in cases if c['id'] in by_id]
        assert [d['id'] for d in details]==[c['id'] for c in cases[:len(details)]],'Non-prefix results'
        count=len(details)
        original.update(details=details,scores=summarize_factual(details),completed_cases=count)
        save_report(args.report,original)
        if count<len(cases):
            remaining=args.report.with_suffix('.remaining_dataset.json')
            remaining.write_text(json.dumps(cases[count:],ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'Resuming {len(cases)-count} cases after {count} verified results',flush=True)
            part=run_factual(remaining,part_path,eng=engine,provider=original['provider'],workers=1)
            assert part['snapshot_sha256']==original['snapshot_sha256']
            assert part['code_sha256']==original['code_sha256']
            details+=part['details']
        assert [d['id'] for d in details]==[c['id'] for c in cases]
        original.update(details=details,scores=summarize_factual(details),completed_cases=len(details),
            complete=True,updated_at=stamp(),resume_provenance={
                'preserved_prefix_cases':count,'continued_cases':len(cases)-count,
                'same_code_dataset_and_normalized_snapshot_verified':True,
                'note':'Real model outputs from consecutive segments; no results synthesized or rescored.'})
        save_report(args.report,original)
        engine.dispose()
        print(json.dumps(original['scores']['overall']),flush=True)


if __name__=='__main__':main()
