import pytest
import json

def gemini_response(payload):
    return {"candidates":[{"content":{"parts":[{"text":json.dumps(payload)}]}}]}

from app.agent import rule_plan,Clarification,render
from app.schemas import AnalysisRequest


def test_llm_omissions_do_not_drop_explicit_identifiers(monkeypatch):
    import json
    from app.agent import llm_plan
    class Response:
        def raise_for_status(self):pass
        def json(self):return gemini_response({'request':{'intent':'fair_price'},'clarification':None})
    monkeypatch.setattr('app.agent.httpx.post',lambda *args,**kwargs:Response())
    request=llm_plan('Оцени лот 22906306-ОЛ-ОК1 ЕНС ТРУ 105111.410.000000 КАТО 750000000',provider='gemini')
    assert request.lot_number=='22906306-ОЛ-ОК1'
    assert request.enstru=='105111.410.000000' and request.kato=='750000000'
    request=llm_plan('2026 жылы 38693950 лоты бойынша 105111.410.000000 сүт ЕНС ТРУ 750000000 КАТО көрсет',provider='gemini')
    assert request.lot_id==38693950 and request.enstru=='105111.410.000000' and request.kato=='750000000'
    request=llm_plan('2025 жылғы 33439794 лотының бағасы мен санын көрсет',provider='gemini')
    assert request.lot_id==33439794


def test_explicit_year_range_repairs_invalid_model_years_before_validation(monkeypatch):
    import json
    from pydantic import ValidationError
    from app.agent import llm_plan
    class Response:
        def raise_for_status(self):pass
        def json(self):return gemini_response({'request':{'intent':'summary','years':[2024,2]}})
    monkeypatch.setattr('app.agent.httpx.post',lambda *a,**k:Response())
    request=llm_plan('051040005150 БСН және 201539.900.000000 коды бойынша 2024–2025 жылдардағы жиынтықты көрсет')
    assert request.years==[2024,2025] and request.bins==['051040005150']
    assert request.intent=='summary' and request.enstru=='201539.900.000000'
    with pytest.raises(ValidationError):
        llm_plan('Покажи сводку закупок')


def test_unavailable_model_is_not_silently_replaced_with_rules(monkeypatch):
    import httpx
    from app import agent
    monkeypatch.setattr(agent.settings,'LLM_PROVIDER','gemini')
    monkeypatch.setattr(agent.settings,'ALLOW_RULES_FALLBACK',False)
    def unavailable(*args,**kwargs):raise httpx.ConnectError('unavailable')
    monkeypatch.setattr(agent,'llm_plan',unavailable)
    monkeypatch.setattr(agent,'rule_plan',lambda *a,**k:pytest.fail('Silent fallback'))
    with pytest.raises(Clarification,match='модель недоступна'):
        agent.answer('Покажи закупки за 2026 год')


@pytest.mark.parametrize('text,expected',[
 ('Покажи договоры за 2026 год',{'intent':'search','years':[2026],'language':'ru'}),
 ('Найди закупки с отклонением цены > 30% по ЕНСТРУ 282932.500.000001',{'intent':'price_anomaly','threshold_pct':30,'direction':'above','enstru':'282932.500.000001'}),
 ('Оцени адекватность цены лота №12345',{'intent':'fair_price','lot_id':12345}),
 ('Выяви нетипичное завышение количества ТРУ за 2024–2026 годы',{'intent':'quantity_anomaly','years':[2024,2025,2026]}),
 ('Сравни закупки БИН 000740001307 за 2024 и 2025',{'intent':'compare','bins':['000740001307'],'years':[2024,2025]}),
 ('2026 жылғы сатып алуларды көрсет',{'intent':'search','language':'kk','years':[2026]}),
 ('Баға ауытқуы 30 пайыздан жоғары сатып алуларды көрсет',{'intent':'price_anomaly','language':'kk','threshold_pct':30,'direction':'above'}),
 ('2024–2026 жылдардағы сатып алуларды салыстыр',{'intent':'compare','language':'kk'}),
 ('Лот №12345 әділ бағасын бағала',{'intent':'fair_price','language':'kk','lot_id':12345}),
 ('Найди закупки "Бумага" КАТО 750000000',{'text':'Бумага','kato':'750000000'}),
])
def test_fact_extraction(text,expected):
    result=rule_plan(text).model_dump()
    for k,v in expected.items():assert result[k]==v


def test_followup_period_preserves_bin():
    prev=rule_plan('Покажи договоры БИН 000740001307 за 2025')
    out=rule_plan('А теперь за 2024',prev)
    assert out.bins==['000740001307'] and out.years==[2024]


def test_unknown_city_requires_clarification():
    with pytest.raises(Clarification):rule_plan('Покажи закупки в Алматы')


def test_below_magnitude_not_silently_inverted():
    with pytest.raises(Clarification):rule_plan('Найди отклонения меньше 30%')


def test_unknown_task_not_sent_to_db():
    with pytest.raises(Clarification):rule_plan('Напиши стихотворение')


