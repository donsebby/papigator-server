"""E-Mail-Eingang: ein IMAP-Postfach regelmäßig abholen, PDF- und Bildanhänge als Dokumente anlegen.

Einstellungen stehen in der Settings-Tabelle (Web/App → E-Mail-Eingang). Ungelesene Mails im
gewählten Ordner werden verarbeitet und danach als gelesen markiert oder in einen Erledigt-Ordner
verschoben. Mails ohne passenden Anhang werden optional als Text-PDF abgelegt (Rechnungen, die
nur im Mailtext stehen). Duplikate (gleiche PDF) überspringt der Import still.
"""
import email
import imaplib
import logging
import tempfile
import textwrap
import threading
import time
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

from .db import connect, get_setting, set_setting
from .inbox import IMAGE_EXT, _image_to_pdf

log = logging.getLogger("papigator.mail")
_lock = threading.Lock()

DEFAULTS = {"mail_enabled": "0", "mail_host": "", "mail_port": "993", "mail_user": "", "mail_password": "",
            "mail_folder": "INBOX", "mail_done_folder": "", "mail_owner": "", "mail_poll": "300", "mail_body_pdf": "1"}


def settings(with_password: bool = False) -> dict:
    with connect() as con:
        s = {k: get_setting(con, k, v) or v for k, v in DEFAULTS.items()}
        from .security import decrypt
        s["mail_password"] = decrypt(s["mail_password"]) or ""
        s["mail_password_set"] = bool(s["mail_password"])
        if not with_password:
            s.pop("mail_password")
        s["mail_last"] = get_setting(con, "mail_last")
        s["mail_last_error"] = get_setting(con, "mail_last_error")
        s["mail_last_imported"] = int(get_setting(con, "mail_last_imported", "0") or 0)
        s["mail_total"] = int(get_setting(con, "mail_total", "0") or 0)
        s["running"] = _lock.locked()
        return s


def _record(ok: bool, imported: int = 0, error: str | None = None) -> None:
    with connect() as con:
        set_setting(con, "mail_last", datetime.now().isoformat(timespec="seconds"))
        set_setting(con, "mail_last_error", None if ok else error)
        set_setting(con, "mail_last_imported", str(imported))
        if imported:
            set_setting(con, "mail_total", str(int(get_setting(con, "mail_total", "0") or 0) + imported))


def _owner_id(con, s: dict) -> int | None:
    if s["mail_owner"]:
        r = con.execute("SELECT id FROM users WHERE id=?", (int(s["mail_owner"]),)).fetchone()
        if r:
            return r["id"]
    r = con.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    return r["id"] if r else None


def _connect(s: dict) -> imaplib.IMAP4_SSL:
    port = int(s["mail_port"] or 993)
    if port == 143:
        box = imaplib.IMAP4(s["mail_host"], port, timeout=30)
        box.starttls()
    else:
        box = imaplib.IMAP4_SSL(s["mail_host"], port, timeout=30)
    box.login(s["mail_user"], s["mail_password"])
    typ, _ = box.select(f'"{s["mail_folder"] or "INBOX"}"')
    if typ != "OK":
        raise RuntimeError(f"Ordner {s['mail_folder']} nicht gefunden")
    return box


