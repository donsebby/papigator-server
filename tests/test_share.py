"""Freigabe-Links: anlegen, öffentlich ansehen, Passwort, Ablauf, Widerruf, nur eigene Dokumente."""
import sqlite3
from urllib.parse import urlparse

from app import config


def _path(link):
    return urlparse(link["url"]).path


def test_document_link_public_and_revocable(client, admin):
    doc = admin.upload("Mietvertrag", "Mietvertrag").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "expires_days": 7}).json()
    assert link["name"] == "Mietvertrag" and link["documents"] == 1 and link["expires_at"]
    client.cookies.clear()
    page = client.get(_path(link))                              # ohne Anmeldung
    assert page.status_code == 200 and "Mietvertrag" in page.text
    assert page.headers["x-robots-tag"].startswith("noindex")
    f = client.get(f"{_path(link)}/file/{doc['id']}")
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    # Andere Dokumente sind über diesen Link nicht erreichbar
    other = admin.upload("Geheim", "Geheim").json()
    assert client.get(f"{_path(link)}/file/{other['id']}").status_code == 404
    # Zugriffe werden gezählt; widerrufen macht den Link tot
    assert admin.get("/links").json()[0]["access_count"] == 1
    admin.delete(f"/links/{link['id']}")
    assert "nicht mehr gültig" in client.get(_path(link)).text
    assert client.get(f"{_path(link)}/file/{doc['id']}").status_code == 404


def test_only_own_documents(admin, sam):
    theirs = sam.upload("Sam", "Sam").json()
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    # Alex sieht das Dokument per Partner-Freigabe, darf es aber nicht öffentlich teilen
    assert admin.get(f"/documents/{theirs['id']}").status_code == 200
    assert admin.post("/links", json={"kind": "doc", "target": theirs["id"]}).status_code == 404


def test_folder_link_includes_subfolders_and_new_docs(client, admin, sam):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    admin.post("/tags", json={"name": "Strom", "parent_id": haus["id"]})
    admin.upload("a", "Grundsteuer", tags="Haus")
    admin.upload("b", "Stromrechnung", tags="Strom")
    admin.upload("c", "Arzt", tags="Arzt")
    sam.upload("d", "Sam Haus", tags="Haus")                  # fremdes Dokument mit gleichem Tag
    link = admin.post("/links", json={"kind": "tag", "target": "Haus"}).json()
    assert link["documents"] == 2 and link["expires_at"]       # Standard: 30 Tage
    client.cookies.clear()
    text = client.get(_path(link)).text
    assert "Grundsteuer" in text and "Stromrechnung" in text and "Arzt" not in text and "Sam Haus" not in text
    admin.upload("e", "Strom 2027", tags="Strom")                # später hinzugefügt → erscheint von selbst
    assert "Strom 2027" in client.get(_path(link)).text
    trashed = admin.get("/documents", params={"tag": "Haus"}).json()[0]
    admin.delete(f"/documents/{trashed['id']}")                  # Papierkorb verschwindet aus der Freigabe
    assert trashed["title"] not in client.get(_path(link)).text


def test_view_link(client, admin):
    d = admin.upload("x", "Spende 2025", tags="Steuer").json()
    admin.patch(f"/documents/{d['id']}", json={"document_date": "2025-05-01"})
    admin.upload("y", "Steuer 2026", tags="Steuer")
    view = admin.post("/views", json={"name": "Steuer 2025", "params": {"tag": "Steuer", "year": 2025}}).json()
    link = admin.post("/links", json={"kind": "view", "target": view["id"]}).json()
    assert link["name"] == "Steuer 2025" and link["documents"] == 1
    client.cookies.clear()
    text = client.get(_path(link)).text
    assert "Spende 2025" in text and "Steuer 2026" not in text


