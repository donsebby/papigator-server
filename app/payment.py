"""Bezahlen per QR (26.11.2026): `GET /documents/{id}/payment` liefert die Daten einer Überweisung samt EPC-QR-Inhalt
(„GiroCode“), wenn das Dokument eine offene Rechnung mit genau einer fremden IBAN ist – sonst 404.

Die IBAN kommt aus dem Dokumenttext (Muster + Prüfziffer), nie aus der KI. Angeboten wird nur:
- eine Ausgabe (extras.flow = out) oder eine Mahnung, keine Kontoauszüge, Gehalts- oder Gutschrift-Belege
- nicht per Lastschrift (`autopay`), Frist nicht erledigt – ohne Frist nur bei Rechnung oder Mahnung
- Betrag > 0 in Euro (ohne Währung gilt Euro, wie bei den Summen) und ein Absender als Empfängername
- genau eine gültige SEPA-IBAN im Text, die nicht zu einem Konto im Reiter Geld gehört (`bank_accounts.number_hash`),
  mit Beschriftung („IBAN“, „Bankverbindung“, „Konto“ …) kurz davor
- nur für Konten, die es eingeschaltet haben (`users.pay_qr`, Standard aus); `sources` zeigt, wo IBAN und Betrag im Brief stehen

EPC-QR nach EPC069-12 v3.1 („Quick Response Code: Guidelines to Enable Data Capture for the Initiation of a SEPA
Credit Transfer“, 19.03.2024; Datenelemente und Beispiele wie v3.0), Abschnitt 2.2/2.3: Zeilen BCD, Version,
Zeichensatz, SCT, BIC, Name, IBAN, Betrag, Zweck, strukturierte Referenz, Text, Hinweis – getrennt durch LF, leere
Elemente am Ende fallen weg, höchstens 331 Byte.
Wir schreiben Version 002 (BIC optional im EWR) und UTF-8 (Zeichensatz 1); der Verwendungszweck steht als Text.
"""
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from . import extras, processing
from .auth import User, current_user
from .db import connect

router = APIRouter()

# IBAN-Längen der SEPA-Länder (ISO 13616-Register); andere Länder bietet der EPC-QR nicht an
IBAN_LENGTHS = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24,
    "FI": 18, "FR": 27, "GB": 22, "GI": 23, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IS": 26, "IT": 27, "LI": 21,
    "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24,
    "SI": 19, "SK": 24, "SM": 27, "VA": 22,
}
_IBAN_START = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2})[ \t]?(\d{2})")
_BIC = re.compile(r"\b(?:BIC|SWIFT(?:[- ]?(?:Code|BIC))?)\b\s*(?:\(SWIFT\))?\s*[:.]?\s*"
                  r"([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(?![A-Za-z0-9])", re.I)
MAX_PAYLOAD = 331      # Byte, EPC069-12 Abschnitt 2.1
EXCLUDED_KINDS = set(processing.NON_MONEY_KINDS) | {"gehalt", "gutschrift", "kontoauszug"}


def iban_valid(iban: str) -> bool:
    """Prüfziffer nach ISO 7064 (mod 97) und Länge des Landes."""
    s = re.sub(r"\s+", "", iban or "").upper()
    if len(s) != IBAN_LENGTHS.get(s[:2], -1) or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def find_ibans(text: str) -> list[str]:
    """Alle gültigen IBANs im Text (ohne Leerzeichen, groß, ohne Doppelte, in Reihenfolge). Gruppen mit Leerzeichen
    („DE89 3704 0044 …“) werden zusammengesetzt; gezählt wird genau so weit, wie das Land lang ist – was danach
    ohne Leerzeichen weitergeht, ist keine IBAN (sonst würde eine lange Zahl angeschnitten)."""
    out: list[str] = []
    for m in _IBAN_START.finditer(text or ""):
        cc = m.group(1).upper()
        n = IBAN_LENGTHS.get(cc)
        if not n:
            continue
        need, body, i = n - 4, "", m.end()
        while i < len(text) and len(body) < need:
            c = text[i]
            if c.isascii() and c.isalnum():
                body += c
            elif c in " \t" and body and i + 1 < len(text) and text[i + 1].isascii() and text[i + 1].isalnum():
                pass        # einzelnes Leerzeichen zwischen Gruppen
            elif c in " \t" and not body:
                pass
            else:
                break
            i += 1
        if len(body) != need or (i < len(text) and text[i].isascii() and text[i].isalnum()):
            continue
        iban = (cc + m.group(2) + body).upper()
        if iban_valid(iban) and iban not in out:
            out.append(iban)
    return out


