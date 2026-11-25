"""Dokumentart allgemein, Empfänger, Nummern (26.11.2026): Erkennen nur, was im Text steht; Geld-Arten unverändert;
von Hand ändern und filtern; Bestand still nachtragen."""
from app import processing
from app.db import connect

TEXT = """Grundschule am Lindenplatz · Karlsruhe
Halbjahresinformation Schuljahr 2025/2026
Name: Mia Muster   Klasse: 2b   Schülernummer S-2231
Karlsruhe, den 30.01.2026"""


def _doc(admin, title="Zeugnis", text=TEXT):
    return admin.upload(text, title).json()["id"]


def test_kind_recipient_refs_only_from_text(admin):
    i = _doc(admin)
    with connect() as con:
        processing.apply_llm(con, i, {"kind": "Zeugnis", "recipient": "Mia Muster", "document_date": "2026-01-30",
                                      "references": {"other": "S-2231", "contract": "V-999999", "case": "abc"}})
    d = admin.get(f"/documents/{i}").json()
    assert d["doc_kind"] == "zeugnis" and d["recipient"] == "Mia Muster"
    assert d["refs"] == {"other": "S-2231"}            # erfundene Nummer und eine ohne Ziffern fallen weg
    with connect() as con:
        processing.apply_llm(con, i, {"kind": "Halbjahresding", "recipient": "Ben Erfunden"})
    d = admin.get(f"/documents/{i}").json()
    assert d["doc_kind"] == "zeugnis" and d["recipient"] == "Mia Muster"   # Unbekanntes/Erfundenes ändert nichts


def test_old_names_map_to_codes_and_statement_is_no_expense(admin):
    assert processing.KINDS["lohnabrechnung"] == "gehalt" and processing.KINDS["spendenquittung"] == "spende"
    assert processing.KINDS["mitteilung"] == "info" and processing.KINDS["versicherungsschein"] == "police"
    out = processing.money_fields({"kind": "Kontoauszug", "direction": "Ausgabe", "recurring": "monatlich"})
    assert out["doc_kind"] == "kontoauszug" and out["direction"] == "none" and "recurring" not in out
    pol = processing.money_fields({"kind": "Versicherungsschein", "direction": "Ausgabe", "recurring": "jährlich"})
    assert pol["doc_kind"] == "police" and pol["direction"] == "none"     # wie ein Vertrag: gezahlt wird per Abbuchung
    from app import extras
    assert extras.flow({"direction": None, "doc_kind": "angebot"}) == "none"


def test_edit_filter_and_kinds_list(admin, sam):
    i, j = _doc(admin), _doc(admin, "Rechnung", "Rechnung Nr. 12345 über 20,00 €")
    kinds = admin.get("/documents/kinds").json()
    assert {"code": "zeugnis", "label": "Zeugnis", "count": 0} in kinds and admin.get("/documents/kinds?lang=en").json()[0]["label"] == "Invoice"
    assert admin.patch(f"/documents/{i}", json={"doc_kind": "quatsch"}).status_code == 400
    r = admin.patch(f"/documents/{i}", json={"doc_kind": "urkunde", "recipient": "  Mia   Muster ",
                                             "refs": {"case": " G 1234/2018 ", "invoice": "", "bogus": "x"}})
    assert r.status_code == 200
    d = admin.get(f"/documents/{i}").json()
    assert d["doc_kind"] == "urkunde" and d["recipient"] == "Mia Muster" and d["refs"] == {"case": "G 1234/2018"}
    admin.patch(f"/documents/{j}", json={"doc_kind": "rechnung"})
    assert [x["id"] for x in admin.get("/documents?kind=urkunde").json()] == [i]
    assert {x["id"] for x in admin.get("/documents?kind=urkunde,rechnung").json()} == {i, j}
    assert admin.get("/documents?kind=gibtsnicht").json() == []
    assert sam.patch(f"/documents/{i}", json={"doc_kind": "zeugnis"}).status_code == 404   # fremdes Dokument
    admin.patch(f"/documents/{i}", json={"doc_kind": "", "recipient": "", "refs": {}})
    d = admin.get(f"/documents/{i}").json()
    assert d["doc_kind"] is None and d["recipient"] is None and d["refs"] == {}


def test_backfill_is_silent_and_keeps_money_kinds(admin, monkeypatch):
    from app import worker
    from app.db import init_db
    monkeypatch.setattr(worker, "ai_usable", lambda: True)
    i, j = _doc(admin), _doc(admin, "Rechnung", "Rechnung Nr. 12345 an Mia Muster über 20,00 €")
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='done', doc_kind=NULL WHERE id=?", (i,))
        con.execute("UPDATE documents SET status='done', llm_status='done', doc_kind='rechnung' WHERE id=?", (j,))
        con.execute("DELETE FROM settings WHERE key='kinds_v1'")
    init_db()
    with connect() as con:
        rows = {r["id"]: r for r in con.execute("SELECT id, status, llm_status FROM documents WHERE id IN (?, ?)", (i, j))}
        assert all(r["status"] == "done" and r["llm_status"] == "refill_k" for r in rows.values())   # kein „Tagging“
    h = admin.get("/health").json()
    assert h["backfill"] == 2 and h["backfill_total"] >= 2                                      # Stand für App und Web
    with connect() as con:
        processing.apply_kind(con, j, {"kind": "Bestätigung", "recipient": "Mia Muster", "title": "anders"})
        r = con.execute("SELECT doc_kind, recipient, title FROM documents WHERE id=?", (j,)).fetchone()
    assert r["doc_kind"] == "rechnung" and r["recipient"] == "Mia Muster" and r["title"] == "Rechnung"
    # Zu prüfen bleibt sichtbar (Status „done“), nichts erscheint als „in Arbeit“
    assert "tagging" not in admin.get("/health").json()["documents"]


def test_idle_backfill_selection_and_give_up(admin, monkeypatch):
    """Nachtragen nur im Leerlauf und nur mit nutzbarer KI; ein Fehler dabei führt nicht zur Endlosschleife;
    ohne KI meldet /health das Nachtragen als „wartet“ statt „KI arbeitet“."""
    from app import worker
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='refill_k' WHERE id=?", (i,))
    monkeypatch.setattr(worker, "ai_usable", lambda: False)
    with connect() as con:
        assert worker.next_llm_doc(con) is None                       # keine KI: nichts anfassen
    h = admin.get("/health").json()
    assert h["backfill"] == 0 and h["backfill_waiting"] == 1
    monkeypatch.setattr(worker, "ai_usable", lambda: True)
    with connect() as con:
        assert worker.next_llm_doc(con)["id"] == i
    assert admin.get("/health").json()["backfill"] == 1
    worker.give_up(i)                                                  # unerwarteter Fehler beim Nachtragen
    with connect() as con:
        assert worker.next_llm_doc(con) is None                       # nicht sofort wieder dasselbe
        r = con.execute("SELECT status, llm_status FROM documents WHERE id=?", (i,)).fetchone()
    assert (r["status"], r["llm_status"]) == ("done", "done")
