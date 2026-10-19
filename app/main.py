from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from . import db, settings
from .schemas import AnalysisRequest, ChatRequest
from .agent import answer, render, Clarification
from .analytics import analyze, AnalysisError
from .quality import quality_report


@asynccontextmanager
async def lifespan(app):
    db.init_db(db.engine)
    yield


app=FastAPI(title='QazProcure Analytics',version='1.0.0',lifespan=lifespan)
static=Path(__file__).parent / 'static'
app.mount('/static',StaticFiles(directory=static),name='static')


@app.get('/')
def index():
    return FileResponse(static / 'index.html')


@app.get('/api/health')
def health():
    with db.engine.connect() as conn:
        conn.execute(select(1))
    return {'status':'ok','planner':settings.planner_name()}


@app.get('/api/status')
def status():
    with db.engine.connect() as conn:
        counts={t.name:conn.execute(select(func.count()).select_from(t)).scalar() for t in [db.plans,db.lots,db.contracts,db.items,db.announcements,db.organizations]}
        jobs=[dict(r) for r in conn.execute(select(db.jobs)).mappings()]
        latest=[dict(r) for r in conn.execute(select(db.runs).order_by(db.runs.c.started_at.desc()).limit(3)).mappings()]
        missing=conn.execute(select(func.count()).select_from(db.items.outerjoin(db.plans,db.items.c.plan_id==db.plans.c.id)).where(db.plans.c.id.is_(None))).scalar()
    from .ingest import scope
    quality=quality_report(eng=db.engine)
    compact={k:quality[k] for k in ('complete_partitions','total_partitions','plan_links','freshness_seconds','stale','backlog')}
    return {'counts':counts,'scope':scope(),'quality':compact,'jobs':{'total':len(jobs),'complete':sum(bool(j['complete']) for j in jobs),
        'failed':sum(bool(j['error']) for j in jobs)},'missing_plan_links':missing,'runs':latest,
        'planner':settings.planner_name()}


@app.get('/api/quality')
def quality():
    return quality_report(eng=db.engine)


@app.get('/api/catalog')
def catalog():
    with db.engine.connect() as conn:
        codes=[dict(r) for r in conn.execute(select(db.plans.c.enstru,func.max(db.plans.c.name_ru).label('name'),func.count().label('n')).where(db.plans.c.enstru.is_not(None)).group_by(db.plans.c.enstru).order_by(func.count().desc()).limit(100)).mappings()]
        locations=[dict(r) for r in conn.execute(select(db.refs.c.code,db.refs.c.name_ru,db.refs.c.name_kz).where(db.refs.c.kind=='RefKato').limit(500)).mappings()]
    return {'enstru':codes,'locations':locations}


@app.post('/api/chat')
def chat(body:ChatRequest):
    try:
        return answer(body.question,body.previous,body.filters,eng=db.engine)
    except Clarification as exc:
        return {'clarification':str(exc)}
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(422,str(exc)) from None


@app.post('/api/analyze')
def analytical_tool(body:AnalysisRequest):
    try:
        result=analyze(body,eng=db.engine)
        return {'answer':render(result),'request':result['request'],'result':result,'planner':'structured'}
    except AnalysisError as exc:
        raise HTTPException(422,str(exc)) from None


@app.get('/api/reports/{report_id}')
def report(report_id:str):
    with db.engine.connect() as conn:
        row=conn.execute(select(db.audits.c.result).where(db.audits.c.id==report_id)).scalar()
    if row is None:raise HTTPException(404,'Report not found')
    return row
