"""Briefkasten-Links: Leute ohne Konto werfen Dokumente ein, sehen aber nichts."""
import io
import re
import sqlite3
from urllib.parse import urlparse

from app import config, share
from conftest import make_pdf


def _path(link):
    return urlparse(link["url"]).path


def _send(client, link, *files, sender=""):
    return client.post(f"{_path(link)}/upload", files=[("files", f) for f in files], data={"sender": sender},
                       follow_redirects=False)


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), "white").save(buf, "PNG")
    return buf.getvalue()


def test_upload_lands_as_pending_review(client, admin):
    admin.post("/tags", json={"name": "Steuer 2025"})
    link = admin.post("/links", json={"kind": "upload", "name": "Steuerberater", "target": "Steuer 2025"}).json()
    assert link["kind"] == "upload" and link["documents"] == 0
    client.cookies.clear()
    page = client.get(_path(link))
    assert page.status_code == 200 and "Einwerfen" in page.text and 'type="file"' in page.text
    r = _send(client, link, ("Bescheid.pdf", make_pdf("Bescheid"), "application/pdf"),
              ("Foto.png", _png(), "image/png"), sender="Max <b>Muster</b>")
    assert r.status_code == 303 and r.headers["location"].endswith("?ok=2")
    assert "2 Dateien" in client.get(r.headers["location"]).text
    docs = admin.get("/documents", params={"review": True}).json()
    assert {d["title"] for d in docs} == {"Bescheid", "Foto"}
    for d in docs:
        assert d["ext_pending"] == 1 and d["ext_from"] == "Steuerberater · Max <b>Muster</b>"
        assert [t["name"] for t in d["tags"]] == ["Steuer 2025"]
    assert admin.get("/documents/review_count").json()["count"] == 2
    assert admin.get("/links").json()[0]["documents"] == 2
    # Übernehmen nimmt es aus „zu prüfen“ (Datum fehlt hier aber noch – das bleibt ein Grund)
    d = docs[0]
    assert admin.post(f"/documents/{d['id']}/accept").json()["ext_pending"] == 0
    # Bearbeiten zählt ebenfalls als angesehen
    assert admin.patch(f"/documents/{docs[1]['id']}", json={"title": "Foto vom Beleg"}).json()["ext_pending"] == 0


def test_box_shows_nothing_and_never_leaks_documents(client, admin):
    mine = admin.upload("Geheim", "Geheim").json()
    link = admin.post("/links", json={"kind": "upload"}).json()
    assert link["name"] == "Briefkasten"
    client.cookies.clear()
    _send(client, link, ("a.pdf", make_pdf("eingeworfen"), "application/pdf"))
    page = client.get(_path(link)).text
    assert "Geheim" not in page and "eingeworfen" not in page and "/file/" not in page
    assert client.get(f"{_path(link)}/file/{mine['id']}").status_code == 404
    assert client.get(f"{_path(link)}/thumb/{mine['id']}").status_code == 404


def test_duplicates_are_silent_and_do_not_restore_trash(client, admin):
    pdf = make_pdf("schon da")
    have = admin.upload(content=pdf, title="Schon da").json()
    admin.delete(f"/documents/{have['id']}")                    # liegt im Papierkorb
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    r = _send(client, link, ("x.pdf", pdf, "application/pdf"))
    assert r.headers["location"].endswith("?ok=1")             # sieht aus wie angekommen
    assert admin.get(f"/documents/{have['id']}").json()["deleted_at"]  # nicht wiederhergestellt
    assert admin.get("/documents").json() == []


def test_rejects_wrong_types_size_and_count(client, admin, monkeypatch):
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    loc = lambda r: r.headers["location"].rsplit("?", 1)[1]
    assert loc(_send(client, link, ("x.pdf", b"<html>kein pdf</html>", "application/pdf"))) == "error=type"
    assert loc(_send(client, link, ("x.exe", b"MZ\x90\x00", "application/octet-stream"))) == "error=type"
    assert loc(_send(client, link, ("kaputt.pdf", b"%PDF-1.4 kaputt", "application/pdf"))) == "error=bad"
    assert loc(_send(client, link, ("leer.pdf", b"", "application/pdf"))) in ("error=size", "error=none")
    many = [(f"{i}.pdf", make_pdf(f"n{i}"), "application/pdf") for i in range(share.UPLOAD_MAX_FILES + 1)]
    assert loc(_send(client, link, *many)) == "error=many"
    monkeypatch.setattr(share, "UPLOAD_MAX_REQUEST_MB", 0)   # ganze Sendung zu groß: wird gar nicht erst gelesen
    assert loc(_send(client, link, ("x.pdf", make_pdf("sendung"), "application/pdf"))) == "error=size"
    monkeypatch.setattr(share, "UPLOAD_MAX_REQUEST_MB", 50)
    monkeypatch.setattr(share, "UPLOAD_MAX_MB", 0)
    assert loc(_send(client, link, ("x.pdf", make_pdf("gross"), "application/pdf"))) == "error=size"
    # Eine schlechte Datei lässt die ganze Sendung zurückgehen
    monkeypatch.setattr(share, "UPLOAD_MAX_MB", 20)
    r = _send(client, link, ("gut.pdf", make_pdf("gut"), "application/pdf"), ("boese.pdf", b"nope", "application/pdf"))
    assert loc(r) == "error=type"
    assert admin.get("/documents").json() == []


