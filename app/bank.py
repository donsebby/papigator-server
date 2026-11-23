"""Geld: Kontoumsätze aus CSV-Exporten (C24 und andere Banken) neben den Belegen.

Kein Bank-Login: man lädt die CSV aus dem Online-Banking hoch. Die Spalten werden an der Kopfzeile erkannt
(Bankexporte haben oft Vorspann-Zeilen), mit Synonymen und notfalls am Inhalt; Soll/Haben-Spalten, Semikolon oder
Komma, UTF-8 (mit BOM) oder Windows-1252. Beträge in Cent (keine Rundungsfehler beim Vergleichen).

Doppelte: jede Buchung bekommt einen Fingerabdruck aus Datum, Cent, Gegenüber und Zweck plus „die wievielte gleiche
in dieser Datei“ – dieselbe Datei zweimal bringt 0 neue, zwei gleiche Buchungen an einem Tag bleiben zwei.

Wer ein Konto sieht, ist eine eigene Einstellung (bank_access), nicht die Dokument-Freigabe. Umsätze erscheinen nie in
Freigabe-Links, Exporten oder Spotlight. Einsortieren: eigene Regeln (bank_rules), eingebaute Händler, die Kategorie
der Bank, sonst offen (optional KI-Vorschlag im Heimnetz, wie beim Erkennen freigegeben).
"""
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import bisect
import re
import threading
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from . import auth, config, llm, processing
from .auth import User, current_user
from .db import connect

log = logging.getLogger("papigator.bank")
router = APIRouter(prefix="/bank")

MAX_CSV = 5 * 1024 * 1024
MAX_ROWS = 20000        # ein Export über viele Jahre hat ein paar Tausend Zeilen
MAX_FILE_ACCOUNTS = 40  # Girokonto, Pockets, Sparkonten – mehr ist kein Kontoauszug
DEMO_MAX_TX = 3000      # Demo: Obergrenze für alle Buchungen zusammen (die erfundenen sind gut 400)
# Feste Kategorien (Reihenfolge = Anzeige). „Umbuchung“ zählt nie (eigene Konten, Kreditkartenabrechnung, Sparen),
# „Einnahmen“ ist die Gegenseite; alles andere sind Ausgaben (Gutschriften eines Händlers ziehen sich dort ab).
CATEGORIES = ["Wohnen & Nebenkosten", "Lebensmittel", "Mobilität", "Versicherungen", "Kinder", "Kleidung",
              "Gesundheit", "Haushalt", "Freizeit & Ausgehen", "Abos", "Online-Shopping", "Bargeld", "Sonstiges",
              "Einnahmen", "Umbuchung"]
NOT_SPENDING = ("Einnahmen", "Umbuchung")

# Eingebaute Händler (klein geschrieben, als Wortanfang gesucht) – eigene Regeln gehen vor
_BUILTIN = [
    ("Umbuchung", r"umbuchung|pocket|unterkonto|übertrag|uebertrag|eigenes konto|eigene konten|kreditkartenabrechnung|"
                  r"kreditkarte abrechnung|tagesgeld|sparplan|sparkonto"),
    ("Bargeld", r"geldautomat|bargeldauszahlung|bargeld|atm\b|auszahlung automat"),
    ("Lebensmittel", r"rewe|edeka|aldi|lidl|netto\b|penny|kaufland|norma\b|tegut|globus|denns|alnatura|bäcker|baecker|metzger"),
    ("Kleidung", r"zalando|h&m|h \+ m|hm\.com|c&a|deichmann|uniqlo|zara|primark|vinted|about you|tk maxx|snipes|kik\b|ernsting|takko|esprit|s\.oliver|jack ?& ?jones|reno\b|humanic"),
    ("Abos", r"netflix|spotify|disney|dazn|audible|apple\.com/bill|apple(?! store| retail)\b|itunes|youtube|amazon prime|"
             r"prime video|wow tv|sky deutschland|icloud|adobe|microsoft 365|anthropic|claude\.ai|openai|chatgpt|github|"
             r"1password|dropbox|google one|google storage|patreon|paramount|rtl\+|joyn|crunchyroll|tidal|deezer"),
    ("Mobilität", r"aral|shell|esso|\bjet\b|total ?energies|\bomv\b|deutsche bahn|db vertrieb|\bdb\b|flixbus|vag\b|mvg|vgn|tier |lime |uber|bolt|parkhaus|parken|easypark|adac|tankstelle"),
    ("Versicherungen", r"versicherung|huk|allianz|ergo|axa|debeka|generali|signal iduna|devk|r\+v|cosmos direkt|haftpflicht"),
    ("Wohnen & Nebenkosten", r"miete|hausgeld|stadtwerke|n-ergie|e\.on|eon|vattenfall|enbw|strom|gas\b|wasser|rundfunk|telekom|vodafone|o2\b|1&1|congstar|grundsteuer|abfall"),
    ("Gesundheit", r"apotheke|arzt|zahnarzt|praxis|klinik|optik|fielmann|physio|docmorris|shop apotheke"),
    # Sammeltopf (18.11.2026): Händler, bei denen alles Mögliche dahintersteckt – die Buchung sagt nicht, was. Abos
    # (Amazon Prime) stehen weiter oben und gehen vor; eine Regel oder ein Beleg mit Artikeln ist genauer.
    ("Online-Shopping", r"amazon|amzn|ebay|otto\b|temu|aliexpress|shein|wish\.com|galaxus|kaufland\.de|joom|"
                        r"real\.de|cdiscount|bol\.com"),
    ("Haushalt", r"ikea|dm-drogerie|\bdm\b|rossmann|müller drog|mueller drog|drogerie müller|obi\b|bauhaus|hornbach|toom|mediamarkt|saturn|tchibo"),
    ("Kinder", r"kita|kindergarten|schule|hort|mytoys|jakoo|smyths|spielwaren|kinder"),
    ("Freizeit & Ausgehen", r"restaurant|pizzeria|cafe|café|kino|cinemaxx|cinestar|cinecitta|uci\b|lieferando|wolt|eventim|"
                            r"museum|zoo\b|tiergarten|schwimmbad|biergarten|mcdonald|burger king|starbucks|steam|nintendo|"
                            r"playstation|xbox|epic games"),
]
_BUILTIN_RE = [(c, re.compile(rf"(?<![a-z0-9])(?:{p})", re.I)) for c, p in _BUILTIN]
_INCOME = re.compile(r"gehalt|lohn|bezüge|bezuege|rente|kindergeld|elterngeld|erstattung finanzamt|steuererstattung|zinsen\b|zinsgutschrift", re.I)
# Zahlungsdienste: dahinter steckt jeder Händler – der eigentliche steht im Verwendungszweck
_AGGREGATOR = re.compile(r"paypal|klarna|sumup|amazon payments|amazon eu|amzn|sofort|giropay|stripe|adyen|mollie", re.I)

