"""Isolated Docker/PostgreSQL/OWS smoke using temporary mounted secrets.

Creates and removes only the uniquely named smoke Compose project and its volume.
Does not modify the local warehouse or other running projects.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request
from app import settings,db


def main():
    project='qazprocure-audit-'+secrets.token_hex(4)
    report={'started_at':db.now(),'project':project,'steps':{},'complete':False}
    output=Path('submission/docker_smoke.json')
    with tempfile.TemporaryDirectory(prefix='docker-audit-',dir=settings.DATA) as directory:
        root=Path(directory)
        (root/'postgres_password').write_text(secrets.token_urlsafe(36),encoding='utf-8')
        (root/'ows_token').write_text(settings.OWS_TOKEN,encoding='utf-8')
        (root/'google_api_key').write_text(settings.GOOGLE_API_KEY,encoding='utf-8')
        override={'services':{s:{'image':'qazprocure:tz-audit'} for s in ('api','worker')},
            'secrets':{s:{'file':str(root/s)} for s in ('postgres_password','ows_token','google_api_key')}}
        override['services']['worker']['environment']={'PAGE_SIZE':'5'}
        path=root/'override.json'
        path.write_text(json.dumps(override),encoding='utf-8')
        command=['docker','compose','-p',project,'-f',str(settings.ROOT/'compose.yaml'),'-f',str(path)]
        env={**os.environ,'API_PORT':'18766'}
        def run(*args,timeout=180):
            r=subprocess.run(command+list(args),capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=timeout)
            if r.returncode:
                # Error messages can include environment values; retain only step and status.
                raise RuntimeError(f'Compose {args[0]} failed, exit {r.returncode}')
            return r.stdout
        def status():
            with urllib.request.urlopen('http://127.0.0.1:18766/api/status',timeout=20) as response:
                return json.load(response)
        try:
            run('up','-d','--no-build','--wait','db','api')
            report['steps']['startup']=True
            with urllib.request.urlopen('http://127.0.0.1:18766/api/ready',timeout=20) as response:
                report['steps']['model_readiness']=json.load(response)
            # Exercise the actual launcher's secret-to-environment handoff, replacing
            # execvpe only to run a bounded sync instead of the indefinite worker loop.
            script="""import os,sys
sys.path.insert(0,'/app')
import docker.launch as launch
def bounded(file,argv,env):
 os.environ.update(env)
 from app.ingest import sync
 result=sync(bins=['000740001307'],years=[2026],max_pages=1,max_link_batches=0)
 print('SMOKE_STATUS='+result['status'])
launch.os.execvpe=bounded
sys.argv=['launch.py','worker']
launch.main()
"""
            result=run('run','--rm','--no-deps','--entrypoint','python','worker','-c',script,timeout=240)
            before=status()
            report['steps']['ows_ingestion']={'bounded':True,'counts':before['counts'],
                'partial_expected':'SMOKE_STATUS=partial' in result}
            assert before['counts']['contracts']>0 and before['counts']['plans']>0
            run('restart','db','api')
            for _ in range(30):
                try:
                    after=status()
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError('API did not recover after restart')
            assert before['counts']==after['counts']
            report['steps']['restart_persistence']={'passed':True,'counts':after['counts']}
            report['complete']=True
        except Exception as exc:
            report['error']=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
        finally:
            try:
                run('down','--volumes','--remove-orphans')
                report['cleanup']=True
            except Exception:
                report['cleanup']=False
            report['finished_at']=db.now()
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
