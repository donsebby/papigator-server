"""Kündigungshelfer (25.11.2026): aus einem erkannten Vertrag ein fertiges Kündigungsschreiben als PDF (DIN-Brief,
schlicht, eine Seite) – zum Herunterladen/Teilen oder gleich als Dokument in Papigator abgelegt.

Vorbelegt wird nur, was in den Dokumenten des Vertrags steht (Empfänger = Absender, Vertrags-/Kundennummer per
Muster); eigene Anschrift und Empfänger-Anschrift trägt man selbst ein (Clients merken sich die eigene). Das PDF
entsteht ohne zusätzliche Bibliothek: Helvetica (in jedem PDF-Leser eingebaut, WinAnsi für Umlaute und €).
"""
import datetime as dt
import re
import tempfile
import textwrap
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import auth, config
from .auth import User, current_user
from .db import connect

router = APIRouter()

# Nummer: ein Wort aus Ziffern/Großbuchstaben/-./; Leerzeichen nur zwischen Zifferngruppen („12 345 678“)
_NUM = r"[:\s.#-]*(?:Nr\.?|Nummer|nummer)?[:\s.#-]*([A-Z0-9][A-Z0-9./-]*(?: \d[0-9A-Z./-]*)*)"
_CONTRACT = re.compile(r"(?:Vertragsnummer|Vertrags-?Nr\.?|Versicherungsschein(?:-?nummer)?|Policen-?(?:nummer|Nr\.?)|"
                       r"Mitgliedsnummer|Mitglieds-?Nr\.?|Darlehensnummer|Vertragskonto)" + _NUM, re.I)
_CUSTOMER = re.compile(r"(?:Kundennummer|Kunden-?Nr\.?|Kundenkonto|Ihre Kunden(?:nummer)?)" + _NUM, re.I)


def _find(rx, text: str) -> Optional[str]:
    m = rx.search(text or "")
    if not m:
        return None
    v = m.group(1).strip(" .-/")
    return v if re.search(r"\d", v) and 4 <= len(v) <= 40 else None


@router.get("/contracts/cancel-prefill")
def prefill(document_ids: str = Query(..., max_length=200), user: User = Depends(current_user)):
    """Empfänger und Nummern aus den Dokumenten eines Vertrags (nur sichtbare; jüngstes zuerst)."""
    try:
        ids = [int(x) for x in document_ids.split(",") if x.strip()][:10]
    except ValueError:
        raise HTTPException(400, "document_ids: Zahlen, durch Komma getrennt")
    out = {"to_name": None, "contract_number": None, "customer_number": None, "about": None}
    with connect() as con:
        for i in ids:
            r = con.execute("SELECT owner_id, sender, title, text FROM documents WHERE id=? AND deleted_at IS NULL", (i,)).fetchone()
            if not r or not auth.can_view(con, user, r["owner_id"]):
                continue
            out["to_name"] = out["to_name"] or r["sender"]
            out["contract_number"] = out["contract_number"] or _find(_CONTRACT, r["text"])
            out["customer_number"] = out["customer_number"] or _find(_CUSTOMER, r["text"])
            out["about"] = out["about"] or re.sub(r"\s+(?:19|20)\d\d$", "", (r["title"] or "").strip()) or None
    return out


class LetterIn(BaseModel):
    from_name: str = Field(..., max_length=120)
    from_address: str = Field("", max_length=300)
    to_name: str = Field(..., max_length=160)
    to_address: str = Field("", max_length=300)
    about: str = Field("", max_length=160)              # z. B. „Kfz-Versicherung“
    contract_number: str = Field("", max_length=60)
    customer_number: str = Field("", max_length=60)
    by_date: Optional[str] = None                       # YYYY-MM-DD: „spätestens zum …“
    place: str = Field("", max_length=80)
    save: bool = False                                  # true: als Dokument ablegen statt PDF zurückgeben


