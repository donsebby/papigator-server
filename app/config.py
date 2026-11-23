import os
from pathlib import Path


def env(name: str, default: str) -> str:
    """PAPIGATOR_<name>; der alte Name BEIBERLESS_<name> gilt weiter, damit bestehende
    Installationen nach dem Umbenennen (07.11.2026) ohne Anpassung laufen."""
    return os.environ.get(f"PAPIGATOR_{name}") or os.environ.get(f"BEIBERLESS_{name}") or default


DATA_DIR = Path(env("DATA", "/data"))
DOCS_DIR = DATA_DIR / "documents"
THUMBS_DIR = DATA_DIR / "thumbnails"
# Neue Installationen: papigator.db; eine vorhandene beiberless.db wird weiter benutzt.
DB_PATH = DATA_DIR / "papigator.db"
if not DB_PATH.exists() and (DATA_DIR / "beiberless.db").exists():
    DB_PATH = DATA_DIR / "beiberless.db"

# OCR
OCR_LANGS = env("OCR_LANGS", "deu+eng")

# LLM-Tagging (OpenAI-kompatible Chat-API: Ollama /v1, LM Studio, mlx-lm ...)
LLM_BASE_URL = env("LLM_URL", "")  # z. B. http://192.168.1.50:11434/v1
LLM_MODEL = env("LLM_MODEL", "gemma4:12b-mlx")
LLM_API_KEY = env("LLM_KEY", "none")
LLM_MAX_CHARS = int(env("LLM_MAX_CHARS", "6000"))
# Reverse-Proxys, deren X-Forwarded-For/X-Real-IP geglaubt wird (kommagetrennt, IPs oder Netze wie 10.0.0.0/8).
# Standard: Loopback und private Netze – direkt aus dem Internet erreicht niemand den Dienst, nur über den Proxy.
TRUSTED_PROXIES = env("TRUSTED_PROXIES", "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7")

# Öffentliche Adresse für Freigabe- und Briefkasten-Links (z. B. https://papigator.example.de). Nur Startwert:
# die Einstellung in der Web-App (Einstellungen → Freigabe-Links) hat Vorrang.
PUBLIC_URL = env("PUBLIC_URL", "").strip().rstrip("/")

# Auth: Bootstrap-Token, bis der erste Nutzer angelegt ist
API_TOKEN = env("TOKEN", "")

THUMB_WIDTH = 400

# Sicherung: Zielordner (Docker: Volume /backup); leer = aus. In den Web-Einstellungen änderbar.
BACKUP_DIR = env("BACKUP", "")

# Eingangsordner (Scanner, Mail, Drag-and-drop per SMB/FTP): <INBOX>/<username>/*.pdf wird importiert
INBOX_DIR = Path(env("INBOX", str(DATA_DIR / "inbox")))
INBOX_POLL = int(env("INBOX_POLL", "10"))

# Öffentliche Demo (erfundene Daten, gemeinsame Konten ohne Admin-Recht): sperrt alles, womit ein Besucher andere
# aussperren, etwas öffentlich machen oder den Server belasten könnte – siehe app/demo.py
DEMO = env("DEMO", "").strip().lower() in ("1", "true", "yes", "on")
DEMO_MAX_UPLOAD_MB = int(env("DEMO_MAX_UPLOAD_MB", "15"))
DEMO_MAX_UPLOADS_PER_DAY = int(env("DEMO_MAX_UPLOADS_PER_DAY", "30"))
# API-Doku unter /docs und /redoc (ohne Anmeldung erreichbar) – für Entwickler, standardmäßig aus
API_DOCS = env("API_DOCS", "").strip().lower() in ("1", "true", "yes", "on")
# Geteilte Demo-Konten, die sich nicht selbst löschen dürfen (ein eigenes Testkonto zum Löschen darf es)
DEMO_KEEP_USERS = {u.strip() for u in env("DEMO_KEEP_USERS", "alex,sam,verwaltung").split(",") if u.strip()}


def ensure_dirs() -> None:
    for d in (DOCS_DIR, THUMBS_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def llm_config() -> dict:
    """LLM-Einstellungen: DB-Settings überschreiben die Env-Werte."""
    from .db import connect, get_setting
    with connect() as con:
        return {
            "enabled": get_setting(con, "llm_enabled", "1" if LLM_BASE_URL else "0") == "1",
            "url": get_setting(con, "llm_url", LLM_BASE_URL) or "",
            "model": get_setting(con, "llm_model", LLM_MODEL) or "",
            # Ersatz-Server, wenn der erste nicht erreichbar ist (z. B. großer Rechner aus, kleiner Server immer an)
            "url2": get_setting(con, "llm_url2", "") or "",
            "model2": get_setting(con, "llm_model2", "") or "",
            # Eigenes (meist kleineres, schnelleres) Modell fürs Fragen je Server; leer = dasselbe wie fürs Erkennen
            "ask_model": get_setting(con, "llm_ask_model", "") or "",
            "ask_model2": get_setting(con, "llm_ask_model2", "") or "",
            "tag_mode": get_setting(con, "llm_tag_mode", "free"),
            "ocr_langs": get_setting(con, "ocr_langs", OCR_LANGS) or OCR_LANGS,
            "trash_days": int(get_setting(con, "trash_days", "30") or 30),
            "duplicate_mode": get_setting(con, "duplicate_mode", "reject"),   # reject | allow
            "thumb_width": int(get_setting(con, "thumb_width", str(THUMB_WIDTH)) or THUMB_WIDTH),
        }
