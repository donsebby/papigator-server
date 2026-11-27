"""Einladungen: anlegen (nur Admin), öffentlich ansehen, einmal einlösen, Ablauf, Zurückziehen, Freigabe."""
import logging
import sqlite3

from app import config, security

from conftest import PASSWORD, Api


def _token(inv: dict) -> str:
    return inv["url"].split("#invite=", 1)[1]


def _redeem(client, token, username="kim", password=PASSWORD, **kw):
    return client.post(f"/invites/{token}/redeem", json={"username": username, "password": password, **kw})


def _as(client, r) -> Api:
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    client.cookies.clear()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    return Api(client, tok, me)


def test_create_invite_link_and_public_info(admin, client):
    r = admin.post("/invites", json={"display_name": "Kim"})
    assert r.status_code == 201, r.text
    inv = r.json()
    assert "/app/#invite=" in inv["url"] and inv["share_with_me"] == "none" and not inv["is_admin"]
    token = _token(inv)
    assert len(token) >= 40
    # Nur die Prüfsumme liegt in der Datenbank
    with sqlite3.connect(config.DB_PATH) as con:
        dump = "\n".join(con.iterdump())
    assert token not in dump and security.token_hash(token) in dump
    info = client.get(f"/invites/{token}")
    assert info.status_code == 200
    assert set(info.json()) == {"display_name", "server_name"} and info.json()["display_name"] == "Kim"
    assert client.get("/invites/" + "x" * 43).status_code == 404


def test_invite_uses_public_address(admin):
    admin.patch("/settings", json={"public_url": "https://papigator.example.org"})
    inv = admin.post("/invites", json={"display_name": "Kim"}).json()
    assert inv["url"].startswith("https://papigator.example.org/app/#invite=")
    info = admin.client.get(f"/invites/{_token(inv)}").json()
    assert info["server_name"] == "papigator.example.org"


def test_redeem_once_logs_in(admin, client):
    token = _token(admin.post("/invites", json={"display_name": "Kim"}).json())
    r = _redeem(client, token, label="iPhone")
    assert "papigator_token" in r.cookies
    kim = _as(client, r)
    assert kim.user["username"] == "kim" and kim.user["display_name"] == "Kim" and not kim.user["is_admin"]
    labels = [s["label"] for s in kim.get("/auth/sessions").json()]
    assert labels == ["iPhone"]
    # Einmal einlösbar
    assert _redeem(client, token, username="kim2").status_code == 410
    assert client.get(f"/invites/{token}").status_code == 410
    assert admin.get("/invites").json() == []
    # Anmelden mit dem gewählten Passwort klappt auch normal
    assert client.post("/auth/login", json={"username": "kim", "password": PASSWORD}).status_code == 200


def test_invalid_input_keeps_invite_usable(admin, client):
    token = _token(admin.post("/invites", json={"display_name": "Kim"}).json())
    assert _redeem(client, token, password="kurz").status_code == 400
    assert _redeem(client, token, username="../x").status_code == 400
    assert _redeem(client, token, username="alex").status_code == 409     # gibt es schon
    assert client.get(f"/invites/{token}").status_code == 200
    assert _redeem(client, token).status_code == 200


def test_expired_and_revoked(admin, client):
    a = admin.post("/invites", json={"display_name": "Alt"}).json()
    b = admin.post("/invites", json={"display_name": "Weg"}).json()
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("UPDATE invites SET expires_at=datetime('now', '-1 day') WHERE id=?", (a["id"],))
    assert client.get(f"/invites/{_token(a)}").status_code == 410
    assert _redeem(client, _token(a)).status_code == 410
    listed = {i["id"]: i for i in admin.get("/invites").json()}
    assert listed[a["id"]]["expired"] and not listed[b["id"]]["expired"]
    assert listed[b["id"]]["invited_by"] == "Alex" and "url" not in listed[b["id"]]
    assert admin.delete(f"/invites/{b['id']}").status_code == 204
    assert client.get(f"/invites/{_token(b)}").status_code == 404
    assert _redeem(client, _token(b)).status_code == 404


