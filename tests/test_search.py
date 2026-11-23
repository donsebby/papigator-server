"""Volltextsuche: Präfixe, Ausschluss mit Minus, Sonderzeichen, Vorschau-Ausschnitt."""
import pytest

from app.db import fts_exclude_query, fts_query


def titles(api, **params):
    return sorted(d["title"] for d in api.get("/documents", params=params).json())


@pytest.fixture
def docs(admin):
    admin.upload("Alex und Sam Muster, gemeinsame Steuererklärung", "Gemeinsam")
    admin.upload("Sam Muster, Arbeitsvertrag", "Sam")
    admin.upload("Alex Muster, Gehaltsabrechnung", "Alex")
    return admin


def test_prefix_search(docs):
    assert titles(docs, q="Steuer") == ["Gemeinsam"]
    assert titles(docs, q="muster") == ["Alex", "Gemeinsam", "Sam"]


def test_umlauts_and_case(docs):
    assert titles(docs, q="STEUERERKLARUNG") == ["Gemeinsam"]


def test_exclude(docs):
    assert titles(docs, q="sam -alex") == ["Sam"]
    assert titles(docs, q="-alex") == ["Sam"]
    assert titles(docs, q="-alex -sam") == []


def test_special_characters_do_not_break(docs):
    for q in ['"', '"Sam', "Sam)", "(", "*", "-", "--", "a OR", "NOT", "sam AND", "^x", "x:y", "100%"]:
        r = docs.get("/documents", params={"q": q})
        assert r.status_code == 200, (q, r.text)


def test_snippet_marks_hit(docs):
    d = docs.get("/documents", params={"q": "Arbeitsvertrag"}).json()[0]
    assert "[[Arbeitsvertrag]]" in d["snippet"]


def test_fts_query_helpers():
    assert fts_query("sam -alex") == '"sam"* NOT "alex"*'
    assert fts_query("-alex") == ""
    assert fts_exclude_query("-a -b") == '"a"* OR "b"*'
    assert fts_query('ab"c') == '"abc"*'


@pytest.fixture
def household(admin):
    admin.upload("Finanzamt Karlsruhe, Einkommensteuerbescheid für 2024, Erstattung", "Bescheid")
    admin.upload("Kündigungsbestätigung Ihrer Mitgliedschaft", "Fitness")
    admin.upload("Betriebskostenabrechnung 2025 für die Wohnung", "Wohnung")
    admin.upload("Gebührenbescheid Kindertagesstätte Sonnenblume", "Gebühren")
    admin.upload("Beitragsrechnung Kraftfahrtversicherung", "Versicherung")
    admin.upload("Rechnung Lego Duplo", "Spielzeug", tags="Kinder")
    admin.upload("Autoren-Lesung, Rechnungsnummer 7720251", "Autor")
    return admin


def test_word_inside_compound(household):
    """„Steuerbescheid“ steckt in „Einkommensteuerbescheid“ – mit Treffer-Ausschnitt."""
    assert titles(household, q="Steuerbescheid") == ["Bescheid"]
    d = household.get("/documents", params={"q": "steuerbescheid"}).json()[0]
    assert "[[" in (d["snippet"] or "")
    # kurze Wörter und Zahlen nur am Wortanfang
    assert titles(household, q="2025") == ["Wohnung"]
    assert titles(household, q="Auto") == ["Autor"]


def test_spelling_without_umlauts(household):
    assert titles(household, q="kuendigung") == ["Fitness"]
    assert titles(household, q="Kündigung") == ["Fitness"]


def test_synonyms_and_hyphen(household):
    assert titles(household, q="Nebenkosten") == ["Wohnung"]
    assert titles(household, q="Kita") == ["Gebühren"]
    assert titles(household, q="Kfz-Versicherung") == ["Versicherung"]


def test_tag_name_finds_documents(household):
    assert titles(household, q="Kinder") == ["Gebühren", "Spielzeug"]   # Tag „Kinder“ + „Kindertagesstätte“
    assert titles(household, q="kinder lego") == ["Spielzeug"]


def test_search_without_trigram_index(household, monkeypatch):
    """Ältere SQLite ohne Trigramme: Suche läuft weiter, nur ohne Treffer mitten im Wort."""
    import app.db as db
    monkeypatch.setattr(db, "_trigram", False)
    assert titles(household, q="Steuerbescheid") == []
    assert titles(household, q="Einkommensteuer") == ["Bescheid"]
    assert titles(household, q="Kita") == ["Gebühren"]
