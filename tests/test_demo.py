"""Öffentliche Demo (PAPIGATOR_DEMO=1): Sperrliste, Upload-Grenzen, Sperre nach Fehlversuchen nur pro IP.

Der Routen-Test unten zwingt jede neue ändernde Route in eine Schublade: Sperrliste (app/demo.py), nur Admin
oder hier geprüft erlaubt."""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import config, demo
from app.auth import require_admin
from app.main import app
from conftest import PASSWORD, login, make_pdf

DENIED = demo.DENIED


@pytest.fixture
def family(client, admin, make_user):
    """Wie auf dem Demo-Server: ein Admin, dazu zwei normale Konten, die sich gegenseitig freigeben."""
    a, s = make_user("kim"), make_user("sam")
    assert a.put("/shares", json={"user_id": s.id, "can_edit": True}).status_code == 200
    assert s.put("/shares", json={"user_id": a.id, "can_edit": True}).status_code == 200
    return a, s


@pytest.fixture
def demo_on(monkeypatch, family):
    """Demo erst nach dem Befüllen einschalten – wie auf dem Server (befüllt wird mit PAPIGATOR_DEMO aus)."""
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DEMO_MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(config, "DEMO_MAX_UPLOADS_PER_DAY", 4)


# Was ein Besucher mit dem gemeinsamen Konto nicht darf (Methode, Pfad, Body)
BLOCKED = [
    ("patch", "/auth/me", {"password": "neues-passwort-1", "current_password": PASSWORD}),
    ("patch", "/auth/me", {"display_name": "Irgendwer"}),
    ("patch", "/auth/me", {"show_partner_in_overview": True, "password": "neues-passwort-1"}),
    ("delete", "/auth/sessions", None),
    ("delete", "/auth/sessions/abcdefgh", None),
    ("put", "/shares", {"user_id": 1, "can_edit": True}),
    ("delete", "/shares/1", None),
    ("post", "/links", {"kind": "upload", "expires_days": 7}),
    ("post", "/links", {"kind": "doc", "target": 1, "expires_days": 7}),
    ("delete", "/links/1", None),
    ("delete", "/trash", None),
    ("delete", "/documents/1?permanent=true", None),
    ("delete", "/documents/1?permanent=1", None),
    ("post", "/users", {"username": "neu", "password": "irgendwas-123"}),
    ("patch", "/users/1", {"password": "irgendwas-123"}),
    ("delete", "/users/1", None),
    ("post", "/invites", {"display_name": "Kim"}),
    ("delete", "/invites/1", None),
    ("patch", "/settings", {"llm_url": "http://evil"}),
    ("patch", "/settings", {"public_url": "https://evil"}),
    ("post", "/settings/backup/run", None),
    ("post", "/settings/backup/restore", {"name": "x", "confirm": "WIEDERHERSTELLEN"}),
    ("patch", "/settings/mail", {"mail_host": "evil"}),
    ("post", "/settings/mail/test", {}),
    ("post", "/settings/mail/check", None),
    ("post", "/settings/llm/test", {}),
    ("post", "/settings/llm/retag", None),
    ("delete", "/bank/accounts/1", None),
    ("delete", "/bank/imports/1", None),
]


@pytest.mark.parametrize("method,url,body", BLOCKED)
def test_blocked_for_visitor(demo_on, family, method, url, body):
    kim, _ = family
    kw = {"json": body} if body is not None else {}
    r = getattr(kim, method)(url, **kw)
    assert r.status_code == 403, (method, url, r.status_code, r.text)
    assert r.json()["detail"] == DENIED


@pytest.mark.parametrize("method,url,body", [b for b in BLOCKED if b[1].startswith(("/settings", "/users", "/invites"))])
def test_blocked_for_admin_too(demo_on, admin, method, url, body):
    """Zweite Sicherung: was nach draußen greift, geht in der Demo auch als Admin nicht."""
    kw = {"json": body} if body is not None else {}
    r = getattr(admin, method)(url, **kw)
    assert r.status_code == 403 and r.json()["detail"] == DENIED, (method, url, r.status_code, r.text)


