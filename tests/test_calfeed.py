"""Kalender-Abo: ein Link mit allen offenen Fristen, die ich sehe (ohne Anmeldung abrufbar), neu = alter ungültig,
abschaltbar; erledigte und fremde Fristen fehlen."""
import datetime as dt

from app.db import connect


def _due(api, title, days, **extra):
    d = api.upload("Frist", title).json()
    api.patch(f"/documents/{d['id']}", json={"due_date": (dt.date.today() + dt.timedelta(days=days)).isoformat(),
                                               "due_note": "Zahlen", **extra})
    return d["id"]


def test_feed_lists_open_dues_and_rotates(admin, sam, client):
    assert admin.get("/calendar/feed").json() == {"path": None}
    _due(admin, "Kfz-Steuer", 10)
    done = _due(admin, "Erledigt", 5)
    admin.patch(f"/documents/{done}", json={"due_done": True})
    _due(sam, "Fremde Frist", 3)                               # Alex sieht Sams Bestand nicht
    path = admin.post("/calendar/feed").json()["path"]
    assert path.startswith("/calendar/") and path.endswith(".ics")
    assert admin.get("/calendar/feed").json()["path"] == path    # lässt sich wieder anzeigen
    r = client.get(path)                                         # ohne Anmeldung
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert "X-WR-CALNAME:Papigator – Fristen" in body and body.count("BEGIN:VEVENT") == 1
    assert "Frist: Kfz-Steuer" in body and "Erledigt" not in body and "Fremde" not in body
    with connect() as con:                                       # Token nur gehasht/verschlüsselt
        row = con.execute("SELECT token_hash, token_enc FROM calendar_feeds").fetchone()
    assert path.split("/")[-1][:-4] not in (row["token_hash"], row["token_enc"])
    # neuer Link: alter ungültig; abschalten: gar keiner
    new = admin.post("/calendar/feed").json()["path"]
    assert new != path and client.get(path).status_code == 404 and client.get(new).status_code == 200
    assert admin.delete("/calendar/feed").status_code == 204 and client.get(new).status_code == 404


def test_feed_includes_shared_dues_with_assignee(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    _due(admin, "Hundesteuer", 7, due_assignee=sam.id)
    path = sam.post("/calendar/feed").json()["path"]
    body = sam.client.get(path).text
    assert "Frist: Hundesteuer" in body and "Kümmert sich: sam" in body


def test_summary_for_home_assistant(admin, sam, client):
    """Kurzfassung für einen REST-Sensor: gleicher Link, gleiche Fristen, ohne Anmeldung; neu/aus wie das Abo."""
    _due(admin, "Überfällig", -2)
    _due(admin, "Kfz-Steuer", 3)
    _due(admin, "Später", 30)
    done = _due(admin, "Erledigt", 1)
    admin.patch(f"/documents/{done}", json={"due_done": True})
    _due(sam, "Fremde Frist", 1)
    path = admin.post("/calendar/feed").json()["path"]
    url = path[:-4] + "/summary.json"
    r = client.get(url)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert (s["open"], s["overdue"], s["due_7_days"]) == (3, 1, 1)
    assert s["next"]["title"] == "Überfällig" and s["next"]["in_days"] == -2 and s["next"]["note"] == "Zahlen"
    assert s["today"] == dt.date.today().isoformat()
    assert client.get("/calendar/falsch/summary.json").status_code == 404
    admin.delete("/calendar/feed")
    assert client.get(url).status_code == 404
