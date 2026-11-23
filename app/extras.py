"""Absender zusammenführen, Steuer-Paket, Verträge & Abos – Auswertungen über den Dokumentbestand."""
import calendar
import csv
import io
import json
import re
import logging
import statistics
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import processing

log = logging.getLogger("papigator.extras")


# ---------- Geldfluss ----------
# Richtung eines Betrags: „out“ = Ausgabe, „in“ = Einnahme (Gehalt, Erstattung, Guthaben, Gutschrift), „none“ = bewegt
# selbst kein Geld (Vertrag, Mahnung zu einer Rechnung, Info). Ältere Dokumente ohne Richtung zählen als Ausgabe,
# außer ihrer Art nach. Summen nur in Euro – andere Währungen würden sonst einfach mitaddiert.
NO_FLOW_KINDS = ("mahnung", "vertrag", "kuendigung", "info")
_SQL_OUT = ("(COALESCE(currency,'EUR')='EUR' AND (direction='out' OR (direction IS NULL AND COALESCE(doc_kind,'') "
            "NOT IN ('mahnung','vertrag','kuendigung','info','gehalt','gutschrift'))))")
_SQL_IN = "(COALESCE(currency,'EUR')='EUR' AND direction='in')"


def flow(r) -> str:
    """'out' | 'in' | 'none' für eine Dokumentzeile (mit doc_kind, direction)."""
    d = r["direction"]
    if d in ("out", "in", "none"):
        return d
    k = r["doc_kind"] or ""
    if k in ("gehalt", "gutschrift"):
        return "in"
    return "none" if k in NO_FLOW_KINDS else "out"


def repeats(rows, date_key: str = "document_date") -> set[int]:
    """IDs, die nur ein weiterer Scan desselben Belegs sind (gleicher Absender, Tag, Betrag, Währung und Art) und in
    Summen nicht noch einmal zählen – sonst verdoppelt ein Zweitscan die Handwerkerkosten im Steuer-Paket.
    Das erste (älteste ID) zählt; Dokumente ohne Absender, Datum oder Betrag sind nie Wiederholungen."""
    seen, out = set(), set()
    for r in sorted(rows, key=lambda r: r["id"]):
        sender, d, amount = (r["sender"] or "").strip().lower(), (r[date_key] or "")[:10], r["amount"]
        if not sender or not d or not amount:
            continue
        key = (sender, d, round(amount, 2), r["currency"] or "EUR", flow(r))
        if key in seen:
            out.add(r["id"])
        seen.add(key)
    return out


# ---------- Absender ----------


