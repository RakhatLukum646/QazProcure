import argparse
import json
import os
import time
from . import db
from .ingest import sync


def run_worker(interval, sleep=time.sleep, monotonic=time.monotonic):
    if interval<=0:raise ValueError('Sync interval must be positive')
    while True:
        started=monotonic()
        try:
            sync(mode='backfill')
            sync(mode='incremental')
        except Exception as exc:
            print(f'Sync failed: {exc}',flush=True)
            sleep(300)
            continue
        # Schedule from the start of a cycle, avoiding an extra interval after a long scan.
        sleep(max(1,interval-(monotonic()-started)))


def main():
    parser=argparse.ArgumentParser(description='Procurement warehouse and analytics')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    p=sub.add_parser('sync')
    p.add_argument('--mode',choices=['backfill','incremental'],default='backfill')
    p.add_argument('--bins',nargs='+')
    p.add_argument('--years',type=int,nargs='+')
    p.add_argument('--max-pages',type=int)
    p.add_argument('--max-link-batches',type=int)
    p=sub.add_parser('peers')
    p.add_argument('--enstru',required=True)
    p.add_argument('--years',type=int,nargs='+',default=[2024,2025,2026])
    p.add_argument('--max-pages',type=int,default=100)
    sub.add_parser('worker')
    sub.add_parser('quality')
    p=sub.add_parser('ask');p.add_argument('question')
    args=parser.parse_args()
    db.init_db()
    if args.command=='sync':
        result=sync(mode=args.mode,bins=args.bins,years=args.years,max_pages=args.max_pages,max_link_batches=args.max_link_batches)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='worker':
        run_worker(float(os.getenv('SYNC_INTERVAL_HOURS','6'))*3600)
    elif args.command=='quality':
        from .quality import quality_report
        print(json.dumps(quality_report(),ensure_ascii=False,indent=2))
    elif args.command=='peers':
        result=sync(mode='peers',bins=[],years=args.years,enstru=args.enstru,max_pages=args.max_pages)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='ask':
        from .agent import answer
        print(answer(args.question)['answer'])


if __name__=='__main__':main()
