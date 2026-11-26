"""Geld-Regeln ohne Datenbank: Beträge lesen, Währung, „steht der Betrag im Text?“, Art/Richtung/Rhythmus/§ 35a
aus der KI-Antwort, Geldfluss und Zweitscans, Kalender-Schritte und Kündigungsfristen der Verträge."""
from datetime import date

import pytest

from app import ask, extras, processing
from app.processing import _amount, _currency, amount_in_text, money_fields


# ---------- Betrag lesen ----------

@pytest.mark.parametrize("raw, expected", [
    ("1.234,56 €", 1234.56),
    ("1.234,56", 1234.56),
    ("1,234.56", 1234.56),
    ("1 234,56 EUR", 1234.56),
    ("190,32 €", 190.32),
    ("EUR 324,87", 324.87),
    ("-12,50", -12.5),
    ("−12,50 €", -12.5),            # typografisches Minus
    ("12,-", 12.0),
    ("12,– €", 12.0),
    ("1.250,-", 1250.0),
    ("1.234", 1234.0),               # nur Tausenderpunkt
    ("12.345.678", 12345678.0),
    ("1,234", 1234.0),               # englischer Tausender ohne Nachkommastellen
    ("12.5", 12.5),
    ("12,5", 12.5),
    ("0,99", 0.99),
    (49.99, 49.99),
    (50, 50.0),
    (12.345, 12.35),                 # auf Cent gerundet (Gleitkomma: 12.345 liegt knapp darüber)
    ("49,999", 49999.0),             # Tausendergruppe – keine drei Nachkommastellen bei Geld
])
def test_amount_parses_common_formats(raw, expected):
    assert _amount(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", "€", "–", "kein Betrag", True, False, "2025-01-01", "1-2", "--5", 1e13])
def test_amount_rejects_garbage(raw):
    assert _amount(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("EUR", "EUR"), ("eur", "EUR"), (" € ", "EUR"), ("Euro", "EUR"), ("USD", "USD"), ("usd", "USD"),
    ("$", "USD"), ("£", "GBP"), ("CHF", "CHF"), ("sFr", "CHF"),
    (None, None), ("", None), ("Dollar", None), ("EU", None), ("EURO€", None), ("12", None),
])
def test_currency_is_normalised(raw, expected):
    assert _currency(raw) == expected


# ---------- Steht der Betrag im Text? ----------

INVOICE = ("Stadtwerke Musterstadt GmbH · Energieweg 12 · 76133 Karlsruhe\nRechnung Nr. 4711 vom 12.03.2025\n"
           "Zwischensumme 1.234,56 €\nGesamtbetrag EUR 324,87\nAbschlag 50,- monatlich\nUSD total 1,234.50\n"
           "Kundennummer 8812233")


@pytest.mark.parametrize("amount", [324.87, -324.87, 1234.56, 50, 50.0, 1234.5])
def test_amount_found_in_text(amount):
    assert amount_in_text(amount, INVOICE)


@pytest.mark.parametrize("amount", [
    12.03,      # steckt im Datum 12.03.2025
    76133,      # Postleitzahl
    4711,       # Rechnungsnummer
    2025,       # Jahreszahl
    24.87,      # Teil von 324,87
    324.8,      # 324,80 steht nicht da
    3.25,
    8812233,
    1234.57,
    0,
])
def test_amount_not_in_text(amount):
    assert not amount_in_text(amount, INVOICE)


@pytest.mark.parametrize("amount, text", [
    (324.87, "Betrag: 324,87"),
    (324.87, "Total 324.87"),
    (324.87, "EUR 324,87"),
    (324.87, "324,87 EUR"),
    (324.87, "324,87€"),
    (324.87, "Summe:324,87."),            # Satzende
    (1234.56, "1.234,56"),
    (1234.56, "1,234.56"),
    (1234.56, "1 234,56 €"),
    (1234.56, "1 234,56 €"),         # schmales Leerzeichen als Tausendertrenner
    (1234.56, "1'234.56 CHF"),
    (1234.56, "1234,56"),
    (1234567.89, "1.234.567,89 €"),
    (50, "Spende 50,00 Euro"),
    (50, "Spende: 50 €"),
    (50, "EUR 50"),
    (50, "50,- bar erhalten"),
    (50, "50,– bar erhalten"),
    (50, "€50"),
    (12.5, "Gutschrift -12,50 €"),
    (-12.5, "Gutschrift 12,50 €"),
    (39.95, "Monatlicher Preis\n39,95\nEUR"),
])
def test_amount_formats_in_text(amount, text):
    assert amount_in_text(amount, text)


