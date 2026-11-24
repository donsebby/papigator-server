"""Jährliche Kosten (24.11.2026): was einmal im Jahr abgeht – Versicherungen, Jahresabos, Beiträge – mit Vorjahr,
aktuellem Preis und der Monatsrate für einen Rücklagen-Topf („Jährlich“-Pocket).

Quellen, in dieser Reihenfolge:
1. Kontoumsätze: eine Abbuchung, zu der es beim selben Händler etwa ein Jahr vorher (330–400 Tage) eine mit
   ähnlichem Betrag (±25 %) gibt. Mehrere Verträge beim selben Versicherer (Kfz im November, Haftpflicht im Februar)
   werden so getrennte Reihen. Kommen im Betragsband drei oder mehr Abbuchungen in einem Jahr, ist es kein
   Jahresposten (Abo, Einkäufe) – zwei im Jahr bleiben (halbjährlich oder zwei Verträge, die Summe stimmt beidesmal).
2. Von Hand: „jährlich“ an einer Buchung (auch nach nur einer Abbuchung) – oder „nicht jährlich“ für Fehltreffer.
   Gilt für Händler + Betragsband, je Person (bank_yearly_marks).
3. Belege: Verträge aus der Ablage mit jährlichem Rhythmus (extras.contracts). Ist einer davon mit einer Abbuchung
   der Reihe verknüpft, zählt nur die Abbuchung; sonst steht er da, zählt aber nicht zur Summe (sonst doppelt).

Die Monatsrate rechnet mit dem jüngsten Betrag je laufendem Posten; die Jahresspalten (Kalenderjahre) sind nur
Anzeige. Alles nur aus Konten, die die Person sieht (bank.visible_accounts), Belege nur, wenn sie sie sehen darf.
Nie in Freigabe-Links, Exporten, Widget oder Spotlight.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import auth
from .auth import User, current_user
from .db import connect

router = APIRouter(prefix="/bank/yearly")

LOOKBACK_DAYS = 3 * 366 + 31     # drei Jahre reichen für Vorjahr, Vorvorjahr und die Reihen
YEAR_MIN, YEAR_MAX = 330, 400    # Abstand einer Jahresabbuchung (Wochenende, Monatsanfang, späte Lastschrift)
BAND = 0.25                      # ähnlicher Betrag: Preiserhöhung ja, ein anderes Produkt nein
ACTIVE_DAYS = 400                # länger keine Abbuchung: weggefallen
POT_NAME = re.compile(r"j(?:ä|ae|a)hrlich|rücklage|ruecklage", re.I)   # nicht „Jahresurlaub“


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _close(a: int, b: int) -> bool:
    return abs(a - b) <= BAND * max(a, b)


def _plus_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:            # 29. Februar
        return d.replace(year=d.year + 1, day=28)


def _chains(rows: list[dict]) -> list[list[dict]]:
    """Abbuchungen eines Händlers (nach Datum) → Reihen, in denen jede etwa ein Jahr nach der vorigen kommt.
    Jede Abbuchung hat höchstens einen Vorgänger und einen Nachfolger; bei mehreren Kandidaten der, der am nächsten
    an 365 Tagen und am Betrag liegt."""
    prev_of: dict[int, dict] = {}
    taken: set[int] = set()
    for i, r in enumerate(rows):
        best, score = None, None
        for e in rows[:i]:
            days = (_d(r["booked"]) - _d(e["booked"])).days
            if not (YEAR_MIN <= days <= YEAR_MAX) or e["id"] in taken or not _close(-e["cents"], -r["cents"]):
                continue
            s = abs(days - 365) + abs(e["cents"] - r["cents"]) / max(1, -r["cents"]) * 100
            if score is None or s < score:
                best, score = e, s
        if best:
            prev_of[r["id"]] = best
            taken.add(best["id"])
    by_id = {r["id"]: r for r in rows}
    nxt = {p["id"]: by_id[rid] for rid, p in prev_of.items()}
    out = []
    for r in rows:
        if r["id"] in prev_of or r["id"] not in nxt:
            continue
        chain = [r]
        while chain[-1]["id"] in nxt:
            chain.append(nxt[chain[-1]["id"]])
        out.append(chain)
    return out


def _too_frequent(chain: list[dict], rows: list[dict]) -> bool:
    """Drei oder mehr Abbuchungen im Betragsband innerhalb eines Jahres: Abo oder Einkäufe, kein Jahresposten."""
    last = chain[-1]
    band = [r for r in rows if _close(-r["cents"], -last["cents"])]
    for r in band:
        start = _d(r["booked"])
        if sum(1 for x in band if 0 <= (_d(x["booked"]) - start).days < 365) >= 3:
            return True
    return False


def _marks(con, user: User) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT mkey, cents, yearly FROM bank_yearly_marks WHERE user_id=?", (user.id,))]


def _visible_doc(con, user: User, doc_id: Optional[int]) -> Optional[dict]:
    if not doc_id:
        return None
    d = con.execute("SELECT id, title, owner_id FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,)).fetchone()
    if d and d["owner_id"] in auth.visible_owner_ids(con, user, "all"):
        # „Beitragsrechnung Kfz 2026“ → „Beitragsrechnung Kfz“: der Posten kommt jedes Jahr
        return {"id": d["id"], "title": re.sub(r"\s+(?:19|20)\d\d$", "", (d["title"] or "").strip()) or d["title"]}
    return None


def pot_account(con, user: User, accounts: list[int]):
    """→ (Konto, selbst gewählt?) – gewählt (bank_pot) oder nach Namen vorgeschlagen; (None, False) ohne Topf."""
    if not accounts:
        return None, False
    chosen = con.execute("SELECT account_id FROM bank_pot WHERE user_id=?", (user.id,)).fetchone()
    if chosen and chosen[0] in accounts:
        return con.execute("SELECT id, name FROM bank_accounts WHERE id=?", (chosen[0],)).fetchone(), True
    marks = ",".join("?" * len(accounts))
    return next((a for a in con.execute(f"SELECT id, name FROM bank_accounts WHERE id IN ({marks}) ORDER BY id", accounts)
                 if POT_NAME.search(a["name"] or "")), None), False


# Wörter, die in Umbuchungen nichts über den Zweck sagen
_POT_NOISE = {"umbuchung", "pocket", "übertrag", "uebertrag", "überweisung", "ueberweisung", "jährlich", "jaehrlich",
              "jahres", "rücklage", "ruecklage", "von", "an", "auf", "nach", "für", "fuer", "giro", "girokonto", "konto",
              "gemeinschaftskonto", "sparen", "ausgleich", "entnahme", "eur", "euro"}


def pot_key(purpose: Optional[str], counterparty: Optional[str] = None) -> str:
    """Gruppe einer Buchung im Topf: die ersten zwei aussagekräftigen Wörter des Zwecks („PS Plus“ → „topf:ps plus“);
    ohne Zweck der Empfänger, ohne beides „topf:“ – dann trennt der Betrag."""
    for text in (purpose, counterparty):
        words = [w for w in re.findall(r"[a-zäöüß0-9+][\w+&\-]*", (text or "").lower()) if w not in _POT_NOISE and not w.isdigit()]
        if words:
            return "topf:" + " ".join(words[:2])
    return "topf:"


def pot_info(con, user: User, accounts: list[int], today: dt.date) -> Optional[dict]:
    """Rücklagen-Topf: gewählt (bank_pot) oder vorgeschlagen (Name wie „Jährlich“). Monatlich eingezahlt = Median der
    Gutschriften der letzten drei vollen Monate."""
    acc, picked = pot_account(con, user, accounts)
    if not acc:
        return None
    first = dt.date(today.year, today.month, 1)
    months = []
    for k in range(6, 0, -1):
        y, m = divmod(first.month - 1 - k, 12)
        start = dt.date(first.year + y, m + 1, 1)
        y2, m2 = divmod(start.month, 12)
        end = dt.date(start.year + y2, m2 + 1, 1)
        c = con.execute("SELECT COALESCE(SUM(cents), 0) FROM bank_tx WHERE account_id=? AND cents > 0 AND booked >= ? "
                        "AND booked < ?", (acc["id"], start.isoformat(), end.isoformat())).fetchone()[0]
        months.append({"month": start.isoformat()[:7], "cents": c})
    recent = [m["cents"] for m in months[-3:]]
    return {"id": acc["id"], "name": acc["name"], "chosen": picked, "months": months,
            "monthly": int(statistics.median(recent)) if any(recent) else 0}


def overview(con, user: User, year: Optional[int] = None, today: Optional[dt.date] = None) -> dict:
    from . import bank, extras
    today = today or dt.date.today()
    year = year or today.year
    accounts = bank.visible_accounts(con, user)
    items: list[dict] = []
    covered = None
    if accounts:
        marks = ",".join("?" * len(accounts))
        covered = con.execute(f"SELECT MIN(booked) AS first, MAX(booked) AS last FROM bank_tx WHERE account_id IN ({marks})",
                              accounts).fetchone()
        covered = {"first": covered["first"], "last": covered["last"]} if covered and covered["first"] else None
        rows = [dict(r) for r in con.execute(
            f"SELECT id, account_id, booked, cents, counterparty, purpose, category, document_id FROM bank_tx "
            f"WHERE account_id IN ({marks}) AND cents < 0 AND currency='EUR' AND booked >= ? "
            f"AND COALESCE(category, '') NOT IN ('Umbuchung', 'Bargeld') ORDER BY booked, id",
            (*accounts, (today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()))]
        pot, _ = pot_account(con, user, accounts)
        if pot:
            # Im Topf zählen nur echte Händler-Abbuchungen (mit Kategorie) als Abbuchung; Umbuchungen und
            # Unbekanntes sind Entnahmen und laufen über _pot_items
            rows = [r for r in rows if r["account_id"] != pot["id"] or r["category"]]
        by_key: dict[str, list[dict]] = {}
        for r in rows:
            k = bank.merchant_key(r["counterparty"], r["purpose"])
            if k:
                by_key.setdefault(k, []).append(r)
        marklist = _marks(con, user)
        for key, krows in by_key.items():
            no = [m["cents"] for m in marklist if m["mkey"] == key and not m["yearly"]]
            yes = [m["cents"] for m in marklist if m["mkey"] == key and m["yearly"]]
            series = [(c, "bank") for c in _chains(krows) if not _too_frequent(c, krows)]
            used = {r["id"] for c, _ in series for r in c}
            for cents in yes:      # von Hand: alles im Band, auch nach nur einer Abbuchung
                band = [r for r in krows if _close(-r["cents"], cents) and r["id"] not in used]
                if band:
                    series.append((band, "marked"))
                    used.update(r["id"] for r in band)
            series = [(c, s) for c, s in series if not any(_close(-c[-1]["cents"], n) for n in no)]
            for chain, source in series:
                items.append(_item(con, user, key, chain, source, year, today, len(series) > 1))
        _pot_items(con, user, accounts, items, marklist, year, today)
        # Belege mit jährlichem Rhythmus, deren Abbuchung nicht schon in einer Reihe steckt
        linked = {i["document_id"] for i in items if i.get("document_id")} | {d for i in items for d in i.pop("_docs", [])}
    else:
        linked = set()
    owners = auth.visible_owner_ids(con, user, "all") if user.id != 0 else []
    for c in extras.contracts(con, owners, today) if owners else []:
        if c["interval"] != "jährlich" or any(d in linked for d in c["documents"]):
            continue
        doc = _visible_doc(con, user, c["documents"][0] if c["documents"] else None)
        cents = round(c["amount"] * 100)
        twin = next((i for i in items if i["source"] != "document" and _same_payee(c["sender"], i)
                     and abs(i["amount"] - cents) <= max(100, 0.01 * cents)), None)
        if twin:
            if doc and not twin["document_id"]:
                twin["document_id"], twin["label"] = doc["id"], twin["label"] or doc["title"]
            continue
        items.append({"key": f"doc:{c['documents'][0] if c['documents'] else c['sender']}", "name": c["sender"],
                      "label": doc["title"] if doc else None, "category": None, "amount": round(c["amount"] * 100),
                      "per_year": round(c["amount"] * 100), "previous": None, "change_pct": None, "last": c["last"],
                      "next": c["next_expected"], "years": {}, "active": True, "source": "document",
                      "in_sum": False, "document_id": doc["id"] if doc else None, "mark_tx": None, "payments": []})
    for i in items:
        i.pop("_docs", None)
        i.pop("_all", None)
    counted = [i for i in items if i["in_sum"]]
    per_year = sum(i["per_year"] for i in counted if i["active"])
    sched: dict[str, list] = {}
    for i in counted:
        if i["active"] and i["next"]:
            nd = _d(i["next"])
            while nd < today:
                nd = _plus_year(nd)
            if nd <= _plus_year(today):
                sched.setdefault(nd.isoformat()[:7], []).append({"name": i["label"] or i["name"], "cents": i["amount"]})
    items.sort(key=lambda i: (not i["active"], not i["in_sum"], -(i["per_year"] or 0)))
    return {
        "year": year, "today": today.isoformat(), "covered": covered,
        "items": items,
        "totals": {"this": sum(i["years"].get(str(year), 0) for i in counted),
                   "last": sum(i["years"].get(str(year - 1), 0) for i in counted),
                   "per_year": per_year, "monthly_needed": round(per_year / 12)},
        "schedule": [{"month": k, "cents": sum(x["cents"] for x in v), "items": v} for k, v in sorted(sched.items())],
        "pot": pot_info(con, user, accounts, today),
        "accounts": [{"id": a["id"], "name": a["name"]} for a in con.execute(
            f"SELECT id, name FROM bank_accounts WHERE id IN ({','.join('?' * len(accounts)) or 'NULL'}) ORDER BY name",
            accounts)],
    }


def _pot_items(con, user: User, accounts: list[int], items: list[dict], marklist: list[dict], year: int,
               today: dt.date) -> None:
    """Entnahmen aus dem Rücklagen-Topf: Was aus „Jährlich“ herausgenommen wird, ist eine Jahresausgabe – auch wenn sie
    von einem Konto bezahlt wird, das Papigator nicht kennt (PS Plus über ein anderes Konto, dann ausgeglichen).
    Passt eine Entnahme zu einer schon erkannten Abbuchung (Betrag ±2 %, höchstens drei Wochen daneben), ist es
    derselbe Posten – der bekommt nur den Vermerk „aus dem Topf“."""
    pot, _ = pot_account(con, user, accounts)
    if not pot:
        return
    used = {p["id"] for i in items for p in i.get("_all", [])}
    rows = [dict(r) for r in con.execute(
        "SELECT id, account_id, booked, cents, counterparty, purpose, category, document_id FROM bank_tx WHERE account_id=? "
        "AND cents < 0 AND booked >= ? AND COALESCE(category, '') IN ('', 'Umbuchung') ORDER BY booked, id",
        (pot["id"], (today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()))]
    rest = []
    for w in rows:
        if w["id"] in used:
            continue           # direkt vom Topf abgebucht: schon als Abbuchung erkannt
        twin = next((i for i in items if i["source"] in ("bank", "marked") and any(
            abs(p["cents"] + w["cents"]) <= max(100, 0.02 * p["cents"]) and abs((_d(p["booked"]) - _d(w["booked"])).days) <= 21
            for p in i["_all"])), None)
        if twin:
            twin["from_pot"] = True
            continue
        rest.append(w)
    groups: dict[str, list[dict]] = {}
    for w in rest:
        groups.setdefault(pot_key(w["purpose"], w["counterparty"]), []).append(w)
    # Schreibweisen zusammen: „Sony PsPlus“ und „Ps plus“ sind dasselbe (ohne Leerzeichen steckt eins im anderen)
    merged: dict[str, list[str]] = {}
    for key in sorted(groups, key=len):
        flat = re.sub(r"[^a-z0-9äöüß]", "", key[len("topf:"):])
        home = next((h for h in merged if len(flat) >= 4 and (lambda hf: hf and (hf in flat or flat in hf))(
            re.sub(r"[^a-z0-9äöüß]", "", h[len("topf:"):]))), None)
        merged.setdefault(home or key, []).append(key)
    for home, keys in merged.items():
        grows = sorted((w for k in keys for w in groups[k]), key=lambda w: (w["booked"], w["id"]))
        # zweimal derselbe Betrag binnen eines Monats: doppelt entnommen oder umgebucht, nicht zwei Jahresposten
        dedup: list[dict] = []
        for w in grows:
            if any(abs(w["cents"] - x["cents"]) <= 0.01 * -x["cents"] and 0 <= (_d(w["booked"]) - _d(x["booked"])).days <= 31
                   for x in dedup):
                continue
            dedup.append(w)
        _pot_series(con, user, pot, home, keys, dedup, marklist, items, year, today)


def _pot_series(con, user: User, pot, key: str, keys: list[str], grows: list[dict], marklist: list[dict],
                items: list[dict], year: int, today: dt.date) -> None:
    """Eine Gruppe Entnahmen (alle Schreibweisen in keys) → Posten: Reihen im Jahresabstand, Rest einzeln."""
    # „Nicht jährlich“ gilt für die Schreibweise, an der es gesetzt wurde – blendet also keine zusammengeführte
    # Reihe aus, deren jüngste Entnahme anders heißt
    no = [(m["mkey"], m["cents"]) for m in marklist if m["mkey"] in keys and not m["yearly"]]
    series = _chains(grows)
    inchain = {r["id"] for c in series for r in c}
    series += [[w] for w in grows if w["id"] not in inchain]       # einmal entnommen zählt auch
    series = [c for c in series if not any(k == pot_key(c[-1]["purpose"], c[-1]["counterparty"]) and _close(-c[-1]["cents"], n)
                                           for k, n in no)]
    for chain in series:
        it = _item(con, user, key, chain, "pot", year, today, len(series) > 1)
        last = chain[-1]
        raw = re.sub(r"\s+", " ", (last["purpose"] or "")).strip()
        it["name"] = (raw[:60] if key != "topf:" else "") or (last["counterparty"] or "").strip()[:60] or f"Entnahme aus {pot['name']}"
        it["label"] = None
        it["from_pot"] = True
        items.append(it)


def _name(counterparty: Optional[str], key: str) -> str:
    """Anzeigename: der Empfänger – bei PayPal, Klarna & Co. der Händler aus dem Verwendungszweck („Sony“)."""
    from . import bank
    cp = (counterparty or "").strip()
    if not cp or bank._AGGREGATOR.search(cp.lower()):
        return key[:1].upper() + key[1:]
    return cp


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())


def _same_payee(sender: Optional[str], item: dict) -> bool:
    """Absender eines Belegs und Empfänger einer Abbuchung: gleiches erstes Wort (≥ 4 Zeichen) reicht."""
    w = re.findall(r"[a-zäöüß0-9]{4,}", (sender or "").lower())
    return bool(w) and (w[0] in _norm(item["name"]) or w[0] in _norm(item["mkey"]))


def _item(con, user: User, key: str, chain: list[dict], source: str, year: int, today: dt.date, several: bool) -> dict:
    last, prev = chain[-1], (chain[-2] if len(chain) > 1 else None)
    amount = -last["cents"]
    years: dict[str, int] = {}
    for r in chain:
        years[r["booked"][:4]] = years.get(r["booked"][:4], 0) - r["cents"]
    docs = [r["document_id"] for r in chain if r["document_id"]]
    doc = next((d for d in (_visible_doc(con, user, x) for x in reversed(docs)) if d), None)
    if source == "marked":
        # von Hand markiert: so oft, wie es im letzten Jahr kam (Quartalsbeitrag ×4), mindestens einmal
        recent = [r for r in chain if (today - _d(r["booked"])).days < 365]
        per_year = sum(-r["cents"] for r in recent) or amount
        if len(recent) > 1:
            per_year = amount * len(recent)
    else:
        per_year = amount
    label = doc["title"] if doc else None
    if not label and several:
        # mehrere Reihen beim selben Händler (Kfz und Haftpflicht beim selben Versicherer): Zweck unterscheidet
        label = re.sub(r"\s+", " ", (last["purpose"] or "")).strip()[:60] or None
    return {
        "key": f"{key}|{amount}", "mkey": key, "name": _name(last["counterparty"], key), "label": label,
        "category": last["category"], "amount": amount, "per_year": per_year,
        "previous": -prev["cents"] if prev else None,
        "change_pct": round((amount / -prev["cents"] - 1) * 100) if prev and prev["cents"] else None,
        "last": last["booked"][:10], "next": _plus_year(_d(last["booked"])).isoformat(),
        "years": years, "active": (today - _d(last["booked"])).days <= ACTIVE_DAYS, "source": source, "in_sum": True,
        "document_id": doc["id"] if doc else None, "mark_tx": last["id"],
        "payments": [{"id": r["id"], "booked": r["booked"][:10], "cents": -r["cents"]} for r in chain[-4:]],
        "_docs": docs, "_all": [{"id": r["id"], "booked": r["booked"][:10], "cents": -r["cents"]} for r in chain],
        "from_pot": False,
    }


# ---------- Endpunkte ----------

@router.get("")
def get_yearly(year: Optional[int] = Query(None, ge=2000, le=2100), user: User = Depends(current_user)):
    with connect() as con:
        return overview(con, user, year)


class PotIn(BaseModel):
    account_id: Optional[int] = None


@router.put("/pot")
def set_pot(body: PotIn, user: User = Depends(current_user)):
    """Rücklagen-Topf wählen (null: keiner, dann wieder der Vorschlag nach Namen)."""
    from . import bank
    if user.id == 0:
        raise HTTPException(400, "Erst anmelden")
    with connect() as con:
        con.execute("DELETE FROM bank_pot WHERE user_id=?", (user.id,))
        if body.account_id is not None:
            bank._account(con, user, body.account_id)      # 404, wenn nicht sichtbar
            con.execute("INSERT INTO bank_pot(user_id, account_id) VALUES (?, ?)", (user.id, body.account_id))
        return {"ok": True}


class MarkIn(BaseModel):
    tx_id: int
    yearly: Optional[bool] = None


@router.put("/mark")
def mark(body: MarkIn, user: User = Depends(current_user)):
    """Eine Abbuchung als jährlich markieren (auch nach nur einer) oder als „nicht jährlich“ ausblenden; null nimmt
    die Markierung für diesen Händler und Betrag zurück."""
    from . import bank
    if user.id == 0:
        raise HTTPException(400, "Erst anmelden")
    with connect() as con:
        r = con.execute("SELECT * FROM bank_tx WHERE id=?", (body.tx_id,)).fetchone()
        if not r or r["account_id"] not in bank.visible_accounts(con, user):
            raise HTTPException(404, "Buchung nicht gefunden")
        if r["cents"] >= 0:
            raise HTTPException(400, "Nur Abbuchungen können jährlich sein")
        pot, _ = pot_account(con, user, bank.visible_accounts(con, user))
        key = pot_key(r["purpose"], r["counterparty"]) if pot and r["account_id"] == pot["id"] else bank.merchant_key(r["counterparty"], r["purpose"])
        if not key:
            raise HTTPException(400, "Für diese Buchung lässt sich kein Händler erkennen")
        cents = -r["cents"]
        # gleiche Markierung im selben Band ersetzen (Preis hat sich seit dem Markieren geändert)
        for m in _marks(con, user):
            if m["mkey"] == key and _close(m["cents"], cents):
                con.execute("DELETE FROM bank_yearly_marks WHERE user_id=? AND mkey=? AND cents=?", (user.id, key, m["cents"]))
        if body.yearly is not None:
            con.execute("INSERT INTO bank_yearly_marks(user_id, mkey, cents, yearly) VALUES (?, ?, ?, ?)",
                        (user.id, key, cents, 1 if body.yearly else 0))
        return {"ok": True, "key": key}