def letter_lines(b: LetterIn, today: dt.date) -> dict:
    de = lambda d: d.strftime("%d.%m.%Y")
    subject = "Kündigung" + (f" – {b.about.strip()}" if b.about.strip() else "")
    refs = [x for x in (f"Vertragsnummer: {b.contract_number.strip()}" if b.contract_number.strip() else "",
                        f"Kundennummer: {b.customer_number.strip()}" if b.customer_number.strip() else "") if x]
    when = "fristgerecht zum nächstmöglichen Zeitpunkt"
    if b.by_date:
        try:
            when += f", spätestens zum {de(dt.date.fromisoformat(b.by_date))}"
        except ValueError:
            raise HTTPException(400, "by_date als JJJJ-MM-TT")
    body = ["Sehr geehrte Damen und Herren,", "",
            f"hiermit kündige ich den oben genannten Vertrag {when}.", "",
            "Bitte bestätigen Sie mir die Kündigung und das Datum des Vertragsendes schriftlich. "
            "Einer weiteren Verwendung meiner Daten zu Werbezwecken widerspreche ich.", "",
            "Mit freundlichen Grüßen", "", "", b.from_name.strip()]
    return {"from": [b.from_name.strip(), *[x.strip() for x in b.from_address.splitlines() if x.strip()]],
            "to": [b.to_name.strip(), *[x.strip() for x in b.to_address.splitlines() if x.strip()]],
            "date": (f"{b.place.strip()}, " if b.place.strip() else "") + de(today),
            "subject": subject, "refs": refs, "body": body}


def _pdf_text(s: str) -> str:
    raw = s.encode("cp1252", "replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(parts: dict) -> bytes:
    """Eine A4-Seite, DIN-5008-nah: Absender oben, Anschriftfeld, Datum rechts, Betreff fett, Text, Gruß."""
    ops: list[str] = []
    def text(x, y, s, size=11, bold=False):
        ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {x:.1f} {y:.1f} Td ({_pdf_text(s)}) Tj ET")
    mm = 72 / 25.4
    left = 25 * mm
    y = 842 - 18 * mm
    for i, line in enumerate(parts["from"]):
        text(left, y - i * 12, line, 9.5, bold=i == 0)
    y = 842 - 50 * mm
    text(left, y + 14, " · ".join(parts["from"])[:95], 7)          # Rücksendeangabe über dem Anschriftfeld
    for i, line in enumerate(parts["to"]):
        text(left, y - i * 14, line)
    y = 842 - 98 * mm
    text(595 - 25 * mm - len(parts["date"]) * 5.3, y, parts["date"])
    y -= 34
    text(left, y, parts["subject"], 12, bold=True)
    for r in parts["refs"]:
        y -= 15
        text(left, y, r)
    y -= 30
    for para in parts["body"]:
        for line in (textwrap.wrap(para, 86) or [""]):
            text(left, y, line)
            y -= 15
    content = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@router.post("/contracts/cancel-letter")
def cancel_letter(body: LetterIn, user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, "Erst anmelden")
    if not body.from_name.strip() or not body.to_name.strip():
        raise HTTPException(400, "Absender und Empfänger angeben")
    today = dt.date.today()
    parts = letter_lines(body, today)
    pdf = render_pdf(parts)
    filename = f"Kuendigung_{re.sub(r'[^A-Za-z0-9]+', '_', body.to_name.strip())[:40].strip('_') or 'Vertrag'}_{today.isoformat()}.pdf"
    if body.save and config.DEMO:     # in der öffentlichen Demo sähen alle Besucher das abgelegte Schreiben
        raise HTTPException(403, "In der Demo nicht möglich")
    if not body.save:
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    from .main import import_pdf
    tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
    tmp.write_bytes(pdf)
    text = "\n".join([*parts["from"], *parts["to"], parts["date"], parts["subject"], *parts["refs"], *parts["body"]])
    title = f"Kündigung {body.to_name.strip()}"[:120]
    try:
        doc_id = import_pdf(tmp, user.id, filename, title=title, text=text)
    finally:
        tmp.unlink(missing_ok=True)
    with connect() as con:
        con.execute("UPDATE documents SET sender=?, document_date=?, doc_kind='kuendigung' WHERE id=?",
                    (body.to_name.strip(), today.isoformat(), doc_id))
    return {"document_id": doc_id, "title": title}
