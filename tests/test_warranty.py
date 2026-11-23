"""Gewährleistung/Garantie: Kaufbelege über Waren bekommen 2 Jahre Gewährleistung, eine längere Garantie nur, wenn sie so
im Text steht; Korrekturen von Hand bleiben; Filter, Zähler, Aufräumen und Frag Papigator kennen das Ende."""
import datetime as dt

from app import processing
from app.db import connect

KAUF = "Rechnung Elektro Markt, Waschmaschine WM 500, 499,00 €, Rechnungsdatum 05.03.2025"


def _apply(doc_id, data):
    with connect() as con:
        processing.apply_llm(con, doc_id, data)
        return dict(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def _doc(admin, text, title="Beleg"):
    return admin.upload(text, title).json()["id"]


def test_warranty_end_clamps_month_end():
    assert processing.warranty_end("2024-02-29", 24) == "2026-02-28"
    assert processing.warranty_end("2026-01-31", 1) == "2026-02-28"
    assert processing.warranty_end("2025-03-05", 36) == "2028-03-05"
    assert processing.warranty_end(None, 24) is None and processing.warranty_end("2025-03-05", 0) is None


def test_garantie_only_when_stated_in_text():
    f = processing.garantie_in_text
    assert f(36, "Inklusive 3 Jahre Herstellergarantie.") and f(36, "Garantie: 36 Monate ab Kaufdatum")
    assert f(60, "fünf Jahre Garantie auf den Motor") and f(36, "Keine Rücknahme. 3 Jahre Garantie")
    assert not f(60, "bis zu 5 Jahre Garantie nach Registrierung")
    assert not f(60, "Garantieverlängerung auf 5 Jahre jetzt zubuchen")
    assert not f(36, "Inklusive 2 Jahre Garantie") and not f(36, "36 Monate Laufzeit")


def test_purchase_gets_legal_warranty_and_stated_garantie(admin):
    d = _doc(admin, KAUF)
    r = _apply(d, {"kind": "Rechnung", "direction": "Ausgabe", "document_date": "2025-03-05", "goods": True})
    assert (r["warranty_months"], r["warranty_kind"], r["warranty_until"]) == (24, "legal", "2027-03-05")
    # nur stehende Garantie zählt
    g = _doc(admin, KAUF + " Inklusive 3 Jahre Herstellergarantie.", "Mit Garantie")
    r = _apply(g, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": True, "warranty_years": 3})
    assert (r["warranty_months"], r["warranty_kind"], r["warranty_until"]) == (36, "garantie", "2028-03-05")
    o = _doc(admin, KAUF + " Bis zu 5 Jahre Garantie nach Registrierung.", "Angebot")
    r = _apply(o, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": True, "warranty_years": 5})
    assert (r["warranty_months"], r["warranty_kind"]) == (24, "legal")


def test_services_and_other_documents_get_none(admin):
    strom = _doc(admin, "Stromrechnung 2025, Rechnungsdatum 05.03.2025", "Strom")
    r = _apply(strom, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": False})
    assert (r["warranty_months"], r["warranty_until"]) == (0, None)
    gehalt = _doc(admin, "Gehaltsabrechnung März, 05.03.2025", "Gehalt")
    r = _apply(gehalt, {"kind": "Gehalt", "document_date": "2025-03-05", "goods": True})
    assert r["warranty_months"] is None
    erst = _doc(admin, "Gutschrift Rücksendung, 05.03.2025", "Erstattung")
    assert _apply(erst, {"kind": "Rechnung", "direction": "Einnahme", "goods": True})["warranty_months"] is None


def test_manual_correction_stays_and_save_all_keeps_it(admin):
    d = _doc(admin, KAUF)
    _apply(d, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": True})
    # Web speichert alles: unveränderter Wert ist keine Korrektur
    r = admin.patch(f"/documents/{d}", json={"title": "Waschmaschine", "warranty_months": 24, "warranty_kind": "legal"}).json()
    assert (r["warranty_months"], r["warranty_kind"]) == (24, "legal")
    r = admin.patch(f"/documents/{d}", json={"warranty_months": 60}).json()
    assert (r["warranty_months"], r["warranty_kind"], r["warranty_until"]) == (60, "garantie", "2030-03-05")
    r = admin.patch(f"/documents/{d}", json={"document_date": "2025-04-01"}).json()
    assert r["warranty_until"] == "2030-04-01"
    # erneutes Erkennen überschreibt die Korrektur nicht
    assert _apply(d, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": True})["warranty_months"] == 60
    r = admin.patch(f"/documents/{d}", json={"warranty_months": 0}).json()
    assert (r["warranty_months"], r["warranty_kind"], r["warranty_until"]) == (0, None, None)
    assert admin.patch(f"/documents/{d}", json={"warranty_months": 999}).status_code == 400
    assert admin.patch(f"/documents/{d}", json={"warranty_months": 24, "warranty_kind": "x"}).status_code == 400


def test_filter_count_cleanup_and_ask(admin):
    today = dt.date.today()
    running, old = _doc(admin, KAUF, "Läuft"), _doc(admin, KAUF, "Abgelaufen")
    with connect() as con:
        con.execute("UPDATE documents SET status='done', document_date=?, warranty_months=24, warranty_kind='legal', "
                    "warranty_until=? WHERE id=?", ((today - dt.timedelta(days=700)).isoformat(),
                                                    (today + dt.timedelta(days=30)).isoformat(), running))
        con.execute("UPDATE documents SET status='done', document_date='2020-01-01', warranty_months=24, "
                    "warranty_until='2022-01-01' WHERE id=?", (old,))
    ids = [x["id"] for x in admin.get("/documents", params={"warranty": True}).json()]
    assert ids == [running]
    assert admin.get("/documents/warranty_count").json() == {"count": 1, "soon": 1}
    from app import ask
    me = admin.get("/auth/me").json()["id"]
    assert "Gewährleistung" not in ask.facts([me], today)          # nur bei Fragen nach Garantie
    text = ask.facts([me], today, warranty=True)
    assert "Läuft" in text and "Gewährleistung läuft bis" in text and "Abgelaufen" not in text
    # Aufräumen: ein alter Beleg mit laufender Garantie (von Hand verlängert) wird nicht zum Löschen vorgeschlagen
    admin.patch(f"/documents/{running}", json={"tags": ["Rechnungen"]})
    admin.patch(f"/documents/{old}", json={"tags": ["Rechnungen"]})
    with connect() as con:
        con.execute("UPDATE documents SET document_date='2015-01-01' WHERE id IN (?, ?)", (running, old))
    found = {d["id"] for g in admin.get("/cleanup").json()["groups"] for d in g["documents"]}
    assert old in found and running not in found


def test_backfill_marks_only_purchase_invoices(tmp_path, monkeypatch):
    import sqlite3 as sq
    from app import db
    path = tmp_path / "alt.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sq.connect(path)
    con.execute("INSERT INTO users(username, display_name, password_hash) VALUES ('x','X','h')")
    for kind, direction in (("rechnung", "out"), ("rechnung", "in"), ("vertrag", "out")):
        con.execute("INSERT INTO documents(owner_id, title, text, status, llm_status, doc_kind, direction) "
                    "VALUES (1, 't', 'Text', 'done', 'done', ?, ?)", (kind, direction))
    con.execute("DELETE FROM settings WHERE key='warranty_v1'")
    con.commit(); con.close()
    db.init_db()
    con = sq.connect(path)
    rows = con.execute("SELECT doc_kind, direction, status, llm_status FROM documents ORDER BY id").fetchall()
    assert rows[0][2:] == ("tagging", "refill_w")
    assert rows[1][2:] == ("done", "done") and rows[2][2:] == ("done", "done")


def test_missing_goods_stays_open_and_end_follows_date(admin):
    d = _doc(admin, KAUF)
    r = _apply(d, {"kind": "Rechnung", "document_date": "2025-03-05"})        # Modell ohne „goods“
    assert r["warranty_months"] is None
    r = _apply(d, {"kind": "Rechnung", "document_date": "2025-03-05", "goods": True})
    assert r["warranty_until"] == "2027-03-05"
    # Neu erkennen mit anderem Datum (steht im Text): Ende zieht nach, die Dauer bleibt
    with connect() as con:
        con.execute("UPDATE documents SET text=? WHERE id=?", (KAUF + " Lieferung 20.03.2025", d))
    r = _apply(d, {"kind": "Rechnung", "document_date": "2025-03-20", "goods": False})
    assert (r["warranty_months"], r["warranty_until"]) == (24, "2027-03-20")
