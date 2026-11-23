"""Fristen, Zähler und die eingebaute Sicherung samt Wiederherstellung."""
import datetime as dt
import time



def test_due_dates(admin):
    doc = admin.upload("Zahlungsziel", "Rechnung").json()
    assert admin.patch(f"/documents/{doc['id']}", json={"due_date": "01.12.2026"}).status_code == 400
    soon = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    past = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    admin.patch(f"/documents/{doc['id']}", json={"due_date": soon, "due_note": "Zahlen"})
    late = admin.upload("Mahnung", "Mahnung").json()
    admin.patch(f"/documents/{late['id']}", json={"due_date": past})
    assert admin.get("/documents/due_count").json() == {"count": 2, "overdue": 1}
    ids = [d["id"] for d in admin.get("/documents", params={"due": True}).json()]
    assert ids == [late["id"], doc["id"]]           # nach Frist sortiert
    admin.patch(f"/documents/{late['id']}", json={"due_done": True})
    assert admin.get("/documents/due_count").json() == {"count": 1, "overdue": 0}
    r = admin.patch(f"/documents/{doc['id']}", json={"due_date": ""}).json()
    assert r["due_date"] is None and r["due_note"] is None


def test_review_count(admin):
    admin.upload("ohne Tags", "Neu")
    doc = admin.upload("fertig", "Fertig").json()
    admin.patch(f"/documents/{doc['id']}", json={"tags": ["Ok"], "document_date": "2026-01-01"})
    assert admin.get("/documents/review_count").json()["count"] == 1


def _wait_backup(admin, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = admin.get("/settings/backup").json()
        if not st["running"] and st.get("snapshots"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"Sicherung nicht fertig: {st}")


def test_backup_and_restore(admin, tmp_path):
    target = tmp_path / "backup"
    target.mkdir()
    assert admin.post("/settings/backup/run").status_code == 400      # noch kein Ziel
    admin.patch("/settings", json={"backup_dir": str(target)})
    keep = admin.upload("bleibt", "Behalten").json()
    assert admin.post("/settings/backup/run").status_code == 200
    st = _wait_backup(admin)
    name = st["snapshots"][0]["name"]

    # Nach der Sicherung: ein Dokument dazu, eins weg
    admin.upload("neu", "Neu nach Sicherung")
    admin.delete(f"/documents/{keep['id']}", params={"permanent": True})

    r = admin.post("/settings/backup/restore", json={"name": name, "confirm": "WIEDERHERSTELLEN"})
    assert r.status_code == 200, r.text
    # Die laufende Sitzung bleibt gültig
    titles = [d["title"] for d in admin.get("/documents").json()]
    assert titles == ["Behalten"]
    assert admin.get(f"/documents/{keep['id']}/file").status_code == 200


def test_llm_falls_back_to_second_server(admin, monkeypatch):
    """Ist der erste LLM-Server nicht erreichbar, antwortet der Ersatz-Server mit seinem Modell."""
    import httpx
    from app import ask
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://mac:11434/v1", "llm_model": "gross",
                                   "llm_url2": "http://lxc:11434/v1", "llm_model2": "klein"})
    from app import llm
    calls = []

    def fake(server, prompt, timeout):
        calls.append((server["url"], server["model"]))
        if server["url"].startswith("http://mac"):
            raise httpx.ConnectError("No route to host")
        return "OK"

    monkeypatch.setattr(llm, "_post", fake)
    assert ask.chat("Hallo") == "OK"
    assert calls == [("http://mac:11434/v1", "gross"), ("http://lxc:11434/v1", "klein")]


def test_trashed_document_does_not_block_progress(admin):
    """Ein Dokument, das während des Taggings in den Papierkorb wandert, zählt nicht mehr als „in Arbeit“."""
    import sqlite3
    from app import config as cfg
    doc = admin.upload("x", "Gleich weg").json()
    con = sqlite3.connect(cfg.DB_PATH)
    con.execute("UPDATE documents SET status='tagging', llm_status='pending' WHERE id=?", (doc["id"],)); con.commit()
    assert admin.get("/health").json()["documents"].get("tagging") == 1
    admin.delete(f"/documents/{doc['id']}")
    assert admin.get("/health").json()["documents"].get("tagging") is None
    con.close()


def test_tagging_survives_permanent_delete(admin, monkeypatch):
    """Wird ein Dokument endgültig gelöscht, während das LLM läuft, darf das Schreiben der Tags nicht abstürzen."""
    from app import processing, worker
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://llm:11434/v1"})
    doc = admin.upload("Stromrechnung der Stadtwerke", "Strom").json()

    def fake_extract(text, existing, restricted=False):
        admin.delete(f"/documents/{doc['id']}", params={"permanent": True})   # löschen, während „das LLM rechnet“
        return {"tags": ["Strom"], "title": "Strom"}, True

    monkeypatch.setattr(processing, "llm_extract", fake_extract)
    assert worker.llm_one(doc["id"]) is True


def test_restore_of_older_snapshot_upgrades_schema_and_keeps_ids(admin, tmp_path):
    """Snapshot von vor einer Programm-Änderung: nach dem Zurückspielen laufen die Anpassungen nach,
    und neue Dokumente bekommen keine Nummer, deren PDF noch von nach der Sicherung herumliegt."""
    import sqlite3
    target = tmp_path / "backup"
    target.mkdir()
    admin.patch("/settings", json={"backup_dir": str(target)})
    admin.upload("Einkommensteuerbescheid", "Bescheid")
    admin.post("/settings/backup/run")
    name = _wait_backup(admin)["snapshots"][0]["name"]
    # Snapshot „alt“ machen: ohne Teilwort-Suche, Absender-Aliase, Tag-Ersteller, Geld-Spalten
    old = sqlite3.connect(target / name / "papigator.db")
    for t in ("documents_tri_ai", "documents_tri_ad", "documents_tri_au"):
        old.execute(f"DROP TRIGGER IF EXISTS {t}")
    old.execute("DROP TABLE documents_tri")
    old.execute("DROP TABLE sender_aliases")
    old.execute("ALTER TABLE tags DROP COLUMN created_by")
    old.execute("ALTER TABLE documents DROP COLUMN tax_35a")
    old.commit(); old.close()

    later = admin.upload("nach der Sicherung", "Später").json()
    assert admin.post("/settings/backup/restore", json={"name": name, "confirm": "WIEDERHERSTELLEN"}).status_code == 200
    assert [d["title"] for d in admin.get("/documents", params={"q": "steuerbescheid"}).json()] == ["Bescheid"]
    assert admin.get("/senders").status_code == 200 and admin.get("/tags").status_code == 200
    assert admin.post("/senders/merge", json={"sources": ["A"], "target": "B"}).status_code == 200
    new = admin.upload("ganz neu", "Neu").json()
    assert new["id"] > later["id"]
