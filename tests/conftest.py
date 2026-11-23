"""Gemeinsame Test-Hilfen: eigene Datenablage pro Test, Nutzer anlegen, PDFs hochladen.

Die Worker (OCR, LLM, Sicherung, Mail, Eingang) starten hier nicht – der TestClient wird ohne
`with` benutzt, damit das Startup-Ereignis nicht läuft. Hochgeladene Dokumente bleiben deshalb
„pending“; Text kommt über das Formularfeld `text`, wie bei der App (Vision-OCR auf dem Gerät).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Vor dem Import der App setzen – config liest die Umgebung beim Import
_ROOT = Path(tempfile.mkdtemp(prefix="papigator-test-"))
os.environ["PAPIGATOR_DATA"] = str(_ROOT / "data")
os.environ["PAPIGATOR_TOKEN"] = "boot-token"
os.environ["PAPIGATOR_INBOX"] = str(_ROOT / "inbox")
os.environ.pop("PAPIGATOR_LLM_URL", None)
os.environ.pop("BEIBERLESS_LLM_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "geheim-123"


@pytest.fixture(autouse=True)
def fresh_data():
    """Jeder Test beginnt mit leerer Datenbank und leeren Ordnern."""
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    shutil.rmtree(config.INBOX_DIR, ignore_errors=True)
    config.ensure_dirs()
    init_db()
    yield


@pytest.fixture(autouse=True)
def llm_servers_alive(monkeypatch):
    """Keine Lebenszeichen-Abfragen ins echte Netz: eingetragene LLM-Server gelten als erreichbar."""
    from app import llm
    llm._hung_until.clear()
    monkeypatch.setattr(llm, "_alive", lambda server: True)


@pytest.fixture
def client():
    return TestClient(app)


class Api:
    """Kleiner Wrapper: angemeldeter Client eines Nutzers."""

    def __init__(self, client: TestClient, token: str, user: dict):
        self.client, self.token, self.user = client, token, user
        self.id = user["id"]

    def _h(self, kw):
        kw.setdefault("headers", {})["Authorization"] = f"Bearer {self.token}"
        return kw

    def get(self, url, **kw): return self.client.get(url, **self._h(kw))
    def post(self, url, **kw): return self.client.post(url, **self._h(kw))
    def patch(self, url, **kw): return self.client.patch(url, **self._h(kw))
    def put(self, url, **kw): return self.client.put(url, **self._h(kw))
    def delete(self, url, **kw): return self.client.delete(url, **self._h(kw))

    def upload(self, text: str = "", title: str = "Dokument", tags: str = "", content: bytes | None = None):
        pdf = content if content is not None else make_pdf(f"{title}|{text}")
        return self.post("/documents", files={"file": (f"{title}.pdf", pdf, "application/pdf")},
                         data={"title": title, "text": text, "tags": tags})


def make_pdf(marker: str) -> bytes:
    """Minimales, gültiges PDF; `marker` macht den Inhalt (und damit den SHA-256) eindeutig."""
    stream = f"BT /F1 12 Tf 50 750 Td ({marker[:60]}) Tj ET".encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def login(client: TestClient, username: str, password: str = PASSWORD) -> Api:
    r = client.post("/auth/login", json={"username": username, "password": password, "label": "Test"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    client.cookies.clear()   # Tests melden sich per Bearer an; das Login-Cookie würde sonst mitgeschickt
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return Api(client, token, me)


@pytest.fixture
def admin(client) -> Api:
    """Erster Nutzer (wird automatisch Admin), angelegt mit dem Bootstrap-Token."""
    r = client.post("/users", json={"username": "alex", "display_name": "Alex", "password": PASSWORD},
                    headers={"Authorization": "Bearer boot-token"})
    assert r.status_code == 201, r.text
    return login(client, "alex")


@pytest.fixture
def make_user(client, admin):
    def _make(username: str, is_admin: bool = False) -> Api:
        r = admin.post("/users", json={"username": username, "password": PASSWORD, "is_admin": is_admin})
        assert r.status_code == 201, r.text
        return login(client, username)
    return _make


@pytest.fixture
def sam(make_user) -> Api:
    return make_user("sam")
