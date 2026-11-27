"""Datum und Fristen: was die KI liefert, zählt nur, wenn es so im Text steht; Fristen-Regeln (Lastschrift, alte
Briefe, Auszahlung, relative Fristen); Prüf-Gründe und die Markierung „Datum prüfen“ im Bestand."""
from datetime import date, timedelta

from app import processing
from app.db import connect


def _apply(doc_id, data):
    with connect() as con:
        processing.apply_llm(con, doc_id, data)
        return dict(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def test_direct_debit_sets_no_payment_deadline(admin):
    soon_d = date.today() + timedelta(days=10)
    soon, de = soon_d.isoformat(), soon_d.strftime("%d.%m.%Y")
    d = admin.upload(f"Telekom Rechnung, Betrag wird am {de} abgebucht", "Telekom").json()["id"]
    row = _apply(d, {"autopay": True, "due_date": soon, "due_note": "Zahlungsziel"})
    assert row["due_date"] is None and row["autopay"] == 1
    # Eine Kündigungsfrist bleibt – auch bei Lastschrift ist da etwas zu tun
    d2 = admin.upload(f"Lindwurm Beitragsrechnung. Kündigung bis {de} möglich", "Lindwurm").json()["id"]
    row = _apply(d2, {"autopay": True, "due_date": soon, "due_note": "Kündigungsfrist"})
    assert row["due_date"] == soon and row["due_done"] == 0


def test_long_past_deadline_is_filed_as_done(admin):
    d = admin.upload("Bitte zahlen Sie bis 01.03.2024", "Alter Brief").json()["id"]
    row = _apply(d, {"due_date": "2024-03-01", "due_note": "Zahlungsziel"})
    assert row["due_date"] == "2024-03-01" and row["due_done"] == 1
    assert admin.get("/documents/due_count").json()["count"] == 0
    soon_d = date.today() + timedelta(days=5)
    soon = soon_d.isoformat()
    d2 = admin.upload(f"Bitte zahlen Sie bis {soon_d.strftime('%d.%m.%Y')}", "Neuer Brief").json()["id"]
    assert _apply(d2, {"due_date": soon, "due_note": "Zahlungsziel"})["due_done"] == 0


def test_dates_must_appear_in_the_text(admin):
    """Ein Datum, das nicht im Text steht, ist erfunden – lieber keins (dann „Zu prüfen“) als ein falsches."""
    text = "Aufhebungsvertrag\nMusterstadt, 14.05.2015\nDas Arbeitsverhältnis endet zum 31.08.2015. Formular Stand 03/2026"
    d = admin.upload(text, "Aufhebungsvertrag").json()["id"]
    assert _apply(d, {"document_date": "2026-03-01", "due_date": "2026-03-31", "due_note": "Kündigungsfrist"})["document_date"] is None
    row = _apply(d, {"document_date": "2015-05-14", "due_date": "2015-08-31", "due_note": "Vertragsende"})
    assert row["document_date"] == "2015-05-14" and row["due_date"] is None       # Vertragsende ist keine Frist
    doc = admin.get(f"/documents/{d}").json()
    assert "no_date" not in doc["review_reasons"]
    # „innerhalb von 14 Tagen“: Frist aus Rechnungsdatum + 14 Tage zählt, auch wenn sie so nicht im Text steht
    d2 = admin.upload("Rechnung vom 04.01.2025. Zahlbar innerhalb von 14 Tagen ohne Abzug.", "Rechnung").json()["id"]
    row = _apply(d2, {"document_date": "2025-01-04", "due_date": "2025-01-18", "due_note": "Zahlungsziel"})
    assert row["due_date"] == "2025-01-18"
    # Gehaltsabrechnung: Monatsangabe reicht fürs Datum, eine „Auszahlung“ ist keine Frist
    d3 = admin.upload("Abrechnung der Brutto/Netto-Bezüge für Januar 2025. Auszahlung am 31.01.2025", "Gehalt").json()["id"]
    row = _apply(d3, {"document_date": "2025-01-31", "due_date": "2025-01-31", "due_note": "Auszahlung"})
    assert row["document_date"] == "2025-01-31" and row["due_date"] is None
    # Ausweis: „gültig bis“ ist keine Frist, „bis … abholen“ schon
    txt = ("Bürgerbüro, 10.11.2026. Ihr Personalausweis (gültig bis 09.11.2036) ist eingetroffen. "
           "Bitte holen Sie ihn bis zum 15.01.2027 ab.")
    d4 = admin.upload(txt, "Ausweis").json()["id"]
    row = _apply(d4, {"document_date": "2026-11-10", "due_date": "2036-11-09", "due_note": "Ausweis gültig bis"})
    assert row["due_date"] is None
    row = _apply(d4, {"document_date": "2026-11-10", "due_date": "2027-01-15", "due_note": "Ausweis abholen"})
    assert row["due_date"] == "2027-01-15"
    # Zahlungswörter retten keine Auszahlung/Erstattung, wohl aber einen Mitgliedsbeitrag
    for note, keep in (("Überweisung der Erstattung", False), ("Zahlung Gehalt", False), ("Mitgliedsbeitrag zahlen", True)):
        dn = admin.upload(txt + " " + note, "Fall " + note).json()["id"]
        row = _apply(dn, {"document_date": "2026-11-10", "due_date": "2027-01-15", "due_note": note})
        assert (row["due_date"] == "2027-01-15") is keep, note


def test_review_reasons_explain_why(admin):
    d = admin.upload("ohne alles", "Brief").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='failed' WHERE id=?", (d,))
    assert admin.get(f"/documents/{d}").json()["review_reasons"] == ["llm_failed", "no_date", "no_tags"]
    admin.patch(f"/documents/{d}", json={"document_date": "2025-01-02", "tags": ["Haus"]})
    with connect() as con:
        con.execute("UPDATE documents SET llm_status='done' WHERE id=?", (d,))
    assert admin.get(f"/documents/{d}").json()["review_reasons"] == []


def test_existing_doubtful_dates_are_flagged_not_deleted(admin):
    from app import processing
    good = admin.upload("Brief vom 05.03.2018", "Gut").json()["id"]
    bad = admin.upload("Aufhebungsvertrag Musterstadt, 14.05.2015", "Falsch").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET document_date='2018-03-05', status='done' WHERE id=?", (good,))
        con.execute("UPDATE documents SET document_date='2026-03-01', status='done' WHERE id=?", (bad,))
        assert processing.flag_doubtful_dates(con) == 1
    doc = admin.get(f"/documents/{bad}").json()
    assert doc["document_date"] == "2026-03-01" and "date_check" in doc["review_reasons"]
    assert bad in {d["id"] for d in admin.get("/documents", params={"review": True}).json()}
    assert "date_check" not in admin.get(f"/documents/{good}").json()["review_reasons"]
    # Datum bestätigt oder korrigiert → Markierung weg
    admin.patch(f"/documents/{bad}", json={"document_date": "2015-05-14"})
    assert "date_check" not in admin.get(f"/documents/{bad}").json()["review_reasons"]


def test_date_check_migration_runs_on_startup_with_existing_documents(tmp_path, monkeypatch):
    """Der einmalige Lauf passiert in init_db – dort liefert die Verbindung Tupel statt Row-Objekten."""
    import sqlite3 as sq
    from app import db
    path = tmp_path / "alt.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sq.connect(path)
    con.execute("INSERT INTO users(username, display_name, password_hash) VALUES ('x','X','h')")
    con.execute("INSERT INTO documents(owner_id, title, document_date, text, status) "
                "VALUES (1, 't', '2026-03-01', 'Brief vom 14.05.2015', 'done')")
    con.execute("DELETE FROM settings WHERE key='date_check_v1'")
    con.commit(); con.close()
    db.init_db()
    con = sq.connect(path)
    assert con.execute("SELECT date_check FROM documents").fetchone()[0] == 1
    assert con.execute("SELECT value FROM settings WHERE key='date_check_v1'").fetchone()[0] == "1"


def test_date_formats_in_text():
    from app.processing import date_in_text as f
    assert f("2026-01-08", "Invoice Date Jan 8, 2026") and f("2026-01-08", "8 January 2026")
    assert f("2015-05-14", "Musterstadt, l4.O5.2O15") and f("2025-04-11", "11. April 2025")
    assert not f("2026-03-01", "Musterstadt, 14.05.2015 · Formular Stand 03/2026")
    assert not f("2026-01-09", "Jan 8, 2026")


def test_month_or_year_only_dates():
    """Zuwendungsbestätigung ohne Tagesdatum: „Bremen, Januar 2022“ und „Tag der Zuwendung: 2021“ zählen."""
    from app.processing import date_in_text as f
    text = "Bestätigung über Mitgliedsbeitrag ... Tag der Zuwendung: 2021 ... Bremen, Januar 2022 ... " \
           "Veranlagungszeitraum 2018"
    assert f("2022-01-01", text) and f("2022-01-15", text)
    assert f("2021-12-31", text) and f("2021-03-01", text)
    assert not f("2018-12-31", text) and not f("2022-02-01", text)
    assert not f("2026-03-01", "Formular Stand: März 2026") and not f("2026-03-01", "gültig ab März 2026")
    assert not f("2022-01-01", text, allow_month=False)
    assert f("2026-04-01", "ORT, DATUM REFERENZ Kassel, im April 2026 987/654321-Y-11")


def test_confirmed_no_date_leaves_review(admin):
    """„Hat kein Datum“: Datum leer, nicht mehr unter „Zu prüfen“; ein späteres Datum hebt das wieder auf."""
    d = admin.upload("Bedienungsanleitung", "Anleitung").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='done', document_date='2026-03-01', date_check=1 "
                    "WHERE id=?", (d,))
    admin.patch(f"/documents/{d}", json={"tags": ["Haus"]})
    r = admin.patch(f"/documents/{d}", json={"no_date": True}).json()
    assert r["no_date"] == 1 and r["document_date"] is None and r["review_reasons"] == []
    assert d not in {x["id"] for x in admin.get("/documents", params={"review": True}).json()}
    assert admin.get("/documents/review_count").json()["count"] == 0
    # Web speichert alles: leeres Datum mitgeschickt → bleibt „kein Datum“
    assert admin.patch(f"/documents/{d}", json={"document_date": "", "title": "Anleitung"}).json()["no_date"] == 1
    r = admin.patch(f"/documents/{d}", json={"document_date": "2026-02-03"}).json()
    assert r["no_date"] == 0 and r["document_date"] == "2026-02-03"
    r = admin.patch(f"/documents/{d}", json={"document_date": ""}).json()
    assert r["review_reasons"] == ["no_date"]
    # Nach „Neu erkennen“ findet die KI doch ein Datum → Markierung weg (ohne Neu erkennen bleibt die Handeingabe,
    # seit 27.11.2026 überschreibt die KI von Hand Geändertes nicht mehr)
    admin.patch(f"/documents/{d}", json={"no_date": True})
    with connect() as con:
        con.execute("UPDATE documents SET text='Brief vom 05.03.2018' WHERE id=?", (d,))
    assert _apply(d, {"document_date": "2018-03-05"})["no_date"] == 1
    with connect() as con:
        con.execute("UPDATE documents SET hand_fields=NULL WHERE id=?", (d,))   # wie „Neu erkennen“
    assert _apply(d, {"document_date": "2018-03-05"})["no_date"] == 0