def test_rate_limit_and_total(client, admin, monkeypatch):
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    monkeypatch.setattr(share, "UPLOAD_MAX_PER_15MIN", 2)
    assert _send(client, link, ("1.pdf", make_pdf("r1"), "application/pdf"),
                 ("2.pdf", make_pdf("r2"), "application/pdf")).headers["location"].endswith("ok=2")
    assert _send(client, link, ("3.pdf", make_pdf("r3"), "application/pdf")).headers["location"].endswith("error=rate")
    monkeypatch.setattr(share, "UPLOAD_MAX_PER_15MIN", 100)
    monkeypatch.setattr(share, "UPLOAD_MAX_TOTAL", 2)
    assert _send(client, link, ("4.pdf", make_pdf("r4"), "application/pdf")).headers["location"].endswith("error=full")


def test_password_expiry_and_revocation(client, admin):
    link = admin.post("/links", json={"kind": "upload", "password": "geheim1"}).json()
    client.cookies.clear()
    assert "Passwort" in client.get(_path(link)).text
    r = _send(client, link, ("x.pdf", make_pdf("ohne pw"), "application/pdf"))
    assert r.status_code == 303 and "ok=" not in r.headers["location"]
    assert admin.get("/documents").json() == []
    client.post(f"{_path(link)}/unlock", data={"password": "geheim1"})
    assert _send(client, link, ("x.pdf", make_pdf("mit pw"), "application/pdf")).headers["location"].endswith("ok=1")
    # Abgelaufen
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("UPDATE share_links SET expires_at='2000-01-01 00:00:00'")
    assert "nicht mehr gültig" in client.get(_path(link)).text
    assert "ok=" not in _send(client, link, ("y.pdf", make_pdf("spät"), "application/pdf")).headers["location"]
    # Zurückgezogen
    admin.delete(f"/links/{link['id']}")
    assert _send(client, link, ("z.pdf", make_pdf("tot"), "application/pdf")).status_code == 404
    assert len(admin.get("/documents").json()) == 1


def test_pending_external_docs_stay_out_of_other_shares(client, admin):
    admin.post("/tags", json={"name": "Belege"})
    box = admin.post("/links", json={"kind": "upload", "target": "Belege"}).json()
    folder = admin.post("/links", json={"kind": "tag", "target": "Belege"}).json()
    client.cookies.clear()
    _send(client, box, ("Fremd.pdf", make_pdf("fremd"), "application/pdf"))
    assert "Fremd" not in client.get(_path(folder)).text        # erst nach „Übernehmen“
    doc = admin.get("/documents").json()[0]
    admin.post(f"/documents/{doc['id']}/accept")
    assert "Fremd" in client.get(_path(folder)).text


def test_only_owner_can_accept(admin, sam, client):
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    _send(client, link, ("a.pdf", make_pdf("nur alex"), "application/pdf"))
    doc = admin.get("/documents").json()[0]
    assert sam.post(f"/documents/{doc['id']}/accept").status_code == 404


def test_share_app_accepts_uploads(admin):
    from fastapi.testclient import TestClient
    from app.share_app import app as share_app
    pub = TestClient(share_app)
    link = admin.post("/links", json={"kind": "upload"}).json()
    r = pub.post(f"{_path(link)}/upload", files=[("files", ("a.pdf", make_pdf("über 8001"), "application/pdf"))],
                 follow_redirects=False)
    assert r.headers["location"].endswith("ok=1")
    assert admin.get("/documents").json()[0]["ext_pending"] == 1


def test_upload_page_escapes_link_name(client, admin):
    link = admin.post("/links", json={"kind": "upload", "name": "<script>x</script>"}).json()
    client.cookies.clear()
    page = client.get(_path(link)).text
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert not re.search(r"<script", page)


def test_unknown_box_is_rejected_before_reading(client):
    r = client.post("/s/erfunden-erfunden-erfunden/upload", files=[("files", ("a.pdf", make_pdf("x"), "application/pdf"))],
                    follow_redirects=False)
    assert r.status_code == 404


# ---------- Befunde der Sicherheitsprüfung (11.11.2026) ----------

def _apng() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    frames = [Image.new("RGB", (40, 40), c) for c in ("white", "black")]
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def test_image_bombs_are_rejected_before_decoding(client, admin, monkeypatch):
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    loc = lambda r: r.headers["location"].rsplit("?", 1)[1]
    assert loc(_send(client, link, ("anim.png", _apng(), "image/png"))) == "error=bad"   # mehrere Bilder
    monkeypatch.setattr(share, "UPLOAD_MAX_PIXELS", 100)
    assert loc(_send(client, link, ("gross.png", _png(), "image/png"))) == "error=bad"   # 60×80 > 100 Pixel
    monkeypatch.setattr(share, "UPLOAD_MAX_PAGES", 0)
    assert loc(_send(client, link, ("x.pdf", make_pdf("seiten"), "application/pdf"))) == "error=bad"
    assert admin.get("/documents").json() == []


