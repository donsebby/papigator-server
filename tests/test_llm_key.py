"""PAPIGATOR_LLM_KEY geht nur an die Adresse aus PAPIGATOR_LLM_URL, nicht an eine im Web eingetragene andere (27.11.2026)."""
from app import config, llm


def test_env_key_only_for_env_host(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "geheim")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://ki.example/v1")
    assert llm.env_key_for("https://ki.example/v1") == "geheim"
    assert llm.env_key_for("https://andere.example/v1") == "none"
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    assert llm.env_key_for("https://ki.example/v1") == "none"
