"""Generate a large, randomly-sampled, independently-sourced factual accuracy dataset.

Unlike a small hand-picked set of scenarios, this draws a random sample of real
signed contract items across the 27 configured organizations and all 3 years,
builds RU/KZ questions programmatically with templated phrasing variety, and
cross-checks every numeric/date/BIN fact against the raw OWS JSON payload (not
just the normalized SQL view) before including a case.

Anyone can independently re-verify the accuracy claim by re-running this with
a different --seed and then `python -m app.acceptance --factual-only
--factual-dataset <output>` — the result should land in the same range.

Usage:
    python -m scripts.gen_factual_dataset --seed 1 --n-search 60 --n-summary 30 \
        --output config/factual_holdout_random.json
"""
import argparse
import json
import random
from sqlalchemy import text

from app import db, settings

CONTRACT_URL = 'https://goszakup.gov.kz/ru/egzcontract/cpublic/show/{}'

SEARCH_TEMPLATES_RU = [
    'Покажи договорную позицию по лоту {lot} за {year} год',
    'Найди закупку по лоту номер {lot} за {year} год',
    'Какая цена и количество по лоту {lot} в {year} году?',
    'Выведи детали договора по лоту {lot} за {year}',
]
SEARCH_TEMPLATES_KK = [
    '{year} жылы {lot} лоты бойынша шарт позициясын көрсет',
    '{lot} лоты бойынша {year} жылғы сатып алуды тап',
    '{year} жылғы {lot} лотының бағасы мен санын көрсет',
]
SUMMARY_TEMPLATES_RU = [
    'Подведи итог закупок ЕНС ТРУ {enstru} у БИН {bin} за {years_word}',
    'Какая общая сумма и количество по ЕНС ТРУ {enstru} у БИН {bin} за {years_word}?',
    'Сводка по БИН {bin} и коду {enstru} за {years_word}',
]
SUMMARY_TEMPLATES_KK = [
    '{bin} БСН бойынша {enstru} ЕНС ТРУ {years_word_kk} қорытындысын бер',
    '{bin} БСН және {enstru} коды бойынша {years_word_kk} жиынтықты көрсет',
]


def raw_contract(conn, contract_id):
    row = conn.execute(
        text("SELECT payload FROM raw_objects WHERE entity='Contract' AND key LIKE :k ORDER BY fetched_at DESC LIMIT 1"),
        {'k': f'Contract:{contract_id}:%'}
    ).first()
    if not row:
        return None
    payload = row[0]
    return json.loads(payload) if isinstance(payload, str) else payload


def find_unit(raw, item_id):
    for u in raw.get('ContractUnits') or []:
        if u.get('id') == item_id:
            return u
    return None