def test_public_routes_blocked(demo_on, client, family):
    kim, _ = family
    for method, url in [("get", "/s/abc"), ("post", "/s/abc/unlock"), ("post", "/s/abc/upload"),
                        ("get", "/s/_/ping"), ("post", "/invites/abc/redeem")]:
        r = getattr(client, method)(url)
        assert r.status_code == 403 and r.json()["detail"] == DENIED, (method, url, r.status_code)


def test_nothing_changed_by_blocked_calls(demo_on, client, family):
    kim, sam = family
    kim.patch("/auth/me", json={"password": "neues-passwort-1", "current_password": PASSWORD})
    kim.put("/shares", json={"user_id": sam.id, "can_edit": False})
    kim.delete(f"/shares/{sam.id}")
    other = login(client, "kim")             # altes Passwort geht noch, Sitzungen bleiben
    other.delete("/auth/sessions")
    assert kim.get("/auth/me").status_code == 200
    me = other.get("/auth/me").json()
    assert me["demo"] is True
    assert me["shared_by_me"] == [{"id": sam.id, "display_name": "sam", "can_edit": True}]


def test_allowed_me_setting_still_works(demo_on, family):
    kim, _ = family
    r = kim.patch("/auth/me", json={"show_partner_in_overview": True, "password": None})
    assert r.status_code == 200, r.text
    assert kim.get("/auth/me").json()["show_partner_in_overview"] is True


def test_sessions_list_shows_only_this_device(demo_on, client, family):
    kim, _ = family
    login(client, "kim")
    sessions = kim.get("/auth/sessions").json()
    assert len(sessions) == 1 and sessions[0]["current"]


def test_allowed_things_still_work(demo_on, family):
    kim, sam = family
    doc = kim.upload(text="Stromrechnung Stadtwerke", title="Strom").json()
    assert kim.patch(f"/documents/{doc['id']}", json={"title": "Strom 2026", "due_date": "2030-01-15",
                                                      "due_note": "Zahlen"}).status_code == 200
    assert kim.get("/documents?due=true").status_code == 200
    assert kim.get("/search?q=Stromrechnung").status_code == 200
    r = kim.post("/persons", json={"name": "Mia"})
    assert r.status_code == 201, r.text
    assert kim.delete(f"/persons/{r.json()['id']}").status_code == 204
    assert sam.get(f"/documents/{doc['id']}").status_code == 200        # Familien-Freigabe wirkt
    assert kim.delete(f"/documents/{doc['id']}").status_code == 204       # in den Papierkorb
    assert kim.delete(f"/documents/{doc['id']}?permanent=false").status_code == 204
    assert kim.post(f"/documents/{doc['id']}/restore").status_code == 200
    assert kim.get("/links").status_code == 200


def test_upload_size_limit(demo_on, family):
    kim, _ = family
    big = make_pdf("gross") + b"%" + b"x" * (1024 * 1024 + 100 * 1024)
    r = kim.upload(title="Gross", content=big)
    assert r.status_code == 413, r.text
    assert kim.upload(title="Klein").status_code == 201