# Spaltennamen der Banken (klein, ohne Satzzeichen verglichen)
_COLS = {
    "date": ["buchungsdatum", "buchungstag", "buchung", "datum", "bookingdate", "date", "transaktionsdatum", "belegdatum"],
    "valuta": ["valuta", "valutadatum", "wertstellung", "wertstellungsdatum"],
    "amount": ["betrag", "betrageur", "betragineur", "umsatz", "umsatzeur", "amount", "betragwährung", "summe"],
    "debit": ["soll", "belastung", "ausgang", "lastschrift", "debit"],
    "credit": ["haben", "gutschrift", "eingang", "credit"],
    "counterparty": ["zahlungsempfänger", "zahlungsempfaenger", "empfänger", "empfaenger", "auftraggeber",
                     "auftraggeberempfänger", "auftraggeberempfaenger", "zahlungspflichtiger", "begünstigterzahlungspflichtiger",
                     "beguenstigterzahlungspflichtiger", "namezahlungsbeteiligter", "name", "gegenpartei", "partner",
                     "empfängerauftraggeber", "zahlungsbeteiligter", "händler", "haendler", "counterparty", "payee"],
    "purpose": ["verwendungszweck", "beschreibung", "buchungstext", "description", "zweck", "details", "mitteilung"],
    "currency": ["währung", "waehrung", "currency"],
    "category": ["kategorie", "category", "unterkategorie"],
    # Art der Buchung („Pocket-Umbuchung“, „Lastschrift“, „Gutschrift“) – C24 & Co. schreiben Umbuchungen nur hierhin
    "kind": ["transaktionstyp", "transaktionsart", "umsatzart", "buchungsart", "vorgang", "typ", "art"],
    # Das eigene Konto der Zeile (C24 exportiert Girokonto, Pockets und Sparkonten in einer Datei)
    "acc_no": ["kontonummer", "kontonr", "eigenekontonummer", "eigeneskonto", "kontoiban", "eigeneiban", "konto"],
    "acc_name": ["kontoname", "kontobezeichnung", "pocketname", "pocket", "unterkonto", "kontoart"],
}


# ---------- CSV lesen ----------

def _norm_head(s: str) -> str:
    return re.sub(r"[^a-zäöüß0-9]", "", (s or "").lower())


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", s)
    try:
        if m:
            y = int(m.group(3))
            return dt.date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1))).isoformat()
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None
    return None


def parse_cents(s: str) -> Optional[int]:
    s = (s or "").strip().replace("−", "-").replace("EUR", "").replace("€", "").strip()
    if not s:
        return None
    neg = s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()").rstrip("-").strip()
    v = processing._amount(s)
    if v is None:
        return None
    cents = int(round(v * 100))
    return -abs(cents) if neg else cents


def _pick(heads: list[str], key: str) -> Optional[int]:
    names = _COLS[key]
    for name in names:                       # genaue Treffer zuerst, in der Reihenfolge der Synonyme
        for i, h in enumerate(heads):
            if h == name:
                return i
    for name in names:
        if len(name) >= 5:
            for i, h in enumerate(heads):
                if h.startswith(name):
                    return i
    return None


def parse_csv(raw: bytes, filename: str = "") -> dict:
    """→ {bank, mapping, rows: [{booked, cents, currency, counterparty, purpose, bank_category}], skipped, error}"""
    text = _decode(raw)
    lines = text.splitlines()
    sample = "\n".join(lines[:40])
    delim = max([";", ",", "\t"], key=lambda d: sample.count(d))
    table = list(csv.reader(io.StringIO(text), delimiter=delim))
    head_i, cols = None, {}
    for i, row in enumerate(table[:40]):
        heads = [_norm_head(c) for c in row]
        date_i = _pick(heads, "date")
        if date_i is None:
            date_i = _pick(heads, "valuta")
        amount_i = _pick(heads, "amount")
        debit_i, credit_i = _pick(heads, "debit"), _pick(heads, "credit")
        if date_i is not None and (amount_i is not None or (debit_i is not None and credit_i is not None)):
            head_i = i
            cols = {"date": date_i, "amount": amount_i, "debit": debit_i, "credit": credit_i,
                    "counterparty": _pick(heads, "counterparty"), "purpose": _pick(heads, "purpose"),
                    "currency": _pick(heads, "currency"), "category": _pick(heads, "category"),
                    "kind": _pick(heads, "kind")}
            # Kategorie und Unterkategorie (C24 hat mehrere Spalten) – alle mitlesen
            cat_cols = [j for j, h in enumerate(heads) if any(h.startswith(n) for n in _COLS["category"])]
            cols["acc_no"] = next((j for j, h in enumerate(heads) if h in _COLS["acc_no"]), None)
            cols["acc_name"] = next((j for j, h in enumerate(heads) if h in _COLS["acc_name"]), None)
            if cols["amount"] is not None:
                cols["debit"] = cols["credit"] = None
            break
    bank = "C24 Bank" if re.search(r"c24|check24", (filename + " " + sample).lower()) else None
    if head_i is None:
        return {"bank": bank, "mapping": {}, "rows": [], "skipped": 0,
                "error": "In der Datei fehlt eine Kopfzeile mit Datum und Betrag – ist es der CSV-Export der Umsätze?"}
    header = table[head_i]
    if cols.get("acc_no") is None:
        cols["acc_no"], name_i = _account_columns(table[head_i + 1:], {v for v in cols.values() if v is not None} | set(cat_cols))
        if cols.get("acc_name") is None:
            cols["acc_name"] = name_i
    mapping = {k: header[v].strip() or f"Spalte {v + 1}" for k, v in cols.items() if v is not None}
    if cols["amount"] is None:
        mapping["amount"] = f"{header[cols['debit']].strip()} / {header[cols['credit']].strip()}"
    rows, skipped = [], 0
    for row in table[head_i + 1:]:
        if not any(c.strip() for c in row):
            continue
        get = lambda k: row[cols[k]].strip() if cols.get(k) is not None and cols[k] < len(row) else ""  # noqa: E731
        booked = parse_date(get("date"))
        if cols["amount"] is not None:
            cents = parse_cents(get("amount"))
        else:
            d, c = parse_cents(get("debit")), parse_cents(get("credit"))
            cents = (c or 0) - abs(d or 0) if (d is not None or c is not None) else None
        if not booked or cents is None:
            skipped += 1
            continue
        rows.append({"booked": booked, "cents": cents, "currency": (get("currency") or "EUR").upper()[:3],
                     "counterparty": re.sub(r"\s+", " ", get("counterparty"))[:200],
                     "purpose": re.sub(r"\s+", " ", get("purpose"))[:400],
                     "bank_category": " / ".join(row[j].strip() for j in cat_cols if j < len(row) and row[j].strip())[:120],
                     "kind": get("kind")[:60],
                     "acc_no": re.sub(r"\s+", "", get("acc_no"))[:40], "acc_name": get("acc_name")[:80]})
    return {"bank": bank, "mapping": mapping, "rows": rows, "skipped": skipped, "error": None}


def _account_columns(data: list[list[str]], used: set[int]) -> tuple[Optional[int], Optional[int]]:
    """Ohne passende Überschrift: eine Spalte mit Kontonummern (8–12 Ziffern, in fast jeder Zeile, wenige
    verschiedene) ist das eigene Konto; die Spalte dahinter mit ebenso wenigen Werten sein Name."""
    rows = [r for r in data if any(c.strip() for c in r)][:2000]
    if len(rows) < 2:
        return None, None
    width = max(len(r) for r in rows)
    def stats(j):
        vals = [r[j].strip() for r in rows if j < len(r)]
        filled = [v for v in vals if v]
        return vals, filled, set(filled)
    for j in range(width):
        if j in used:
            continue
        vals, filled, distinct = stats(j)
        if len(filled) >= 0.9 * len(rows) and 1 <= len(distinct) <= 40 and all(re.fullmatch(r"\d{8,12}", v) for v in filled):
            k = j + 1
            if k < width and k not in used:
                _, nfilled, ndistinct = stats(k)
                if len(nfilled) >= 0.9 * len(rows) and len(ndistinct) <= 40 and not all(re.fullmatch(r"[\d.,\s€-]+", v) for v in nfilled):
                    return j, k
            return j, None
    return None, None