@pytest.mark.parametrize("amount, text", [
    (50, "76150 Karlsruhe"),               # glatter Betrag ohne Währung/Nachkommastellen zählt nicht
    (50, "Seite 50 von 60"),
    (50, "50,5 kg"),
    (324.87, "324,B7"),                    # OCR-Fehler müssen nicht erkannt werden
    (324.87, "1324,87"),
    (324.87, "324,875"),
    (324.87, "324,87,5"),
    (1234.56, "1.234,5"),
    (12.0, "12.03.2025"),
    (12.0, "Rechnung 12/2025"),
    (5, ""),
    (None, "Betrag 5,00"),
])
def test_amount_formats_not_in_text(amount, text):
    assert not amount_in_text(amount, text)


# ---------- Art, Richtung, Rhythmus, § 35a aus der KI-Antwort ----------

@pytest.mark.parametrize("data, kind, direction", [
    ({"kind": "Rechnung", "direction": "Ausgabe"}, "rechnung", "out"),
    ({"kind": "rechnung", "direction": "ausgabe"}, "rechnung", "out"),
    ({"kind": "Gehalt", "direction": "Ausgabe"}, "gehalt", "in"),             # Gehalt ist nie Ausgabe
    ({"kind": "Gehalt"}, "gehalt", "in"),
    ({"kind": "Gutschrift", "direction": "keine"}, "gutschrift", "in"),
    ({"kind": "Mahnung", "direction": "Ausgabe"}, "mahnung", "none"),         # die Rechnung zählt schon
    ({"kind": "Kündigung", "direction": "Ausgabe"}, "kuendigung", "none"),
    ({"kind": "Kuendigung"}, "kuendigung", "none"),
    ({"kind": "Vertrag", "direction": "Ausgabe"}, "vertrag", "out"),          # Kaufvertrag: echte Ausgabe
    ({"kind": "Vertrag", "direction": "Ausgabe", "recurring": "monatlich"}, "vertrag", "none"),   # Rate
    ({"kind": "Vertrag", "direction": "Einnahme"}, "vertrag", "none"),        # Arbeitsvertrag: kein Geldfluss
    ({"kind": "Abrechnung", "direction": "Einnahme"}, "abrechnung", "in"),    # Guthaben
    ({"kind": "Bescheid", "direction": "Einnahme"}, "bescheid", "in"),        # Steuererstattung
    ({"kind": "Spende", "direction": "Ausgabe"}, "spende", "out"),
    ({"kind": "Info", "direction": "keine"}, "info", "none"),
    ({"direction": "out"}, None, "out"),
    ({"direction": "in"}, None, "in"),
])
def test_kind_and_direction_rules(data, kind, direction):
    f = money_fields(data)
    assert f.get("doc_kind") == kind and f.get("direction") == direction


@pytest.mark.parametrize("data, recurring", [
    ({"kind": "Vertrag", "recurring": "Monatlich"}, "monatlich"),
    ({"kind": "Bescheid", "recurring": "vierteljährlich"}, "vierteljährlich"),
    ({"kind": "Vertrag", "recurring": "halbjährlich"}, "halbjährlich"),
    ({"kind": "Vertrag", "recurring": "jährlich"}, "jährlich"),
    ({"kind": "Vertrag", "recurring": "täglich"}, None),
    ({"kind": "Vertrag", "recurring": "monthly"}, None),
    # Arbeitsvertrag & Co.: regelmäßiges Geld kommt herein – kein Vertrag, der etwas kostet
    ({"kind": "Vertrag", "recurring": "monatlich", "title": "Arbeitsvertrag Kranich"}, None),
    ({"kind": "Vertrag", "recurring": "monatlich", "title": "Anstellungsvertrag Praxis"}, None),
    ({"kind": "Vertrag", "recurring": "monatlich", "title": "Gehaltserhöhung 2026"}, None),
    ({"kind": "Vertrag", "recurring": "monatlich", "direction": "Einnahme", "title": "Mietvertrag (Vermieter)"}, None),
    ({"kind": "Bescheid", "recurring": "monatlich", "title": "Rentenbescheid"}, "monatlich"),   # nur bei Verträgen
])
def test_recurring_rules(data, recurring):
    assert money_fields(data).get("recurring") == recurring