def test_fair_price_identical_city_phrase_does_not_require_clarification():
    # This is the ТЗ's own mandatory fair-price example question. It names no
    # specific city ("идентичном городе поставки" = the lot's own city, resolved
    # automatically by fair_price) and must not be mistaken for a named-city mention.
    req=rule_plan('Оцени адекватность цены лота №12345 относительно аналогичных '
                   'контрактов других ведомств в идентичном городе поставки')
    assert req.intent=='fair_price' and req.lot_id==12345


def test_two_hyphen_lot_number_not_truncated():
    req=rule_plan('Покажи договоры по лоту 22906306-ОЛ-ОК1')
    assert req.lot_number=='22906306-ОЛ-ОК1'
    assert req.lot_id is None


def test_llm_fabricated_identifier_is_rejected(monkeypatch):
    from app.agent import llm_plan
    import app.agent as agent
    import httpx,json
    monkeypatch.setattr(agent.settings,'GEMINI_MODEL','test')
    def post(url,**kw):
        assert kw['json']['generationConfig']['responseJsonSchema']['properties']['request']
        return httpx.Response(200,request=httpx.Request('POST',url),json=gemini_response({'request':{'bins':['000740001307']}}))
    monkeypatch.setattr(agent.httpx,'post',post)
    with pytest.raises(Clarification):llm_plan('Покажи закупки министерства')


def test_llm_explicit_parameters_are_validated(monkeypatch):
    from app.agent import llm_plan
    import app.agent as agent
    import httpx,json
    monkeypatch.setattr(agent.settings,'GEMINI_MODEL','test')
    def post(url,**kw):
        return httpx.Response(200,request=httpx.Request('POST',url),json=gemini_response({'request':{'bins':['000740001307'],'years':[2025]}}))
    monkeypatch.setattr(agent.httpx,'post',post)
    result=llm_plan('Покажи закупки БИН 000740001307 за 2025')
    assert result.bins==['000740001307'] and result.years==[2025]


@pytest.fixture
def quantity_model(monkeypatch):
    import app.agent as agent
    import httpx,json
    monkeypatch.setattr(agent.settings,'planner_name',lambda:'gemini')
    monkeypatch.setattr(agent.settings,'GEMINI_MODEL','test')
    def post(url,**kwargs):
        assert 'quantity_anomaly' in kwargs['json']['systemInstruction']['parts'][0]['text']
        return httpx.Response(200,request=httpx.Request('POST',url),json=gemini_response({
            'request':{'intent':'quantity_anomaly','language':'kk','years':[2025],'direction':'above'}
        }))
    monkeypatch.setattr(agent.httpx,'post',post)


def test_llm_quantity_current_question_corrects_stale_language_and_period(quantity_model):
    from app.agent import llm_plan
    previous=AnalysisRequest(intent='search',language='kk',years=[2025])
    result=llm_plan('Выяви нетипичное завышение количества ТРУ за 2024–2026 годы',previous)
    assert result.intent=='quantity_anomaly'
    assert result.language=='ru'
    assert result.years==[2024,2025,2026]


@pytest.fixture
def captured_analysis(monkeypatch):
    import app.agent as agent
    captured=[]
    def analyze(request,question,**kwargs):
        captured.append((request,question,kwargs))
        return {'request':request.model_dump(mode='json'),'n_selected':0,'n_evaluated':0,
                'confidence':'insufficient','limitations':[],'id':'test','method_version':'test',
                'quality':{'snapshot_hash':'test'}}
    monkeypatch.setattr(agent,'analyze',analyze)
    return captured


def test_answer_renders_current_quantity_question_in_russian(quantity_model,captured_analysis):
    from app.agent import answer
    question='Выяви нетипичное завышение количества ТРУ за 2024–2026 годы'
    result=answer(question,previous=AnalysisRequest(intent='search',language='kk',years=[2025]))
    request,actual_question,_=captured_analysis[0]
    assert actual_question==question
    assert request.intent=='quantity_anomaly'
    assert request.language=='ru' and request.years==[2024,2025,2026]
    assert result['planner']=='gemini' and result['warnings']==[]
    assert 'Краткий вывод' in result['answer']
    assert 'Годы: 2024, 2025, 2026' in result['answer']
    assert 'Рост количества относительно медианы' in result['answer']


def test_answer_explicit_structured_parameters_take_precedence(monkeypatch,captured_analysis):
    import app.agent as agent
    def unexpected_planner(*args,**kwargs):
        pytest.fail('Explicit structured parameters must bypass text planning')
    monkeypatch.setattr(agent,'llm_plan',unexpected_planner)
    monkeypatch.setattr(agent,'rule_plan',unexpected_planner)
    filters=AnalysisRequest(intent='search',language='kk',years=[2025],bins=['000740001307'])
    result=agent.answer('Выяви нетипичное завышение количества ТРУ за 2024–2026 годы',filters=filters)
    assert captured_analysis[0][0]==filters
    assert result['request']==filters.model_dump(mode='json')
    assert result['planner']=='structured'
    assert 'Қысқаша қорытынды' in result['answer']