# Eine IBAN gilt als Bankverbindung zum Bezahlen nur mit Beschriftung kurz davor (dieselbe oder die vorige Zeile) –
# eine IBAN irgendwo im Fließtext (z. B. „Ihre Bankverbindung … wurde geändert“) reicht nicht (26.11.2026)
_LABEL = re.compile(r"\b(iban|bankverbindung|konto(?:nummer|verbindung)?|kto|bank\s*account|account\s*(?:no|number)?)\b", re.I)


def _iban_spans(text: str, iban: str) -> list[tuple[int, int]]:
    """Fundstellen einer IBAN im Text (mit oder ohne Leerzeichen-Gruppen)."""
    pat = r"[ \t]?".join(re.escape(c) for c in iban)
    return [(m.start(), m.end()) for m in re.finditer(pat, text or "", re.I)]


def iban_labeled(text: str, iban: str) -> bool:
    for a, _ in _iban_spans(text, iban):
        before = text[max(0, a - 80):a]
        lines = before.split("\n")[-2:]          # dieselbe und die vorige Zeile
        if _LABEL.search("\n".join(lines)):
            return True
    return False


def _line_at(text: str, a: int, b: int, width: int = 90) -> str:
    """Die Zeile um eine Fundstelle, auf höchstens `width` Zeichen um den Treffer gekürzt – zum Vergleichen mit dem Brief."""
    start = text.rfind("\n", 0, a) + 1
    end = text.find("\n", b)
    end = len(text) if end < 0 else end
    line = text[start:end]
    if len(line) > width:
        mid = (a - start + b - start) // 2
        lo = max(0, mid - width // 2)
        line = ("…" if lo > 0 else "") + line[lo:lo + width].strip() + ("…" if lo + width < len(line) else "")
    return re.sub(r"[ \t]+", " ", line).strip()


def amount_snippet(amount: float, text: str) -> Optional[str]:
    """Wo der Betrag im Brief steht („Gesamtbetrag 547,88 €“) – None, wenn er so nicht im Text vorkommt."""
    cents = round(abs(amount) * 100)
    whole, frac = divmod(cents, 100)
    plain = str(whole)
    grouped = f"{whole:,}"                        # „1,234“
    forms = {f"{plain},{frac:02d}", f"{plain}.{frac:02d}"}
    if "," in grouped:                            # 1.234,56 · 1,234.56 · 1 234,56 · 1'234.56
        forms |= {f"{grouped.replace(',', '.')},{frac:02d}", f"{grouped}.{frac:02d}",
                  f"{grouped.replace(',', ' ')},{frac:02d}", f"{grouped.replace(',', chr(39))}.{frac:02d}"}
    if frac == 0:
        forms |= {f"{plain},-", f"{plain} €", f"{plain}€", f"EUR {plain}"}
    best = None
    for f in forms:
        for m in re.finditer(r"(?<![\d.,])" + re.escape(f) + r"(?![\d])", text or ""):
            if best is None or m.start() < best[0]:
                best = (m.start(), m.end())
    return _line_at(text, *best) if best else None


def find_bic(text: str, iban: str) -> Optional[str]:
    """BIC nur, wenn genau einer mit Beschriftung („BIC:“, „SWIFT“) im Text steht und zum Land der IBAN passt."""
    found = {m.group(1).upper() for m in _BIC.finditer(text or "")}
    if len(found) != 1:
        return None
    bic = found.pop()
    return bic if bic[4:6] == iban[:2] else None


def _own_hashes(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT number_hash FROM bank_accounts WHERE number_hash IS NOT NULL")}


def is_own(iban: str, hashes: set[str]) -> bool:
    """Gehört die IBAN zu einem Konto im Reiter Geld? Dort steht die Nummer als Hash, so wie sie in der CSV stand
    (IBAN oder nur Kontonummer) – also alle üblichen Schreibweisen prüfen."""
    if not hashes:
        return False
    from .bank import _num_hash
    forms = {iban, iban.lower()}
    if iban[:2] == "DE":              # BLZ (8) + Kontonummer (10)
        forms |= {iban[12:], iban[12:].lstrip("0")}
    elif iban[:2] == "AT":            # BLZ (5) + Kontonummer (11)
        forms |= {iban[9:], iban[9:].lstrip("0")}
    return any(_num_hash(f) in hashes for f in forms if f)


def _clean(s: str, limit: int) -> str:
    """Eine Zeile: keine Zeilenumbrüche oder Steuerzeichen (die trennen im QR die Felder)."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:limit].strip()


def format_amount(amount: float) -> str:
    """„EUR12.3“ wie im Beispiel der Spezifikation: Punkt, höchstens zwei Nachkommastellen, ohne Nullen am Ende."""
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "EUR" + s


def epc_payload(name: str, iban: str, amount: Optional[float] = None, bic: Optional[str] = None, purpose: str = "",
                reference: str = "", text: str = "", info: str = "", version: str = "002", charset: str = "1") -> str:
    """EPC-QR-Inhalt (EPC069-12 Abschnitt 2.2). reference = strukturierte Referenz (RF…), text = unstrukturierter
    Verwendungszweck – nur eins von beiden. Leere Elemente am Ende fallen weg, das letzte hat kein Trennzeichen."""
    if reference and text:
        raise ValueError("Nur Referenz oder Text")
    lines = ["BCD", version, charset, "SCT", bic or "", name, iban,
             format_amount(amount) if amount is not None else "", purpose, reference, text, info]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _payload_bytes(payload: str, charset: str = "1") -> int:
    return len(payload.encode("utf-8" if charset == "1" else "latin-1"))


def reference_for(row) -> str:
    """Verwendungszweck: Rechnungs- und Kundennummer aus den erkannten Nummern, sonst der Titel."""
    try:
        refs = json.loads(row["refs"] or "{}")
    except ValueError:
        refs = {}
    refs = refs if isinstance(refs, dict) else {}
    parts = []
    for key, label in (("invoice", "Rechnung"), ("customer", "Kunde")):
        v = refs.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        v = _clean(str(v or ""), 35)
        if v:
            parts.append(f"{label} {v}")
    return _clean(", ".join(parts) or (row["title"] or ""), 140)


def payment_for(con, row) -> Optional[dict]:
    """Zahlungsdaten zu einer Dokumentzeile oder None, wenn nichts angeboten wird."""
    if row["deleted_at"]:
        return None
    kind = row["doc_kind"] or ""
    if kind in EXCLUDED_KINDS:
        return None
    if kind != "mahnung" and extras.flow(row) != "out":
        return None
    if row["autopay"] == 1:
        return None
    if row["due_date"]:
        if row["due_done"]:
            return None
    elif kind not in ("rechnung", "mahnung"):
        return None
    amount = row["amount"]
    if amount is None or amount <= 0 or amount > 999999999.99:
        return None
    if (row["currency"] or "EUR").upper() != "EUR":
        return None
    name = _clean(row["sender"] or "", 70)
    if not name:
        return None
    hashes = _own_hashes(con)
    text = row["text"] or ""
    ibans = [i for i in find_ibans(text) if not is_own(i, hashes)]
    if len(ibans) != 1 or not iban_labeled(text, ibans[0]):
        return None
    iban = ibans[0]
    bic = find_bic(row["text"] or "", iban)
    ref = reference_for(row)
    amount = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    payload = epc_payload(name, iban, amount, bic=bic, text=ref)
    while ref and _payload_bytes(payload) > MAX_PAYLOAD:
        ref = ref[:-1].rstrip()
        payload = epc_payload(name, iban, amount, bic=bic, text=ref)
    if _payload_bytes(payload) > MAX_PAYLOAD:
        return None
    spans = _iban_spans(text, iban)
    # Fundstellen zum Vergleichen mit dem Brief; amount None = Betrag steht so nicht im Text (von Hand geändert?)
    sources = {"iban": _line_at(text, *spans[0]) if spans else None, "amount": amount_snippet(amount, text)}
    return {"iban": iban, "bic": bic, "name": name, "amount": amount, "currency": "EUR", "reference": ref,
            "epc": payload, "sources": sources}


@router.get("/documents/{doc_id}/payment")
def document_payment(doc_id: int, user: User = Depends(current_user)):
    """Überweisungsdaten und EPC-QR-Inhalt; 404, wenn das Dokument nicht sichtbar ist oder nichts zu bezahlen ist."""
    from .main import get_doc   # späte Bindung: main bindet diesen Router ein
    with connect() as con:
        row = get_doc(con, doc_id, user)
        # Standardmäßig aus – jedes Konto schaltet es selbst ein (Einstellungen, `pay_qr`)
        if not con.execute("SELECT pay_qr FROM users WHERE id=?", (user.id,)).fetchone()["pay_qr"]:
            raise HTTPException(404, "Bezahlen per QR ist ausgeschaltet")
        out = payment_for(con, row)
    if not out:
        raise HTTPException(404, "Keine Zahlung zu diesem Dokument")
    return out