@pytest.mark.parametrize("raw, expected", [
    (324.87, 324.87), ("324,87 €", 324.87), ("1.084,87", 1084.87), (0.01, 0.01), (99999.99, 99999.99),
    (0, None), (-5, None), (100000, None), (250000, None), ("abc", None), (None, None), ("", None),
])
def test_tax_35a_bounds(raw, expected):
    assert money_fields({"tax_35a": raw}).get("tax_35a") == expected


def test_autopay_only_from_real_booleans():
    assert money_fields({"autopay": True})["autopay"] == 1
    assert money_fields({"autopay": False})["autopay"] == 0
    assert "autopay" not in money_fields({"autopay": "ja"})
    assert "autopay" not in money_fields({"autopay": 1})
    assert money_fields({}) == {}
    assert money_fields({"kind": "Quatsch", "direction": "vielleicht", "recurring": "ab und zu"}) == {}


def test_prompt_asks_for_money_and_keeps_date_rules():
    p = processing.LLM_PROMPT
    for field in ('"amount"', '"currency"', '"kind"', '"direction"', '"recurring"', '"tax_35a"', '"autopay"'):
        assert field in p
    # die neuen Datums- und Fristen-Regeln bleiben
    assert "Formularstände" in p and "nicht raten" in p and "innerhalb von 14 Tagen" in p
    assert "Scan-, Fax- und Druckvermerke" in p and "{today}" in p                     # Stand von main
    assert p.format(tag_rule="X", text="Y", today="16.11.2026", lang_note="")    # Platzhalter passen noch


# ---------- Geldfluss und Zweitscans ----------

def _row(**f):
    base = {"id": 1, "sender": "Becker", "document_date": "2025-09-15", "amount": 547.88, "currency": "EUR",
            "direction": None, "doc_kind": None}
    return {**base, **f}


@pytest.mark.parametrize("direction, kind, expected", [
    ("out", None, "out"), ("in", None, "in"), ("none", None, "none"),
    ("out", "gehalt", "out"),                 # von Hand gesetzte Richtung gewinnt
    (None, None, "out"),                      # Altbestand ohne Richtung: Ausgabe …
    (None, "rechnung", "out"),
    (None, "gehalt", "in"),                   # … außer der Art nach
    (None, "gutschrift", "in"),
    (None, "mahnung", "none"), (None, "vertrag", "none"), (None, "kuendigung", "none"), (None, "info", "none"),
    ("quatsch", None, "out"),
])
def test_flow(direction, kind, expected):
    assert extras.flow(_row(direction=direction, doc_kind=kind)) == expected


def test_repeats_are_same_sender_day_amount_currency_and_flow():
    rows = [
        _row(id=1),
        _row(id=2, sender=" becker "),                       # Zweitscan (Schreibweise egal)
        _row(id=3, document_date="2025-09-16"),              # anderer Tag
        _row(id=4, amount=547.89),                           # anderer Betrag
        _row(id=5, currency="USD"),                          # andere Währung
        _row(id=6, direction="in"),                          # andere Richtung
        _row(id=7, sender=""),                               # ohne Absender: nie Wiederholung
        _row(id=8, sender=""),
        _row(id=9, amount=None), _row(id=10, amount=None),   # ohne Betrag: nie
        _row(id=11, document_date=None), _row(id=12, document_date=None),
        _row(id=13, document_date="2025-09-15T10:00"),       # gleicher Tag mit Uhrzeit
    ]
    assert extras.repeats(rows) == {2, 13}
    # die älteste ID zählt – auch wenn die Zeilen anders sortiert kommen
    assert extras.repeats(list(reversed(rows))) == {2, 13}


# ---------- Verträge: Kalender-Schritte, Kündigungsfristen ----------