def test_password(client, admin):
    doc = admin.upload("p", "Passwortschutz").json()
    assert admin.post("/links", json={"kind": "doc", "target": doc["id"], "password": "ab"}).status_code == 400
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "password": "geheim1"}).json()
    assert link["has_password"]
    client.cookies.clear()
    page = client.get(_path(link))
    assert "Passwort" in page.text and "Passwortschutz" in page.text   # Name sichtbar, Inhalt nicht
    assert client.get(f"{_path(link)}/file/{doc['id']}").status_code == 404
    r = client.post(f"{_path(link)}/unlock", data={"password": "falsch"}, follow_redirects=False)
    assert r.status_code == 303 and "error=pw" in r.headers["location"]
    r = client.post(f"{_path(link)}/unlock", data={"password": "geheim1"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"{_path(link)}/file/{doc['id']}").status_code == 200


def test_password_lockout(client, admin):
    doc = admin.upload("q", "Sperre").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "password": "richtig1"}).json()
    client.cookies.clear()
    for _ in range(10):
        client.post(f"{_path(link)}/unlock", data={"password": "falsch"}, follow_redirects=False)
    r = client.post(f"{_path(link)}/unlock", data={"password": "richtig1"}, follow_redirects=False)
    assert "error=locked" in r.headers["location"]


def test_expired_link(client, admin):
    doc = admin.upload("z", "Alt").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "expires_days": 1}).json()
    con = sqlite3.connect(config.DB_PATH)
    con.execute("UPDATE share_links SET expires_at='2000-01-01 00:00:00'"); con.commit(); con.close()
    client.cookies.clear()
    assert "nicht mehr gültig" in client.get(_path(link)).text
    assert client.get(f"{_path(link)}/file/{doc['id']}").status_code == 404
    assert admin.get("/links").json()[0]["expired"] is True


def test_links_are_private_and_public_url(admin, sam):
    doc = admin.upload("u", "Urkunde").json()
    admin.post("/links", json={"kind": "doc", "target": doc["id"], "expires_days": 0})
    assert sam.get("/links").json() == []
    lid = admin.get("/links").json()[0]["id"]
    sam.delete(f"/links/{lid}")
    assert len(admin.get("/links").json()) == 1
    assert admin.get("/links").json()[0]["expires_at"] is None   # unbefristet
    assert admin.patch("/settings", json={"public_url": "papigator.example.de"}).status_code == 400
    admin.patch("/settings", json={"public_url": "https://papigator.example.de/"})
    assert admin.get("/links").json()[0]["url"].startswith("https://papigator.example.de/s/")
    assert sam.patch("/settings", json={"public_url": "https://evil.example"}).status_code == 403


def test_public_url_from_env_unless_set_in_app(admin, monkeypatch):
    from app import config
    doc = admin.upload("e", "Env").json()
    admin.post("/links", json={"kind": "doc", "target": doc["id"]})
    monkeypatch.setattr(config, "PUBLIC_URL", "https://papigator.example.org")
    assert admin.get("/links").json()[0]["url"].startswith("https://papigator.example.org/s/")
    assert admin.get("/settings").json()["public_url"] == "https://papigator.example.org"
    admin.patch("/settings", json={"public_url": "https://anders.example.org"})   # Einstellung hat Vorrang
    assert admin.get("/links").json()[0]["url"].startswith("https://anders.example.org/s/")


def test_unknown_token(client):
    client.cookies.clear()
    assert "nicht mehr gültig" in client.get("/s/gibtsnicht").text
    assert client.get("/s/gibtsnicht/file/1").status_code == 404


def test_public_page_needs_only_s_paths(client, admin):
    """Alles, was die öffentliche Seite lädt, liegt unter /s/ – dann reicht es, nur diesen Pfad freizugeben."""
    import re
    doc = admin.upload("i", "Icon").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"]}).json()
    client.cookies.clear()
    page = client.get(_path(link)).text
    refs = re.findall(r'(?:src|href)="(/[^"]*)"', page)
    assert refs and all(r.startswith("/s/") for r in refs), refs
    assert client.get("/s/_/icon.png").headers["content-type"] == "image/png"


