"""Sicherheit: keine Geheimnisse im Klartext in der Datenbank, Sitzungsablauf, Admin-Grenzen."""
import hashlib
import sqlite3
import stat

from app import config, mail, security
from app.db import connect
from conftest import PASSWORD, login


def _db():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def test_session_tokens_only_hashed(client, admin):
    con = _db()
    stored = [r["token"] for r in con.execute("SELECT token FROM sessions")]
    assert admin.token not in stored
    assert hashlib.sha256(admin.token.encode()).hexdigest() in stored
    # Wer nur die Datenbank hat, kann sich mit dem gespeicherten Wert nicht anmelden
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {stored[0]}"}).status_code == 401


def test_old_plaintext_sessions_are_migrated(client, admin):
    con = _db()
    con.execute("INSERT INTO sessions(token, user_id, label, last_used) VALUES ('alter-klartext-token', ?, 'alt', datetime('now'))", (admin.id,))
    con.commit(); con.close()
    with connect() as c:
        security.migrate(c)
        security.migrate(c)   # zweimal ist harmlos
    assert "alter-klartext-token" not in [r["token"] for r in _db().execute("SELECT token FROM sessions")]
    assert client.get("/auth/me", headers={"Authorization": "Bearer alter-klartext-token"}).status_code == 200


def test_idle_sessions_expire(client, admin):
    con = _db()
    con.execute("UPDATE sessions SET last_used=datetime('now', '-91 days')"); con.commit(); con.close()
    assert admin.get("/auth/me").status_code == 401
    assert _db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_session_list_and_revoke_still_work(client, sam):
    other = login(client, "sam")
    sessions = sam.get("/auth/sessions").json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    victim = next(s for s in sessions if not s["current"])
    assert sam.delete(f"/auth/sessions/{victim['id']}").status_code == 204
    assert other.get("/auth/me").status_code == 401 and sam.get("/auth/me").status_code == 200


def test_mail_password_encrypted_at_rest(admin):
    admin.patch("/settings/mail", json={"mail_host": "imap.example.de", "mail_user": "u", "mail_password": "sehr-geheim"})
    raw = _db().execute("SELECT value FROM settings WHERE key='mail_password'").fetchone()["value"]
    assert raw.startswith("enc:v1:") and "sehr-geheim" not in raw
    assert mail.settings(with_password=True)["mail_password"] == "sehr-geheim"
    out = admin.get("/settings/mail").json()
    assert "mail_password" not in out and out["mail_password_set"] is True
    # Leeres Passwort beim Speichern lässt das alte stehen
    admin.patch("/settings/mail", json={"mail_password": ""})
    assert mail.settings(with_password=True)["mail_password"] == "sehr-geheim"


def test_plaintext_mail_password_is_migrated(admin):
    con = _db()
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('mail_password', 'altes-klartext')"); con.commit(); con.close()
    with connect() as c:
        security.migrate(c)
    raw = _db().execute("SELECT value FROM settings WHERE key='mail_password'").fetchone()["value"]
    assert raw.startswith("enc:v1:")
    assert mail.settings(with_password=True)["mail_password"] == "altes-klartext"


def test_share_tokens_hashed_but_link_still_copyable(client, admin):
    doc = admin.upload("s", "Geteilt").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"]}).json()
    token = link["url"].rsplit("/", 1)[1]
    row = _db().execute("SELECT token, token_enc FROM share_links").fetchone()
    assert row["token"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row["token_enc"]
    assert admin.get("/links").json()[0]["url"].endswith(token)
    client.cookies.clear()
    assert "Geteilt" in client.get(f"/s/{token}").text


def test_old_plaintext_share_link_is_migrated(client, admin):
    doc = admin.upload("o", "Alter Link").json()
    con = _db()
    con.execute("INSERT INTO share_links(token, user_id, kind, target, name) VALUES ('klartext-link-token', ?, 'doc', ?, 'Alt')",
                (admin.id, str(doc["id"])))
    con.commit(); con.close()
    with connect() as c:
        security.migrate(c)
    assert _db().execute("SELECT COUNT(*) FROM share_links WHERE token='klartext-link-token'").fetchone()[0] == 0
    client.cookies.clear()
    assert "Alter Link" in client.get("/s/klartext-link-token").text


def test_settings_admin_only(admin, sam):
    assert sam.get("/settings").status_code == 403
    assert admin.get("/settings").status_code == 200


def test_inbox_status_shows_only_own_folder(admin, sam):
    names = [f["username"] for f in sam.get("/settings/inbox").json()["folders"]]
    assert names == ["sam"]
    assert sorted(f["username"] for f in admin.get("/settings/inbox").json()["folders"]) == ["alex", "sam"]


def test_merge_rejects_trashed(admin):
    a = admin.upload("a", "A").json()
    b = admin.upload("b", "B").json()
    admin.delete(f"/documents/{b['id']}")
    r = admin.post("/documents/merge", json={"document_ids": [a["id"], b["id"]]})
    assert r.status_code == 400


def test_backup_contains_no_raw_tokens(admin, tmp_path):
    import time
    target = tmp_path / "bk"; target.mkdir()
    admin.patch("/settings", json={"backup_dir": str(target)})
    admin.patch("/settings/mail", json={"mail_password": "postfach-geheim"})
    admin.post("/settings/backup/run")
    for _ in range(100):
        st = admin.get("/settings/backup").json()
        snaps = st["snapshots"]
        if snaps and not st["running"]:
            break
        time.sleep(0.1)
    snap = target / snaps[0]["name"]
    blob = (snap / "papigator.db").read_bytes()
    assert admin.token.encode() not in blob and b"postfach-geheim" not in blob
    assert not (snap / ".papigator.key").exists()
    assert stat.S_IMODE(snap.stat().st_mode) == 0o700


def test_data_files_not_world_readable(admin):
    security.harden_files()
    assert stat.S_IMODE(config.DOCS_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.DB_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.DATA_DIR.stat().st_mode) == 0o711