@pytest.mark.parametrize("start, interval, n, expected", [
    (date(2025, 1, 31), "monatlich", 1, date(2025, 2, 28)),      # Monatsende bleibt im Monat
    (date(2024, 1, 31), "monatlich", 1, date(2024, 2, 29)),      # Schaltjahr
    (date(2025, 1, 31), "monatlich", 2, date(2025, 3, 31)),
    (date(2025, 11, 15), "monatlich", 2, date(2026, 1, 15)),     # über den Jahreswechsel
    (date(2025, 1, 15), "vierteljährlich", 1, date(2025, 4, 15)),
    (date(2025, 8, 31), "halbjährlich", 1, date(2026, 2, 28)),
    (date(2024, 2, 29), "jährlich", 1, date(2025, 2, 28)),
])
def test_calendar_step(start, interval, n, expected):
    assert extras._step(start, interval, n) == expected


def _due(id_, due_date, note, kind=None):
    return {"id": id_, "due_date": due_date, "due_note": note, "doc_kind": kind}


def test_cancel_by_picks_latest_and_rolls_yearly_forward():
    today = date(2026, 11, 16)
    docs = [_due(1, "2024-11-30", "Kündigungsfrist"), _due(2, "2025-11-30", "Kündigungsfrist Vertrag")]
    assert extras._cancel_by(docs, "jährlich", today) == {"date": "2026-11-30", "note": "Kündigungsfrist Vertrag", "document_id": 2}
    # schon vorbei in diesem Jahr → nächstes Jahr
    assert extras._cancel_by([_due(1, "2025-10-31", "kündbar bis")], "jährlich", today)["date"] == "2027-10-31"
    assert extras._cancel_by([_due(1, "2025-09-30", "Kündigung bis")], "jährlich", today)["date"] == "2027-09-30"
    # monatliche Verträge: eine vergangene Frist wiederholt sich nicht
    assert extras._cancel_by([_due(1, "2026-10-31", "Kündigungsfrist")], "monatlich", today) is None
    # eine künftige Frist gilt auch bei monatlichen Verträgen
    assert extras._cancel_by([_due(1, "2026-12-31", "Kündigungsfrist")], "monatlich", today)["date"] == "2026-12-31"


def test_cancel_by_ignores_other_deadlines_and_confirmations():
    today = date(2026, 11, 16)
    assert extras._cancel_by([_due(1, "2026-12-01", "Zahlungsziel")], "jährlich", today) is None
    assert extras._cancel_by([_due(1, "2026-12-01", "Kündigungsfrist", kind="kuendigung")], "jährlich", today) is None
    assert extras._cancel_by([_due(1, None, "Kündigungsfrist")], "jährlich", today) is None
    # Sonderkündigungsrecht: einmalig, wird nicht fortgeschrieben
    assert extras._cancel_by([_due(1, "2025-12-31", "Sonderkündigungsrecht")], "jährlich", today) is None


# ---------- Frag Papigator: Summen nur auf Nachfrage ----------

@pytest.mark.parametrize("question", [
    "Was habe ich insgesamt an die Telekom gezahlt?",
    "Wie viel habe ich 2025 für Strom ausgegeben?",
    "Wieviel hat mich die Lindwurm gekostet?",
    "Summe der Handwerkerrechnungen 2025",
    "Was haben wir zusammen an Kita-Gebühren bezahlt?",
    "Was habe ich 2025 an die Stadtwerke gezahlt?",
    "Wie viel verdiene ich?",
    "Was habe ich vom Finanzamt bekommen?",
])
def test_sum_questions_are_recognised(question):
    assert ask._SUM_Q.search(question)


@pytest.mark.parametrize("question", [
    "Zeig mir die Rechnungen der Telekom",
    "Wann läuft mein Handyvertrag aus?",
    "Welche Versicherungen habe ich?",
    "Habe ich die Rechnung vom Schornsteinfeger schon?",
    "Was ist bald fällig?",
    "Wo ist meine Steuer-ID?",
])
def test_other_questions_get_no_sums(question):
    assert not ask._SUM_Q.search(question)


def test_eur_text():
    assert ask._eur_text(1084.87) == "1.084,87 €"
    assert ask._eur_text(14.27, "USD") == "14,27 USD"
    assert ask._eur_text(0.5, "EUR") == "0,50 €"
