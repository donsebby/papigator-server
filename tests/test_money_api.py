"""Geld über die Schnittstelle und in den Auswertungen: KI-Ergebnis speichern (Betrag nur, wenn er im Text steht),
PATCH/GET/Sortieren, Nachtragen im Bestand, Steuer-Paket, Verträge & Abos, Absender-Summen, Ordner-Export,
Frag Papigator (Summen nur auf Nachfrage)."""
import csv
import io
import json
import sqlite3
import zipfile
from datetime import date

import pytest

from app import ask, config, extras, processing, worker
from app.db import connect


def _set(doc_id, **f):
    con = sqlite3.connect(config.DB_PATH)
    con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), doc_id))
    con.commit(); con.close()


def _doc(api, title, sender, amount, day, **f):
    d = api.upload(f"{title} {sender} {day}", title).json()
    _set(d["id"], sender=sender, amount=amount, document_date=day, **f)
    return d["id"]


def _apply(doc_id, data):
    with connect() as con:
        processing.apply_llm(con, doc_id, data)
        return dict(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def _zip(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _package(api, **params):
    z = _zip(api.get("/export/package.zip", params=params))
    sums = json.loads(z.read("summen.json"))
    rows = list(csv.reader(io.StringIO(next(z.read(n) for n in z.namelist() if n.endswith(".csv")).decode("utf-8-sig")),
                           delimiter=";"))
    return z, sums, rows


# ---------- KI-Ergebnis speichern ----------

def test_llm_answer_stores_money_when_amount_is_in_text(admin):
    d = admin.upload("Telekom Deutschland GmbH\nRechnung vom 04.10.2026\nRechnungsbetrag 49,99 €\nwird abgebucht",
                     "Rechnung").json()["id"]
    row = _apply(d, {"title": "Rechnung Telekom Oktober 2026", "amount": "49,99 €", "currency": "€",
                     "direction": "Ausgabe", "kind": "Rechnung", "autopay": True, "document_date": "2026-10-04"})
    assert (row["amount"], row["currency"], row["direction"], row["doc_kind"], row["autopay"]) == (49.99, "EUR", "out", "rechnung", 1)
    assert row["document_date"] == "2026-10-04"
    doc = admin.get(f"/documents/{d}").json()
    assert {"amount", "currency", "direction", "doc_kind", "autopay", "recurring", "tax_35a"} <= doc.keys()
    assert doc["amount"] == 49.99 and doc["currency"] == "EUR" and doc["direction"] == "out"


def test_llm_amount_not_in_text_is_dropped_rest_stays(admin):
    d = admin.upload("Handwerker Becker\nRechnung vom 15.09.2025\nArbeitslohn 324,87 €\nMaterial 223,01 €\nGesamt 547,88 €",
                     "Rechnung").json()["id"]
    # zusammengerechnet (324,87 + 223,01 + 100) – steht so nicht da
    row = _apply(d, {"title": "Rechnung Becker", "amount": 647.88, "currency": "EUR", "kind": "Rechnung",
                     "direction": "Ausgabe", "tax_35a": 324.87, "sender": "Becker"})
    assert row["amount"] is None and row["currency"] is None
    assert row["title"] == "Rechnung Becker" and row["sender"] == "Becker"
    assert row["doc_kind"] == "rechnung" and row["direction"] == "out" and row["tax_35a"] == 324.87
    # richtig erkannt: steht im Text
    row = _apply(d, {"amount": "547,88", "currency": "EUR"})
    assert row["amount"] == 547.88 and row["currency"] == "EUR"


def test_llm_amount_check_needs_text(admin):
    """Ohne Text (sollte es nicht geben) bleibt es beim alten Verhalten: der Betrag wird übernommen."""
    d = admin.upload("", "Leer").json()["id"]
    assert _apply(d, {"amount": 12.5})["amount"] == 12.5


@pytest.mark.parametrize("text, raw, expected", [
    ("Spendenbescheinigung über 50,00 Euro", 50, 50.0),
    ("Bitte überweisen Sie EUR 1.250,- bis Monatsende", "1.250,-", 1250.0),
    ("Gutschrift: -12,50 € werden erstattet", "-12,50", -12.5),
    ("Invoice total USD 1,234.50", "1,234.50", 1234.5),
    ("Ihr Beitrag: 55,08 € vierteljährlich", "55.08", 55.08),
])
def test_llm_amount_formats(admin, text, raw, expected):
    d = admin.upload(text, "Beleg").json()["id"]
    assert _apply(d, {"amount": raw})["amount"] == expected


def test_currency_from_llm(admin):
    d = admin.upload("Invoice Adobe total USD 14,27", "Adobe").json()["id"]
    assert _apply(d, {"amount": 14.27, "currency": "usd"})["currency"] == "USD"
    d2 = admin.upload("Brief ohne Betrag", "Brief").json()["id"]
    row = _apply(d2, {"amount": None, "currency": "Dollar"})
    assert row["amount"] is None and row["currency"] is None
    # Betrag verworfen → auch die Währung nicht
    d3 = admin.upload("Brief ohne Betrag", "Brief 2").json()["id"]
    assert _apply(d3, {"amount": 99.0, "currency": "EUR"})["currency"] is None


def test_salary_and_reminder_from_llm(admin):
    pay = admin.upload("Abrechnung der Brutto/Netto-Bezüge für Oktober 2026\nAuszahlungsbetrag 2.930,52 EUR",
                       "Gehalt").json()["id"]
    row = _apply(pay, {"kind": "Gehalt", "direction": "Ausgabe", "amount": "2.930,52", "currency": "EUR"})
    assert row["direction"] == "in" and row["amount"] == 2930.52
    dun = admin.upload("Zahlungserinnerung: offener Betrag 552,88 € inkl. 5,00 € Mahngebühr", "Mahnung").json()["id"]
    row = _apply(dun, {"kind": "Mahnung", "direction": "Ausgabe", "amount": 552.88})
    assert row["direction"] == "none" and row["amount"] == 552.88


# ---------- PATCH, GET, Sortieren ----------

def test_patch_stores_money_again(admin):
    d = admin.upload("Beleg", "Beleg").json()["id"]
    r = admin.patch(f"/documents/{d}", json={"amount": 99.5, "currency": "usd", "direction": "in"})
    assert r.status_code == 200
    doc = r.json()
    assert (doc["amount"], doc["currency"], doc["direction"]) == (99.5, "USD", "in")
    doc = admin.get(f"/documents/{d}").json()
    assert (doc["amount"], doc["currency"], doc["direction"]) == (99.5, "USD", "in")


@pytest.mark.parametrize("body", [{"direction": "rein"}, {"direction": "Ausgabe"}, {"direction": ""},
                                  {"currency": "Dollar"}, {"currency": "E"}])
def test_patch_rejects_bad_money_values(admin, body):
    d = admin.upload("Beleg", "Beleg").json()["id"]
    _set(d, amount=10.0, currency="EUR", direction="out")
    assert admin.patch(f"/documents/{d}", json=body).status_code == 400
    doc = admin.get(f"/documents/{d}").json()
    assert (doc["amount"], doc["currency"], doc["direction"]) == (10.0, "EUR", "out")


def test_old_clients_without_money_fields_keep_them(admin):
    """App-Fassungen ohne Geld (Build 19/20) schicken nur Titel/Datum/Tags – Betrag & Co. bleiben stehen."""
    d = admin.upload("Beleg", "Beleg").json()["id"]
    _set(d, amount=12.5, currency="EUR", direction="out", doc_kind="rechnung", recurring="monatlich", tax_35a=3.5)
    admin.patch(f"/documents/{d}", json={"title": "Neu", "document_date": "2025-01-02", "sender": "X", "tags": ["Haus"]})
    with connect() as con:
        row = con.execute("SELECT * FROM documents WHERE id=?", (d,)).fetchone()
    assert (row["amount"], row["currency"], row["direction"], row["doc_kind"], row["recurring"], row["tax_35a"]) == \
        (12.5, "EUR", "out", "rechnung", "monatlich", 3.5)
    # ausdrücklich null löscht den Betrag (Web: Feld geleert)
    admin.patch(f"/documents/{d}", json={"amount": None, "currency": None})
    doc = admin.get(f"/documents/{d}").json()
    assert doc["amount"] is None and doc["currency"] is None and doc["direction"] == "out"


def test_patch_money_marks_briefkasten_doc_as_seen(admin):
    d = admin.upload("Beleg", "Beleg").json()["id"]
    _set(d, ext_pending=1)
    admin.patch(f"/documents/{d}", json={"amount": 5})
    assert admin.get(f"/documents/{d}").json()["ext_pending"] == 0


def test_sort_by_amount(admin):
    ids = {}
    for title, amount in [("Klein", 9.99), ("Groß", 1200.0), ("Ohne", None), ("Mittel", 49.5)]:
        ids[title] = admin.upload(title, title).json()["id"]
        _set(ids[title], amount=amount)
    desc = [d["title"] for d in admin.get("/documents", params={"sort": "amount", "dir": "desc"}).json()]
    asc = [d["title"] for d in admin.get("/documents", params={"sort": "amount", "dir": "asc"}).json()]
    assert desc == ["Groß", "Mittel", "Klein", "Ohne"]          # ohne Betrag immer hinten
    assert asc == ["Klein", "Mittel", "Groß", "Ohne"]


def test_export_zip_metadata_has_amount(admin):
    d = admin.upload("x", "Beleg").json()["id"]
    _set(d, amount=12.34, currency="EUR")
    meta = json.loads(_zip(admin.get("/export.zip")).read("metadata.json"))
    assert meta[0]["amount"] == 12.34 and meta[0]["currency"] == "EUR"


# ---------- Nachtragen im Bestand ----------

def test_refill_fills_missing_amount_only_if_in_text(admin, monkeypatch):
    """Abgelegt, als es keine Geld-Felder gab: Betrag kommt dazu, wenn er im Text steht; Titel/Datum bleiben."""
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://llm:11434/v1"})
    d = admin.upload("Stadtwerke Abrechnung 2025\nNachzahlung 116,58 €", "Mein Titel").json()["id"]
    _set(d, status="tagging", llm_status="refill", document_date="2026-02-14")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (
        {"title": "LLM-Titel", "amount": "116,58", "currency": "EUR", "kind": "Abrechnung", "direction": "Ausgabe",
         "document_date": "2020-01-01"}, True))
    assert worker.llm_one(d) is True
    doc = admin.get(f"/documents/{d}").json()
    assert (doc["amount"], doc["currency"], doc["direction"], doc["doc_kind"]) == (116.58, "EUR", "out", "abrechnung")
    assert doc["title"] == "Mein Titel" and doc["document_date"] == "2026-02-14" and doc["llm_status"] == "done"
    # erfundener Betrag: nicht übernommen, Richtung/Rhythmus auch nicht (sie hängen am Betrag)
    d2 = admin.upload("Vertrag ohne Zahlen", "Vertrag").json()["id"]
    _set(d2, status="tagging", llm_status="refill")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (
        {"amount": 39.95, "kind": "Vertrag", "direction": "Ausgabe", "recurring": "monatlich"}, True))
    worker.llm_one(d2)
    doc = admin.get(f"/documents/{d2}").json()
    assert doc["amount"] is None and doc["recurring"] is None and doc["direction"] is None and doc["doc_kind"] == "vertrag"


