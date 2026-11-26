"""Browser-Tests der Web-App (Playwright, Chromium ohne Fenster).

Startet einen echten Server (uvicorn) mit leerer Datenablage im Temp-Ordner, legt einen Testnutzer mit
zufälligem Passwort an und füllt ein paar erfundene Dokumente ein. Laufen getrennt von den Server-Tests:

    (im Server-Ordner) pip install -r requirements-e2e.txt && python -m playwright install chromium
    python -m pytest e2e
"""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]

USER = "alex"
PASSWORD = secrets.token_urlsafe(12)


def make_pdf(marker: str) -> bytes:
    """Minimales gültiges PDF mit eindeutigem Inhalt (wie in tests/conftest.py)."""
    stream = f"BT /F1 12 Tf 50 750 Td ({marker[:60]}) Tj ET".encode("latin-1", "replace")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    data = Path(tempfile.mkdtemp(prefix="papigator-e2e-"))
    port = _free_port()
    env = {**os.environ, "PAPIGATOR_DATA": str(data / "data"), "PAPIGATOR_TOKEN": "boot-e2e",
           "PAPIGATOR_INBOX": str(data / "inbox"), "PAPIGATOR_BACKUP": str(data / "backup"),
           "PAPIGATOR_UPDATE_CHECK": "0",   # keine echten Anfragen an GitHub; die Meldung testet test_update_banner
           "PAPIGATOR_DISCOVERY": "0"}      # kein Bonjour aus den Browser-Tests
    env.pop("PAPIGATOR_LLM_URL", None)
    # Server-Log in eine Datei, nicht in eine Pipe: die liest niemand, und nach vielen Uploads (je Dokument ein
    # Traceback ohne OCR-Werkzeuge) war sie voll – dann hing jeder Thread, der loggt (z. B. der Paperless-Import)
    log = open(data / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=SERVER, env=env, stdout=subprocess.DEVNULL, stderr=log)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/ping", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        log.close()
        raise RuntimeError("Server startet nicht: " + (data / "server.log").read_text(errors="replace")[-2000:])
    _seed(base)
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:   # Verarbeitung vieler Test-Uploads läuft noch – dann hart beenden
        proc.kill()
        proc.wait(timeout=10)
    log.close()


def _seed(base: str) -> None:
    c = httpx.Client(base_url=base)
    c.post("/users", json={"username": USER, "display_name": "Alex", "password": PASSWORD},
           headers={"Authorization": "Bearer boot-e2e"}).raise_for_status()
    tok = c.post("/auth/login", json={"username": USER, "password": PASSWORD}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    c.post("/whatsnew/seen", headers=h).raise_for_status()   # Tour gesehen – sonst läge sie über jedem Test
    c.post("/onboarding/dismiss", headers=h).raise_for_status()   # „Los geht’s“ aus – test_onboarding_card speist sie ein
    c.post("/tags", json={"name": "Haus"}, headers=h).raise_for_status()
    haus = next(t for t in c.get("/tags", headers=h).json() if t["name"] == "Haus")
    c.post("/tags", json={"name": "Strom", "parent_id": haus["id"]}, headers=h).raise_for_status()
    # Mit Dokumentdatum (vor einem Monat): Zeiträume wie „letzte 6 Monate“ zählen nur das echte Datum, nicht den Upload
    recent = (date.today() - timedelta(days=30)).isoformat()
    for title, tags in [("Stromrechnung 2025", "Strom"), ("Grundsteuer", "Haus"), ("Arztbrief", "Gesundheit")]:
        d = c.post("/documents", headers=h, files={"file": (f"{title}.pdf", make_pdf(title), "application/pdf")},
                   data={"title": title, "text": f"{title} Beispieltext", "tags": tags})
        d.raise_for_status()
        c.patch(f"/documents/{d.json()['id']}", json={"document_date": recent}, headers=h).raise_for_status()
    trash = c.post("/documents", headers=h, files={"file": ("Alt.pdf", make_pdf("alt"), "application/pdf")},
                   data={"title": "Altes Schreiben", "text": "alt"}).json()
    c.delete(f"/documents/{trash['id']}", headers=h).raise_for_status()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    """Angemeldete Seite; JavaScript-Fehler lassen den Test scheitern."""
    ctx = browser.new_context(base_url=server, viewport={"width": 1280, "height": 860}, locale="de-DE")
    # Die meisten Tests gehen von den Kacheln aus; neue Browser starten sonst in „Heute“ (test_today_*)
    ctx.add_init_script("try { if (!localStorage.getItem('bl.mode')) localStorage.setItem('bl.mode', 'tiles') } catch (e) {}")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("/app/")
    pg.fill("#login-user", USER)
    pg.fill("#login-pass", PASSWORD)
    pg.click("#login-form button[type=submit]")
    pg.wait_for_selector("#main:not(.hidden)")
    pg.wait_for_selector(".card")
    pg.errors = errors
    yield pg
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)