def test_invite_as_admin_with_share(admin, client):
    doc = admin.upload("Mietvertrag Wohnung", "Mietvertrag").json()
    token = _token(admin.post("/invites", json={"display_name": "Sam", "is_admin": True, "share_with_me": "read"}).json())
    sam = _as(client, _redeem(client, token, username="sam2"))
    assert sam.user["is_admin"]
    assert [p["display_name"] for p in sam.user["shared_with_me"]] == ["Alex"]
    assert sam.get(f"/documents/{doc['id']}").status_code == 200
    assert sam.patch(f"/documents/{doc['id']}", json={"title": "x"}).status_code in (403, 404)


def test_share_edit(admin, client):
    doc = admin.upload("Strom", "Strom").json()
    token = _token(admin.post("/invites", json={"display_name": "Sam", "share_with_me": "edit"}).json())
    sam = _as(client, _redeem(client, token, username="sam3"))
    assert sam.patch(f"/documents/{doc['id']}", json={"title": "Strom 2026"}).status_code == 200


def test_friend_without_share_sees_nothing(admin, client):
    doc = admin.upload("Arztbrief Befund Kardiologie", "Arztbrief", tags="Gesundheit").json()
    admin.patch(f"/documents/{doc['id']}", json={"sender": "Praxis Herz"})
    token = _token(admin.post("/invites", json={"display_name": "Freund"}).json())
    friend = _as(client, _redeem(client, token, username="freund"))
    assert friend.user["shared_with_me"] == []
    for scope in ("mine", "partner", "all"):
        assert friend.get("/documents", params={"scope": scope}).json() == []
        assert "Gesundheit" not in [t["name"] for t in friend.get("/tags", params={"scope": scope}).json()]
        assert friend.get("/senders", params={"scope": scope}).json() == []
    assert friend.get(f"/documents/{doc['id']}").status_code in (403, 404)
    assert friend.get(f"/documents/{doc['id']}/file").status_code in (403, 404)
    r = friend.get("/search", params={"q": "Arztbrief Kardiologie"})
    assert r.status_code == 200 and r.json()["documents"] == []


def test_validation_on_create(admin, sam):
    assert admin.post("/invites", json={"display_name": "  "}).status_code == 400
    assert admin.post("/invites", json={"display_name": "X", "share_with_me": "alles"}).status_code == 400
    assert admin.post("/invites", json={"display_name": "X" * 81}).status_code == 422
    # Nicht-Admins dürfen nicht zurückziehen (und sehen nichts)
    inv = admin.post("/invites", json={"display_name": "Kim"}).json()
    assert sam.delete(f"/invites/{inv['id']}").status_code == 403
    assert len(admin.get("/invites").json()) == 1


def test_bootstrap_cannot_invite(client):
    r = client.post("/invites", json={"display_name": "Kim"}, headers={"Authorization": "Bearer boot-token"})
    assert r.status_code == 400


def test_misses_are_rate_limited(admin, client):
    for _ in range(20):
        assert client.get("/invites/" + "y" * 43).status_code == 404
    assert client.get("/invites/" + "y" * 43).status_code == 429
    token = _token(admin.post("/invites", json={"display_name": "Kim"}).json())
    assert client.get(f"/invites/{token}").status_code == 429   # auch gültige, bis das Fenster vorbei ist


def test_access_log_hides_token():
    from app.logredact import RedactFilter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "POST", "/invites/AbC-def_123456789012345/redeem", "1.1", 200), None)
    RedactFilter().filter(rec)
    assert "AbC" not in rec.getMessage() and "/invites/…/redeem" in rec.getMessage()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s %s', ("DELETE", "/invites/12"), None)
    RedactFilter().filter(rec)
    assert rec.getMessage() == "DELETE /invites/12"
    # Freigabe-Links, Kalender-Abo und Suchanfragen (27.11.2026)
    for path, hidden, shown in [("/s/Tok3nXYZ/file", "Tok3nXYZ", "/s/…/file"), ("/calendar/Abc123Def.ics", "Abc123Def", "/calendar/…"),
                                ("/calendar/Abc123Def/summary.json", "Abc123Def", "/calendar/…/summary.json"),
                                ("/search?q=Psychotherapie", "Psychotherapie", "/search?…")]:
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s %s', ("GET", path), None)
        RedactFilter().filter(rec)
        assert hidden not in rec.getMessage() and shown in rec.getMessage()
