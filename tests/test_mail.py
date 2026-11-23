"""E-Mail-Eingang mit einem gespielten IMAP-Postfach: Anhänge, Signatur-Logos, kaputte Anhänge, Mails ohne PDF."""
import io
from email.message import EmailMessage

import pytest
from PIL import Image

from app import mail
from app.db import connect, get_setting
from conftest import make_pdf


def _png(w, h):
    buf = io.BytesIO()
    Image.effect_noise((w, h), 60).convert("RGB").save(buf, "PNG")   # Rauschen: groß genug, nicht wegkomprimiert
    return buf.getvalue()


def _mail(subject, attachments=(), body="Hallo, anbei die Rechnung."):
    m = EmailMessage()
    m["Subject"], m["From"], m["Date"] = subject, "Versand <shop@example.org>", "Thu, 12 Nov 2026 10:00:00 +0100"
    m.set_content(body)
    m.add_alternative(f"<p>{body}</p><img src='cid:logo'>", subtype="html")
    for name, data, maintype, subtype, disp in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name, disposition=disp)
    return m.as_bytes()


class FakeBox:
    def __init__(self, mails):
        self.mails = {str(i + 1).encode(): raw for i, raw in enumerate(mails)}
        self.seen, self.copied = set(), []

    def search(self, *_):
        return "OK", [b" ".join(u for u in self.mails if u not in self.seen)]

    def fetch(self, uid, _):
        return "OK", [(b"1", self.mails[uid])]

    def store(self, uid, op, flag):
        if flag == "\\Seen":
            self.seen.add(uid)
        return "OK", []

    def copy(self, uid, folder):
        self.copied.append(uid)
        return "OK", []

    def expunge(self):
        pass

    def logout(self):
        pass


@pytest.fixture
def box(admin, monkeypatch):
    def setup(mails):
        b = FakeBox(mails)
        base = mail.settings(with_password=True)
        monkeypatch.setattr(mail, "settings", lambda with_password=False: {
            **base, "mail_host": "imap.example.org", "mail_user": "x", "mail_password": "y",
            "mail_done_folder": "Erledigt", "mail_body_pdf": "1"})
        monkeypatch.setattr(mail, "_connect", lambda s: b)
        return b
    return setup


def titles(api):
    return sorted(d["original_name"] for d in api.get("/documents", params={"limit": 100}).json())


def test_broken_attachment_does_not_block_other_mails(admin, box):
    b = box([
        _mail("Kaputt", [("rechnung.pdf", b"%PDF-1.4 kaputt" + b"x" * 3000, "application", "pdf", "attachment")]),
        _mail("Gut", [("beleg.pdf", make_pdf("Beleg Gut") + b" " * 2500, "application", "pdf", "attachment")]),
    ])
    r = mail.check("Test")
    assert r["imported"] == 1 and titles(admin) == ["beleg.pdf"]
    assert b.seen == {b"1", b"2"}           # beide gelesen: der nächste Abruf hängt nicht an der kaputten
    assert b.copied == [b"2"]               # die kaputte bleibt im Postfach liegen
    with connect() as con:
        err = get_setting(con, "mail_last_error")
    assert "rechnung.pdf" in err and "kein lesbares PDF" in err
    assert mail.check("Test")["checked"] == 0


def test_signature_logo_is_not_a_document(admin, box):
    box([_mail("Rechnung mit Logo", [
        ("image001.png", _png(200, 60), "image", "png", "inline"),
        ("rechnung.pdf", make_pdf("Rechnung Logo") + b" " * 2500, "application", "pdf", "attachment"),
    ]), _mail("Foto vom Beleg", [("IMG_1234.png", _png(1200, 1600), "image", "png", "inline")])])
    mail.check("Test")
    assert titles(admin) == ["IMG_1234.png", "rechnung.pdf"]


def test_encrypted_pdf_is_rejected_with_reason(admin):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(make_pdf("geheim"))))
    w.encrypt("geheim")
    buf = io.BytesIO(); w.write(buf)
    r = admin.post("/documents", files={"file": ("auszug.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 422 and "Passwort" in r.json()["detail"]
    # Nur Rechte eingeschränkt (öffnen ohne Passwort): wird abgelegt
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(make_pdf("nur Rechte"))))
    w.encrypt(user_password="", owner_password="eigentuemer")
    buf = io.BytesIO(); w.write(buf)
    assert admin.post("/documents", files={"file": ("kontoauszug.pdf", buf.getvalue(), "application/pdf")}).status_code == 201
    assert admin.post("/documents", files={"file": ("x.pdf", b"%PDF-1.4 nix", "application/pdf")}).status_code == 422
