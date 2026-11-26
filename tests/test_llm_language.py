"""Sprache der Erkennung (25.11.2026): Einstellung, Prompt und englische Schreibweisen bei Datum/Betrag.

Der deutsche Prompt muss byte-gleich zum Stand vor der Einführung bleiben (c08e6e3) – die Genauigkeit ist mit genau
diesem Text gemessen (tools/llm-eval). Englisch ergänzt nur einen Absatz zur Ausgabesprache; alle Werte, die der Code
auswertet (kind, direction, recurring, expires_what, service_what), bleiben deutsch; die Fristnotiz darf seit 26.11.2026
englisch sein (die Wortlisten kennen beide Sprachen, tests/test_due_note_en.py)."""
import datetime
import hashlib

import pytest

from app import config, processing
from app.processing import amount_in_text, build_prompt, date_in_text

DAY = datetime.date(2026, 11, 25)
TEXT = "Rechnung Nr. 42 vom 03.11.2026 über 19,99 EUR"
TAGS = ["Rechnung", "Steuer"]


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# SHA-256 des Prompts mit dem Code von c08e6e3 (Prompt-Bau von dort nachgebaut, feste Eingaben)
@pytest.mark.parametrize("text,tags,restricted,digest", [
    (TEXT, TAGS, False, "01d8fb9b9ac5883782df251a29ea2bc4b43f43d4759e4fc308c7ff1edc3fca52"),
    (TEXT, TAGS, True, "d6262d5ee2302a4c5c6df6c992d143988f43c2f73eccf56f0fa5fcfdcd0ca837"),
    ("Brief", [], False, "66d094374052d1883636f66e48cd43ca6b4121573a4003fb3e7d8f657209494a"),
    ("Brief", [], True, "66d094374052d1883636f66e48cd43ca6b4121573a4003fb3e7d8f657209494a"),
])
def test_german_prompt_unchanged(text, tags, restricted, digest):
    assert sha(build_prompt(text, tags, restricted, "de", DAY)) == digest


def test_english_prompt_only_adds_language_note():
    de = build_prompt(TEXT, TAGS, False, "de", DAY)
    en = build_prompt(TEXT, TAGS, False, "en", DAY)
    head, tail = de.split("\n\nDokumenttext:", 1)
    en_head, en_tail = en.split("\n\nDokumenttext:", 1)
    assert en_tail == tail
    # Bis auf die Tag-Regel derselbe Text, danach nur der englische Absatz
    de_rule = head.split("Verwende bevorzugt")[1].split("Kein Absendername als Tag.")[0]
    en_rule = en_head.split("Verwende bevorzugt")[1].split("Kein Absendername als Tag.")[0]
    assert '"Invoice"' in en_rule and '"Rechnung", "Versicherung"' not in en_rule and '"Rechnung", "Versicherung"' in de_rule
    assert en_head.replace(en_rule, de_rule, 1) == head + processing.LLM_LANG_EN + processing.LLM_LANG_EN_TAGS
    # Die Aufzählungen, die KINDS/DIRECTIONS/INTERVALS/EXPIRES/SERVICES parsen, stehen unverändert deutsch drin
    for word in ('"Rechnung" (auch Quittung', '"Ausgabe" (ich muss zahlen', '"monatlich", "vierteljährlich"',
                 '"Personalausweis", "Reisepass"', '"Hauptuntersuchung"', '"Zahlungsziel"'):
        assert word in en
    assert '"title" und "summary" auf Englisch' in en and '"due_note" auf Englisch' in en and '"Tax"' in en


def test_english_restricted_keeps_tag_list():
    en = build_prompt(TEXT, TAGS, True, "en", DAY)
    de = build_prompt(TEXT, TAGS, True, "de", DAY)
    assert "AUSSCHLIESSLICH Tags aus dieser Liste" in en and '"Tax"' not in en   # nur vorhandene Tags, kein „Tax“
    head, tail = de.split("\n\nDokumenttext:", 1)
    assert en == head + processing.LLM_LANG_EN + "\n\nDokumenttext:" + tail


def test_llm_extract_uses_setting(monkeypatch):
    seen, lang = [], ["de"]
    monkeypatch.setattr(config, "llm_config", lambda: {"enabled": True, "url": "http://x", "language": lang[0]})
    from app import ask
    monkeypatch.setattr(ask, "chat", lambda prompt, timeout=0: seen.append(prompt) or '{"title": "x"}')
    processing.llm_extract("Hallo", [])
    lang[0] = "en"
    processing.llm_extract("Hallo", [])
    assert processing.LLM_LANG_EN not in seen[0] and processing.LLM_LANG_EN in seen[1]


def test_setting_admin_default_and_patch(admin, monkeypatch):
    assert admin.get("/settings").json()["llm_language"] == "de"
    monkeypatch.setattr(config, "LANGUAGE", "en")          # PAPIGATOR_LANGUAGE=en
    assert admin.get("/settings").json()["llm_language"] == "en"
    assert admin.patch("/settings", json={"llm_language": "de"}).json()["llm_language"] == "de"   # DB übersteuert
    assert admin.patch("/settings", json={"llm_language": "fr"}).json()["llm_language"] == "de"   # Unbekanntes bleibt
    assert admin.patch("/settings", json={"llm_language": "en"}).json()["llm_language"] == "en"
    assert config.language() == "en"


# ---------- Englische Schreibweisen bei Datum und Betrag ----------

@pytest.mark.parametrize("text", ["Invoice date: Jan 8, 2026", "January 8th, 2026", "8 January 2026", "Jan. 08, 2026",
                                  "on 2026-01-08", "Date: 08/01/2026"])
def test_english_dates(text):
    assert date_in_text("2026-01-08", text, allow_month=False)


def test_us_order_unambiguous_always():
    # 01/15/2026 kann kein deutsches Datum sein (Monat 15) – also der 15. Januar
    assert date_in_text("2026-01-15", "Invoice date 01/15/2026", allow_month=False)
    assert date_in_text("2026-01-15", "Invoice date 1/15/26", allow_month=False)
    assert not date_in_text("2026-01-15", "Invoice date 01.15.2026", allow_month=False)   # nur mit Schrägstrich


def test_us_order_ambiguous_only_in_english(admin):
    # 01/08/2026 ist auf Deutsch der 1. August – als 8. Januar nur, wenn die Erkennung auf Englisch steht
    assert date_in_text("2026-08-01", "Datum 01/08/2026", allow_month=False)
    assert not date_in_text("2026-01-08", "Date 01/08/2026", allow_month=False)
    admin.patch("/settings", json={"llm_language": "en"})
    assert date_in_text("2026-01-08", "Date 01/08/2026", allow_month=False)
    assert date_in_text("2026-08-01", "Datum 01/08/2026", allow_month=False)   # die deutsche Lesart gilt weiter


@pytest.mark.parametrize("amount,text", [(1234.56, "Total $1,234.56"), (14.27, "Total USD 14.27"), (14.27, "14.27 USD"),
                                         (50, "Total: $50"), (50, "USD 50"), (50, "50 USD"), (20, "£20"),
                                         (1234.56, "£1,234.56"), (99, "CHF 99")])
def test_english_amounts(amount, text):
    assert amount_in_text(amount, text)


@pytest.mark.parametrize("amount,text", [(50, "Postfach 50, 28195 Bremen"), (50, "50 Seiten"), (8, "$8.50"),
                                         (2026, "USD-Konto 2026-01-08")])
def test_round_amounts_still_need_currency(amount, text):
    assert not amount_in_text(amount, text)
