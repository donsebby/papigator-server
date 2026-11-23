"""KI-Erkennung pausieren: nichts wird übersprungen, Pause endet von selbst oder per „Fortsetzen“."""
import sqlite3
from datetime import datetime, timedelta, timezone

from app import config, worker


def test_pause_until_and_resume(admin):
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = admin.post("/settings/llm/pause", json={"until": later}).json()
    assert r["llm_paused_until"].endswith("Z")
    assert admin.get("/health").json()["llm_paused_until"] == r["llm_paused_until"]
    assert admin.get("/settings").json()["llm_paused_until"] == r["llm_paused_until"]
    assert admin.post("/settings/llm/pause", json={}).json()["llm_paused_until"] is None
    assert admin.post("/settings/llm/pause", json={"until": "forever"}).json()["llm_paused_until"] == "forever"
    assert admin.post("/settings/llm/pause", json={"until": "morgen"}).status_code == 400
    assert admin.post("/settings/llm/pause", json={"until": "2030-01-01T07:00:00"}).status_code == 400   # ohne Zeitzone


def test_pause_is_capped_and_expires(admin):
    far = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    until = admin.post("/settings/llm/pause", json={"until": far}).json()["llm_paused_until"]
    assert datetime.fromisoformat(until.replace("Z", "+00:00")) < datetime.now(timezone.utc) + timedelta(days=32)
    # Abgelaufen: läuft wieder, die Einstellung verschwindet
    con = sqlite3.connect(config.DB_PATH)
    con.execute("UPDATE settings SET value='2020-01-01T00:00:00Z' WHERE key='llm_paused_until'"); con.commit(); con.close()
    assert worker.paused_until() is None
    assert admin.get("/health").json()["llm_paused_until"] is None


def test_paused_documents_stay_queued(admin, monkeypatch):
    """Während der Pause nimmt der Worker nichts aus der Warteschlange (und markiert nichts als übersprungen)."""
    admin.post("/settings/llm/pause", json={"until": "forever"})
    calls = []
    monkeypatch.setattr(worker, "llm_one", lambda doc_id: calls.append(doc_id) or True)
    monkeypatch.setattr(worker._wake_llm, "wait", lambda timeout=None: (_ for _ in ()).throw(StopIteration))
    doc = admin.upload("x", "Wartet").json()
    con = sqlite3.connect(config.DB_PATH)
    con.execute("UPDATE documents SET status='tagging', llm_status='pending' WHERE id=?", (doc["id"],)); con.commit(); con.close()
    try:
        worker.llm_loop()
    except StopIteration:
        pass
    assert calls == []
    assert admin.get(f"/documents/{doc['id']}").json()["status"] == "tagging"