def csv_cell(v):
    """Text für CSV: beginnt er mit = + - @ (oder Tab/CR), würde Excel ihn als Formel ausführen – dann ein ' davor."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v

def senders(con, owners: list[int]) -> list[dict]:
    """Absender der sichtbaren Dokumente mit Anzahl und Summe (Groß-/Kleinschreibung zusammengefasst).
    Angezeigt wird die häufigste Schreibweise – bei Gleichstand nicht die in Großbuchstaben. Zweitscans
    desselben Belegs zählen mit, ihr Betrag aber nur einmal."""
    marks = ",".join("?" * len(owners)) or "NULL"
    rows = con.execute(
        f"SELECT id, trim(sender) AS sender, document_date, COALESCE(document_date, created_at) AS last, amount, currency, "
        f"direction, doc_kind FROM documents WHERE deleted_at IS NULL AND ext_pending=0 AND sender IS NOT NULL "
        f"AND trim(sender) != '' AND owner_id IN ({marks})", owners).fetchall()
    again = repeats(rows)
    spellings: dict[str, Counter] = defaultdict(Counter)
    groups: dict[str, dict] = {}
    for r in rows:
        k = r["sender"].lower()
        spellings[k][r["sender"]] += 1
        g = groups.setdefault(k, {"count": 0, "spent": 0.0, "received": 0.0, "last": None})
        g["count"] += 1
        g["last"] = max(g["last"] or "", r["last"] or "") or None
        if r["id"] in again or not r["amount"] or (r["currency"] or "EUR") != "EUR":
            continue
        f = flow(r)
        if f == "out":
            g["spent"] += r["amount"]
        elif f == "in":
            g["received"] += r["amount"]
    out = []
    for k, g in groups.items():
        name = sorted(spellings[k].items(), key=lambda x: (-x[1], x[0] == x[0].upper(), x[0]))[0][0]
        spent, received = round(g["spent"], 2), round(g["received"], 2)
        # total = Ausgaben (wie bisher angezeigt); received = Einnahmen von diesem Absender (Gehalt, Erstattung, Guthaben)
        out.append({"id": name, "name": name, "count": g["count"], "spent": spent, "received": received,
                    "total": spent, "last": g["last"]})
    return sorted(out, key=lambda g: (-g["count"], g["name"].lower()))


def merge_senders(con, owners: list[int], sources: list[str], target: str) -> int:
    """Schreibweisen vereinheitlichen und als Alias merken – sonst bringt der nächste Brief „Deutsche Telekom“ zurück."""
    marks = ",".join("?" * len(owners)) or "NULL"
    target = target.strip()
    moved = 0
    for s in sources:
        s = s.strip()
        if not s or s.lower() == target.lower():
            continue
        moved += con.execute(
            f"UPDATE documents SET sender=?, updated_at=datetime('now') WHERE lower(trim(sender)) = lower(?) "
            f"AND deleted_at IS NULL AND owner_id IN ({marks})", (target, s, *owners)).rowcount
        con.execute("INSERT OR REPLACE INTO sender_aliases(alias, target) VALUES (?, ?)", (s, target))
        con.execute("UPDATE sender_aliases SET target=? WHERE lower(target) = lower(?)", (target, s))  # Ketten auflösen
    con.execute("DELETE FROM sender_aliases WHERE lower(alias) = lower(?)", (target,))  # Ziel ist nie selbst Alias
    return moved


def canonical_sender(con, name: str) -> str:
    """Erkannten Absender auf die gewohnte Schreibweise bringen: erst zusammengeführte Namen (Alias),
    sonst die häufigste vorhandene Schreibweise bei bloß anderer Groß-/Kleinschreibung."""
    r = con.execute("SELECT target FROM sender_aliases WHERE alias = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    if r:
        return r["target"]
    r = con.execute("SELECT trim(sender) AS s, COUNT(*) AS n FROM documents WHERE lower(trim(sender)) = lower(?) "
                    "AND deleted_at IS NULL AND ext_pending=0 GROUP BY s ORDER BY n DESC, s = upper(s), s LIMIT 1", (name.strip(),)).fetchone()
    return r["s"] if r else name.strip()


# ---------- Steuer-Paket ----------

def _num(v) -> str:
    return f"{v:.2f}".replace(".", ",") if v is not None else ""


def _safe(s: str, n: int = 70) -> str:
    return "".join(c if c.isalnum() or c in " -_.,()" else "_" for c in (s or "")).strip()[:n] or "Dokument"


def build_package(con, owners: list[int], year: int | None, tags: list[str], sender: str | None, title: str) -> Path:
    """ZIP mit umbenannten PDFs, einem Gesamt-PDF mit Inhaltsverzeichnis und Lesezeichen, CSV-Übersicht und Summen."""
    from pypdf import PdfReader, PdfWriter
    from .mail import text_to_pdf

    marks = ",".join("?" * len(owners)) or "NULL"
    where, params = [f"d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks})"], list(owners)
    if year:
        # Jahr nur über das echte Dokumentdatum – ein undatierter Beleg gehört in kein Jahr (das Hochladedatum sagt nichts)
        where.append("substr(d.document_date, 1, 4) = ?")
        params.append(str(year))
    if sender:
        where.append("lower(trim(d.sender)) = lower(trim(?))")
        params.append(sender)
    for t in tags:
        where.append("EXISTS (SELECT 1 FROM document_tags dt JOIN tags tg ON tg.id=dt.tag_id WHERE dt.document_id=d.id AND tg.name=? COLLATE NOCASE)")
        params.append(t)
    rows = con.execute(f"SELECT d.* FROM documents d WHERE {' AND '.join(where)} "
                       "ORDER BY COALESCE(d.document_date, d.created_at), d.id", params).fetchall()
    if not rows:
        raise ValueError("Keine Dokumente für diese Auswahl")

    again = repeats(rows)
    entries = []
    for r in rows:
        tg = [t["name"] for t in con.execute("SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=? ORDER BY t.name", (r["id"],))]
        d = (r["document_date"] or "")[:10]
        name = f"{d or 'ohne Datum'} {_safe(r['sender'] or '', 30)} {_safe(r['title'], 60)}".replace("  ", " ").strip() + ".pdf"
        entries.append({"id": r["id"], "date": d, "title": r["title"], "sender": r["sender"] or "", "amount": r["amount"],
                        "currency": r["currency"] or "EUR", "flow": flow(r), "tax_35a": r["tax_35a"],
                        "tags": tg, "file": f"Dokumente/{r['id']:04d} {name}", "pages": r["page_count"] or 0,
                        "again": r["id"] in again})

    # Summen: Ausgaben und Einnahmen getrennt, nur Euro; Mahnungen, Verträge u. Ä. zählen nicht (bewegen kein Geld),
    # ein Zweitscan desselben Belegs auch nicht
    spent = received = tax35a = 0.0
    by_tag: dict[str, float] = defaultdict(float)
    by_sender: dict[str, float] = defaultdict(float)
    other: dict[str, float] = defaultdict(float)
    for e in entries:
        if e["again"]:
            continue
        if e["tax_35a"]:
            tax35a += e["tax_35a"]
        if not e["amount"] or e["flow"] == "none":
            continue
        if e["currency"] != "EUR":
            other[e["currency"]] += e["amount"] if e["flow"] == "out" else -e["amount"]
            continue
        if e["flow"] == "in":
            received += e["amount"]
            continue
        spent += e["amount"]
        by_sender[e["sender"] or "(ohne Absender)"] += e["amount"]
        for t in e["tags"] or ["(ohne Tag)"]:
            by_tag[t] += e["amount"]
    spent, received = round(spent, 2), round(received, 2)
    sums = {"title": title, "documents": len(entries), "repeats": sum(e["again"] for e in entries), "total": spent, "spent": spent, "received": received,
            "tax_35a": round(tax35a, 2), "other_currencies": {k: round(v, 2) for k, v in other.items()},
            "by_tag": dict(sorted(((k, round(v, 2)) for k, v in by_tag.items()), key=lambda x: -x[1])),
            "by_sender": dict(sorted(((k, round(v, 2)) for k, v in by_sender.items()), key=lambda x: -x[1]))}

    def signed(e) -> str:
        if e["amount"] is None:
            return ""
        v = f"{e['amount']:.2f}".replace(".", ",")
        if e["again"]:
            return f"({v})"
        return {"in": "+" + v, "none": f"({v})"}.get(e["flow"], "-" + v)

    # CSV
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nr", "Datum", "Absender", "Titel", "Betrag", "Währung", "Art", "§ 35a", "Tags", "Datei"])
    art = {"out": "Ausgabe", "in": "Einnahme", "none": "ohne Zahlung"}
    for i, e in enumerate(entries, 1):
        w.writerow([i, e["date"], csv_cell(e["sender"]), csv_cell(e["title"]), signed(e),
                    csv_cell(e["currency"]), "Zweitscan (nicht gezählt)" if e["again"] else art[e["flow"]], _num(e["tax_35a"]), csv_cell(", ".join(e["tags"])), csv_cell(e["file"])])
    w.writerow([])
    w.writerow(["", "", "", "Ausgaben", "-" + _num(spent), "EUR"])
    w.writerow(["", "", "", "Einnahmen", "+" + _num(received), "EUR"])
    if tax35a:
        w.writerow(["", "", "", "davon § 35a (Arbeitskosten)", _num(tax35a), "EUR"])
    for cur, v in other.items():
        w.writerow(["", "", "", f"in {cur} (nicht umgerechnet)", _num(v), cur])

    # Gesamt-PDF: Inhaltsverzeichnis als erste Seiten, dann alle Dokumente mit Lesezeichen
    toc_lines = [f"{i:>3}. {e['date'] or 'ohne Datum':<10}  {(e['sender'] or '')[:28]:<28}  {e['title'][:48]:<48}  {signed(e).rjust(11)}"
                 for i, e in enumerate(entries, 1)]
    toc_lines += ["", f"Ausgaben: {_num(spent)} EUR  ·  Einnahmen: {_num(received)} EUR  ·  {len(entries)} Dokumente"]
    if sums["repeats"]:
        toc_lines.append(f"Beträge in Klammern ohne Vorzeichen: Zweitscan eines schon aufgeführten Belegs, nicht mitgezählt ({sums['repeats']})")
    if tax35a:
        toc_lines.append(f"Arbeitskosten nach § 35a EStG: {_num(tax35a)} EUR")
    for cur, v in other.items():
        toc_lines.append(f"Außerdem in {cur}: {_num(v)} (nicht umgerechnet)")
    toc_pdf = text_to_pdf(title, [f"Erstellt am {date.today().isoformat()}"] + ([f"Jahr {year}"] if year else []) + ([f"Tags: {', '.join(tags)}"] if tags else []),
                          "\n".join(toc_lines))
    writer = PdfWriter()
    toc_reader = PdfReader(str(toc_pdf))
    for p in toc_reader.pages:
        writer.add_page(p)
    writer.add_outline_item("Inhaltsverzeichnis", 0)
    page_no = len(toc_reader.pages)
    for i, e in enumerate(entries, 1):
        p = processing.doc_path(e["id"])
        if not p.exists():
            continue
        try:
            rd = PdfReader(str(p))
            for pg in rd.pages:
                writer.add_page(pg)
            writer.add_outline_item(f"{i}. {e['date']} {e['title'][:60]}", page_no)
            page_no += len(rd.pages)
        except Exception as ex:
            log.warning("Steuer-Paket: %s übersprungen (%s)", e["file"], ex)
    merged = Path(tempfile.mkstemp(suffix=".pdf")[1])
    with open(merged, "wb") as fh:
        writer.write(fh)
    toc_pdf.unlink(missing_ok=True)

    out = Path(tempfile.mkstemp(suffix=".zip")[1])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.write(merged, f"{_safe(title)} - alle Dokumente.pdf")
        z.writestr(f"{_safe(title)} - Uebersicht.csv", "﻿" + buf.getvalue())
        z.writestr("summen.json", json.dumps(sums, ensure_ascii=False, indent=1))
        for e in entries:
            p = processing.doc_path(e["id"])
            if p.exists():
                z.write(p, e["file"], compress_type=zipfile.ZIP_STORED)
    merged.unlink(missing_ok=True)
    return out


# ---------- Verträge & Abos ----------

INTERVALS = [("monatlich", 30, 22, 38), ("vierteljährlich", 91, 80, 104), ("halbjährlich", 182, 165, 200), ("jährlich", 365, 340, 395)]


PER_MONTH = {"monatlich": 1, "vierteljährlich": 1 / 3, "halbjährlich": 1 / 6, "jährlich": 1 / 12}
TYPICAL = {name: t for name, t, _, _ in INTERVALS}
SAME_BILL_DAYS = 10   # gleicher Betrag binnen weniger Tage: dieselbe Rechnung (zweiter Scan, Kopie per Mail)


def _d(s) -> date | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _step(d: date, interval: str, n: int = 1) -> date:
    """d plus n Intervalle, kalendertreu (Monatsende bleibt im Monat)."""
    months = {"monatlich": 1, "vierteljährlich": 3, "halbjährlich": 6, "jährlich": 12}[interval] * n
    y, m = divmod(d.month - 1 + months, 12)
    return d.replace(year=d.year + y, month=m + 1, day=min(d.day, calendar.monthrange(d.year + y, m + 1)[1]))


def _cancel_by(docs, interval: str, today: date) -> dict | None:
    """Nächste Kündigungsfrist: die jüngste genannte; bei jährlichen Verträgen wiederholt sie sich jedes Jahr.
    Ein Sonderkündigungsrecht gilt nur einmal."""
    best = None
    for r in docs:
        note = (r["due_note"] or "").lower()
        if r["due_date"] and ("kündig" in note or "kündbar" in note) and (r["doc_kind"] or "") != "kuendigung":
            if best is None or r["due_date"] > best["due_date"]:
                best = r
    if not best or not (d := _d(best["due_date"])):
        return None
    once = "sonder" in (best["due_note"] or "").lower()
    if d < today and interval == "jährlich" and not once:
        n = 0
        while d < today and n < 10:
            d, n = _step(d, "jährlich"), n + 1
    if d < today:
        return None
    return {"date": d.isoformat(), "note": best["due_note"], "document_id": best["id"]}


def _increase(pays, interval: str = "monatlich") -> dict | None:
    """Preiserhöhung: der jüngste Betrag liegt mind. 3 % über dem davor und hat sich gehalten (alle Belege seit der
    Änderung gleich). Abrechnungen zählen nicht – ihr Betrag ist die Nachzahlung, kein Preis."""
    seq = [r for r in pays if r["doc_kind"] != "abrechnung" and _d(r["d"])]
    if len(seq) < 2:
        return None
    new = seq[-1]["amount"]
    i = len(seq) - 1
    while i > 0 and abs(seq[i - 1]["amount"] - new) <= 0.01 * new:
        i -= 1
    if i == 0:
        return None
    old = seq[i - 1]["amount"]
    # monatlich/vierteljährlich: erst wenn der neue Betrag zweimal kam (ein Roaming-Aufschlag ist keine Preiserhöhung)
    if new < old * 1.03 or (len(seq) - i < 2 and interval in ("monatlich", "vierteljährlich")):
        return None
    return {"from": round(old, 2), "to": round(new, 2), "since": seq[i]["d"][:10], "document_id": seq[i]["id"],
            "percent": round((new / old - 1) * 100)}


def contracts(con, owners: list[int], today: date | None = None) -> list[dict]:
    """Verträge, Abos, Darlehen, Gebühren: wiederkehrende Ausgaben an denselben Absender.

    Zwei Quellen: (1) Muster – mind. drei Zahlungsbelege mit ähnlichem Betrag in regelmäßigem Abstand (zweite Scans
    derselben Rechnung zählen einmal, Mahnungen gar nicht); (2) ein einzelner Vertrag/Bescheid, der den Rhythmus
    nennt (Kita, Kredit, Rundfunkbeitrag). Einnahmen (Gehalt) sind keine Verträge; nach einer Kündigung fällt der
    Vertrag weg. Dazu die nächste Kündigungsfrist, bei Jahresverträgen fortgeschrieben."""
    today = today or date.today()
    marks = ",".join("?" * len(owners)) or "NULL"
    rows = con.execute(
        f"SELECT id, title, sender, amount, currency, document_date AS d, due_date, due_note, "
        f"due_done, doc_kind, direction, recurring FROM documents WHERE deleted_at IS NULL AND ext_pending=0 "
        f"AND sender IS NOT NULL AND sender != '' AND owner_id IN ({marks}) ORDER BY d, id", owners).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r["sender"].strip().lower()].append(r)
    out = []
    for docs in groups.values():
        ended = max((r["d"] for r in docs if r["doc_kind"] == "kuendigung" and r["d"]), default=None)
        # (1) Muster aus Zahlungsbelegen
        pays = []
        for r in docs:
            # nur datierte Belege: ohne Dokumentdatum lässt sich kein Rhythmus ablesen (Hochladedatum sagt nichts)
            if not r["amount"] or r["amount"] <= 0 or flow(r) != "out" or (r["currency"] or "EUR") != "EUR" or not _d(r["d"]):
                continue
            if pays and _d(r["d"]) and _d(pays[-1]["d"]) and (_d(r["d"]) - _d(pays[-1]["d"])).days <= SAME_BILL_DAYS \
                    and abs(r["amount"] - pays[-1]["amount"]) <= 0.01 * r["amount"]:
                continue
            pays.append(r)
        item = None
        if len(pays) >= 3:
            # Betragsgruppe um den Median der jüngsten Belege (Preiserhöhungen, Roaming-Aufschläge),
            # Einmalkäufe beim selben Absender fallen heraus
            med = statistics.median(r["amount"] for r in pays[-6:])
            cluster = [r for r in pays if abs(r["amount"] - med) <= max(0.2 * med, 1.0) and _d(r["d"])]
            dates = sorted(_d(r["d"]) for r in cluster)
            gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
            gap = statistics.median(gaps) if len(cluster) >= 3 and gaps else None
            interval = next((name for name, _, lo, hi in INTERVALS if gap and lo <= gap <= hi), None)
            if interval:
                latest = sorted(cluster, key=lambda r: r["d"], reverse=True)
                amount = statistics.median(r["amount"] for r in latest[:6])   # jüngste Belege: Preiserhöhungen zählen
                nxt = dates[-1] + timedelta(days=TYPICAL[interval])
                due = next(({"date": r["due_date"], "document_id": r["id"]} for r in latest if r["due_date"] and not r["due_done"]), None)
                item = {"sender": latest[0]["sender"], "amount": round(amount, 2), "interval": interval, "source": "pattern",
                        "count": len(cluster), "first": dates[0].isoformat(), "last": dates[-1].isoformat(),
                        "next_expected": nxt.isoformat(), "overdue": nxt < today - timedelta(days=TYPICAL[interval] // 3),
                        "documents": [r["id"] for r in latest[:3]], "due": due}
        # (2) ein Vertrag/Bescheid nennt den Rhythmus – nicht beim Arbeitgeber: wer mir Gehalt zahlt, ist kein Vertrag,
        # der etwas kostet (auch wenn der Arbeitsvertrag beim Nachtragen als „monatlich“ erkannt wurde)
        employer = any(r["doc_kind"] == "gehalt" for r in docs)
        if not item and not employer:
            declared = [r for r in docs if r["recurring"] in PER_MONTH and r["doc_kind"] in ("vertrag", "bescheid")
                        and r["amount"] and r["amount"] > 0 and not processing._INCOME_CONTRACT.search(r["title"] or "")
                        and flow(r) != "in" and r["doc_kind"] != "gehalt" and (r["currency"] or "EUR") == "EUR" and _d(r["d"])]
            if declared:
                r = declared[-1]
                interval, start = r["recurring"], _d(r["d"])
                nxt, n = start, 0
                while nxt <= today and n < 600:
                    nxt, n = _step(start, interval, n + 1), n + 1
                due = {"date": r["due_date"], "document_id": r["id"]} if r["due_date"] and not r["due_done"] else None
                # Der Betrag im Vertrag ist der Stand bei Abschluss; spätere Rechnungen desselben Absenders (Preiserhöhung)
                # zeigen, was heute gezahlt wird
                later = [x for x in pays if x["d"] > r["d"]]
                amount, amount_from = (later[-1]["amount"], later[-1]["d"][:10]) if later else (r["amount"], None)
                item = {"sender": r["sender"], "amount": round(amount, 2), "interval": interval, "source": "document",
                        "count": len(declared), "first": declared[0]["d"][:10], "last": r["d"][:10], "next_expected": nxt.isoformat(),
                        "overdue": False, "documents": [x["id"] for x in reversed(declared[-3:])], "due": due,
                        "contract_amount": round(r["amount"], 2) if amount_from else None, "amount_from": amount_from}
        if not item or (ended and ended >= item["last"]):
            continue
        item["increase"] = _increase(pays, item["interval"])
        item["per_month"] = round(PER_MONTH[item["interval"]] * item["amount"], 2)
        item["cancel_by"] = _cancel_by(docs, item["interval"], today)
        out.append(item)
    out.sort(key=lambda c: -c["per_month"])
    return out


# Kfz-Kennzeichen („KA-AB 123“, „KAR-X 7E“) – trennt die HU-Berichte zweier Autos
_PLATE = re.compile(r"\b[A-ZÄÖÜ]{1,3}[- ][A-Z]{1,2} ?\d{1,4}[EH]?\b")


def upcoming(con, owners: list[int], today: date | None = None) -> list[dict]:
    """Was bald abläuft oder fällig wird – je Person und Art nur der jüngste Beleg (der neue Ausweis löst den alten ab,
    der neue HU-Bericht den alten), dazu offene Erstattungen und Preiserhöhungen der letzten 180 Tage."""
    import datetime as dt
    today = today or date.today()
    marks = ",".join("?" * len(owners))
    base = f"deleted_at IS NULL AND ext_pending=0 AND owner_id IN ({marks})"
    items = []
    # Ablaufdaten: jedes für sich – der Kinderpass beim Konto der Eltern soll nicht hinter dem eigenen verschwinden;
    # alte Karten fallen durch das Zeitfenster heraus
    lo, hi = (today - dt.timedelta(days=60)).isoformat(), (today + dt.timedelta(days=120)).isoformat()
    for r in con.execute(f"SELECT id, title, expires_what AS what, expires_on AS d FROM documents WHERE {base} "
                         f"AND expires_on BETWEEN ? AND ? ORDER BY expires_on", (*owners, lo, hi)).fetchall():
        items.append({"type": "expiry", "document_id": r["id"], "title": r["title"], "what": r["what"], "date": r["d"]})
    # Prüfungen/Wartungen: ein neuer Beleg löst nur den älteren für dasselbe ab (HU: dasselbe Kennzeichen, sonst derselbe
    # Absender) – zwei Autos oder zwei Heizungen bleiben getrennt; Unklares lieber doppelt als verschluckt
    newest: dict[tuple, dict] = {}
    for r in con.execute(f"SELECT id, title, owner_id, sender, text, service_what AS what, service_next AS d, "
                         f"COALESCE(document_date, created_at) AS at FROM documents WHERE {base} "
                         f"AND service_what IS NOT NULL ORDER BY at, id", owners).fetchall():
        plate = _PLATE.search(r["text"] or "") if r["what"] == "Hauptuntersuchung" else None
        subject = plate.group(0).replace(" ", "").upper() if plate else (r["sender"] or "").strip().lower() or f"#{r['id']}"
        newest[(r["owner_id"], r["what"], subject)] = r
    for r in newest.values():
        if r["d"] and (today - dt.timedelta(days=365)).isoformat() <= r["d"] <= (today + dt.timedelta(days=60)).isoformat():
            items.append({"type": "service", "document_id": r["id"], "title": r["title"], "what": r["what"], "date": r["d"]})
    refunds = con.execute(f"SELECT id, title, sender, amount, currency, document_date AS d FROM documents WHERE {base} "
                          f"AND refund_state='open' ORDER BY document_date, id", owners).fetchall()
    if len(refunds) > 3:   # viele offene: eine Zeile mit Zahl und Summe, öffnet die älteste
        items.append({"type": "refund", "document_id": refunds[0]["id"], "title": f"{len(refunds)} Arztrechnungen",
                      "what": None, "date": refunds[0]["d"], "amount": round(sum(r["amount"] or 0 for r in refunds), 2),
                      "count": len(refunds)})
    else:
        for r in refunds:
            items.append({"type": "refund", "document_id": r["id"], "title": r["title"], "what": r["sender"],
                          "date": r["d"], "amount": r["amount"]})
    for c in contracts(con, owners, today):
        inc = c.get("increase")
        if inc and inc["since"] >= (today - dt.timedelta(days=180)).isoformat():
            items.append({"type": "price", "document_id": inc["document_id"], "title": c["sender"], "what": c["sender"],
                          "date": inc["since"], "amount": inc["to"], "from": inc["from"], "percent": inc["percent"],
                          "cancel_by": (c.get("cancel_by") or {}).get("date")})
    order = {"expiry": 0, "service": 1, "price": 2, "refund": 3}
    items.sort(key=lambda i: (order[i["type"]], i["date"] or ""))
    return items




# ---------- Tag als Paket zum Herausgeben (Auto verkaufen, Unterlagen für die Familie) ----------

def _eur(v) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if v is not None else ""


def build_tag_export(con, owners: list[int], tag_id: int) -> tuple[Path, str]:
    """ZIP für einen Tag/Ordner: Struktur entsteht erst hier, aus den übrigen Tags der Dokumente.

    <Tag>/[Unterordner/]<anderer Tag oder „Sonstiges“>/[Jahr/]JJJJ-MM-TT Titel.pdf, dazu Inhaltsverzeichnis
    als PDF und CSV. Hat ein Dokument mehrere andere Tags, liegt es in jedem dieser Ordner – wer das Paket
    bekommt, soll lieber doppelt finden als gar nicht. Das Jahr kommt nur dazu, wo ein Ordner mehrere Jahre hat.
    Gibt (ZIP-Datei, Name des Tags) zurück; ValueError, wenn nichts darin liegt."""
    from .folders import _safe as safe_name, path_of, tag_tree
    from .mail import text_to_pdf

    tree = tag_tree(con)
    if tag_id not in tree:
        raise ValueError("Ordner nicht gefunden")
    root = tree[tag_id][0]
    subtree, todo = {tag_id}, [tag_id]
    while todo:                                    # Unterordner einsammeln
        cur = todo.pop()
        for tid, (_, parent) in tree.items():
            if parent == cur and tid not in subtree:
                subtree.add(tid); todo.append(tid)
    ancestors = set()
    p = tree[tag_id][1]
    while p is not None and p in tree and p not in ancestors:
        ancestors.add(p); p = tree[p][1]
    root_depth = len(path_of(tree, tag_id))

    marks, smarks = ",".join("?" * len(owners)) or "NULL", ",".join("?" * len(subtree))
    rows = con.execute(
        f"SELECT d.* FROM documents d WHERE d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks}) "
        f"AND d.id IN (SELECT document_id FROM document_tags WHERE tag_id IN ({smarks})) "
        "ORDER BY COALESCE(d.document_date, d.created_at), d.id", (*owners, *subtree)).fetchall()
    if not rows:
        raise ValueError("In diesem Ordner liegen keine Dokumente")

    placed = []                                    # (Ordnerweg ohne Jahr, Dokument)
    for r in rows:
        tids = [t[0] for t in con.execute("SELECT tag_id FROM document_tags WHERE document_id=?", (r["id"],))]
        inside = [t for t in tids if t in subtree]
        # tiefste Orte innerhalb des Ordners (ein Tag, der Vorfahre eines anderen ist, zählt nicht extra)
        paths = [path_of(tree, t)[root_depth - 1:] for t in inside]
        paths = [p for p in paths if not any(len(q) > len(p) and q[:len(p)] == p for q in paths)] or [[root]]
        others = sorted({tree[t][0] for t in tids if t not in subtree and t not in ancestors and t in tree},
                        key=str.lower) or ["Sonstiges"]
        for base in {tuple(p) for p in paths}:
            for o in others:
                placed.append(((*base, o), r))

    years: dict[tuple, set] = defaultdict(set)
    for folder, r in placed:
        years[folder].add((r["document_date"] or "")[:4] or "ohne Datum")
    entries, used = [], set()
    for folder, r in placed:
        date_s = (r["document_date"] or "")[:10]
        y = date_s[:4] or "ohne Datum"
        parts = [safe_name(x) for x in folder] + ([y] if len(years[folder]) > 1 else [])
        name = " ".join(filter(None, [date_s, safe_name(r["title"] or f"Dokument {r['id']}")]))
        path = "/".join([*parts, f"{name}.pdf"])
        if path.lower() in used:                   # gleicher Titel am gleichen Tag
            path = path[:-4] + f" ({r['id']}).pdf"
        used.add(path.lower())
        entries.append({"path": path, "folder": "/".join(parts[1:]) or root, "row": r})

    # Inhaltsverzeichnis: nach Ordnern gruppiert, Summe über die Dokumente (nicht über Kopien)
    lines, last = [], None
    for e in sorted(entries, key=lambda e: (e["folder"].lower(), e["path"].lower())):
        if e["folder"] != last:
            lines += (["", f"{e['folder']}/"] if last is not None else [f"{e['folder']}/"]); last = e["folder"]
        r = e["row"]
        d = r["document_date"]
        d = f"{d[8:10]}.{d[5:7]}.{d[:4]}" if d and len(d) >= 10 else "ohne Datum"
        lines.append("   " + " · ".join(filter(None, [d, r["title"], r["sender"], _eur(r["amount"])])))
    unique = {e["row"]["id"]: e["row"] for e in entries}
    again = repeats(list(unique.values()))      # Zweitscans: im Ordner, aber nicht in der Summe
    counted = [r for i, r in unique.items() if i not in again]
    spent = sum(r["amount"] or 0 for r in counted if flow(r) == "out" and (r["currency"] or "EUR") == "EUR")
    received = sum(r["amount"] or 0 for r in counted if flow(r) == "in" and (r["currency"] or "EUR") == "EUR")
    lines += ["", f"{len(unique)} Dokumente" + (f" · Ausgaben {_eur(spent)}" if spent else "")
              + (f" · Einnahmen {_eur(received)}" if received else "")]
    toc = text_to_pdf(f"{root} – Unterlagen", [f"Erstellt am {date.today().strftime('%d.%m.%Y')} mit Papigator"], "\n".join(lines))

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Ordner", "Datum", "Titel", "Absender", "Betrag", "Währung", "Datei"])
    for e in entries:
        r = e["row"]
        w.writerow([csv_cell(e["folder"]), (r["document_date"] or "")[:10], csv_cell(r["title"]), csv_cell(r["sender"]),
                    f"{r['amount']:.2f}".replace(".", ",") if r["amount"] is not None else "",
                    csv_cell(r["currency"]), csv_cell(e["path"])])

    top = safe_name(root)
    out = Path(tempfile.mkstemp(suffix=".zip")[1])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.write(toc, f"{top}/Inhaltsverzeichnis.pdf")
        z.writestr(f"{top}/Inhaltsverzeichnis.csv", "﻿" + buf.getvalue())
        for e in entries:
            src = processing.doc_path(e["row"]["id"])
            if src.exists():
                z.write(src, e["path"])
    toc.unlink(missing_ok=True)
    return out, root
