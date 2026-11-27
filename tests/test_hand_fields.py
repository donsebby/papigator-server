"""Von Hand geänderte Felder überschreibt die KI nicht mehr (27.11.2026): weder das Nachholen fehlgeschlagener
Erkennung noch ein Ergebnis, das während der Bearbeitung eintrifft. „Neu erkennen“ darf es ausdrücklich."""
from app import processing
from app.db import connect

TEXT = "Stadtwerke Musterstadt\nRechnungsdatum 14.05.2025\nBetrag 40,00 €"
AI = {"title": "Stromrechnung Mai", "sender": "Stadtwerke Musterstadt", "document_date": "2025-05-14", "amount": 40.0,
      "summary": "Abschlag", "tags": []}


def _doc(admin, llm_status="failed"):
    i = admin.upload(text=TEXT, title="scan_0001").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status=? WHERE id=?", (llm_status, i))
    return i


def test_retry_after_failed_keeps_hand_edits(admin):
    i = _doc(admin)
    admin.patch(f"/documents/{i}", json={"title": "Mein Titel", "sender": "Meine Firma"})
    with connect() as con:
        con.execute("UPDATE documents SET status='tagging' WHERE id=?", (i,))   # wie das Nachholen alle 10 Minuten
    with connect() as con:
        processing.apply_llm(con, i, dict(AI))   # das, was der Worker am Ende tut
    d = admin.get(f"/documents/{i}").json()
    assert d["title"] == "Mein Titel" and d["sender"] == "Meine Firma"
    assert d["summary"] == "Abschlag" and d["document_date"] == "2025-05-14"   # Rest kommt trotzdem


def test_edit_during_run_wins(admin):
    i = _doc(admin, "pending")
    # PATCH kommt, während die KI noch rechnet – danach schreibt der Worker sein Ergebnis
    admin.patch(f"/documents/{i}", json={"document_date": "2025-05-15"})
    with connect() as con:
        processing.apply_llm(con, i, dict(AI))
    assert admin.get(f"/documents/{i}").json()["document_date"] == "2025-05-15"


def test_reprocess_clears_hand_fields(admin):
    i = _doc(admin)
    admin.patch(f"/documents/{i}", json={"title": "Mein Titel"})
    admin.post(f"/documents/{i}/reprocess")
    with connect() as con:
        assert con.execute("SELECT hand_fields FROM documents WHERE id=?", (i,)).fetchone()[0] is None
        processing.apply_llm(con, i, dict(AI))
    assert admin.get(f"/documents/{i}").json()["title"] == "Stromrechnung Mai"


def test_unchanged_fields_are_not_marked(admin):
    """Ein Client, der beim Speichern alle Felder mitschickt, sperrt damit keine leeren Felder für die KI."""
    i = _doc(admin)
    admin.patch(f"/documents/{i}", json={"title": "Mein Titel", "sender": None, "document_date": None})
    with connect() as con:
        assert processing.hand_fields(con, i) == {"title"}
        processing.apply_llm(con, i, dict(AI))
    d = admin.get(f"/documents/{i}").json()
    assert d["title"] == "Mein Titel" and d["sender"] == "Stadtwerke Musterstadt" and d["document_date"] == "2025-05-14"