def test_money_v2_marks_documents_without_money_for_refill(tmp_path, monkeypatch):
    """Einmalig beim Start: was in der Zeit ohne Geld erkannt wurde (keine Art, kein Betrag), wird nachgetragen."""
    from app import db
    path = tmp_path / "alt.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sqlite3.connect(path)
    con.execute("INSERT INTO users(username, display_name, password_hash) VALUES ('x','X','h')")
    ins = ("INSERT INTO documents(owner_id, title, text, status, llm_status, doc_kind, amount, deleted_at) "
           "VALUES (1, ?, ?, 'done', 'done', ?, ?, ?)")
    con.execute(ins, ("ohne Geld", "Rechnung 12,00 €", None, None, None))
    con.execute(ins, ("mit Art", "Brief", "info", None, None))
    con.execute(ins, ("mit Betrag", "Rechnung 5,00 €", None, 5.0, None))
    con.execute(ins, ("ohne Text", "", None, None, None))
    con.execute(ins, ("gelöscht", "Rechnung 1,00 €", None, None, "2026-11-16"))
    con.execute("DELETE FROM settings WHERE key='money_v2'")
    con.commit(); con.close()
    db.init_db()
    con = sqlite3.connect(path)
    marked = {r[0] for r in con.execute("SELECT title FROM documents WHERE llm_status='refill'")}
    assert marked == {"ohne Geld"}
    assert con.execute("SELECT value FROM settings WHERE key='money_v2'").fetchone()[0] == "1"
    con.execute("UPDATE documents SET llm_status='done', status='done'")
    con.commit(); con.close()
    db.init_db()                                                  # nur einmal
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE llm_status='refill'").fetchone()[0] == 0


