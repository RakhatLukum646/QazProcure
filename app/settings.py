import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA / "procurement.db"}')
OWS_URL = os.getenv('OWS_URL', 'https://ows.goszakup.gov.kz/v3/graphql')
OWS_TOKEN = os.getenv('OWS_TOKEN', '')
# Current OWS publishDate matches the portal's UTC+5 clock; captured evidence is
# in submission/source_timezone_evidence.json. Blank explicitly disables inference.
_publication_offset = os.getenv('OWS_PUBLICATION_UTC_OFFSET_MINUTES', '300').strip()
OWS_PUBLICATION_UTC_OFFSET_MINUTES = int(_publication_offset) if _publication_offset else None
if OWS_PUBLICATION_UTC_OFFSET_MINUTES is not None and not -840 <= OWS_PUBLICATION_UTC_OFFSET_MINUTES <= 840:
    raise ValueError('OWS publication UTC offset must be between -840 and 840 minutes')
PAGE_SIZE = min(100, max(1, int(os.getenv('PAGE_SIZE', '50'))))
REQUEST_INTERVAL = float(os.getenv('REQUEST_INTERVAL', '0.3'))
MIN_PEERS = int(os.getenv('MIN_PEERS', '5'))
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')
LLM_PROVIDER = os.getenv('LLM_PROVIDER') or 'gemini'
if LLM_PROVIDER not in ('gemini','rules'):
    raise ValueError('LLM_PROVIDER must be gemini or explicitly selected rules')
ALLOW_RULES_FALLBACK = os.getenv('ALLOW_RULES_FALLBACK','false').lower()=='true'


def planner_name():
    return LLM_PROVIDER
