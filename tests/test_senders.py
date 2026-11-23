"""Absender: angezeigte Schreibweise, Zusammenführen merkt sich den Namen für neue Dokumente."""
import sqlite3

from app import ask, config, processing
from app.db import connect


def _sender(doc_id, name):
    con = sqlite3.connect(config.DB_PATH)
    con.execute("UPDATE documents SET sender=? WHERE id=?", (name, doc_id))
    con.commit(); con.close()


def _recognize(doc_id, sender):
    with connect() as con:
        processing.apply_llm(con, doc_id, {"sender": sender})
        return con.execute("SELECT sender FROM documents WHERE id=?", (doc_id,)).fetchone()["sender"]


def test_most_common_spelling_is_shown(admin):
    for i in range(3):
        _sender(admin.upload(f"Rechnung {i}", f"R{i}").json()["id"], "Telekom Deutschland GmbH")
    _sender(admin.upload("Rechnung x", "Rx").json()["id"], "TELEKOM DEUTSCHLAND GMBH")
    s = admin.get("/senders").json()
    assert [(x["name"], x["count"]) for x in s] == [("Telekom Deutschland GmbH", 4)]


def test_merge_sticks_for_new_documents(admin):
    a = admin.upload("Rechnung 1", "R1").json()["id"]
    b = admin.upload("Rechnung 2", "R2").json()["id"]
    _sender(a, "Telekom Deutschland GmbH"); _sender(b, "Deutsche Telekom")
    assert admin.post("/senders/merge", json={"sources": ["Deutsche Telekom"], "target": "Telekom Deutschland GmbH"}).json()["moved"] == 1
    c = admin.upload("Rechnung 3", "R3").json()["id"]
    assert _recognize(c, "deutsche telekom") == "Telekom Deutschland GmbH"     # Alias
    assert _recognize(c, "TELEKOM DEUTSCHLAND GMBH") == "Telekom Deutschland GmbH"   # nur andere Schreibung
    assert _recognize(c, "Vodafone GmbH") == "Vodafone GmbH"
    # Kette: später „Telekom Deutschland GmbH“ in „Telekom“ – der alte Alias zeigt mit
    admin.post("/senders/merge", json={"sources": ["Telekom Deutschland GmbH"], "target": "Telekom"})
    assert _recognize(c, "Deutsche Telekom") == "Telekom"
    # Zurück: das Ziel ist nie selbst Alias
    admin.post("/senders/merge", json={"sources": ["Telekom"], "target": "Telekom Deutschland GmbH"})
    assert _recognize(c, "Telekom Deutschland GmbH") == "Telekom Deutschland GmbH"
    assert _recognize(c, "Deutsche Telekom") == "Telekom Deutschland GmbH"


def test_suggestion_avoids_all_caps_target(monkeypatch):
    monkeypatch.setattr(ask, "_chat", lambda *a, **k:
                        '[{"target": "TELEKOM", "sources": ["Telekom Deutschland GmbH", "Deutsche Telekom"], "reason": "x"}]')
    items = [{"id": n, "name": n, "count": c} for n, c in
             [("TELEKOM", 5), ("Telekom Deutschland GmbH", 3), ("Deutsche Telekom", 1)]]
    g = ask.suggest_sender_merges(items)[0]
    assert g["target"]["name"] == "Telekom Deutschland GmbH"
    assert {s["name"] for s in g["sources"]} == {"TELEKOM", "Deutsche Telekom"}


def test_tag_suggestion_drops_narrower_terms(monkeypatch):
    monkeypatch.setattr(ask, "_chat", lambda *a, **k: '[{"target": "Telekommunikation", "sources": ["Handy"], '
                        '"reason": "Handy ist eine Unterkategorie von Telekommunikation"}, '
                        '{"target": "KFZ", "sources": ["Auto"], "reason": "dasselbe"}]')
    tags = [{"id": i, "name": n, "count": 1} for i, n in enumerate(["Telekommunikation", "Handy", "KFZ", "Auto"])]
    assert [g["target"]["name"] for g in ask.suggest_tag_merges(tags)] == ["KFZ"]