def test_upload_needs_length(demo_on, family):
    kim, _ = family

    def chunks():
        yield b"--x\r\n"
    r = kim.client.post("/documents", content=chunks(), headers={
        "Authorization": f"Bearer {kim.token}", "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411


def test_merge_size_limit(demo_on, family, monkeypatch):
    kim, _ = family
    a = kim.upload(title="A").json()
    b = kim.upload(title="B").json()
    monkeypatch.setattr(config, "DEMO_MAX_UPLOAD_MB", 0)
    r = kim.post("/documents/merge", json={"document_ids": [a["id"], b["id"]]})
    assert r.status_code == 413, r.text


def test_daily_upload_limit(demo_on, family):
    kim, sam = family
    ids = [kim.upload(title=f"Doc {i}").json()["id"] for i in range(3)]
    assert kim.post(f"/documents/{ids[0]}/reprocess").status_code == 200    # zählt mit: 4 von 4
    r = kim.upload(title="Zu viel")
    assert r.status_code == 429, r.text
    assert kim.post(f"/documents/{ids[0]}/reprocess").status_code == 429
    # Abgelehnte (Dublette) zählen nicht, andere Konten haben ihr eigenes Limit
    assert sam.upload(title="Sam 1").status_code == 201
    assert sam.upload(title="Sam 1").status_code == 409
    assert sam.upload(title="Sam 2").status_code == 201


def test_daily_limit_holds_for_parallel_uploads(demo_on, family):
    """Gleichzeitige Uploads kommen nicht am Limit vorbei (Platz wird vor dem Hochladen reserviert)."""
    from concurrent.futures import ThreadPoolExecutor
    kim, _ = family
    for i in range(3):
        assert kim.upload(title=f"Vorher {i}").status_code == 201        # 3 von 4

    def one(i):
        c = TestClient(app)
        return c.post("/documents", headers={"Authorization": f"Bearer {kim.token}"},
                      files={"file": (f"P{i}.pdf", make_pdf(f"parallel {i}"), "application/pdf")},
                      data={"title": f"P{i}"}).status_code
    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(one, range(6)))
    assert codes.count(201) == 1 and codes.count(429) == 5, codes


def test_oversized_json_rejected(demo_on, family):
    kim, _ = family
    r = kim.patch("/auth/me", content=b"{" + b" " * (1024 * 1024 + 10) + b"}",
                  headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_login_lockout_only_per_ip(demo_on, family):
    attacker = TestClient(app, client=("203.0.113.5", 40000))
    for _ in range(5):
        assert attacker.post("/auth/login", json={"username": "kim", "password": "falsch"}).status_code == 401
    assert attacker.post("/auth/login", json={"username": "kim", "password": PASSWORD}).status_code == 429
    reviewer = TestClient(app, client=("198.51.100.7", 40000))
    assert reviewer.post("/auth/login", json={"username": "kim", "password": PASSWORD}).status_code == 200


def test_lockout_per_user_without_demo(family):
    attacker = TestClient(app, client=("203.0.113.5", 40000))
    for _ in range(5):
        attacker.post("/auth/login", json={"username": "kim", "password": "falsch"})
    reviewer = TestClient(app, client=("198.51.100.7", 40000))
    assert reviewer.post("/auth/login", json={"username": "kim", "password": PASSWORD}).status_code == 429


def test_demo_off_nothing_blocked(family):
    kim, sam = family
    assert kim.get("/auth/me").json()["demo"] is False
    assert kim.patch("/auth/me", json={"display_name": "Kim K."}).status_code == 200
    assert kim.post("/links", json={"kind": "upload", "expires_days": 7}).status_code == 201
    assert kim.delete(f"/shares/{sam.id}").status_code == 204
    assert kim.delete("/trash").status_code == 204


# ---------- Jede ändernde Route muss eingeordnet sein ----------

# Geprüft und in der Demo erlaubt: betrifft nur die (täglich neu angelegten) Demo-Daten des eigenen Kontos
REVIEWED_ALLOWED = {
    ("POST", "/auth/login"),                  # Sperre nach Fehlversuchen in der Demo nur pro IP
    ("POST", "/auth/logout"),                 # beendet nur die eigene Sitzung
    ("POST", "/documents"),                   # Größe und Tageslimit (demo.QUOTA)
    ("POST", "/documents/merge"),             # Größe der Quellen und Tageslimit
    ("POST", "/documents/{doc_id}/reprocess"),  # OCR/KI erneut: Tageslimit
    ("POST", "/ask"),                         # ohne KI-Server bricht das freundlich ab
    ("POST", "/documents/owner"),
    ("PATCH", "/documents/{doc_id}"),
    ("POST", "/documents/{doc_id}/accept"),
    ("POST", "/documents/{doc_id}/restore"),
    ("POST", "/views"), ("PATCH", "/views/{view_id}"), ("DELETE", "/views/{view_id}"),
    ("POST", "/tags"),
    ("POST", "/duplicates/resolve"),          # legt das andere in den Papierkorb
    ("POST", "/duplicates/ignore"),
    ("POST", "/persons"), ("DELETE", "/persons/{tag_id}"),
    ("POST", "/emergency/documents"), ("DELETE", "/emergency/documents/{doc_id}"),
    ("POST", "/bank/import"),                 # in der Demo bis 1 MB je Datei und bank.DEMO_MAX_TX Buchungen insgesamt
    ("PATCH", "/bank/accounts/{account_id}"),  # Name, wer mitsieht – lässt sich zurückstellen
    ("PATCH", "/bank/tx/{tx_id}"), ("DELETE", "/bank/rules/{rule_id}"),
}


def _routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):      # FastAPI ≥ 0.160: eingebundene Router
            yield from _routes(r.original_router.routes)
        elif hasattr(r, "routes") and not hasattr(r, "app_mount"):
            yield from _routes(getattr(r, "routes", []) or [])


def _needs_admin(dep) -> bool:
    return dep.call is require_admin or any(_needs_admin(d) for d in dep.dependencies)


def _mutating():
    for r in _routes(app.routes):
        for m in sorted(r.methods - {"GET", "HEAD", "OPTIONS"}):
            yield m, r.path, r


def test_enumeration_sees_included_routers():
    found = {(m, p) for m, p, _ in _mutating()}
    for must in [("POST", "/links"), ("POST", "/s/{token}/upload"), ("POST", "/invites/{token}/redeem"),
                 ("POST", "/persons"), ("PUT", "/cleanup/rules"), ("PATCH", "/auth/me"), ("POST", "/documents")]:
        assert must in found, must


def test_every_mutating_route_is_classified():
    unclassified = []
    for method, path, route in _mutating():
        denied = any(demo._regex(p).match(path) and m in ("*", method) for m, p, _ in demo.DENY)
        if denied or (method, path) in REVIEWED_ALLOWED or _needs_admin(route.dependant):
            continue
        unclassified.append(f"{method} {path}")
    assert not unclassified, ("Neue ändernde Routen für die Demo einordnen (app/demo.py DENY, nur Admin oder "
                              f"REVIEWED_ALLOWED in tests/test_demo.py): {unclassified}")


def test_reviewed_list_has_no_stale_entries():
    existing = {(m, p) for m, p, _ in _mutating()}
    assert REVIEWED_ALLOWED <= existing, REVIEWED_ALLOWED - existing
    for m, p, _ in demo.DENY:
        assert m == "*" or any(m == em and demo._regex(p).match(ep) for em, ep in existing), (m, p)


def test_bank_rows_capped_in_demo(demo_on, family, monkeypatch):
    """Demo: Obergrenze für alle Buchungen zusammen – sonst bliebe Müll (Konten und Importe sind nicht löschbar)."""
    from app import bank
    monkeypatch.setattr(bank, "DEMO_MAX_TX", 2)
    kim, _ = family
    csv_ = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n01.11.2026;A;B;-1,00\n02.11.2026;A;C;-2,00\n03.11.2026;A;D;-3,00\n"
    r = kim.post("/bank/import", files={"file": ("x.csv", csv_.encode(), "text/csv")}, data={"account_name": "Giro"})
    assert r.status_code == 403 and "höchstens 2" in r.json()["detail"]
    pre = kim.post("/bank/import", files={"file": ("x.csv", csv_.encode(), "text/csv")}, data={"preview": "true"})
    assert pre.status_code == 200                                     # ansehen geht


def test_no_backups_in_demo(demo_on):
    from app import backup
    backup.loop()                                                     # kehrt in der Demo sofort zurück


def test_crooked_host_header_does_not_skip_blocklist(demo_on, family):
    kim, _ = family
    r = kim.delete("/trash", headers={"host": "x?"})
    assert r.status_code == 403
