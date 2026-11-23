"""Geld: Art und Richtung von Beträgen, Fristen bei Lastschrift, Summen mit Vorzeichen, Verträge, Übersicht für Fragen.
Szenarien aus dem Demo-Haushalt (demo/), bei denen die erste Fassung ein falsches Bild gab."""
import io
import json
import sqlite3
import zipfile
from datetime import date, timedelta

import pytest

from app import config, extras, processing, worker
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


def test_money_fields_from_llm():
    f = processing.money_fields
    assert f({"kind": "Gehalt", "direction": "Ausgabe"})["direction"] == "in"            # Gehalt ist nie Ausgabe
    assert f({"kind": "Mahnung", "direction": "Ausgabe"})["direction"] == "none"         # die Rechnung zählt schon
    assert f({"kind": "Vertrag", "direction": "Ausgabe"})["direction"] == "out"          # Kaufvertrag: echte Ausgabe
    assert f({"kind": "Vertrag", "direction": "Ausgabe", "recurring": "monatlich"})["direction"] == "none"   # Rate
    assert f({"kind": "Kündigung"})["doc_kind"] == "kuendigung"
    assert f({"kind": "Rechnung", "direction": "Ausgabe", "autopay": True, "recurring": "Monatlich", "tax_35a": "324,87 €"}) == \
        {"doc_kind": "rechnung", "direction": "out", "autopay": 1, "recurring": "monatlich", "tax_35a": 324.87}
    assert f({"kind": "Quatsch", "direction": "vielleicht", "autopay": "ja", "recurring": "täglich"}) == {}


def test_refill_only_adds_money_fields(admin, monkeypatch):
    """Altbestand: Nachtragen schreibt nur Art/Richtung/…; korrigierter Titel und Betrag bleiben, Lastschrift-Frist wird erledigt."""
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://llm:11434/v1"})
    d = _doc(admin, "Mein Titel", "Telekom", 39.95, "2026-01-04", due_date="2026-01-14", due_note="Zahlungsziel", due_done=0)
    _set(d, status="tagging", llm_status="refill")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (
        {"title": "LLM-Titel", "amount": 39.95, "sender": "Anders", "kind": "Rechnung", "direction": "Ausgabe", "autopay": True}, True))
    assert worker.llm_one(d) is True
    row = admin.get(f"/documents/{d}").json()
    assert row["title"] == "Mein Titel" and row["amount"] == 39.95 and row["sender"] == "Telekom"
    assert row["doc_kind"] == "rechnung" and row["direction"] == "out" and row["due_done"] == 1
    assert row["status"] == "done" and row["llm_status"] == "done"
    # Eine Frist, die beim Ablegen schon vorbei war, wird beim Nachtragen erledigt; eine spätere nicht
    old_doc = _doc(admin, "Versicherung alt", "Lindwurm", 612.40, "2023-11-18", due_date="2023-11-30", due_note="Kündigungsfrist", due_done=0)
    _set(old_doc, status="tagging", llm_status="refill")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: ({"amount": 612.40, "kind": "Rechnung"}, True))
    worker.llm_one(old_doc)
    assert admin.get(f"/documents/{old_doc}").json()["due_done"] == 1
    # Anderer Betrag als gespeichert (Guthaben statt Gesamtkosten): Richtung und Rhythmus bleiben offen
    d2 = _doc(admin, "Nebenkosten", "Verwaltung", 3080.50, "2025-06-03")
    _set(d2, status="tagging", llm_status="refill")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (
        {"amount": 159.50, "kind": "Abrechnung", "direction": "Einnahme", "recurring": "monatlich"}, True))
    worker.llm_one(d2)
    row2 = admin.get(f"/documents/{d2}").json()
    assert row2["doc_kind"] == "abrechnung" and row2["direction"] is None and row2["recurring"] is None
    # KI nicht erreichbar: bleibt zum Nachtragen vorgemerkt statt „fehlgeschlagen“ (das würde später alles neu erkennen)
    _set(d, status="tagging", llm_status="refill")
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (None, False))
    assert worker.llm_one(d) is False
    assert admin.get(f"/documents/{d}").json()["llm_status"] == "refill"