def test(override: dict | None = None) -> dict:
    """Verbindung prüfen und ungelesene Mails zählen; override erlaubt Werte aus dem noch ungespeicherten Formular."""
    s = settings(with_password=True)
    for k, v in (override or {}).items():
        if k in DEFAULTS and v not in (None, ""):
            s[k] = str(v)
    if not s["mail_host"] or not s["mail_user"] or not s["mail_password"]:
        return {"ok": False, "error": "Server, Benutzer und Passwort angeben"}
    try:
        box = _connect(s)
        typ, data = box.search(None, "UNSEEN")
        n = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        typ, folders = box.list()
        names = []
        for f in folders or []:
            try:
                names.append(f.decode(errors="replace").rsplit(' "', 1)[-1].strip('"').rsplit('" ', 1)[-1].strip('"'))
            except Exception:
                pass
        box.logout()
        return {"ok": True, "unseen": n, "folders": names[:50]}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def _decode(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _attachments(msg: Message) -> list[tuple[str, bytes]]:
    """(Dateiname, Inhalt) für PDF- und Bildanhänge."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _decode(part.get_filename() or "")
        ctype = part.get_content_type()
        suffix = Path(name).suffix.lower()
        is_pdf = ctype == "application/pdf" or suffix == ".pdf"
        is_img = (ctype.startswith("image/") and suffix in IMAGE_EXT) or suffix in IMAGE_EXT
        if not (is_pdf or is_img):
            continue
        if part.get("Content-Disposition", "").startswith("inline") and is_img and not name:
            continue  # eingebettete Signatur-Logos
        payload = part.get_payload(decode=True)
        if not payload or len(payload) <= 2000:
            continue
        if is_img and not is_pdf and _is_logo(payload):
            continue  # Signatur-Logo mit Namen (Outlook: image001.png) – kein Dokument
        out.append((name or ("anhang.pdf" if is_pdf else "bild" + (suffix or ".jpg")), payload))
    return out


LOGO_MAX_PX = 800   # Logos und Symbole sind kleiner; Fotos und Scans von Belegen deutlich größer


def _is_logo(data: bytes) -> bool:
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            return max(im.size) < LOGO_MAX_PX
    except Exception:
        return False   # unlesbar: beim Import als Fehler melden statt still zu schlucken


def _body_text(msg: Message) -> str:
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            plain += (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        elif part.get_content_type() == "text/html" and not part.get_filename():
            html += (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
    if plain.strip():
        return plain
    import re
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def text_to_pdf(title: str, meta: list[str], body: str) -> Path:
    """Einfaches Text-PDF (A4, mehrseitig) für Mails ohne Anhang."""
    from PIL import Image, ImageDraw, ImageFont
    W, H, M = 1240, 1754, 90  # A4 bei 150 dpi
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 26)
        bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except Exception:
        font = ImageFont.load_default(size=26)
        bold = ImageFont.load_default(size=34)
    lines: list[tuple[str, bool]] = [(title[:90], True)] + [(m, False) for m in meta] + [("", False)]
    for para in body.splitlines():
        lines += [(l, False) for l in (textwrap.wrap(para, 95) or [""])]
    pages, y, img, draw = [], None, None, None
    for text, is_title in lines:
        if img is None or y > H - M - 40:
            img = Image.new("L", (W, H), "white"); draw = ImageDraw.Draw(img); pages.append(img); y = M
        draw.text((M, y), text, fill="black", font=bold if is_title else font)
        y += 48 if is_title else 34
    out = Path(tempfile.mkstemp(suffix=".pdf")[1])
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return out


def _import_bytes(data: bytes, name: str, owner_id: int, title: str | None, text: str | None) -> int | None:
    from fastapi import HTTPException
    from .main import import_pdf
    suffix = Path(name).suffix.lower() or ".pdf"
    tmp = Path(tempfile.mkstemp(suffix=suffix)[1])
    tmp.write_bytes(data)
    pdf = tmp
    try:
        if suffix in IMAGE_EXT:
            pdf = _image_to_pdf(tmp)
        return import_pdf(pdf, owner_id, name, title=title, text=text)
    except HTTPException as e:
        if e.status_code == 409:
            log.info("Mail: %s ist schon vorhanden", name)
            return None
        raise
    finally:
        tmp.unlink(missing_ok=True)
        if pdf != tmp:
            pdf.unlink(missing_ok=True)


def check(reason: str = "Zeitplan") -> dict:
    s = settings(with_password=True)
    if not s["mail_host"] or not s["mail_user"] or not s["mail_password"]:
        raise ValueError("E-Mail-Eingang ist nicht eingerichtet")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Abruf läuft bereits")
    imported = 0
    try:
        with connect() as con:
            owner = _owner_id(con, s)
        if owner is None:
            raise ValueError("Kein Nutzer, dem die Mails zugeordnet werden können")
        box = _connect(s)
        typ, data = box.search(None, "UNSEEN")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        problems: list[str] = []
        for uid in uids[:50]:
            typ, parts = box.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            subject = _decode(msg.get("Subject")) or "E-Mail"
            sender = _decode(msg.get("From"))
            date = msg.get("Date", "")
            atts = _attachments(msg)
            got, failed = 0, []
            for name, payload in atts:
                # Ein kaputter Anhang darf den Abruf nicht anhalten – sonst bliebe die Mail ungelesen und
                # jeder weitere Abruf bräche an ihr ab
                try:
                    if _import_bytes(payload, name, owner, None, None) is not None:
                        got += 1
                except Exception as e:
                    failed.append(f"{name}: {getattr(e, 'detail', None) or e.__class__.__name__}")
                    log.warning("Mail „%s“: Anhang %s nicht importiert: %s", subject[:60], name, failed[-1])
            if not atts and s["mail_body_pdf"] == "1":
                body = _body_text(msg).strip()
                if body:
                    pdf = text_to_pdf(subject, [f"Von: {sender}", f"Datum: {date}"], body)
                    try:
                        if _import_bytes(pdf.read_bytes(), f"{subject[:60]}.pdf", owner, subject[:120],
                                         f"{subject}\n{sender}\n{date}\n\n{body}") is not None:
                            got += 1
                    finally:
                        pdf.unlink(missing_ok=True)
            imported += got
            box.store(uid, "+FLAGS", "\\Seen")
            if failed:
                problems += failed
            elif s["mail_done_folder"]:   # mit Fehlern bleibt die Mail im Postfach liegen
                if box.copy(uid, f'"{s["mail_done_folder"]}"')[0] == "OK":
                    box.store(uid, "+FLAGS", "\\Deleted")
            log.info("Mail „%s“ von %s: %d Dokument(e)", subject[:60], sender[:40], got)
        if s["mail_done_folder"]:
            box.expunge()
        box.logout()
        if problems:
            _record(False, imported, "Nicht importiert (Mail bleibt im Postfach): " + "; ".join(problems)[:400])
        else:
            _record(True, imported)
        return {"checked": len(uids), "imported": imported, "reason": reason}
    except Exception as e:
        _record(False, imported, f"{e.__class__.__name__}: {e}")
        log.warning("E-Mail-Abruf fehlgeschlagen: %s", e)
        raise
    finally:
        _lock.release()


def loop() -> None:
    while True:
        wait = 300
        try:
            s = settings()
            wait = max(60, int(s["mail_poll"] or 300))
            if s["mail_enabled"] == "1" and s["mail_host"] and not _lock.locked():
                try:
                    check("Zeitplan")
                except Exception:
                    pass  # steht in mail_last_error
        except Exception:
            log.exception("E-Mail-Zeitplan")
        time.sleep(wait)
