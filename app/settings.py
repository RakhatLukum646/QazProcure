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
PAGE_SIZE = min(100, max(1, int(os.getenv('PAGE_SIZE', '50'))))
REQUEST_INTERVAL = float(os.getenv('REQUEST_INTERVAL', '0.3'))
MIN_PEERS = int(os.getenv('MIN_PEERS', '5'))
OLLAMA_URL = os.getenv('OLLAMA_URL', '').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', '')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'gemini' if GOOGLE_API_KEY else 'ollama' if OLLAMA_URL and OLLAMA_MODEL else 'rules')


def planner_name():
    return LLM_PROVIDER