def _fingerprints(rows: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        base = "|".join([r["booked"], str(r["cents"]), (r["counterparty"] or "").lower().strip(),
                         re.sub(r"\s+", " ", (r["purpose"] or "").lower()).strip()])
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(hashlib.sha256(f"{base}|{n}".encode()).hexdigest()[:32])
    return out


# ---------- Einsortieren ----------

def merchant_key(counterparty: str, purpose: str) -> Optional[str]:
    """Woran sich eine Regel festmacht: der Händler – bei PayPal & Co. der aus dem Verwendungszweck."""
    # Vorsätze der Kartenabwickler („WL *STEAM PURCHASE“, „SQ *Café“, „SUMUP *Flohmarkt“) gehören nicht zum Händler
    cp = re.sub(r"^\s*(?:wl|sq|sumup|zettle_?|pp|ec|vpay)\s*\*\s*", "", (counterparty or "").lower())
    if _AGGREGATOR.search(cp) or not cp.strip():
        m = re.search(r"(?:einkauf bei|bestellung bei|bei)\s+([a-z0-9][\w&.\-]{2,})", (purpose or "").lower())
        if not m:
            m = re.search(r"\.\s+([a-z][\w&\-]{2,})", (purpose or "").lower())
        key = m.group(1).strip(".,") if m else None
        return None if not key or key in _GENERIC else key
    words = re.findall(r"[a-zäöüß0-9&][\w&\-]*", cp)
    words = [w for w in words if len(w) >= 2 and w not in ("gmbh", "ag", "se", "kg", "co", "eu", "de", "sa", "s", "r", "l",
                                                           "purchase", "payment", "zahlung")]
    return " ".join(words[:2]) if words else None


_GENERIC = {"privat", "ihnen", "ihrem", "ihrer", "einem", "einer", "kunde", "kunden", "zahlung", "rechnung", "bestellung",
            "online", "shop", "europe", "payment", "pp", "der", "die", "das", "und"}


_TRANSFER = _BUILTIN_RE[0][1]
BANKCAT = "kat:"   # Regel-Präfix: gilt für die Kategorie der Bank statt für Händler/Zweck


def categorize(row: dict, rules: list[tuple[str, str]]) -> tuple[Optional[str], Optional[str]]:
    """→ (Kategorie, Quelle rule|builtin|bank) oder (None, None).
    Reihenfolge: eigene Regeln; Umbuchung (auch aus Buchungsart und Bankkategorie – Pocket-Überträge stehen oft nur
    dort); eindeutige Einnahmen; Händler; Kategorie der Bank. Andere Gutschriften bleiben offen zum Einsortieren –
    lieber einmal fragen als Geld zwischen eigenen Konten als „Einnahme“ zählen."""
    text = f"{row.get('counterparty') or ''} {row.get('purpose') or ''}".lower()
    meta = f"{row.get('kind') or ''} {row.get('bank_category') or ''}".lower()
    bankcat = (row.get("bank_category") or "").strip().lower()
    for pattern, cat in rules:
        if pattern and not pattern.startswith(BANKCAT) and pattern.lower() in text:
            return cat, "rule"
    if _TRANSFER.search(meta) or _TRANSFER.search(text):
        return "Umbuchung", "builtin"
    # Regel auf die Kategorie der Bank („kat:lebensmittel / supermarkt“): gleiche Bankkategorie, gleiche Zuordnung
    for pattern, cat in rules:
        if pattern.startswith(BANKCAT) and bankcat and pattern[len(BANKCAT):] == bankcat:
            return cat, "rule"
    if row["cents"] > 0 and _INCOME.search(text):
        return "Einnahmen", "builtin"
    for cat, rx in _BUILTIN_RE[1:]:
        if rx.search(text):
            # Gutschrift eines Händlers (Rücksendung) bleibt in seiner Kategorie und zieht sich dort ab
            return cat, "builtin"
    bank = (row.get("bank_category") or "").lower()
    for cat in CATEGORIES:
        if bank and cat.split(" ")[0].lower() in bank and not (cat == "Einnahmen" and row["cents"] < 0):
            return cat, "bank"
    return None, None


def recategorize(con, account_ids: list[int]) -> int:
    """Neu einsortieren, was nicht von Hand oder per Regel sortiert ist (nach besseren eingebauten Händlern)."""
    n = 0
    for aid in account_ids:
        acc = con.execute("SELECT owner_id FROM bank_accounts WHERE id=?", (aid,)).fetchone()
        if not acc:
            continue
        members = [acc[0], *[r[0] for r in con.execute("SELECT user_id FROM bank_access WHERE account_id=?", (aid,))]]
        rules = _rules_for(con, members)
        for r in con.execute("SELECT id, cents, counterparty, purpose, kind, bank_category, category FROM bank_tx "
                             "WHERE account_id=? AND COALESCE(cat_source, '') NOT IN ('user', 'rule')", (aid,)).fetchall():
            row = {"cents": r[1], "counterparty": r[2], "purpose": r[3], "kind": r[4], "bank_category": r[5]}
            cat, src = categorize(row, rules)
            if cat and cat != r[6]:
                con.execute("UPDATE bank_tx SET category=?, cat_source=? WHERE id=?", (cat, src, r[0]))
                n += 1
    return n


def pair_transfers(con, account_ids: list[int]) -> int:
    """Hin und zurück: derselbe Betrag am selben oder nächsten Tag einmal ab, einmal gut – in Konten desselben
    Haushalts, beide nicht von Hand einsortiert → beide Umbuchung (Pockets, Tagesgeld ohne Stichwort)."""
    if len(account_ids) < 2:
        return 0
    # Linear statt Selbst-Join: viele gleiche Beträge (präparierte Datei) ergäben sonst Millionen Paare.
    # Regel wie bisher: gleicher Betrag mit umgekehrtem Vorzeichen, anderes Konto, höchstens einen Tag auseinander,
    # beide nicht von Hand oder per Regel einsortiert; jede Buchung höchstens einmal, in fester Reihenfolge.
    marks = ",".join("?" * len(account_ids))
    rows = con.execute(f"SELECT id, account_id, cents, booked FROM bank_tx WHERE account_id IN ({marks}) AND cents <> 0 "
                       f"AND COALESCE(cat_source, '') NOT IN ('user', 'rule') ORDER BY booked, id", account_ids).fetchall()
    incoming: dict[tuple, dict[int, deque]] = defaultdict(lambda: defaultdict(deque))
    for r in rows:
        if r[2] > 0:
            incoming[(r[2], r[3])][r[1]].append(r[0])
    n = 0
    for tx_id, acc, cents, booked in (r for r in rows if r[2] < 0):
        d = dt.date.fromisoformat(booked[:10])
        hit = None
        for day in (d, d - dt.timedelta(days=1), d + dt.timedelta(days=1)):
            by_acc = incoming.get((-cents, day.isoformat()))
            if not by_acc:
                continue
            other = next((a for a in sorted(by_acc) if a != acc and by_acc[a]), None)
            if other is not None:
                hit = by_acc[other].popleft()
                break
        if hit is not None:
            n += con.execute("UPDATE bank_tx SET category='Umbuchung', cat_source='builtin' WHERE id IN (?, ?)",
                             (tx_id, hit)).rowcount
    return n


def _rules_for(con, user_ids: list[int]) -> list[tuple[str, str]]:
    marks = ",".join("?" * len(user_ids)) or "NULL"
    return [(r["pattern"], r["category"]) for r in con.execute(
        f"SELECT pattern, category FROM bank_rules WHERE created_by IN ({marks}) ORDER BY length(pattern) DESC",
        user_ids)]


# ---------- Rechte ----------

def visible_accounts(con, user: User) -> list[int]:
    if user.id == 0:
        return [r[0] for r in con.execute("SELECT id FROM bank_accounts")]
    return [r[0] for r in con.execute(
        "SELECT id FROM bank_accounts a WHERE a.owner_id=? OR EXISTS (SELECT 1 FROM bank_access x "
        "WHERE x.account_id=a.id AND x.user_id=?)", (user.id, user.id))]


def _account(con, user: User, account_id: int, own: bool = False):
    row = con.execute("SELECT * FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
    if not row or account_id not in visible_accounts(con, user):
        raise HTTPException(404, "Konto nicht gefunden")
    if own and row["owner_id"] != user.id and user.id != 0:
        raise HTTPException(403, "Nur wer das Konto angelegt hat, kann das")
    return row


def _people(con, user: User) -> list[dict]:
    """Wer ein Konto mitsehen kann: die Leute, mit denen ich Dokumente teile (in eine Richtung reicht)."""
    ids = {r[0] for r in con.execute("SELECT to_user FROM partner_shares WHERE from_user=? UNION "
                                     "SELECT from_user FROM partner_shares WHERE to_user=?", (user.id, user.id))}
    ids.discard(user.id)
    marks = ",".join("?" * len(ids)) or "NULL"
    return [{"id": r["id"], "name": r["display_name"]} for r in
            con.execute(f"SELECT id, display_name FROM users WHERE id IN ({marks}) ORDER BY display_name", list(ids))]


def _tx_where(con, user: User, account: Optional[int], date_from: Optional[str], date_to: Optional[str]):
    accs = visible_accounts(con, user)
    if account is not None:
        accs = [a for a in accs if a == account]
    if not accs:
        return None
    where = [f"t.account_id IN ({','.join('?' * len(accs))})"]
    params: list = list(accs)
    for v, op in ((date_from, ">="), (date_to, "<=")):
        if v:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise HTTPException(400, "Datum als JJJJ-MM-TT angeben")
            where.append(f"t.booked {op} ?")
            params.append(v)
    return where, params


def tx_out(con, r, user: User) -> dict:
    d = {k: r[k] for k in ("id", "account_id", "booked", "cents", "currency", "counterparty", "purpose",
                           "category", "cat_source")}
    # Beleg nur nennen, wenn ich ihn auch sehen darf (Konto mitsehen heißt nicht, die Dokumente zu sehen)
    d["document_id"] = None
    if r["document_id"]:
        doc = con.execute("SELECT owner_id FROM documents WHERE id=? AND deleted_at IS NULL", (r["document_id"],)).fetchone()
        if doc and doc["owner_id"] in auth.visible_owner_ids(con, user, "all"):
            d["document_id"] = r["document_id"]
    d["amount"] = r["cents"] / 100
    d["key"] = merchant_key(r["counterparty"], r["purpose"])
    d["bank_category"] = r["bank_category"] if "bank_category" in r.keys() else None
    return d


# ---------- Endpunkte ----------

@router.get("/accounts")
def accounts(user: User = Depends(current_user)):
    with connect() as con:
        out = []
        for i in visible_accounts(con, user):
            a = con.execute("SELECT * FROM bank_accounts WHERE id=?", (i,)).fetchone()
            s = con.execute("SELECT COUNT(*) AS n, MIN(booked) AS first, MAX(booked) AS last FROM bank_tx WHERE account_id=?",
                            (i,)).fetchone()
            out.append({"id": a["id"], "name": a["name"], "bank": a["bank"], "is_own": a["owner_id"] == user.id,
                        "count": s["n"], "first": s["first"], "last": s["last"],
                        "visible_to": [r[0] for r in con.execute("SELECT user_id FROM bank_access WHERE account_id=?", (i,))]})
        return {"accounts": out, "people": _people(con, user), "categories": CATEGORIES}


class AccountIn(BaseModel):
    name: Optional[str] = None
    visible_to: Optional[list[int]] = None


def _set_access(con, user: User, account_id: int, ids: list[int]) -> None:
    allowed = {p["id"] for p in _people(con, user)}
    con.execute("DELETE FROM bank_access WHERE account_id=?", (account_id,))
    for i in ids:
        if i in allowed:
            con.execute("INSERT OR IGNORE INTO bank_access(account_id, user_id) VALUES (?,?)", (account_id, i))


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: AccountIn, user: User = Depends(current_user)):
    with connect() as con:
        _account(con, user, account_id, own=True)
        if body.name is not None and body.name.strip():
            con.execute("UPDATE bank_accounts SET name=? WHERE id=?", (body.name.strip()[:80], account_id))
        if body.visible_to is not None:
            _set_access(con, user, account_id, body.visible_to)
    return {"ok": True}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, user: User = Depends(current_user)):
    with connect() as con:
        _account(con, user, account_id, own=True)
        con.execute("DELETE FROM bank_tx WHERE account_id=?", (account_id,))
        con.execute("DELETE FROM bank_imports WHERE account_id=?", (account_id,))
        con.execute("DELETE FROM bank_access WHERE account_id=?", (account_id,))
        con.execute("DELETE FROM bank_accounts WHERE id=?", (account_id,))
    return {"ok": True}


