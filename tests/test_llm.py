"""KI-Server: Cloud nur mit Zustimmung, Schlüssel nur an den eigenen Server, Aufgaben einzeln abschaltbar."""
import sqlite3

import httpx
import pytest

from app import config, llm

CLOUD = "https://api.cloud.example/v1"
CLOUD2 = "https://api.other.example/v1"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Keine echten DNS-Anfragen: *.example ist öffentlich, intern.example zeigt ins Heimnetz."""
    table = {"api.cloud.example": "34.120.0.5", "api.other.example": "35.190.0.6", "intern.example": "192.168.1.60"}

    def resolve(host, *a, **k):
        if host not in table:
            raise OSError("unbekannt")
        return [(2, 1, 6, "", (table[host], 0))]

    monkeypatch.setattr(llm.socket, "getaddrinfo", resolve)
    llm._dns_cache.clear()
    llm._no_temperature.clear()


def db_setting(key):
    con = sqlite3.connect(config.DB_PATH)
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else None


def test_local_or_cloud():
    for url in ("http://192.168.1.50:11434/v1", "http://mac:11434/v1", "http://localhost:1234/v1",
                "http://ollama.local/v1", "http://100.101.102.103:11434/v1", "http://[fd00::5]:11434/v1",
                "https://intern.example/v1"):
        assert llm.is_local(url), url
    for url in (CLOUD, "https://8.8.8.8/v1", "https://gibtsnicht.example/v1"):
        assert not llm.is_local(url), url


def test_cloud_server_needs_explicit_consent(admin):
    r = admin.patch("/settings", json={"llm_enabled": True, "llm_url": CLOUD, "llm_model": "m"})
    assert r.status_code == 409
    assert r.json()["detail"]["cloud_confirm"] == ["api.cloud.example"]
    # Nichts gespeichert
    assert admin.get("/settings").json()["llm_url"] != CLOUD

    r = admin.patch("/settings", json={"llm_enabled": True, "llm_url": CLOUD, "llm_model": "m", "llm_cloud_ack": True})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["llm_url"] == CLOUD and s["llm_cloud"] and s["llm_cloud_ok"]

    # Ein weiterer Cloud-Anbieter braucht wieder eine eigene Zustimmung
    r = admin.patch("/settings", json={"llm_url2": CLOUD2})
    assert r.status_code == 409 and r.json()["detail"]["cloud_confirm"] == ["api.other.example"]


def test_cloud_only_over_https(admin):
    r = admin.patch("/settings", json={"llm_url": "http://api.cloud.example/v1", "llm_cloud_ack": True})
    assert r.status_code == 400


def test_key_is_encrypted_and_never_returned(admin):
    secret = "sk-test-geheim-1234567890"
    r = admin.patch("/settings", json={"llm_url": CLOUD, "llm_key": secret, "llm_cloud_ack": True})
    assert r.status_code == 200
    assert secret not in r.text and r.json()["llm_key_set"] is True
    assert secret not in admin.get("/settings").text
    stored = db_setting("llm_key")
    assert stored.startswith("enc:v1:") and secret not in stored
    # Leerer String löscht
    assert admin.patch("/settings", json={"llm_key": ""}).json()["llm_key_set"] is False


def test_key_dropped_when_server_changes(admin):
    admin.patch("/settings", json={"llm_url": CLOUD, "llm_key": "sk-a", "llm_cloud_ack": True})
    # Anderer Server ohne neuen Schlüssel: der alte darf nicht mitwandern
    s = admin.patch("/settings", json={"llm_url": "http://192.168.1.50:11434/v1"}).json()
    assert s["llm_key_set"] is False
    # Nur das Modell ändern lässt den Schlüssel stehen
    admin.patch("/settings", json={"llm_url": CLOUD, "llm_key": "sk-b", "llm_cloud_ack": True})
    assert admin.patch("/settings", json={"llm_model": "anderes"}).json()["llm_key_set"] is True


def test_no_text_to_unapproved_cloud(admin, monkeypatch):
    """Selbst wenn die Adresse an der Prüfung vorbei in die DB kommt (z. B. altes Backup): keine Anfrage."""
    con = sqlite3.connect(config.DB_PATH)
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('llm_enabled','1'), ('llm_url', ?), ('llm_model','m')", (CLOUD,))
    con.commit(); con.close()
    sent = []
    monkeypatch.setattr(llm, "_post", lambda server, prompt, timeout: sent.append(server) or "x")
    assert llm.chat("Kontoauszug …") is None
    assert sent == []


def test_purposes_can_be_switched_off(admin, monkeypatch):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": CLOUD, "llm_model": "m", "llm_cloud_ack": True,
                                   "llm_cloud_ask": False})
    sent = []
    monkeypatch.setattr(llm, "_post", lambda server, prompt, timeout: sent.append(server["host"]) or "[]")
    assert llm.chat("Rechnung", purpose="tag") == "[]"
    assert llm.chat("Frage", purpose="ask") is None
    assert sent == ["api.cloud.example"]
    r = admin.post("/ask", json={"question": "Wo ist meine Steuer-ID?"}).json()
    assert r["error"] and "ausgeschaltet" in r["answer"]
    assert sent == ["api.cloud.example"]   # die Frage ging nirgendwohin


def test_local_server_still_answers_when_cloud_not_allowed(admin, monkeypatch):
    """Cloud fürs Fragen aus, lokaler Ersatz-Server vorhanden: Fragen gehen nur an den lokalen."""
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": CLOUD, "llm_model": "m", "llm_cloud_ack": True,
                                   "llm_url2": "http://192.168.1.51:11434/v1", "llm_model2": "lokal", "llm_cloud_ask": False})
    sent = []
    monkeypatch.setattr(llm, "_post", lambda server, prompt, timeout: sent.append(server["host"]) or "ok")
    assert llm.chat("Frage", purpose="ask") == "ok"
    assert sent == ["192.168.1.51"]


def test_each_key_goes_only_to_its_server(admin, monkeypatch):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": CLOUD, "llm_model": "m", "llm_key": "sk-cloud",
                                   "llm_url2": "http://192.168.1.51:11434/v1", "llm_model2": "lokal", "llm_cloud_ack": True})
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, (headers or {}).get("Authorization")))
        if "cloud" in url:
            raise httpx.ConnectError("weg")
        if url.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": "lokal ok"}}, request=httpx.Request("POST", url))
        raise AssertionError("unerwartet")

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.chat("Hallo") == "lokal ok"
    assert calls[0] == (CLOUD + "/chat/completions", "Bearer sk-cloud")
    # Der lokale Server bekommt den Cloud-Schlüssel nie zu sehen
    assert all(auth != "Bearer sk-cloud" for url, auth in calls[1:])
    # Und an die Cloud geht nie die Ollama-Probe
    assert not any(url.startswith("https://api.cloud.example/api/") for url, _ in calls)


def test_temperature_dropped_when_model_rejects_it(monkeypatch):
    bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(dict(json))
        req = httpx.Request("POST", url)
        if "temperature" in json:
            return httpx.Response(400, text='{"error": "temperature: only the default is supported"}', request=req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=req)

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.completion(CLOUD, "m", [{"role": "user", "content": "x"}], "k", 5) == "OK"
    assert llm.completion(CLOUD, "m", [{"role": "user", "content": "x"}], "k", 5) == "OK"
    assert ["temperature" in b for b in bodies] == [True, False, False]   # beim zweiten Mal gleich ohne


def test_stored_key_only_tested_against_its_server(admin, monkeypatch):
    admin.patch("/settings", json={"llm_url": CLOUD, "llm_key": "sk-cloud", "llm_cloud_ack": True})
    seen = []
    monkeypatch.setattr(llm, "test", lambda url, model, key: seen.append((url, key)) or {"ok": True, "models": []})
    admin.post("/settings/llm/test", json={"url": CLOUD, "model": "m", "slot": 1})
    admin.post("/settings/llm/test", json={"url": CLOUD2, "model": "m", "slot": 1})
    assert seen[0] == (CLOUD, "sk-cloud")
    assert seen[1][1] != "sk-cloud"


def test_separate_model_for_questions(admin, monkeypatch):
    """Großes Modell fürs Erkennen, kleines fürs Fragen – je Server; leer = dasselbe Modell."""
    s = admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "gross",
                                       "llm_ask_model": "klein", "llm_url2": "http://192.168.1.51:11434/v1",
                                       "llm_model2": "ersatz"}).json()
    assert s["llm_ask_model"] == "klein" and s["llm_ask_model2"] == ""
    used = []

    def fake(server, prompt, timeout):
        used.append((server["host"], server["model"]))
        if server["host"] == "192.168.1.50":
            raise httpx.ConnectError("Mac aus")
        return "ok"

    monkeypatch.setattr(llm, "_post", fake)
    llm.chat("Rechnung", purpose="tag")
    llm.chat("Frage", purpose="ask")
    assert used == [("192.168.1.50", "gross"), ("192.168.1.51", "ersatz"),
                    ("192.168.1.50", "klein"), ("192.168.1.51", "ersatz")]


REAL_ALIVE = llm._alive


def test_hanging_server_is_skipped_for_a_while(admin, monkeypatch):
    """Halb schlafender Mac: nimmt Verbindungen an, antwortet aber nicht – Ersatz übernimmt sofort und
    der Mac wird für eine Weile gar nicht mehr gefragt."""
    monkeypatch.setattr(llm, "_alive", REAL_ALIVE)
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "gross",
                                   "llm_url2": "http://192.168.1.51:11434/v1", "llm_model2": "ersatz"})
    probes, used = [], []

    def fake_get(url, timeout=None):
        probes.append(url)
        if "192.168.1.50" in url:
            raise httpx.ReadTimeout("schläft")
        return httpx.Response(200, json={"version": "x"})

    monkeypatch.setattr(llm.httpx, "get", fake_get)
    monkeypatch.setattr(llm, "_post", lambda server, prompt, timeout: used.append(server["host"]) or "ok")
    assert llm.chat("Frage", purpose="ask") == "ok"
    assert llm.chat("Frage", purpose="ask") == "ok"
    assert used == ["192.168.1.51", "192.168.1.51"]
    assert probes == ["http://192.168.1.50:11434/api/version"]   # zweites Mal gar nicht mehr gewartet


def test_read_timeout_falls_back(admin, monkeypatch):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "gross",
                                   "llm_url2": "http://192.168.1.51:11434/v1", "llm_model2": "ersatz"})
    used = []

    def fake(server, prompt, timeout):
        used.append(server["host"])
        if server["host"] == "192.168.1.50":
            raise httpx.ReadTimeout("keine Antwort")
        return "ok"

    monkeypatch.setattr(llm, "_post", fake)
    assert llm.chat("Rechnung") == "ok"
    assert used == ["192.168.1.50", "192.168.1.51"]
    assert llm._hung_until.get("http://192.168.1.50:11434/v1")