def test_pending_docs_stay_out_of_saved_search_shares(client, admin):
    admin.post("/tags", json={"name": "Belege"})
    box = admin.post("/links", json={"kind": "upload", "target": "Belege"}).json()
    view = admin.post("/links", json={"kind": "view", "target": {"tag": "Belege"}}).json()
    client.cookies.clear()
    _send(client, box, ("Fremd.pdf", make_pdf("fremd in suche"), "application/pdf"))
    assert "Fremd" not in client.get(_path(view)).text
    doc = admin.get("/documents").json()[0]
    assert client.get(f"{_path(view)}/file/{doc['id']}").status_code == 404
    admin.post(f"/documents/{doc['id']}/accept")
    assert "Fremd" in client.get(_path(view)).text


def test_rejected_sends_count_and_ip_limit(client, admin, monkeypatch):
    link = admin.post("/links", json={"kind": "upload"}).json()
    other = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    monkeypatch.setattr(share, "UPLOAD_MAX_PER_15MIN", 2)
    empty = lambda l: client.post(f"{_path(l)}/upload", data={"sender": "x"}, follow_redirects=False)
    assert empty(link).headers["location"].endswith("error=none")
    assert empty(link).headers["location"].endswith("error=none")
    assert empty(link).headers["location"].endswith("error=rate")            # abgelehnte zählen mit
    monkeypatch.setattr(share, "UPLOAD_MAX_PER_15MIN", 100)
    monkeypatch.setattr(share, "UPLOAD_MAX_PER_IP", 2)                    # zwei Sendungen gezählt (die dritte kam nicht durch)
    assert empty(other).headers["location"].endswith("error=rate")           # dieselbe IP, anderer Briefkasten


def test_total_counts_known_files_too(client, admin, monkeypatch):
    pdf = make_pdf("bekannt")
    admin.upload(content=pdf, title="Bekannt")
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    monkeypatch.setattr(share, "UPLOAD_MAX_TOTAL", 2)
    assert _send(client, link, ("a.pdf", pdf, "application/pdf")).headers["location"].endswith("ok=1")   # Dublette
    assert _send(client, link, ("b.pdf", make_pdf("neu"), "application/pdf")).headers["location"].endswith("ok=1")
    # Ohne Zählung der Dublette ginge hier noch eine – so verriete der Zähler, dass „bekannt“ schon da war
    assert _send(client, link, ("c.pdf", make_pdf("noch"), "application/pdf")).headers["location"].endswith("error=full")


def test_pending_docs_not_in_due_list_or_tax_package(client, admin):
    link = admin.post("/links", json={"kind": "upload"}).json()
    client.cookies.clear()
    _send(client, link, ("Frist.pdf", make_pdf("frist"), "application/pdf"))
    doc = admin.get("/documents").json()[0]
    with sqlite3.connect(config.DB_PATH) as con:          # wie vom LLM erkannt
        con.execute("UPDATE documents SET due_date='2030-01-01', amount=99, document_date='2026-01-01' WHERE id=?", (doc["id"],))
    assert admin.get("/documents", params={"due": True}).json() == []
    assert admin.get("/documents/due_count", params={"days": 3650}).json()["count"] == 0
    # auch nicht in Absendern und Summen – bis „Übernehmen“
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("UPDATE documents SET sender='Fremdfirma' WHERE id=?", (doc["id"],))
    assert admin.get("/senders").json() == []
    admin.post(f"/documents/{doc['id']}/accept")
    assert [s["name"] for s in admin.get("/senders").json()] == ["Fremdfirma"]


def test_csrf_same_site_cookie_requests_are_blocked(client, admin):
    r = client.post("/auth/login", json={"username": "alex", "password": "geheim-123"})
    assert r.status_code == 200 and "papigator_token" in client.cookies
    doc = admin.upload("x", "Csrf").json()
    # z. B. von fotos.example.de: SameSite=Lax schickt das Cookie mit, Sec-Fetch-Site sagt „same-site“
    bad = client.post(f"/documents/{doc['id']}/reprocess", headers={"Sec-Fetch-Site": "same-site"})
    assert bad.status_code == 403
    ok = client.patch(f"/documents/{doc['id']}", json={"title": "y"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200
    # App (Bearer) ist nicht betroffen
    assert admin.patch(f"/documents/{doc['id']}", json={"title": "z"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    client.cookies.clear()


def test_csv_cells_and_folder_names_are_defused():
    from app.extras import csv_cell
    from app.folders import _safe
    assert csv_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert csv_cell("-5") == "'-5" and csv_cell("Rechnung") == "Rechnung" and csv_cell(None) is None
    assert _safe("..") == "_" and _safe(".") == "_" and _safe("a..b") == "a..b"