def test_selection_link(client, admin, sam):
    a = admin.upload("a", "Lohn Mai").json()
    b = admin.upload("b", "Lohn Juni").json()
    other = admin.upload("c", "Nicht dabei").json()
    link = admin.post("/links", json={"kind": "docs", "target": [a["id"], b["id"], a["id"]]}).json()
    assert link["name"] == "2 Dokumente" and link["documents"] == 2 and link["kind"] == "docs"
    client.cookies.clear()
    text = client.get(_path(link)).text
    assert "Lohn Mai" in text and "Lohn Juni" in text and "Nicht dabei" not in text
    assert client.get(f"{_path(link)}/file/{b['id']}").status_code == 200
    assert client.get(f"{_path(link)}/file/{other['id']}").status_code == 404
    # Papierkorb fällt heraus
    admin.delete(f"/documents/{b['id']}")
    assert "Lohn Juni" not in client.get(_path(link)).text
    # Fremde oder gelöschte Dokumente lassen sich nicht in eine Auswahl schmuggeln
    theirs = sam.upload("j", "Sam").json()
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    r = admin.post("/links", json={"kind": "docs", "target": [a["id"], theirs["id"]]})
    assert r.status_code == 404 and "eigenen" in r.json()["detail"]
    assert admin.post("/links", json={"kind": "docs", "target": [a["id"], b["id"]]}).status_code == 404
    assert admin.post("/links", json={"kind": "docs", "target": []}).status_code == 400
    assert admin.post("/links", json={"kind": "docs", "target": a["id"]}).status_code == 400
    assert sam.post("/links", json={"kind": "docs", "target": [a["id"]]}).status_code == 404


def test_share_app_serves_only_public_pages(admin):
    """Der eigene Freigabe-Port (app.share_app) kennt nur /s/… – keine Anmeldung, keine API, keine Web-App."""
    from fastapi.testclient import TestClient
    from app.share_app import app as share_app
    pub = TestClient(share_app)
    doc = admin.upload("Steuerbescheid", "Steuerbescheid").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "password": "geheim1"}).json()
    sel = admin.post("/links", json={"kind": "docs", "target": [doc["id"]]}).json()
    # Freigaben funktionieren vollständig: Seite, Passwort, PDF, Vorschaubild-Route, Icon, Lebenszeichen
    assert "Passwort" in pub.get(_path(link)).text
    assert pub.post(f"{_path(link)}/unlock", data={"password": "geheim1"}, follow_redirects=False).status_code == 303
    assert pub.get(f"{_path(link)}/file/{doc['id']}").content.startswith(b"%PDF")
    assert "Steuerbescheid" in pub.get(_path(sel)).text
    assert pub.get("/s/_/icon.png").status_code == 200
    assert pub.get("/s/_/ping").json() == {"ok": True}
    # Alles andere gibt es hier nicht – auch nicht mit gültiger Anmeldung
    auth = {"Authorization": f"Bearer {admin.token}"}
    for method, path in [("get", "/links"), ("post", "/links"), ("post", "/auth/login"), ("get", "/auth/me"),
                         ("get", "/documents"), ("get", f"/documents/{doc['id']}/file"), ("get", "/app/"),
                         ("get", "/"), ("get", "/ping"), ("get", "/health"), ("get", "/docs"),
                         ("get", "/openapi.json"), ("get", "/export.zip"), ("get", "/s/../documents"),
                         ("get", "/s/%2e%2e/documents")]:
        r = getattr(pub, method)(path, headers=auth)
        assert r.status_code in (404, 405), (method, path, r.status_code)
        assert b"%PDF" not in r.content


def test_link_does_not_reveal_foreign_folder_names(admin, sam):
    """Ein Link auf einen fremden Ordner würde dessen Namen verraten (Name = Ordnername) – wie /tags: unbekannt."""
    admin.upload("Befund", "Befund", tags="Psychotherapie")
    tid = next(t["id"] for t in admin.get("/tags").json() if t["name"] == "Psychotherapie")
    for target in (tid, "Psychotherapie"):
        r = sam.post("/links", json={"kind": "tag", "target": target})
        assert r.status_code == 404 and "Psychotherapie" not in r.text
        r = sam.post("/links", json={"kind": "upload", "target": target})
        assert r.status_code == 404 and "Psychotherapie" not in r.text
    # eigener Ordner gleichen Bestands geht weiter; mit Freigabe sieht Sam den Ordner und darf ihn verlinken
    assert admin.post("/links", json={"kind": "tag", "target": tid}).status_code == 201
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert sam.post("/links", json={"kind": "tag", "target": tid}).json()["documents"] == 0   # nur eigene Dokumente