# ---------- Steuer-Paket ----------

def test_tax_package_signs_and_flows(admin):
    _doc(admin, "Handwerker", "Becker", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Mahnung", "Becker", 552.88, "2025-10-20", doc_kind="mahnung")            # bewegt kein Geld
    _doc(admin, "Erstattung", "Finanzamt", 1338.0, "2025-07-22", direction="in")
    _doc(admin, "Lohn", "Kranich", 2900.0, "2025-01-31", doc_kind="gehalt")                # Art → Einnahme
    _doc(admin, "Altbeleg", "Bäcker", 12.5, "2025-03-01")                                  # ohne Richtung → Ausgabe
    _doc(admin, "Kaufvertrag", "Autohaus", 18000.0, "2025-05-02", direction="none")
    _doc(admin, "Brief", "Amt", None, "2025-06-01")
    _, sums, rows = _package(admin, year=2025)
    assert sums["documents"] == 7 and sums["repeats"] == 0
    assert sums["spent"] == 560.38 and sums["total"] == 560.38 and sums["received"] == 4238.0
    assert sums["by_sender"] == {"Becker": 547.88, "Bäcker": 12.5}
    assert sums["by_tag"] == {"(ohne Tag)": 560.38}
    by_title = {r[3]: r for r in rows[1:] if len(r) > 6 and r[0]}
    assert by_title["Handwerker"][4] == "-547,88" and by_title["Handwerker"][6] == "Ausgabe" and by_title["Handwerker"][7] == "324,87"
    assert by_title["Mahnung"][4] == "(552,88)" and by_title["Mahnung"][6] == "ohne Zahlung"
    assert by_title["Erstattung"][4] == "+1338,00" and by_title["Erstattung"][6] == "Einnahme"
    assert by_title["Lohn"][4] == "+2900,00"
    assert by_title["Brief"][4] == ""
    footer = [r for r in rows if len(r) > 4 and not r[0] and r[3]]
    assert ["", "", "", "Ausgaben", "-560,38", "EUR"] in footer and ["", "", "", "Einnahmen", "+4238,00", "EUR"] in footer
    assert ["", "", "", "davon § 35a (Arbeitskosten)", "324,87", "EUR"] in footer


def test_tax_package_other_currencies_separate(admin):
    _doc(admin, "Adobe", "Adobe", 14.27, "2025-01-08", currency="USD", direction="out")
    _doc(admin, "Adobe Refund", "Adobe", 4.27, "2025-02-08", currency="USD", direction="in")
    _doc(admin, "Hotel", "Hotel", 200.0, "2025-04-01", currency="CHF")
    _doc(admin, "Strom", "Stadtwerke", 80.0, "2025-02-01", direction="out")
    _, sums, rows = _package(admin, year=2025)
    assert sums["spent"] == 80.0 and sums["received"] == 0
    assert sums["other_currencies"] == {"USD": 10.0, "CHF": 200.0}       # Gutschrift abgezogen, nichts umgerechnet
    assert ["", "", "", "in USD (nicht umgerechnet)", "10,00", "USD"] in rows


def test_tax_package_second_scan_counted_once_and_35a(admin):
    _doc(admin, "Sanitär", "Becker", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Sanitär Scan", "BECKER", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Nebenkosten", "Verwaltung", 3080.5, "2025-06-03", direction="out", tax_35a=760.0)
    _doc(admin, "Gärtner", "Grün", 240.0, "2025-04-11", direction="out", tax_35a=180.0)
    _, sums, rows = _package(admin, year=2025)
    assert sums["documents"] == 4 and sums["repeats"] == 1
    assert sums["tax_35a"] == 1264.87 and sums["spent"] == 3868.38
    scan = next(r for r in rows if len(r) > 3 and r[3] == "Sanitär Scan")
    assert scan[4] == "(547,88)" and scan[6] == "Zweitscan (nicht gezählt)"


def test_tax_package_filters_year_tags_sender(admin):
    a = _doc(admin, "Spende A", "Verein", 50.0, "2025-05-01", direction="out")
    admin.patch(f"/documents/{a}", json={"tags": ["Steuer"]})
    b = _doc(admin, "Spende B", "Kirche", 30.0, "2025-06-01", direction="out")
    admin.patch(f"/documents/{b}", json={"tags": ["Steuer"]})
    _doc(admin, "Spende 2024", "Verein", 70.0, "2024-05-01", direction="out")
    _doc(admin, "Strom", "Stadtwerke", 80.0, "2025-02-01", direction="out")
    _, sums, _ = _package(admin, year=2025, tags="Steuer")
    assert sums["documents"] == 2 and sums["spent"] == 80.0 and sums["by_tag"] == {"Steuer": 80.0}
    _, sums, _ = _package(admin, year=2025, tags="steuer", sender="verein")
    assert sums["documents"] == 1 and sums["spent"] == 50.0
    _, sums, _ = _package(admin, sender="Verein")                       # alle Jahre
    assert sums["documents"] == 2 and sums["spent"] == 120.0
    assert admin.get("/export/package.zip", params={"year": 2025, "tags": "Gibtsnicht"}).status_code == 404


def test_undated_documents_in_no_year_and_no_contract(admin):
    """Ohne Dokumentdatum: im Steuer-Paket nur unter „alle Jahre“ (als „ohne Datum“), in Verträgen gar nicht –
    das Hochladedatum sagt nichts über den Beleg."""
    _doc(admin, "Datiert", "Becker", 100.0, "2025-03-01", direction="out")
    for i in range(3):
        _doc(admin, f"Undatiert {i}", "Netflix", 13.99, None, direction="out")
    year = date.today().year
    _, sums, _ = _package(admin, year=2025)
    assert sums["documents"] == 1
    assert admin.get("/export/package.zip", params={"year": year}).status_code == 404
    z, sums, rows = _package(admin)
    assert sums["documents"] == 4 and sums["spent"] == 141.97
    assert any("ohne Datum Netflix Undatiert" in n for n in z.namelist())
    assert next(r for r in rows if len(r) > 3 and r[3] == "Undatiert 0")[1] == ""
    assert _contracts(admin) == []


def test_tax_package_contents_and_title(admin):
    _doc(admin, "Handwerker", "Becker", 547.88, "2025-09-15", direction="out")
    z, sums, _ = _package(admin, year=2025, title="Steuer 2025")
    names = z.namelist()
    assert "Steuer 2025 - alle Dokumente.pdf" in names and "Steuer 2025 - Uebersicht.csv" in names
    assert any(n.startswith("Dokumente/") and "2025-09-15 Becker Handwerker" in n for n in names)
    assert z.read("Steuer 2025 - alle Dokumente.pdf").startswith(b"%PDF")
    assert sums["title"] == "Steuer 2025"


def test_tax_package_visibility_and_briefkasten(admin, sam):
    _doc(admin, "Alex Beleg", "Becker", 100.0, "2025-03-01", direction="out")
    pending = _doc(admin, "Eingeworfen", "Fremd", 999.0, "2025-03-02", direction="out", ext_pending=1)
    _doc(sam, "Sam Beleg", "Becker", 20.0, "2025-03-01", direction="out")
    _, sums, _ = _package(admin, year=2025, scope="mine")
    assert sums["documents"] == 1 and sums["spent"] == 100.0             # Briefkasten-Einwurf erst nach „Übernehmen“
    _, sums, _ = _package(sam, year=2025, scope="all")
    assert sums["documents"] == 1 and sums["spent"] == 20.0              # ohne Freigabe nichts von Alex
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    _, sums, _ = _package(sam, year=2025, scope="all")
    assert sums["documents"] == 2 and sums["spent"] == 120.0
    _set(pending, ext_pending=0)
    _, sums, _ = _package(admin, year=2025, scope="mine")
    assert sums["spent"] == 1099.0


# ---------- Verträge & Abos ----------

def _monthly(api, sender, amount, months, year=2026, day=4, **f):
    f = {"direction": "out", **f}
    return [_doc(api, f"{sender} {m}", sender, amount, f"{year}-{m:02d}-{day:02d}", **f) for m in months]


def _contracts(api, today=None, **params):
    if today is None:
        return api.get("/contracts", params=params).json()
    with connect() as con:
        return extras.contracts(con, [api.id], today)


def test_pattern_needs_three_regular_bills(admin):
    _monthly(admin, "Netflix", 13.99, [1, 2])
    assert _contracts(admin) == []
    _monthly(admin, "Netflix", 13.99, [3])
    c = _contracts(admin, today=date(2026, 4, 10))[0]
    assert c["sender"] == "Netflix" and c["interval"] == "monatlich" and c["source"] == "pattern" and c["count"] == 3
    assert c["amount"] == 13.99 and c["per_month"] == 13.99 and c["next_expected"] == "2026-04-03"
    assert c["overdue"] is False and c["first"] == "2026-01-04" and c["last"] == "2026-03-04"


def test_pattern_intervals_and_per_month(admin):
    for d in ("2025-01-15", "2025-04-15", "2025-07-15", "2025-10-15"):
        _doc(admin, "Abschlag", "Wasser", 55.08, d, direction="out")
    for y in (2023, 2024, 2025):
        _doc(admin, "Beitrag", "ADAC", 94.0, f"{y}-03-01", direction="out")
    by = {c["sender"]: c for c in _contracts(admin, today=date(2025, 11, 1))}
    assert by["Wasser"]["interval"] == "vierteljährlich" and by["Wasser"]["per_month"] == 18.36
    assert by["ADAC"]["interval"] == "jährlich" and by["ADAC"]["per_month"] == 7.83
    assert [c["sender"] for c in _contracts(admin, today=date(2025, 11, 1))] == ["Wasser", "ADAC"]   # nach €/Monat


def test_irregular_or_dissimilar_bills_are_no_contract(admin):
    for d in ("2026-01-03", "2026-01-20", "2026-05-11", "2026-05-12"):            # unregelmäßig
        _doc(admin, "Kauf", "Baumarkt", 40.0, d, direction="out")
    for d, a in (("2026-01-04", 10.0), ("2026-02-04", 250.0), ("2026-03-04", 900.0)):   # Beträge passen nicht
        _doc(admin, "Kauf", "Elektro", a, d, direction="out")
    assert _contracts(admin) == []


def test_pattern_overdue_when_bill_is_missing(admin):
    _monthly(admin, "Spotify", 10.99, [1, 2, 3, 4])
    c = _contracts(admin, today=date(2026, 7, 1))[0]
    assert c["next_expected"] == "2026-05-04" and c["overdue"] is True
    assert _contracts(admin, today=date(2026, 5, 6))[0]["overdue"] is False


def test_income_foreign_currency_and_pending_are_no_contracts(admin):
    _monthly(admin, "Kranich", 2900.0, [1, 2, 3, 4], doc_kind="gehalt", direction=None)   # Art → Einnahme
    for m in (1, 2, 3):
        _doc(admin, f"Miete {m}", "Mieter", 800.0, f"2026-{m:02d}-01", direction="in")
    _monthly(admin, "AWS", 5.0, [1, 2, 3], currency="USD")
    _monthly(admin, "Fremd", 9.0, [1, 2, 3], ext_pending=1)
    _doc(admin, "Arbeitsvertrag", "Kranich", 4200.0, "2023-03-15", doc_kind="vertrag", recurring="monatlich", direction="none")
    _doc(admin, "Mietvertrag", "Mieter", 800.0, "2024-01-01", doc_kind="vertrag", recurring="monatlich", direction="in")
    assert _contracts(admin) == []


def test_contract_ended_by_cancellation(admin):
    _monthly(admin, "FitPunkt", 24.99, [1, 2, 3, 4])
    assert [c["sender"] for c in _contracts(admin)] == ["FitPunkt"]
    _doc(admin, "Kündigungsbestätigung", "FitPunkt", None, "2026-05-02", doc_kind="kuendigung", direction="none")
    assert _contracts(admin) == []
    # neuer Vertrag nach der Kündigung zählt wieder
    _doc(admin, "Neuer Vertrag", "FitPunkt", 29.99, "2026-06-01", doc_kind="vertrag", direction="none", recurring="monatlich")
    _monthly(admin, "FitPunkt", 29.99, [7, 8, 9])
    c = _contracts(admin)[0]
    assert c["sender"] == "FitPunkt" and c["last"] == "2026-09-04"


def test_contract_from_single_document(admin):
    _doc(admin, "Rundfunkbeitrag", "Beitragsservice", 55.08, "2025-01-15", doc_kind="bescheid", recurring="vierteljährlich",
         direction="none")
    _doc(admin, "Kredit", "Bank", 388.72, "2025-05-17", doc_kind="vertrag", recurring="monatlich", direction="none",
         due_date="2026-12-31", due_note="Kündigungsfrist")
    _doc(admin, "Kaufvertrag Auto", "Autohaus", 18000.0, "2025-05-02", doc_kind="vertrag", direction="out")   # ohne Rhythmus
    by = {c["sender"]: c for c in _contracts(admin, today=date(2026, 11, 16))}
    assert set(by) == {"Beitragsservice", "Bank"}
    r = by["Beitragsservice"]
    assert r["source"] == "document" and r["interval"] == "vierteljährlich" and r["per_month"] == 18.36
    assert r["next_expected"] == "2027-01-15"
    k = by["Bank"]
    assert k["next_expected"] == "2026-11-17" and k["cancel_by"] == {"date": "2026-12-31", "note": "Kündigungsfrist", "document_id": k["documents"][0]}
    assert k["due"]["date"] == "2026-12-31"


def test_contract_amount_follows_later_bills_and_shows_contract_amount(admin):
    _doc(admin, "Mobilfunkvertrag", "Telekom", 39.95, "2025-01-10", doc_kind="vertrag", direction="none", recurring="monatlich")
    c = _contracts(admin)[0]
    assert c["amount"] == 39.95 and c["contract_amount"] is None and c["amount_from"] is None
    _doc(admin, "Rechnung Sep", "Telekom", 42.95, "2026-09-04", doc_kind="rechnung", direction="out")
    _doc(admin, "Rechnung Okt", "Telekom", 44.95, "2026-10-04", doc_kind="rechnung", direction="out")
    c = _contracts(admin)[0]
    assert c["amount"] == 44.95 and c["contract_amount"] == 39.95 and c["amount_from"] == "2026-10-04"


def test_contract_amount_is_the_current_price(admin):
    # Jahresbeitrag steigt jedes Jahr: der jüngste gilt, nicht der Median (war 655,80 statt 701,20)
    for y, a in ((2023, 612.40), (2024, 655.80), (2025, 701.20)):
        _doc(admin, f"Beitragsrechnung Kfz {y + 1}", "Hanse Kraft", a, f"{y}-11-18", direction="out")
    # monatlich teurer geworden und gehalten → neuer Preis; ein einzelner Aufschlag → bisheriger Preis
    for m, a in ((1, 40.0), (2, 40.0), (3, 40.0), (4, 40.0), (5, 44.0), (6, 44.0)):
        _doc(admin, f"Rechnung {m}", "Netzwerk", a, f"2026-{m:02d}-04", direction="out")
    for m, a in ((1, 20.0), (2, 20.0), (3, 20.0), (4, 31.5)):
        _doc(admin, f"Rechnung {m}", "Funkfix", a, f"2026-{m:02d}-06", direction="out")
    by = {c["sender"]: c for c in _contracts(admin, today=date(2026, 7, 1))}
    assert by["Hanse Kraft"]["amount"] == 701.20 and by["Hanse Kraft"]["interval"] == "jährlich"
    assert by["Netzwerk"]["amount"] == 44.0
    assert by["Funkfix"]["amount"] == 20.0


def test_cancellation_deadline_rolls_forward_for_yearly_bills(admin):
    for y in (2023, 2024, 2025):
        _doc(admin, f"Beitrag {y}", "Lindwurm", 650.0, f"{y}-11-18", direction="out",
             due_date=f"{y}-11-30", due_note="Kündigungsfrist", due_done=1)
    c = _contracts(admin, today=date(2026, 11, 16))[0]
    assert c["interval"] == "jährlich" and c["cancel_by"]["date"] == "2026-11-30"
    assert _contracts(admin, today=date(2026, 12, 1))[0]["cancel_by"]["date"] == "2027-11-30"


def test_contracts_respect_visibility(admin, sam):
    _monthly(admin, "Netflix", 13.99, [1, 2, 3])
    assert sam.get("/contracts", params={"scope": "all"}).json() == []
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert [c["sender"] for c in sam.get("/contracts", params={"scope": "all"}).json()] == ["Netflix"]
    assert sam.get("/contracts", params={"scope": "mine"}).json() == []


# ---------- Absender-Summen ----------

def test_sender_sums(admin, sam):
    _doc(admin, "Abrechnung 2024", "Stadtwerke", 116.58, "2025-02-14", direction="out")
    _doc(admin, "Abrechnung 2025", "stadtwerke", 76.60, "2026-02-12", direction="in")
    _doc(admin, "Abschlag", "Stadtwerke", 80.0, "2026-03-01")                              # ohne Richtung: Ausgabe
    _doc(admin, "Abschlag Scan", "Stadtwerke", 80.0, "2026-03-01")                         # Zweitscan: einmal
    _doc(admin, "Vertrag", "Stadtwerke", 80.0, "2025-01-01", doc_kind="vertrag")           # bewegt kein Geld
    _doc(admin, "Invoice", "Stadtwerke", 10.0, "2026-04-01", currency="USD", direction="out")
    _doc(admin, "Einwurf", "Stadtwerke", 500.0, "2026-04-02", direction="out", ext_pending=1)
    _doc(admin, "Lohn", "Kranich", 2900.0, "2026-01-31", doc_kind="gehalt")
    _doc(sam, "Sam", "Stadtwerke", 1000.0, "2026-01-01", direction="out")
    by = {s["name"]: s for s in admin.get("/senders", params={"scope": "mine"}).json()}
    s = by["Stadtwerke"]
    assert s["count"] == 6 and s["spent"] == 196.58 and s["total"] == 196.58 and s["received"] == 76.6
    assert by["Kranich"]["received"] == 2900.0 and by["Kranich"]["spent"] == 0
    assert s["last"].startswith("2026-04-01")


# ---------- Ordner-Export mit Beträgen ----------

def test_tag_export_csv_has_amount_columns_and_sums(admin):
    for title, amount, cur, day in [("Inspektion", 412.3, "EUR", "2024-05-02"), ("Maut", 9.5, "CHF", "2024-06-01"),
                                    ("HU", None, None, "2025-03-20")]:
        d = admin.upload(title, title, tags="Auto").json()["id"]
        _set(d, amount=amount, currency=cur, document_date=day, direction="out" if amount else None)
    z = _zip(admin.get("/export/tag.zip", params={"tag": "Auto"}))
    rows = list(csv.reader(io.StringIO(z.read("Auto/Inhaltsverzeichnis.csv").decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == ["Ordner", "Datum", "Titel", "Absender", "Betrag", "Währung", "Datei"]
    by = {r[2]: r for r in rows[1:]}
    assert by["Inspektion"][4:6] == ["412,30", "EUR"] and by["Maut"][4:6] == ["9,50", "CHF"] and by["HU"][4] == ""


# ---------- Frag Papigator ----------

def _ask_prompt(monkeypatch, admin, question, today=date(2026, 12, 30)):
    seen = {}

    def fake_chat(prompt, timeout=180, purpose="tag"):
        if "Suchbegriffe" in prompt:
            return '["Telekom", "Rechnung"]'
        seen["prompt"] = prompt
        return "Antwort [Dok 1]"
    monkeypatch.setattr(ask, "_chat", fake_chat)
    monkeypatch.setattr(ask.llm, "usable", lambda purpose: True)
    ask.ask(question, [admin.id], today=today)
    return seen.get("prompt", "")


def test_sums_per_sender_only_when_asked(admin, monkeypatch):
    for m in ("2025-03-04", "2025-04-04", "2026-03-04"):
        _doc(admin, f"Telekom Rechnung {m}", "Telekom Deutschland GmbH", 40.0, m, doc_kind="rechnung", direction="out")
    p = _ask_prompt(monkeypatch, admin, "Zeig mir die Rechnungen der Telekom")
    assert p and "Summen je Absender aus der Datenbank" not in p and "Rechne keine Summen aus" in p
    p = _ask_prompt(monkeypatch, admin, "Wie viel habe ich an die Telekom gezahlt?")
    assert "Summen je Absender aus der Datenbank" in p and "zusammen Ausgaben 120,00 €" in p


def test_sums_respect_question_period(admin, monkeypatch):
    for m in ("2025-03-04", "2025-04-04", "2026-03-04"):
        _doc(admin, f"Telekom Rechnung {m}", "Telekom Deutschland GmbH", 40.0, m, doc_kind="rechnung", direction="out")
    p = _ask_prompt(monkeypatch, admin, "Was habe ich letztes Jahr insgesamt an Telekom bezahlt?")
    assert "im gefragten Zeitraum" in p and "2025: 2 Dokumente, Ausgaben 80,00 €" in p and "2026:" not in p.split("Summen je Absender aus der Datenbank")[1].split("\n\n")[0]


def test_overview_lists_deadlines_with_amount_contracts_payslips_35a(admin):
    today = date(2026, 11, 13)
    _doc(admin, "Schornsteinfeger", "T. Ruß", 86.45, "2026-11-05", due_date="2026-11-19", due_note="Zahlungsziel", due_done=0)
    _doc(admin, "Beleg in Dollar", "Adobe", 14.27, "2026-11-06", currency="USD", due_date="2026-11-20", due_note="Zahlungsziel", due_done=0)
    _doc(admin, "Kita", "Stadt", 186.0, "2025-08-01", recurring="monatlich", doc_kind="bescheid", direction="out")
    _doc(admin, "Gehaltsabrechnung Okt", "Kranich", 2930.52, "2026-10-31", doc_kind="gehalt", direction="in")
    _doc(admin, "Gehaltsabrechnung Sep", "Kranich", 2900.10, "2026-09-30", doc_kind="gehalt", direction="in")
    _doc(admin, "Sanitär", "Becker", 547.88, "2025-09-15", tax_35a=324.87)
    _doc(admin, "Uralt", "Becker", 100.0, "2020-09-15", tax_35a=50.0)                    # älter als 3 Jahre
    text = ask.facts([admin.id], today)
    assert "19.11.2026: Zahlungsziel – Schornsteinfeger (T. Ruß), 86,45 €" in text
    assert "Beleg in Dollar (Adobe), 14,27 USD" in text
    assert "Stadt („Kita“): 186,00 € monatlich (≈ 186,00 € im Monat)" in text
    assert "Kranich, 31.10.2026 „Gehaltsabrechnung Okt“: 2.930,52 €" in text and "2.900,10" not in text
    assert "2025: Sanitär 324,87 € – zusammen 324,87 €" in text and "Uralt" not in text


def test_overview_without_money_has_no_money_sections(admin):
    _doc(admin, "Brief", "Amt", None, "2026-01-01")
    text = ask.facts([admin.id], date(2026, 11, 13))
    assert "Offene Fristen bis" in text
    assert "Verträge" not in text and "Gehalt" not in text and "§ 35a" not in text


def test_contract_question_is_answered_from_overview_without_hits(admin, monkeypatch):
    """„Welche Abos habe ich?“ findet per Volltext nichts – die Übersicht reicht trotzdem für eine Antwort."""
    _monthly(admin, "Netflix", 13.99, [1, 2, 3])
    p = _ask_prompt(monkeypatch, admin, "Welche Abos laufen gerade?", today=date(2026, 3, 20))
    assert "Laufende Verträge und regelmäßige Zahlungen" in p and "Netflix" in p


def test_check_sum_corrects_wrong_totals_but_keeps_database_sums():
    wrong = "- Januar: 39,95 € [Dok 1]\n- Februar: 39,95 € [Dok 2]\nSumme: 89,90 €"
    assert "Summe: 79,90 €" in ask.check_sum(wrong)
    right = "- Januar: 39,95 € [Dok 1]\n- Februar: 39,95 € [Dok 2]\nSumme: 79,90 €"
    assert ask.check_sum(right) == right
    db_sum = "- 04.03.2025: 39,95 € [Dok 1]\nSumme: 489,38 € [Übersicht]"
    assert ask.check_sum(db_sum) == db_sum


# ---------- Aufräumen nach Aufbewahrungsfristen ----------

def test_cleanup_respects_money_fields(admin):
    """Ordner sagen „Rechnungen: 3 Jahre“ – Verträge mit Rhythmus, Gehalt und § 35a-Belege bleiben trotzdem."""
    plain = _doc(admin, "Toaster", "Elektromarkt", 39.99, "2015-03-01", direction="out")
    contract = _doc(admin, "Kredit", "Bank", 388.72, "2015-03-01", doc_kind="rechnung", recurring="monatlich")
    pay = _doc(admin, "Lohn", "Kranich", 2900.0, "2015-03-01", doc_kind="gehalt")
    work = _doc(admin, "Maler", "Malerbetrieb", 240.0, "2015-03-01", direction="out", tax_35a=180.0)
    young_work = _doc(admin, "Maler neu", "Malerbetrieb", 240.0, "2020-03-01", direction="out", tax_35a=180.0)
    for d in (plain, contract, pay, work, young_work):
        admin.patch(f"/documents/{d}", json={"tags": ["Rechnungen"]})
    r = admin.get("/cleanup").json()
    by = {d["id"]: g for g in r["groups"] for d in g["documents"]}
    assert set(by) == {plain, work}
    assert by[plain]["label"] == "Rechnungen & Quittungen"
    assert by[work]["label"] == "Steuer" and by[work]["years"] == 10

