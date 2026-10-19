"""Small read-only OWS client: bounded retries, no credentials in logs."""
import time
from urllib.parse import urlsplit
import httpx
from . import settings


class OWSError(RuntimeError):
    pass


class OWSClient:
    def __init__(self, token=None, transport=None):
        token = token or settings.OWS_TOKEN
        if not token:
            raise OWSError('OWS_TOKEN is required')
        self.client = httpx.Client(
            timeout=60, transport=transport,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def query(self, query, variables=None):
        for attempt in range(4):
            try:
                response = self.client.post(settings.OWS_URL, json={'query': query, 'variables': variables or {}})
            except httpx.TransportError:
                if attempt == 3:
                    raise OWSError('OWS network timeout or connection failure') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < 3:
                    retry = response.headers.get('retry-after', '')
                    time.sleep(min(float(retry), 30) if retry.isdigit() else 2 ** attempt)
                    continue
            if response.status_code != 200:
                raise OWSError(f'OWS HTTP {response.status_code}; check access/service availability')
            try:
                body = response.json()
            except ValueError:
                raise OWSError('OWS returned invalid JSON') from None
            if body.get('errors'):
                raise OWSError('; '.join(e.get('message', 'GraphQL error') for e in body['errors']))
            if not isinstance(body.get('data'), dict):
                raise OWSError('OWS response has no data object')
            time.sleep(settings.REQUEST_INTERVAL)
            return body
        raise OWSError('OWS retry budget exhausted')

    def kato(self, code):
        """Return an official KATO node by exact code; never infer ancestors from digits."""
        if not str(code).isdigit():
            raise OWSError('Invalid KATO code')
        origin = urlsplit(settings.OWS_URL)
        url = f'{origin.scheme}://{origin.netloc}/v3/refs/ref_kato/{code}'
        for attempt in range(4):
            try:
                response = self.client.get(url)
            except httpx.TransportError:
                if attempt == 3:
                    raise OWSError('OWS KATO network failure') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise OWSError(f'OWS KATO HTTP {response.status_code}')
            try:
                body = response.json()
                items = body['items']
            except (ValueError, KeyError, TypeError):
                raise OWSError('OWS KATO invalid response') from None
            if not isinstance(items, list) or len(items) > 1:
                raise OWSError('OWS KATO ambiguous response')
            if not items:
                return None
            node = items[0]
            if str(node.get('code')) != str(code):
                raise OWSError('OWS KATO code mismatch')
            time.sleep(settings.REQUEST_INTERVAL)
            return node
        raise OWSError('OWS KATO retry budget exhausted')


PLAN_FIELDS = '''id rootrecordId subjectBiin subjectNameRu subjectNameKz
nameRu nameKz descRu descKz extraDescRu extraDescKz refUnitsCode refEnstruCode
count price amount plnPointYear refSubjectTypeId refPlnPointStatusId
isActive isDeleted indexDate
RefUnits { code nameRu nameKz }
RefEnstru { id code nameRu nameKz }
RefSubjectType { id nameRu nameKz }
RefPlnPointStatus { id nameRu nameKz }
PlansKato { id refKatoCode count isActive isDeleted fullDeliveryPlaceNameRu
  RefKato { code nameRu nameKz level parentCode } }
'''

ANNOUNCEMENT_FIELDS = '''id numberAnno nameRu nameKz orgBin customerBin
publishDate finYear refBuyStatusId indexDate RefBuyStatus { id nameRu nameKz }'''

LOT_FIELDS = '''id lotNumber nameRu nameKz descriptionRu descriptionKz customerBin
count amount trdBuyId refLotStatusId isDeleted indexDate pointList
RefLotsStatus { id nameRu nameKz }
TrdBuy { ''' + ANNOUNCEMENT_FIELDS + ''' }
Plans { ''' + PLAN_FIELDS + ''' }'''

CONTRACT_FIELDS = '''id rootId parentId customerBin supplierBiin signDate finYear
contractNumber contractNumberSys refContractStatusId refCurrencyCode withNds
contractSum contractSumWnds deleted indexDate trdBuyId
RefContractStatus { id nameRu nameKz }
RefCurrency { code name }
Customer { pid bin nameRu nameKz }
Supplier { pid bin nameRu nameKz }
TrdBuy { ''' + ANNOUNCEMENT_FIELDS + ''' }
ContractUnits { id rootId parentId lotId plnPointId itemPrice itemPriceWnds quantity
totalSum totalSumWnds deleted crDeleted indexDate
Plans { ''' + PLAN_FIELDS + ''' } }'''

SPECS = {
    'Contract': ('ContractFiltersInput', CONTRACT_FIELDS),
    'Plans': ('PlansFiltersInput', PLAN_FIELDS),
    'Lots': ('LotsFiltersInput', LOT_FIELDS),
}


def page_query(entity, units=False):
    filter_type, fields = SPECS[entity]
    declaration=', $units: ContractUnitsFiltersInput' if units else ''
    argument=', contractUnitsFilter: $units' if units else ''
    return (f'query($filter: {filter_type}, $limit: Int, $after: Int{declaration}) '
            f'{{ {entity}(filter: $filter, limit: $limit, after: $after{argument}) {{ {fields} }} }}')
