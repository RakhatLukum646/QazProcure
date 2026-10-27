import importlib
import os


def test_llm_provider_defaults_to_gemini_when_key_present(monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: None)
    # so a user who fills in GOOGLE_API_KEY should get real NL parsing automatically
    # instead of silently falling back to the restricted rule-based parser.
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-key')
    from app import settings
    importlib.reload(settings)
    try:
        assert settings.LLM_PROVIDER == 'gemini'
    finally:
        monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
        importlib.reload(settings)


def test_llm_provider_defaults_to_gemini_without_cloud_key(monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    # Prevent a real local .env (this machine has a working GOOGLE_API_KEY) from
    # refilling the vars this test just removed.
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: None)
    from app import settings
    importlib.reload(settings)
    try:
        assert settings.LLM_PROVIDER == 'gemini'
        assert settings.ALLOW_RULES_FALLBACK is False
    finally:
        importlib.reload(settings)