def _num_hash(no: str) -> str:
    return hashlib.sha256(f"papigator-konto|{no}".encode()).hexdigest()[:32]


@router.post("/import")
def import_csv(file: UploadFile = File(...), preview: bool = Form(False), account_id: Optional[int] = Form(None),
                     account_name: Optional[str] = Form(None), visible_to: Optional[str] = Form(None),
                     shares: Optional[str] = Form(None), user: User = Depends(current_user)):
    """preview=true: nur lesen und zeigen (Spalten, Konten, neu/schon da, Vorschläge); sonst importieren.

    Nennt die Datei das eigene Konto je Zeile (C24: Girokonto, Pockets, Sparkonto in einer Datei), wird je Konto
    importiert: ein Konto, das der Importierende schon sieht (gleiche Nummer), wird ergänzt – so laufen die Exporte
    beider Partner zusammen, ohne Doppelte; neue Konten legt er an, je Konto geteilt oder privat (`shares`:
    JSON {Schlüssel: true/false}). Kontonummern stehen nur als Hash und die letzten vier Ziffern in der DB."""
    raw = file.file.read(MAX_CSV + 1)
    if len(raw) > MAX_CSV:
        raise HTTPException(413, "Die Datei ist größer als 5 MB")
    parsed = parse_csv(raw, file.filename or "")
    if parsed["error"]:
        raise HTTPException(400, parsed["error"])
    if not parsed["rows"]:
        raise HTTPException(400, "Keine Umsätze in der Datei gefunden")
    rows = parsed["rows"]
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"Mehr als {MAX_ROWS} Umsätze in einer Datei – bitte in kleineren Zeiträumen exportieren")
    if len({r.get("acc_no") for r in rows if r.get("acc_no")}) > MAX_FILE_ACCOUNTS:
        raise HTTPException(400, f"Mehr als {MAX_FILE_ACCOUNTS} Konten in einer Datei")
    fps = _fingerprints(rows)
    people = [int(x) for x in (visible_to or "").split(",") if x.strip().isdigit()]
    try:
        share_map = json.loads(shares) if shares else {}
    except ValueError:
        raise HTTPException(400, "shares: JSON erwartet")
    multi = any(r.get("acc_no") for r in rows)
    with connect() as con:
        mine = set(visible_accounts(con, user))
        # Gruppen: je eigenem Konto der Zeile – oder alles in ein Konto (gewählt oder neu)
        groups: dict[str, dict] = {}
        for r, fp in zip(rows, fps):
            no = r.get("acc_no") or "" if multi else ""
            g = groups.setdefault(no, {"no": no, "name": r.get("acc_name") or "", "rows": []})
            g["rows"].append((r, fp))
            if not g["name"] and r.get("acc_name"):
                g["name"] = r["acc_name"]
        for no, g in groups.items():
            g["key"] = _num_hash(no)[:12] if no else "main"
            if multi and no:
                hit = con.execute(f"SELECT * FROM bank_accounts WHERE number_hash=? AND id IN "
                                  f"({','.join('?' * len(mine)) or 'NULL'}) ORDER BY id LIMIT 1",
                                  (_num_hash(no), *mine)).fetchone()
                g["account"] = hit
            elif account_id is not None:
                g["account"] = _account(con, user, account_id, own=True)
            else:
                g["account"] = None
            acc = g["account"]
            g["have"] = {x[0] for x in con.execute("SELECT fp FROM bank_tx WHERE account_id=?", (acc["id"],))} if acc else set()
            members = ([acc["owner_id"], *[x[0] for x in con.execute("SELECT user_id FROM bank_access WHERE account_id=?",
                                                                     (acc["id"],))]] if acc else [user.id, *people])
            rules = _rules_for(con, members)
            g["new"] = [(r, fp) for r, fp in g["rows"] if fp not in g["have"]]
            g["cats"] = [categorize(r, rules) for r, _ in g["new"]]
        all_new = [x for g in groups.values() for x in g["new"]]
        all_cats = [c for g in groups.values() for c in g["cats"]]
        summary = {"bank": parsed["bank"], "mapping": parsed["mapping"], "count": len(rows), "new": len(all_new),
                   "duplicates": len(rows) - len(all_new), "skipped": parsed["skipped"],
                   "first": min(r["booked"] for r in rows), "last": max(r["booked"] for r in rows),
                   "to_sort": sum(1 for c, _ in all_cats if not c)}
        if multi:
            summary["accounts"] = [{
                "key": g["key"], "name": (g["account"]["name"] if g["account"] else g["name"]) or f"Konto ··{g['no'][-4:]}",
                "tail": g["no"][-4:], "count": len(g["rows"]), "new": len(g["new"]),
                "existing": bool(g["account"]), "shared_default": True} for g in groups.values()]
        if config.DEMO and not preview:
            have = con.execute("SELECT COUNT(*) FROM bank_tx").fetchone()[0]
            if have + len(all_new) > DEMO_MAX_TX:
                raise HTTPException(403, f"In der Demo höchstens {DEMO_MAX_TX} Buchungen insgesamt")
        if preview:
            summary["rows"] = [{**r, "amount": r["cents"] / 100, "category": c, "cat_source": s_}
                               for (r, _), (c, s_) in list(zip(all_new, all_cats))[:8]]
            return summary
        batch = hashlib.sha256(f"{user.id}|{dt.datetime.now().isoformat()}".encode()).hexdigest()[:16]
        touched, first_imp = [], None
        for no, g in groups.items():
            acc = g["account"]
            if acc is None:
                name = (g["name"] or (account_name or "").strip() or (f"Konto ··{no[-4:]}" if no else parsed["bank"] or "Konto"))
                if not multi and (account_name or "").strip():
                    name = account_name.strip()
                aid = con.execute("INSERT INTO bank_accounts(name, bank, owner_id, number_hash, number_tail) VALUES (?,?,?,?,?)",
                                  (name[:80], parsed["bank"], user.id, _num_hash(no) if no else None, no[-4:] if no else None)).lastrowid
                _set_access(con, user, aid, people if share_map.get(g["key"], True) else [])
            else:
                aid = acc["id"]
            if not g["new"]:
                touched.append(aid)
                continue
            imp = con.execute("INSERT INTO bank_imports(account_id, filename, created_by, count, first, last, batch) "
                              "VALUES (?,?,?,?,?,?,?)", (aid, (file.filename or "")[:120], user.id, len(g["new"]),
                                                         min(r["booked"] for r, _ in g["new"]),
                                                         max(r["booked"] for r, _ in g["new"]), batch)).lastrowid
            first_imp = first_imp or imp
            for (r, fp), (c, s_) in zip(g["new"], g["cats"]):
                con.execute("INSERT OR IGNORE INTO bank_tx(account_id, import_id, booked, cents, currency, counterparty, "
                            "purpose, category, cat_source, fp, kind, bank_category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (aid, imp, r["booked"], r["cents"], r["currency"], r["counterparty"], r["purpose"],
                             c, s_, fp, r.get("kind") or None, r.get("bank_category") or None))
            link_receipts(con, aid)
            touched.append(aid)
        summary["transfers"] = pair_transfers(con, sorted(set(visible_accounts(con, user))))
        summary.update(account_id=touched[0] if touched else None, import_id=first_imp, accounts_touched=touched)
    if summary["to_sort"]:
        for aid in touched:
            threading.Thread(target=suggest_categories, args=(aid,), daemon=True).start()
    return summary


