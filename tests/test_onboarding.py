"""„Los geht’s“: nur neue Konten, nie in der Demo, Ausblenden gilt pro Konto, Häkchen kommen vom Server."""
from app import config, inbox
from app.db import connect, init_db
from conftest import PASSWORD, Api


def _web_login(client, username: str) -> Api:
    """Anmeldung wie die Web-App (Label „Web“) – zählt nicht als App."""
    r = client.post("/auth/login", json={"username": username, "password": PASSWORD, "label": "Web"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    client.cookies.clear()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return Api(client, token, me)


def _steps(o) -> dict:
    return {s["key"]: s["done"] for s in o["steps"]}


def test_new_admin_sees_steps(client, admin):
    with connect() as con:
        con.execute("DELETE FROM sessions")
    web = _web_login(client, "alex")
    o = web.get("/onboarding").json()
    assert o["show"] is True and o["dismissed"] is False
    assert [s["key"] for s in o["steps"]] == ["first_document", "app", "invite", "persons", "inbox"]
    assert not any(_steps(o).values())


def test_steps_get_done(client, admin):
    with connect() as con:
        con.execute("DELETE FROM sessions")
    web = _web_login(client, "alex")
    assert web.upload(title="Erstes").status_code == 201
    assert web.post("/persons", json={"name": "Mia"}).status_code == 201
    assert web.post("/invites", json={"display_name": "Kim"}).status_code in (200, 201)
    s = _steps(web.get("/onboarding").json())
    assert s == {"first_document": True, "app": False, "invite": True, "persons": True, "inbox": False}
    # Anmeldung der App (Gerätename als Label)
    r = client.post("/auth/login", json={"username": "alex", "password": PASSWORD, "label": "iPhone von Alex"})
    assert r.status_code == 200
    client.cookies.clear()
    # Scanner-Eingang hat etwas abgelegt
    inbox_dir = config.INBOX_DIR / "alex"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    from conftest import make_pdf
    f = inbox_dir / "scan.pdf"
    f.write_bytes(make_pdf("Scan aus dem Eingang"))
    inbox._import(f, admin.id, [])
    o = web.get("/onboarding").json()
    assert all(_steps(o).values())
    assert o["show"] is False and o["dismissed"] is False        # alles erledigt: keine Karte mehr


def test_mail_inbox_counts(client, admin):
    with connect() as con:
        con.execute("INSERT INTO settings(key, value) VALUES ('mail_enabled', '1'), ('mail_host', 'imap.example.org')")
    assert _steps(admin.get("/onboarding").json())["inbox"] is True


def test_non_admin_has_no_invite_step_and_own_inbox(client, admin, sam):
    o = sam.get("/onboarding").json()
    assert o["show"] is True
    assert "invite" not in _steps(o)
    # Eingang des Admins zählt nicht für sam, eigener schon
    with connect() as con:
        from app.onboarding import mark_inbox_import
        mark_inbox_import(con, admin.id)
    assert _steps(sam.get("/onboarding").json())["inbox"] is False
    assert _steps(admin.get("/onboarding").json())["inbox"] is True
    with connect() as con:
        mark_inbox_import(con, sam.id)
    assert _steps(sam.get("/onboarding").json())["inbox"] is True


def test_persons_seen_through_share(client, admin, sam):
    assert admin.post("/persons", json={"name": "Ben"}).status_code == 201
    assert _steps(sam.get("/onboarding").json())["persons"] is False   # sieht alex' Bestand nicht
    assert admin.put("/shares", json={"user_id": sam.id, "can_edit": False}).status_code == 200
    assert _steps(sam.get("/onboarding").json())["persons"] is True


def test_dismiss_per_account(admin, sam):
    assert admin.post("/onboarding/dismiss").status_code == 204
    o = admin.get("/onboarding").json()
    assert o["show"] is False and o["dismissed"] is True
    assert admin.post("/onboarding/dismiss").status_code == 204       # zweimal schadet nicht
    assert sam.get("/onboarding").json()["show"] is True


def test_existing_accounts_never_see_it(admin):
    with connect() as con:
        con.execute("DROP TABLE onboarding_dismissed")
    init_db()                                                         # wie beim Update: Konto gab es schon
    o = admin.get("/onboarding").json()
    assert o["show"] is False and o["dismissed"] is True


def test_never_in_demo(admin, monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    assert admin.get("/onboarding").json()["show"] is False


def test_needs_login(client):
    assert client.get("/onboarding").status_code == 401
    assert client.post("/onboarding/dismiss").status_code == 401