@pytest.mark.parametrize('language',['ru','kk'])
@pytest.mark.parametrize('status',['estimated','insufficient_peers'])
def test_planned_lot_render_preserves_unsigned_semantics(language,status):
    req=AnalysisRequest(intent='fair_price',language=language,lot_id=12)
    example={'target_kind':'planned_lot','planned_price':120,'quantity':10,'name_ru':'Бумага',
             'name_kz':'Қағаз','contract_id':None,'contract_url':None,'lot_url':None,'lot_id':12,
             'price_net':None,'deviation_pct':None,'status':status,'n_peers':5,
             'region_factor':1.2,'region_reference_city':'750000000','city_code':'710000000'}
    if status=='estimated':
        example.update(fair_price=100,q25=90,q75=110,regional_model={
            'months':24,'r2':.8,'calibration_contract_ids':[1,2], 'calibration_item_ids':[3,4,5],
            'factor_components':{'2024-01':{'empirical_trend_factor':1.1,'seasonality_factor':.9}}})
    result={'request':req.model_dump(mode='json'),'examples':[example],'n_selected':1,'n_evaluated':1,
            'n_matches':int(status=='estimated'),'confidence':'limited','limitations':[],
            'id':'test','method_version':'test','quality':{'snapshot_hash':'test'}}
    text=render(result)
    assert 'None' not in text
    assert 'ID None' not in text
    assert ('Планируемый лот' if language=='ru' else 'Жоспарланған лот') in text
    assert ('НДС неизвестен' if language=='ru' else 'ҚҚС белгісіз') in text
    assert ('фактическая цена и отклонение не рассчитываются' if language=='ru' else 'нақты баға мен ауытқу есептелмейді') in text
    if status=='estimated':
        assert ('Калибровка: договоров 2' if language=='ru' else 'Калибрлеу: шарттар 2') in text
        assert ('множитель тренда' if language=='ru' else 'тренд көбейткіші') in text
    else:
        assert ('Недостаточно сопоставимых договоров' if language=='ru' else 'Салыстырмалы шарттар жеткіліксіз') in text


def test_fair_price_insufficient_target_keeps_contract_source():
    req=AnalysisRequest(intent='fair_price',lot_id=12)
    source='https://goszakup.gov.kz/ru/egzcontract/cpublic/show/123'
    result={'request':req.model_dump(mode='json'),'examples':[{
        'contract_id':123,'contract_url':source,'name_ru':'Бумага','name_kz':'Қағаз',
        'price_net':100,'quantity':10,'n_peers':0,'status':'insufficient_peers'}],
        'n_selected':1,'n_evaluated':1,'n_matches':0,'confidence':'insufficient',
        'limitations':[],'id':'test','method_version':'test','quality':{'snapshot_hash':'test'}}
    text=render(result)
    assert source in text and 'Недостаточно сопоставимых договоров' in text


@pytest.mark.parametrize('language',['ru','kk'])
def test_exact_kato_factor_one_is_not_presented_as_city_calibration(language):
    req=AnalysisRequest(intent='fair_price',language=language,lot_id=12)
    result={'request':req.model_dump(mode='json'),'examples':[{
        'contract_id':123,'contract_url':None,'name_ru':'Бумага','name_kz':'Қағаз',
        'price_net':100,'quantity':10,'n_peers':5,'status':'estimated','fair_price':90,'q25':80,'q75':100,
        'region_factor':1,'region_reference_city':None,'region_reason':'identical_delivery_KATO','city_code':None}],
        'n_selected':1,'n_evaluated':1,'n_matches':1,'confidence':'limited','limitations':[],
        'id':'test','method_version':'test','quality':{'snapshot_hash':'test'}}
    text=render(result)
    assert ('город не подтвержден' if language=='ru' else 'қала расталмаған') in text
    assert ('условность, не оценка калибровки' if language=='ru' else 'шартты мән, калибрлеу бағасы емес') in text
    assert ('Аналоги того же города' if language=='ru' else 'Сол қаланың баламалары') not in text


def test_search_render_exposes_provenance_fields():
    req=AnalysisRequest(intent='search',years=[2026])
    result={'request':req.model_dump(mode='json'),'examples':[{
        'contract_id':24155641,'item_id':54747313,'customer_bin':'980440001034',
        'signed_at':'2026-01-12 10:00:00','kato':'711510000','name_ru':'Молоко',
        'price_net':360,'quantity':1916,'contract_url':'https://goszakup.gov.kz/ru/egzcontract/cpublic/show/24155641'}],
        'n_selected':1,'n_evaluated':1,'n_matches':1,'confidence':'limited',
        'limitations':[],'id':'test','method_version':'test','quality':{'snapshot_hash':'test'}}
    text=render(result)
    for source_fact in ('54747313','2026-01-12','980440001034','711510000'):
        assert source_fact in text