@router.delete("/imports/{import_id}")
def undo_import(import_id: int, user: User = Depends(current_user)):
    """Einen Import zurücknehmen (falsche Datei, falsches Konto)."""
    with connect() as con:
        row = con.execute("SELECT account_id, batch, created_by FROM bank_imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Import nicht gefunden")
        # ganzer Import (alle Konten aus einer Datei); zurücknehmen darf, wer importiert hat, oder der Kontoinhaber
        ids = [r[0] for r in con.execute("SELECT id FROM bank_imports WHERE batch=? AND created_by=?",
                                         (row["batch"], row["created_by"]))] if row["batch"] else [import_id]
        n = 0
        for i in ids:
            acc_id = con.execute("SELECT account_id FROM bank_imports WHERE id=?", (i,)).fetchone()[0]
            a = _account(con, user, acc_id)
            if row["created_by"] != user.id and a["owner_id"] != user.id:
                raise HTTPException(403, "Zurücknehmen kann nur, wer importiert hat")
            n += con.execute("DELETE FROM bank_tx WHERE import_id=?", (i,)).rowcount
            con.execute("DELETE FROM bank_imports WHERE id=?", (i,))
        # leer gewordene, eben angelegte Konten wieder weg
        con.execute("DELETE FROM bank_accounts WHERE owner_id=? AND id NOT IN (SELECT DISTINCT account_id FROM bank_tx) "
                    "AND id NOT IN (SELECT DISTINCT account_id FROM bank_imports)", (user.id,))
    return {"ok": True, "removed": n}


_linked_at: dict[int, float] = {}


def _relink(account_ids: list[int]) -> None:
    try:
        with connect() as con:
            for a in account_ids:
                link_receipts(con, a, recent_only=True)
    except Exception:
        log.exception("Geld: Belege nachverknüpfen fehlgeschlagen")


@router.get("/summary")
def summary(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
            account: Optional[int] = None, user: User = Depends(current_user)):
    import time
    with connect() as con:
        # Belege kommen oft nach dem Import (Scan, Mail): höchstens alle 10 Minuten je Konto nachsehen – im
        # Hintergrund, damit das Öffnen von „Geld“ nicht darauf wartet
        due = [a for a in visible_accounts(con, user) if time.monotonic() - _linked_at.get(a, -1e9) > 600]
        for a in due:
            _linked_at[a] = time.monotonic()
        if due:
            threading.Thread(target=_relink, args=(due,), daemon=True).start()
        return summary_data(con, user, date_from, date_to, account)