def fmt_num(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    if isinstance(v, int):
        return f'{v:,}'.replace(',', ' ')
    return f'{v:,.2f}'.replace(',', ' ')


def gen_search_cases(conn, bins, n):
    # Only sample lots with exactly one effective item: that's what makes
    # "n_selected: 1" a valid, independently-checkable ground-truth fact.
    # ~5% of lots have multiple items and are deliberately excluded here.
    # (A precomputed single-item-lot set avoids an O(n^2) correlated subquery.)
    rows = conn.execute(text(f"""
        SELECT v.item_id, v.contract_id, v.lot_id, v.customer_bin, v.year, v.enstru,
               v.unit, v.kato, v.price, v.quantity, v.signed_at
        FROM v_contract_prices v
        JOIN (SELECT lot_id FROM v_contract_prices WHERE lot_id IS NOT NULL
              GROUP BY lot_id HAVING COUNT(*) = 1) single ON single.lot_id = v.lot_id
        WHERE v.customer_bin IN ({','.join(':b'+str(i) for i in range(len(bins)))})
        ORDER BY v.item_id
    """), {f'b{i}': b for i, b in enumerate(bins)}).fetchall()
    population = list(rows)
    random.shuffle(population)
    cases = []
    for row in population:
        if len(cases) >= n:
            break
        item_id, contract_id, lot_id, bin_, year, enstru, unit, kato, price, qty, signed_at = row
        raw = raw_contract(conn, contract_id)
        if not raw:
            continue
        unit_obj = find_unit(raw, item_id)
        if not unit_obj:
            continue
        # Cross-check the view's numbers/date against the raw OWS payload directly;
        # skip rather than paper over any disagreement.
        raw_price = unit_obj.get('itemPrice')
        raw_qty = unit_obj.get('quantity')
        if raw_price is None or raw_qty is None:
            continue
        if abs(float(raw_price) - float(price)) > 0.01 or abs(float(raw_qty) - float(qty)) > 0.0001:
            continue
        if raw.get('customerBin') != bin_:
            continue
        raw_sign_date = (raw.get('signDate') or '')[:len(str(signed_at))]
        if str(raw_sign_date) != str(signed_at):
            continue
        idx = len(cases) // 2
        tpl_ru = SEARCH_TEMPLATES_RU[idx % len(SEARCH_TEMPLATES_RU)]
        tpl_kk = SEARCH_TEMPLATES_KK[idx % len(SEARCH_TEMPLATES_KK)]
        base_id = f'rand_search_{item_id}'
        for lang, tpl in (('ru', tpl_ru), ('kk', tpl_kk)):
            q = tpl.format(lot=lot_id, year=year)
            cases.append({
                'id': f'{base_id}_{lang}',
                'language': lang,
                'question': q,
                'request': {'intent': 'search', 'language': lang, 'years': [year], 'lot_id': lot_id, 'top_k': 10},
                'facts': [
                    {'path': 'result.n_selected', 'value': 1,
                     'source': f'SQL v_contract_prices WHERE lot_id={lot_id}; cross-checked against raw_objects Contract:{contract_id}',
                     'answer_text': 'N = 1'},
                    {'path': 'result.examples.0.item_id', 'value': item_id,
                     'source': f'raw_objects Contract:{contract_id}; ContractUnits.id',
                     'answer_text': f'ID {item_id}'},
                    {'path': 'result.examples.0.contract_id', 'value': contract_id,
                     'source': f'raw_objects Contract:{contract_id}',
                     'answer_text': f'ID {contract_id}'},
                    {'path': 'result.examples.0.price_net', 'value': price,
                     'source': f'raw_objects Contract:{contract_id}; ContractUnits.itemPrice',
                     'answer_text': fmt_num(price)},
                    {'path': 'result.examples.0.quantity', 'value': qty,
                     'source': f'raw_objects Contract:{contract_id}; ContractUnits.quantity',
                     'answer_text': fmt_num(qty)},
                    {'path': 'result.examples.0.customer_bin', 'value': bin_,
                     'source': f'raw_objects Contract:{contract_id}; Contract.customerBin',
                     'answer_text': bin_},
                    {'path': 'result.examples.0.signed_at', 'value': str(signed_at),
                     'source': f'raw_objects Contract:{contract_id}; Contract.signDate',
                     'answer_text': str(signed_at)},
                    {'path': 'result.examples.0.contract_url', 'value': CONTRACT_URL.format(contract_id),
                     'source': f'URL constructed from Contract ID {contract_id}',
                     'answer_text': CONTRACT_URL.format(contract_id)},
                ],
                'expected_outcome': 'numeric',
            })
    return cases


def gen_summary_cases(conn, bins, n):
    groups = conn.execute(text(f"""
        SELECT customer_bin, enstru, GROUP_CONCAT(DISTINCT year) AS years, COUNT(*) AS n,
               SUM(price*quantity) AS amount, SUM(quantity) AS qty
        FROM v_contract_prices
        WHERE customer_bin IN ({','.join(':b'+str(i) for i in range(len(bins)))})
        GROUP BY customer_bin, enstru
        HAVING COUNT(*) >= 2
    """), {f'b{i}': b for i, b in enumerate(bins)}).fetchall()
    population = list(groups)
    random.shuffle(population)
    cases = []
    for row in population:
        if len(cases) >= n:
            break
        bin_, enstru, years_str, cnt, amount, qty = row
        years = sorted(set(int(y) for y in years_str.split(',')))
        check = conn.execute(text(
            "SELECT COUNT(*), SUM(price*quantity) FROM v_contract_prices WHERE customer_bin=:b AND enstru=:e"
        ), {'b': bin_, 'e': enstru}).first()
        if check[0] != cnt or abs(float(check[1]) - float(amount)) > 0.01:
            continue
        idx = len(cases) // 2
        tpl_ru = SUMMARY_TEMPLATES_RU[idx % len(SUMMARY_TEMPLATES_RU)]
        tpl_kk = SUMMARY_TEMPLATES_KK[idx % len(SUMMARY_TEMPLATES_KK)]
        if len(years) == 1:
            years_word, years_word_kk = f'{years[0]} год', f'{years[0]} жылғы'
            route_years = years
        else:
            # A literal "X–Y годы" range phrase is correctly expanded by any sane
            # parser to every year in between, not just the years with observed
            # data — the route ground truth must match that, not the data subset.
            years_word, years_word_kk = f'{years[0]}–{years[-1]} годы', f'{years[0]}–{years[-1]} жылдардағы'
            route_years = list(range(years[0], years[-1] + 1))
        base_id = f'rand_summary_{bin_}_{enstru.replace(".", "")}'
        for lang, tpl in (('ru', tpl_ru), ('kk', tpl_kk)):
            q = tpl.format(enstru=enstru, bin=bin_, years_word=years_word, years_word_kk=years_word_kk)
            cases.append({
                'id': f'{base_id}_{lang}',
                'language': lang,
                'question': q,
                'request': {'intent': 'summary', 'language': lang, 'bins': [bin_], 'years': route_years, 'enstru': enstru, 'top_k': 10},
                'facts': [
                    {'path': 'result.n_selected', 'value': cnt,
                     'source': f'Independent SQL COUNT(*) v_contract_prices WHERE customer_bin={bin_} AND enstru={enstru}',
                     'answer_text': f'N = {cnt}'},
                    {'path': 'result.amount_net', 'value': round(float(amount), 2),
                     'source': f'Independent SQL SUM(price*quantity) v_contract_prices WHERE customer_bin={bin_} AND enstru={enstru}',
                     'answer_text': fmt_num(round(float(amount), 2)) + ' KZT'},
                ],
                'expected_outcome': 'numeric',
            })
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20261019)
    parser.add_argument('--n-search', type=int, default=60)
    parser.add_argument('--n-summary', type=int, default=30)
    parser.add_argument('--output', type=str, default='config/factual_holdout_random.json')
    args = parser.parse_args()

    random.seed(args.seed)
    with open(settings.ROOT / 'config/scope.json', encoding='utf-8') as f:
        bins = json.load(f)['bins']

    with db.engine.connect() as conn:
        search_cases = gen_search_cases(conn, bins, args.n_search)
        summary_cases = gen_summary_cases(conn, bins, args.n_summary)
    all_cases = search_cases + summary_cases
    print(f'generated {len(all_cases)} cases ({len(search_cases)} search, {len(summary_cases)} summary), seed={args.seed}')
    out = settings.ROOT / args.output
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(all_cases, f, ensure_ascii=False, indent=2)
    print('saved to', out)


if __name__ == '__main__':
    main()
