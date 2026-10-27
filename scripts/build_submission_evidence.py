"""Refresh local coverage and conservative real-data calibration evidence."""
import hashlib
import json
from collections import Counter,defaultdict
from pathlib import Path
from sqlalchemy import text
from app import db
from app.analytics import snapshot, fair_price
from app.ingest import scope
from app.quality import quality_report
from app.schemas import AnalysisRequest


def write(name,value):
    Path('submission',name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def main():
    Path('submission').mkdir(exist_ok=True)
    configured=scope()
    with db.engine.connect() as conn:
        rows=conn.execute(text('''SELECT c.customer_bin AS bin,c.year,COUNT(*) AS items,
          SUM(CASE WHEN i.plan_id IS NULL THEN 1 ELSE 0 END) AS null_plan_id,
          SUM(CASE WHEN i.plan_id IS NOT NULL AND p.id IS NULL THEN 1 ELSE 0 END) AS missing_plan_lookup,
          SUM(CASE WHEN p.id IS NOT NULL AND (p.deleted OR p.active=0) THEN 1 ELSE 0 END) AS invalid_or_deleted_plan
          FROM contract_items i JOIN contracts c ON c.id=i.contract_id
          LEFT JOIN plans p ON p.id=i.plan_id GROUP BY c.customer_bin,c.year''')).mappings().all()
    found={(r['bin'],r['year']):dict(r) for r in rows}
    coverage=[]
    for bin_ in configured['bins']:
        for year in configured['years']:
            row=found.get((bin_,year),dict(bin=bin_,year=year,items=0,null_plan_id=0,missing_plan_lookup=0,invalid_or_deleted_plan=0))
            row['linked_active_plan_items']=row['items']-row['null_plan_id']-row['missing_plan_lookup']-row['invalid_or_deleted_plan']
            coverage.append(row)
    totals={k:sum(r[k] for r in coverage) for k in coverage[0] if k not in ('bin','year')}
    evidence={'generated_at':db.now(),'scope':configured,'totals':totals,'rows':coverage,
              'definition':'All stored contract item rows in scope; not deduplicated signed analytical positions. Link status is not analytical eligibility.'}
    write('coverage_by_org_year.json',evidence)
    table=['# Покрытие по организациям и годам','',evidence['definition'],'',
           '| БИН | Год | Позиций в хранилище | Нет plan_id | План не найден | Удалён/неактивен | Активный план |',
           '|---|---:|---:|---:|---:|---:|---:|']
    table += ['| '+' | '.join(str(r[k]) for k in ('bin','year','items','null_plan_id','missing_plan_lookup','invalid_or_deleted_plan','linked_active_plan_items'))+' |' for r in coverage]
    Path('submission/coverage_table.md').write_text('\n'.join(table)+'\n',encoding='utf-8')
    quality=quality_report()
    write('quality_report.json',{'generated_at':db.now(),**quality})
    universe,snap=snapshot()
    in_scope=[r for r in universe if r['customer_bin'] in configured['bins'] and r['year'] in configured['years']]
    write('analytical_coverage.json',{'generated_at':db.now(),'quality':snap,'scope_usable_items':len(in_scope),
        'rows':[{'bin':b,'year':y,'usable_items':n} for (b,y),n in sorted(Counter((r['customer_bin'],r['year']) for r in in_scope).items())],
        'note':'Actual analytics.snapshot eligibility; differs from raw link coverage and includes version, status, currency, price and unit checks.'})
    groups=defaultdict(list)
    for r in universe:
        desc=(r['description'] or '').casefold().strip()
        if desc and r.get('city_code'):
            groups[(r['enstru'],r['unit'],r['subject_type'],desc)].append(r)
    largest=sorted(groups.items(),key=lambda kv:(-len(kv[1]),str(kv[0])))[:30]
    checks=[]
    for key,group in largest:
        # Latest scoped target for each city/quantity in each large cohort.
        targets={}
        for r in sorted(group,key=lambda r:(r['signed_at'],r['item_id'])):
            if r['customer_bin'] in configured['bins']:
                targets[(r['city_code'],r['quantity'])]=r
        for target in list(targets.values())[:50]:
            if not target['lot_id']:
                continue
            result=fair_price([target],group,AnalysisRequest(intent='fair_price',lot_id=target['lot_id']))['examples'][0]
            checks.append({'target_item_id':target['item_id'],'lot_id':target['lot_id'],
                'enstru':key[0],'cohort_items':len(group),'city':target['city_code'],
                'status':result['status'],'n_peers':result['n_peers'],
                'regional_model_applied':bool(result.get('regional_model')),
                'time_model_applied':bool(result.get('time_model'))})
    write('fair_price_calibration_audit.json',{'generated_at':db.now(),'snapshot_sha256':snap['snapshot_hash'],
        'selection':'30 largest exact-description/unit/ENSTRU/type cohorts; up to 50 latest city/quantity targets in scope per cohort. Diagnostic sample, not proof of absence in every possible cohort.',
        'checked':len(checks),'regional_activated':sum(c['regional_model_applied'] for c in checks),
        'time_activated':sum(c['time_model_applied'] for c in checks),'checks':checks})
    print(json.dumps({'coverage':totals,'scope_usable_items':len(in_scope),'calibration_checks':len(checks),
        'regional_activated':sum(c['regional_model_applied'] for c in checks),
        'time_activated':sum(c['time_model_applied'] for c in checks)},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