def summary_data(con, user: User, date_from=None, date_to=None, account=None) -> dict:
    w = _tx_where(con, user, account, date_from, date_to)
    empty = {"covered": None, "spent": 0, "income": 0, "categories": [], "months": [], "to_sort": 0, "with_doc": 0,
             "count": 0, "out_count": 0}
    if not w:
        return empty
    where, params = w
    q = " AND ".join(where)
    # Abgedeckt: alle Umsätze der Konten, nicht nur die im Zeitraum – sonst hieße jeder Monatsanfang ohne Buchung
    # „es fehlen Umsätze“
    all_w = _tx_where(con, user, account, None, None)
    cov = con.execute(f"SELECT MIN(booked), MAX(booked) FROM bank_tx t WHERE {' AND '.join(all_w[0])}", all_w[1]).fetchone()
    n = con.execute(f"SELECT COUNT(*) FROM bank_tx t WHERE {q}", params).fetchone()[0]
    if not cov[0]:
        return empty
    if not n:
        return {**empty, "covered": {"first": cov[0], "last": cov[1]}}
    ns = ",".join("?" * len(NOT_SPENDING))
    cats = [{"name": r[0] or "Nicht einsortiert", "cents": -r[1], "count": r[2]} for r in con.execute(
        f"SELECT category, SUM(cents), COUNT(*) FROM bank_tx t WHERE {q} AND COALESCE(category, '') NOT IN ({ns}) "
        f"GROUP BY category", (*params, *NOT_SPENDING))]
    cats = [c for c in cats if c["cents"] > 0 or c["name"] == "Nicht einsortiert"]
    cats.sort(key=lambda c: -c["cents"])
    income = con.execute(f"SELECT COALESCE(SUM(cents), 0) FROM bank_tx t WHERE {q} AND category='Einnahmen'",
                         params).fetchone()[0]
    months = [{"month": r[0], "cents": -r[1]} for r in con.execute(
        f"SELECT substr(booked, 1, 7), SUM(cents) FROM bank_tx t WHERE {q} AND COALESCE(category, '') NOT IN ({ns}) "
        f"GROUP BY 1 ORDER BY 1", (*params, *NOT_SPENDING))]
    to_sort = con.execute(f"SELECT COUNT(*) FROM bank_tx t WHERE {q} AND (category IS NULL OR cat_source='ai')",
                          params).fetchone()[0]
    with_doc = con.execute(f"SELECT COUNT(*) FROM bank_tx t WHERE {q} AND document_id IS NOT NULL AND cents < 0",
                           params).fetchone()[0]
    out_n = con.execute(f"SELECT COUNT(*) FROM bank_tx t WHERE {q} AND cents < 0", params).fetchone()[0]
    return {"covered": {"first": cov[0], "last": cov[1]}, "spent": sum(c["cents"] for c in cats), "income": income,
            "categories": cats, "months": months, "to_sort": to_sort, "with_doc": with_doc, "count": n,
            "out_count": out_n}


@router.get("/category")
def category(name: str, date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
             account: Optional[int] = None, user: User = Depends(current_user)):
    with connect() as con:
        w = _tx_where(con, user, account, date_from, date_to)
        if not w:
            return {"name": name, "cents": 0, "months": [], "transactions": [], "top": None}
        where, params = w
        cond = "t.category IS NULL" if name == "Nicht einsortiert" else "t.category = ?"
        args = params if name == "Nicht einsortiert" else [*params, name]
        q = " AND ".join(where) + " AND " + cond
        rows = con.execute(f"SELECT t.* FROM bank_tx t WHERE {q} ORDER BY t.booked DESC, t.id DESC LIMIT 500", args).fetchall()
        sign = 1 if name == "Einnahmen" else -1
        months: dict[str, int] = {}
        top: dict[str, int] = {}
        for r in rows:
            months[r["booked"][:7]] = months.get(r["booked"][:7], 0) + sign * r["cents"]
            k = merchant_key(r["counterparty"], r["purpose"]) or (r["counterparty"] or "?")
            top[k] = top.get(k, 0) + sign * r["cents"]
        best = max(top.items(), key=lambda kv: kv[1]) if top else None
        return {"name": name, "cents": sum(months.values()), "count": len(rows),
                "months": [{"month": m, "cents": c} for m, c in sorted(months.items())],
                "top": {"name": best[0], "cents": best[1]} if best else None,
                "transactions": [tx_out(con, r, user) for r in rows]}


@router.get("/to_sort")
def to_sort(user: User = Depends(current_user)):
    """Zum Einsortieren: ohne Kategorie oder nur KI-Vorschlag – die neuesten zuerst."""
    with connect() as con:
        w = _tx_where(con, user, None, None, None)
        if not w:
            return {"transactions": []}
        where, params = w
        rows = con.execute(f"SELECT t.* FROM bank_tx t WHERE {' AND '.join(where)} AND (t.category IS NULL OR "
                           f"t.cat_source='ai') ORDER BY t.booked DESC, t.id DESC LIMIT 200", params).fetchall()
        return {"transactions": [tx_out(con, r, user) for r in rows]}


class TxPatch(BaseModel):
    category: Optional[str] = None
    remember: Optional[str] = None     # Händler-Schlüssel: künftig (und offene) immer so einsortieren
    remember_bank: Optional[bool] = None   # alle mit derselben Kategorie der Bank (C24) künftig so einsortieren


@router.patch("/tx/{tx_id}")
def patch_tx(tx_id: int, body: TxPatch, user: User = Depends(current_user)):
    with connect() as con:
        r = con.execute("SELECT * FROM bank_tx WHERE id=?", (tx_id,)).fetchone()
        if not r or r["account_id"] not in visible_accounts(con, user):
            raise HTTPException(404, "Buchung nicht gefunden")
        if body.category not in CATEGORIES:
            raise HTTPException(400, "Unbekannte Kategorie")
        con.execute("UPDATE bank_tx SET category=?, cat_source='user' WHERE id=?", (body.category, tx_id))
        applied = 0
        key = (body.remember or "").strip().lower()
        if key:
            if len(key) < 3:
                raise HTTPException(400, "Händler zu kurz für eine Regel")
            con.execute("INSERT INTO bank_rules(created_by, pattern, category) VALUES (?,?,?) "
                        "ON CONFLICT(created_by, pattern) DO UPDATE SET category=excluded.category",
                        (user.id, key[:60], body.category))
            accs = visible_accounts(con, user)
            # alle vom selben Händler, außer einzeln von Hand sortierten
            for o in con.execute(f"SELECT id, counterparty, purpose FROM bank_tx WHERE account_id IN "
                                 f"({','.join('?' * len(accs))}) AND id <> ? AND COALESCE(cat_source, '') <> 'user'",
                                 (*accs, tx_id)).fetchall():
                if key in f"{o['counterparty'] or ''} {o['purpose'] or ''}".lower() or \
                        merchant_key(o["counterparty"], o["purpose"]) == key:
                    con.execute("UPDATE bank_tx SET category=?, cat_source='rule' WHERE id=?", (body.category, o["id"]))
                    applied += 1
        bankcat = (r["bank_category"] or "").strip().lower()
        if body.remember_bank and bankcat:
            con.execute("INSERT INTO bank_rules(created_by, pattern, category) VALUES (?,?,?) "
                        "ON CONFLICT(created_by, pattern) DO UPDATE SET category=excluded.category",
                        (user.id, (BANKCAT + bankcat)[:120], body.category))
            accs = visible_accounts(con, user)
            applied += con.execute(
                f"UPDATE bank_tx SET category=?, cat_source='rule' WHERE account_id IN ({','.join('?' * len(accs))}) "
                f"AND lower(trim(COALESCE(bank_category, ''))) = ? AND id <> ? AND "
                f"(category IS NULL OR cat_source IN ('ai', 'bank'))", (body.category, *accs, bankcat, tx_id)).rowcount
        return {**tx_out(con, con.execute("SELECT * FROM bank_tx WHERE id=?", (tx_id,)).fetchone(), user), "applied": applied}


@router.get("/rules")
def rules(user: User = Depends(current_user)):
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT id, pattern, category FROM bank_rules WHERE created_by=? "
                                             "ORDER BY pattern", (user.id,))]


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(current_user)):
    with connect() as con:
        if not con.execute("DELETE FROM bank_rules WHERE id=? AND created_by=?", (rule_id, user.id)).rowcount:
            raise HTTPException(404, "Regel nicht gefunden")
    return {"ok": True}


# ---------- Belege zu Buchungen ----------

def _overlap(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"[a-zäöüß0-9]{3,}", (a or "").lower()) if w not in _GENERIC and w not in ("gmbh", "europe")}
    wb = set(re.findall(r"[a-zäöüß0-9]{3,}", (b or "").lower()))
    return bool(wa & wb)