def test_senders_split_spent_and_received(admin):
    _doc(admin, "Abrechnung 2024", "Stadtwerke", 116.58, "2025-02-14", direction="out")
    _doc(admin, "Abrechnung 2025", "Stadtwerke", 76.60, "2026-02-12", direction="in")
    _doc(admin, "Rechnung", "Becker", 547.88, "2025-09-15", doc_kind="rechnung", direction="out")
    _doc(admin, "Mahnung", "Becker", 552.88, "2025-10-20", doc_kind="mahnung", direction="none")
    _doc(admin, "Invoice", "Adobe", 14.27, "2026-01-08", currency="USD", direction="out")
    by = {s["name"]: s for s in admin.get("/senders").json()}
    assert by["Stadtwerke"]["spent"] == 116.58 and by["Stadtwerke"]["received"] == 76.60
    assert by["Becker"]["total"] == 547.88                    # Mahnung nicht doppelt
    assert by["Adobe"]["spent"] == 0                          # Dollar nicht in Euro-Summen


def test_tax_package_sums_have_a_sign(admin):
    _doc(admin, "Bescheid", "Finanzamt", 1338.0, "2025-07-22", direction="in", doc_kind="bescheid")
    _doc(admin, "Handwerker", "Becker", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Adobe", "Adobe", 14.27, "2025-01-08", direction="out", currency="USD")
    r = admin.get("/export/package.zip", params={"year": 2025})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    sums = json.loads(z.read("summen.json"))
    assert sums["spent"] == 547.88 and sums["received"] == 1338.0 and sums["tax_35a"] == 324.87
    assert sums["other_currencies"] == {"USD": 14.27}
    csv_text = next(z.read(n) for n in z.namelist() if n.endswith(".csv")).decode("utf-8-sig")
    assert "+1338,00" in csv_text and "-547,88" in csv_text and "324,87" in csv_text


def test_contracts_without_income_and_duplicates(admin):
    for m in range(1, 6):   # Gehalt: regelmäßig, aber eine Einnahme
        _doc(admin, f"Gehalt {m}", "Kranich", 2844.87, f"2025-{m:02d}-25", doc_kind="gehalt", direction="in")
    # Rechnung, zweiter Scan derselben Rechnung, Mahnung: kein „monatlicher Vertrag“
    _doc(admin, "Rechnung", "Becker", 547.88, "2025-09-15", direction="out")
    _doc(admin, "Scan", "Becker", 547.88, "2025-09-16", direction="out")
    _doc(admin, "Mahnung", "Becker", 552.88, "2025-10-20", doc_kind="mahnung", direction="none")
    for m in range(1, 5):
        _doc(admin, f"Telekom {m}", "Telekom", 39.95, f"2026-{m:02d}-04", direction="out", autopay=1)
    _doc(admin, "Telekom mit Roaming", "Telekom", 47.94, "2026-05-04", direction="out", autopay=1)
    cs = admin.get("/contracts").json()
    assert [c["sender"] for c in cs] == ["Telekom"]
    assert cs[0]["last"] == "2026-05-04"                     # Monat mit Aufschlag zählt mit


def test_contract_from_single_document_and_cancellation(admin):
    _doc(admin, "Kita-Bescheid", "Stadt", 186.0, "2025-08-01", doc_kind="bescheid", direction="out", recurring="monatlich")
    _doc(admin, "Darlehen", "Avero Bank", 388.72, "2024-05-17", doc_kind="vertrag", direction="none", recurring="monatlich")
    _doc(admin, "FitPunkt Vertrag", "FitPunkt", 24.99, "2024-03-01", doc_kind="vertrag", direction="none", recurring="monatlich")
    _doc(admin, "FitPunkt Kündigung", "FitPunkt", None, "2025-10-02", doc_kind="kuendigung", direction="none")
    # Eine Jahresabrechnung nennt den Abschlag, ist aber selbst kein Vertrag
    _doc(admin, "Stromabrechnung", "Stadtwerke", 116.58, "2025-02-14", doc_kind="abrechnung", direction="out", recurring="monatlich")
    by = {c["sender"]: c for c in admin.get("/contracts").json()}
    assert set(by) == {"Stadt", "Avero Bank"}                       # FitPunkt ist gekündigt
    assert by["Stadt"]["source"] == "document" and by["Stadt"]["per_month"] == 186.0
    assert by["Avero Bank"]["next_expected"] > date.today().isoformat()


def test_yearly_cancellation_deadline_rolls_forward(admin):
    """Kfz-Versicherung: „bis 30.11. kündbar“ steht in der Beitragsrechnung vom Vorjahr – die nächste Frist ist dieses Jahr."""
    today = date.today()
    for y in (today.year - 3, today.year - 2, today.year - 1):
        _doc(admin, f"Beitrag {y + 1}", "Lindwurm", 650.0, f"{y}-11-18", direction="out", recurring="jährlich",
             due_date=f"{y}-11-30", due_note="Kündigungsfrist", due_done=1)
    with connect() as con:
        c = next(x for x in extras.contracts(con, [admin.id], today) if x["sender"] == "Lindwurm")
    expected = date(today.year, 11, 30) if date(today.year, 11, 30) >= today else date(today.year + 1, 11, 30)
    assert c["cancel_by"]["date"] == expected.isoformat()
    # Ein Sonderkündigungsrecht wird nicht fortgeschrieben
    for m in (1, 2, 3):
        _doc(admin, f"Telekom {m}", "Telekom", 39.95, f"{today.year - 1}-{m:02d}-04", direction="out")
    _doc(admin, "Preisanpassung", "Telekom", None, f"{today.year - 1}-02-20", due_date=f"{today.year - 1}-03-31",
         due_note="Sonderkündigungsrecht", doc_kind="info", direction="none")
    with connect() as con:
        t = next(x for x in extras.contracts(con, [admin.id], today) if x["sender"] == "Telekom")
    assert t["cancel_by"] is None


def test_ask_overview_lists_deadlines_and_contracts(admin):
    from app import ask
    today = date(2026, 11, 13)
    _doc(admin, "Schornsteinfeger", "T. Ruß", 86.45, "2026-11-05", due_date="2026-11-19", due_note="Zahlungsziel", due_done=0)
    _doc(admin, "Kita", "Stadt", 186.0, "2025-08-01", recurring="monatlich", doc_kind="bescheid", direction="out")
    text = ask.facts([admin.id], today)
    assert "19.11.2026" in text and "86,45 €" in text and "Schornsteinfeger" in text
    assert "Stadt („Kita“): 186,00 € monatlich" in text
    _doc(admin, "Sanitär", "Becker", 547.88, "2025-09-15", tax_35a=324.87)
    _doc(admin, "Nebenkosten 2024", "Verwaltung", 3080.5, "2025-06-03", tax_35a=760.0)
    _doc(admin, "Sanitär Scan", "Becker", 547.88, "2025-09-15", tax_35a=324.87)   # zweiter Scan zählt nicht doppelt
    assert "2025: Nebenkosten 2024 760,00 €; Sanitär 324,87 € – zusammen 1.084,87 €" in ask.facts([admin.id], today)


def test_direction_can_be_corrected(admin):
    d = admin.upload("x", "Guthaben").json()["id"]
    assert admin.patch(f"/documents/{d}", json={"direction": "in"}).json()["direction"] == "in"
    assert admin.patch(f"/documents/{d}", json={"direction": "rein"}).status_code == 400


def test_employment_contract_is_no_cost_and_payslips_win(admin):
    """Arbeitsvertrag mit Gehaltsangabe: kein Vertrag, der etwas kostet – und die Lohnzettel gelten, nicht der Vertrag."""
    from app import ask
    today = date(2026, 11, 13)
    # beim Nachtragen als „monatlich“ erkannt (Altbestand)
    _doc(admin, "Arbeitsvertrag Kranich", "Kranich Logistik", 4200.0, "2023-03-15", doc_kind="vertrag", direction="none",
         recurring="monatlich")
    for m, amt in [("2026-09-30", 2900.10), ("2026-10-31", 2930.52)]:
        _doc(admin, f"Gehaltsabrechnung {m[:7]}", "Kranich Logistik", amt, m, doc_kind="gehalt", direction="in")
    # ohne Lohnzettel im Archiv hilft der Titel
    _doc(admin, "Anstellungsvertrag Praxis", "Praxis Dr. Weiß", 1800.0, "2024-01-01", doc_kind="vertrag", direction="none",
         recurring="monatlich")
    assert admin.get("/contracts").json() == []
    text = ask.facts([admin.id], today)
    assert "Kranich Logistik, 31.10.2026 „Gehaltsabrechnung 2026-10“: 2.930,52 €" in text
    assert "4.200" not in text
    # Neu erkannt: Einnahme-Vertrag bekommt keinen Rhythmus
    f = processing.money_fields({"kind": "Vertrag", "direction": "Einnahme", "recurring": "monatlich", "title": "Arbeitsvertrag"})
    assert f["direction"] == "none" and "recurring" not in f
    f = processing.money_fields({"kind": "Vertrag", "direction": "keine", "recurring": "monatlich", "title": "Arbeitsvertrag Kranich"})
    assert "recurring" not in f


def test_contract_amount_follows_later_bills(admin):
    """Im Vertrag 39,95 €, seit der Preiserhöhung 42,95 €: der Vertrag zeigt den aktuellen Betrag."""
    from app import ask
    _doc(admin, "Mobilfunkvertrag", "Telekom", 39.95, "2025-01-10", doc_kind="vertrag", direction="none", recurring="monatlich")
    _doc(admin, "Rechnung Okt", "Telekom", 42.95, "2026-10-04", doc_kind="rechnung", direction="out")
    c = admin.get("/contracts").json()[0]
    assert c["amount"] == 42.95 and c["contract_amount"] == 39.95 and c["amount_from"] == "2026-10-04"
    assert "laut Rechnung vom 04.10.2026 – im Vertrag standen noch 39,95 €" in ask.facts([admin.id], date(2026, 11, 13))


def test_question_sees_newest_of_a_series_and_sums_per_sender(admin, monkeypatch):
    from app import ask
    for i in range(1, 13):
        _doc(admin, f"Gehaltsabrechnung {i:02d}/2026", "Kranich", 2900.0 + i, f"2026-{i:02d}-28", doc_kind="gehalt", direction="in")
    for m in ("2025-03-04", "2025-04-04", "2026-03-04"):
        _doc(admin, f"Telekom Rechnung {m}", "Telekom Deutschland GmbH", 40.0, m, doc_kind="rechnung", direction="out")
    _doc(admin, "Telekom Rechnung Scan", "Telekom Deutschland GmbH", 40.0, "2026-03-04", doc_kind="rechnung", direction="out")
    _doc(admin, "Seltene Beleg-Quittung", "Amazon", 12.0, "2026-05-01")
    seen = {}

    def fake_chat(prompt, timeout=180, purpose="tag"):
        if "Suchbegriffe" in prompt:
            return '["Telekom", "Rechnung", "Beleg"]' if "Telekom" in prompt else '["Gehaltsabrechnung", "Gehalt", "Beleg"]'
        seen["prompt"] = prompt
        return "Antwort"
    monkeypatch.setattr(ask, "_chat", fake_chat)
    monkeypatch.setattr(ask.llm, "usable", lambda purpose: True)
    r = ask.ask("Wie viel verdiene ich?", [admin.id], today=date(2026, 12, 30))
    pay = [s["title"] for s in r["sources"] if s["title"].startswith("Gehalt")]
    assert pay and set(pay) <= {"Gehaltsabrechnung 10/2026", "Gehaltsabrechnung 11/2026", "Gehaltsabrechnung 12/2026"}
    # Verlauf: über die Zeit verteilt, ältester und jüngster dabei
    r = ask.ask("Wie hat sich mein Gehalt entwickelt?", [admin.id], today=date(2026, 12, 30))
    pay = [s["title"] for s in r["sources"] if s["title"].startswith("Gehalt")]
    assert "Gehaltsabrechnung 01/2026" in pay and "Gehaltsabrechnung 12/2026" in pay
    # Genannte Firma: nur ihre Belege, Summen je Jahr aus der Datenbank (Doppelscan einmal)
    r = ask.ask("Was habe ich insgesamt an Telekom bezahlt?", [admin.id], today=date(2026, 12, 30))
    assert r["sources"] and all("Telekom" in s["title"] for s in r["sources"])
    assert ("- Telekom Deutschland GmbH: 2025: 2 Dokumente, Ausgaben 80,00 €; 2026: 1 Dokument, Ausgaben 40,00 € "
            "– zusammen Ausgaben 120,00 €") in seen["prompt"]


def test_database_sum_is_not_recalculated():
    from app import ask
    answer = "- 04.03.2025: 39,95 € [Dok 1]\n- 04.05.2025: 39,95 € [Dok 2]\nSumme: 489,38 € [Übersicht]"
    assert ask.check_sum(answer) == answer


def test_second_scan_counts_once(admin):
    """Derselbe Beleg zweimal gescannt: im Steuer-Paket und bei Absendern einmal gezählt –
    sonst verdoppeln sich die Handwerkerkosten nach § 35a."""
    _doc(admin, "Rechnung", "Becker", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Rechnung Scan 2", "becker", 547.88, "2025-09-15", direction="out", tax_35a=324.87)
    _doc(admin, "Andere Rechnung", "Becker", 547.88, "2025-11-03", direction="out")   # anderer Tag: zählt
    z = zipfile.ZipFile(io.BytesIO(admin.get("/export/package.zip", params={"year": 2025}).content))
    sums = json.loads(z.read("summen.json"))
    assert sums["documents"] == 3 and sums["repeats"] == 1
    assert sums["spent"] == 1095.76 and sums["tax_35a"] == 324.87
    csv_text = next(z.read(n) for n in z.namelist() if n.endswith(".csv")).decode("utf-8-sig")
    assert "Zweitscan (nicht gezählt)" in csv_text
    becker = next(s for s in admin.get("/senders").json() if s["name"].lower() == "becker")
    assert becker["count"] == 3 and becker["spent"] == 1095.76