def link_receipts(con, account_id: int, recent_only: bool = False) -> int:
    """Buchung ↔ Beleg: gleicher Betrag, Beleg 0–14 Tage vor (bis 5 nach) der Buchung, im Bestand des Kontoinhabers –
    nur bei genau einem passenden Beleg (sonst lieber keiner als ein falscher), derselbe Absender geht vor.
    Belege einmal laden und im Speicher nach Betrag nachschlagen – früher eine Abfrage je Buchung (sekundenlang)."""
    acc = con.execute("SELECT owner_id FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        return 0
    txs = con.execute("SELECT id, booked, cents, counterparty, purpose FROM bank_tx WHERE account_id=? AND "
                      "document_id IS NULL AND cents < 0" + (" AND booked >= date('now', '-400 days')" if recent_only else ""),
                      (account_id,)).fetchall()
    if not txs:
        return 0
    lo = (dt.date.fromisoformat(min(t["booked"] for t in txs)) - dt.timedelta(days=14)).isoformat()
    hi = (dt.date.fromisoformat(max(t["booked"] for t in txs)) + dt.timedelta(days=5)).isoformat()
    used = {r[0] for r in con.execute("SELECT document_id FROM bank_tx WHERE document_id IS NOT NULL")}
    by_cents: dict[int, list] = {}
    for d in con.execute("SELECT id, sender, amount, substr(document_date, 1, 10) AS d FROM documents WHERE owner_id=? "
                         "AND deleted_at IS NULL AND ext_pending=0 AND amount IS NOT NULL AND document_date BETWEEN ? AND ?",
                         (acc["owner_id"], lo, hi)):
        if d["id"] not in used:
            by_cents.setdefault(int(round(d["amount"] * 100)), []).append(d)
    n = 0
    for t in txs:
        cands = by_cents.get(-t["cents"])
        if not cands:
            continue
        b = dt.date.fromisoformat(t["booked"])
        lo_d, hi_d = (b - dt.timedelta(days=14)).isoformat(), (b + dt.timedelta(days=5)).isoformat()
        cands = [c for c in cands if lo_d <= c["d"] <= hi_d and c["id"] not in used]
        # derselbe Absender geht vor; ein Beleg eines anderen Absenders nie
        who = f"{t['counterparty'] or ''} {t['purpose'] or ''}"
        same = [c for c in cands if c["sender"] and _overlap(c["sender"], who)]
        hits = same or [c for c in cands if not c["sender"]]
        if len(hits) == 1:
            con.execute("UPDATE bank_tx SET document_id=? WHERE id=?", (hits[0]["id"], t["id"]))
            used.add(hits[0]["id"])
            n += 1
    return n


# ---------- KI-Vorschläge (optional, wie beim Erkennen freigegeben) ----------

_SUGGEST_PROMPT = """Ordne jede Kontobuchung genau einer Kategorie zu. Kategorien: {cats}.
Antworte NUR mit einem JSON-Objekt {{"<Nummer>": "<Kategorie>"}}, ohne Erklärung. Unsicher: "Sonstiges".
Buchungen:
{rows}"""


def suggest_categories(account_id: int, limit: int = 40) -> int:
    """Offene Buchungen eines Kontos von der KI vorschlagen lassen (cat_source='ai' = Vorschlag zum Bestätigen).
    KI aus, pausiert oder nicht erreichbar: nichts – die Buchungen bleiben zum Einsortieren."""
    from . import worker
    # Zweck „bank“: kein Cloud-Server ist dafür freigegeben – Kontoumsätze gehen nur an die KI im Heimnetz
    if not llm.usable("bank") or worker.paused_until():
        return 0
    with connect() as con:
        rows = con.execute("SELECT id, cents, counterparty, purpose FROM bank_tx WHERE account_id=? AND category IS NULL "
                           "ORDER BY booked DESC LIMIT ?", (account_id, limit)).fetchall()
    if not rows:
        return 0
    lines = "\n".join(f"{i}: {r['counterparty'] or ''} | {(r['purpose'] or '')[:80]} | {r['cents'] / 100:.2f} EUR"
                      for i, r in enumerate(rows))
    spend = [c for c in CATEGORIES if c != "Einnahmen"]
    try:
        text = llm.chat(_SUGGEST_PROMPT.format(cats=", ".join(spend), rows=lines), timeout=240, purpose="bank") or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:   # KI weg oder Unsinn: nicht schlimm
        log.info("Geld: KI-Vorschläge nicht möglich (%s)", type(e).__name__)
        return 0
    n = 0
    with connect() as con:
        for k, cat in data.items():
            if str(k).isdigit() and int(k) < len(rows) and cat in spend:
                n += con.execute("UPDATE bank_tx SET category=?, cat_source='ai' WHERE id=? AND category IS NULL",
                                 (cat, rows[int(k)]["id"])).rowcount
    log.info("Geld: %s KI-Vorschläge für Konto %s", n, account_id)
    return n


# ---------- Aus dem Konto erkannt ----------

def insights(con, user: User, today: Optional[dt.date] = None) -> list[dict]:
    """Regelmäßige Abbuchungen (mind. 3, gleichmäßiger Abstand) je Händler aus dem letzten gut einem Jahr: teurer
    geworden (stabil ≥ 3 %, wie bei den Belegen) oder neu (erste Abbuchung in den letzten 120 Tagen, kein Beleg
    vom selben Absender im Archiv)."""
    from . import extras
    today = today or dt.date.today()
    w = _tx_where(con, user, None, (today - dt.timedelta(days=430)).isoformat(), None)
    if not w:
        return []
    where, params = w
    groups: dict[str, list] = {}
    for r in con.execute(f"SELECT t.* FROM bank_tx t WHERE {' AND '.join(where)} AND t.cents < 0 AND "
                         f"COALESCE(t.category, '') NOT IN ('Umbuchung', 'Bargeld') ORDER BY t.booked, t.id", params):
        k = merchant_key(r["counterparty"], r["purpose"])
        if k:
            groups.setdefault(k, []).append(r)
    from . import auth as _auth
    owners = _auth.visible_owner_ids(con, user, "all") if user.id != 0 else []
    out = []
    for k, rows in groups.items():
        # Einmaliges beim selben Empfänger (Nebenkosten-Nachzahlung neben der Miete, Reparatur neben dem Abo) gehört
        # nicht zur Reihe – sonst „+606 % teurer“. Weg fällt nur, was weit vom Üblichen liegt und sich nicht wiederholt;
        # ein neuer Preis (Einführungspreis vorbei) kommt jeden Monat wieder und bleibt
        amounts = sorted(-r["cents"] for r in rows)
        usual = amounts[len(amounts) // 2]

        def _one_off(r):
            a = -r["cents"]
            near = bisect.bisect_right(amounts, a + a * 0.03) - bisect.bisect_left(amounts, a - a * 0.03)
            return abs(a - usual) > usual / 2 and near == 1
        rows = [r for r in rows if not _one_off(r)]
        if len(rows) < 3:
            continue
        days = [dt.date.fromisoformat(r["booked"]) for r in rows]
        gaps = sorted((b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0)
        gap = gaps[len(gaps) // 2] if gaps else 0
        interval = "monatlich" if 25 <= gap <= 35 else "vierteljährlich" if 80 <= gap <= 100 else \
            "jährlich" if 350 <= gap <= 380 else None
        if not interval:
            continue
        name = rows[-1]["counterparty"] or k
        pays = [{"amount": -r["cents"] / 100, "d": r["booked"], "doc_kind": None, "id": r["id"]} for r in rows]
        inc = extras._increase(pays, interval)
        if inc and inc["since"] >= (today - dt.timedelta(days=180)).isoformat():
            out.append({"type": "price", "key": k, "name": name, "from": inc["from"], "to": inc["to"],
                        "since": inc["since"], "percent": inc["percent"], "interval": interval})
            continue
        if rows[0]["booked"] >= (today - dt.timedelta(days=120)).isoformat() and owners:
            marks = ",".join("?" * len(owners))
            known = con.execute(f"SELECT 1 FROM documents WHERE owner_id IN ({marks}) AND deleted_at IS NULL AND "
                                f"lower(COALESCE(sender, '')) LIKE ? LIMIT 1", (*owners, f"%{k.split()[0]}%")).fetchone()
            if not known:
                out.append({"type": "new", "key": k, "name": name, "to": round(-rows[-1]["cents"] / 100, 2),
                            "since": rows[0]["booked"], "interval": interval})
    out.sort(key=lambda i: i["since"], reverse=True)
    return out


@router.get("/insights")
def get_insights(user: User = Depends(current_user)):
    with connect() as con:
        return {"items": insights(con, user)}


# ---------- Frag Papigator ----------

_SPEND_Q = re.compile(r"ausgegeben|ausgaben|ausgeben|gekostet|kosten wir|verbraucht|bezahlt|gezahlt|spent|spending|"
                      r"kategorie|wofür|wofuer|eingenommen|einnahmen", re.I)


def ask_facts(user: User, question: str, span) -> str:
    """Summen aus dem Konto für Frag Papigator – hier gerechnet, nicht vom Sprachmodell. Nur bei Fragen nach Ausgaben
    (oder einer Kategorie), mit dem abgedeckten Zeitraum, damit eine zu kurze CSV nicht als „wenig ausgegeben“ gilt."""
    q = question.lower()
    named = [c for c in CATEGORIES if c.split(" ")[0].lower() in q]
    # nur bei Fragen nach Geld – „Welche Versicherungen haben wir?“ braucht keine Kontosummen
    if not _SPEND_Q.search(question) and not (named and span):
        return ""
    today = dt.date.today()
    first = span[0] if span else (today.replace(day=1) - dt.timedelta(days=150)).replace(day=1)
    last = span[1] if span else today
    with connect() as con:
        s = summary_data(con, user, first.isoformat(), last.isoformat())
        if not s["count"]:
            return ""
        lines = [f"Kontoumsätze {first.strftime('%d.%m.%Y')} bis {last.strftime('%d.%m.%Y')} (Summen aus der Datenbank, "
                 f"verlässlich; Rücksendungen abgezogen, Umbuchungen nicht gezählt):",
                 f"- abgedeckt: Umsätze vom {s['covered']['first']} bis {s['covered']['last']}"
                 + (" – für den Anfang des Zeitraums fehlen Umsätze" if s["covered"]["first"] > first.isoformat() else ""),
                 f"- Ausgaben gesamt: {s['spent'] / 100:.2f} €, Einnahmen: {s['income'] / 100:.2f} €"]
        for c in s["categories"]:
            lines.append(f"- {c['name']}: {c['cents'] / 100:.2f} € ({c['count']} Buchungen)")
        for name in named:
            if name in NOT_SPENDING:
                continue
            w = _tx_where(con, user, None, first.isoformat(), last.isoformat())
            where, params = w
            top: dict[str, int] = {}
            for r in con.execute(f"SELECT counterparty, purpose, cents FROM bank_tx t WHERE {' AND '.join(where)} "
                                 f"AND t.category=?", (*params, name)):
                key = merchant_key(r["counterparty"], r["purpose"]) or r["counterparty"] or "?"
                top[key] = top.get(key, 0) - r["cents"]
            if top:
                best = sorted(top.items(), key=lambda kv: -kv[1])[:3]
                lines.append(f"- {name} nach Händler: " + ", ".join(f"{k} {v / 100:.2f} €" for k, v in best))
    return "\n".join(lines) + "\n"


# ---------- Suche („Amazon September 2026“) ----------

_MONTHS = {"januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "april": 4, "apr": 4, "mai": 5,
           "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
           "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12}
_SEASONS = {"frühling": (3, 5), "fruehling": (3, 5), "sommer": (6, 8), "herbst": (9, 11), "winter": (12, 2)}
_SEARCH_STOP = {"was", "wie", "viel", "hat", "haben", "wir", "ich", "im", "in", "am", "an", "für", "fuer", "bei", "von",
                "vom", "alle", "alles", "die", "der", "das", "den", "dem", "und", "mit", "ausgegeben", "gekostet",
                "kosten", "gezahlt", "bezahlt", "buchungen", "buchung", "umsätze", "umsaetze", "zahlungen", "geld",
                "euro", "insgesamt", "zusammen", "jahr", "monat", "seit", "bis", "letzte", "letzten", "letzter", "letztes"}


def search_range(q: str, today: dt.date) -> tuple[Optional[tuple[dt.date, dt.date]], set[str]]:
    """Zeitraum für Buchungen – anders als bei Dokumenten zählt hier auch eine nackte Jahreszahl („Kleidung 2025“)
    und „September 2026“, denn das Buchungsdatum ist genau. → (Zeitraum, verbrauchte Wörter)."""
    import calendar
    from . import smartsearch
    low = q.lower()
    m = re.search(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s*(\d{4})?\b", low)
    if m:
        mon = _MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if mon <= today.month else today.year - 1)
        return (dt.date(y, mon, 1), dt.date(y, mon, calendar.monthrange(y, mon)[1])), {m.group(1), m.group(2) or ""}
    m = re.search(r"\b(" + "|".join(_SEASONS) + r")\s*(\d{4})?\b", low)
    if m:
        a, b = _SEASONS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else today.year
        if a > b:   # Winter: Dezember bis Februar
            start, end = dt.date(y - 1 if not m.group(2) and today.month <= 2 else y, 12, 1), None
            end = dt.date(start.year + 1, 2, calendar.monthrange(start.year + 1, 2)[1])
        else:
            if not m.group(2) and today.month < a:
                y -= 1
            start, end = dt.date(y, a, 1), dt.date(y, b, calendar.monthrange(y, b)[1])
        return (start, end), {m.group(1), m.group(2) or ""}
    span = smartsearch._range(q, today)
    if span:
        return span, set()
    m = re.search(r"\b(19\d\d|20\d\d)\b", low)
    if m:
        y = int(m.group(1))
        return (dt.date(y, 1, 1), dt.date(y, 12, 31)), {m.group(1)}
    return None, set()


@router.get("/search")
def search(q: str = Query(..., min_length=1, max_length=200), user: User = Depends(current_user)):
    """Buchungen zu einem Satz: Zeitraum (auch „September 2026“, „2025“, „Sommer“) und Suchwörter in Gegenüber,
    Zweck und Kategorie (jedes Wort muss passen). Summe: was rausging minus Gutschriften, ohne Umbuchungen."""
    today = dt.date.today()
    span, used = search_range(q, today)
    words = [w for w in re.findall(r"[a-zäöüß0-9&+][\w&+.\-]*", q.lower())
             if w not in used and w not in _SEARCH_STOP and not re.fullmatch(r"\d{1,2}", w)
             and not re.search(r"monat|woche|tag$|jahr", w)]
    with connect() as con:
        w = _tx_where(con, user, None, span[0].isoformat() if span else None, span[1].isoformat() if span else None)
        empty = {"query": q, "words": words, "range": None, "count": 0, "cents": 0, "transactions": []}
        if span:
            empty["range"] = {"from": span[0].isoformat(), "to": span[1].isoformat()}
        if not w or not words:
            return empty
        where, params = w
        for word in words:
            where.append("lower(COALESCE(t.counterparty, '') || ' ' || COALESCE(t.purpose, '') || ' ' || "
                         "COALESCE(t.category, '') || ' ' || COALESCE(t.bank_category, '')) LIKE ?")
            params.append(f"%{word}%")
        rows = con.execute(f"SELECT t.* FROM bank_tx t WHERE {' AND '.join(where)} ORDER BY t.booked DESC, t.id DESC",
                           params).fetchall()
        spend = [r for r in rows if r["category"] != "Umbuchung"]
        return {**empty, "count": len(rows), "cents": -sum(r["cents"] for r in spend),
                "transactions": [tx_out(con, r, user) for r in rows[:300]]}
