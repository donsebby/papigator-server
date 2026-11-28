"""Zusatzkarten für „Heute“ (28.11.2026) – alles nur gerechnet, nichts gespeichert, alles abschaltbar (Heute anpassen,
`main.TODAY_SECTIONS`). Eine Abfrage für Web und App: `GET /today/extras`.

- cancel: Verträge, deren Kündigungsfrist in den nächsten 8 Wochen endet (extras.contracts, Jahresverträge fortgeschrieben)
  – mit dem, was der Vertrag im Jahr kostet. Steht die Frist schon offen unter „Fristen“, steht sie hier trotzdem: hier
  geht es um das Geld, dort um den Termin.
- tax: Steuerjahr – Januar bis Juli das Vorjahr (Erklärung bis 31.07.), sonst das laufende: Handwerkerleistungen
  (§ 35a, Arbeitskosten) und Spendenquittungen.
- inbox: was über Briefkasten-Links eingeworfen wurde und noch nicht übernommen ist (nur die eigenen Bestände – nur der
  Besitzer übernimmt), mit den Namen der Einwerfenden.
- year_ago: Briefe von heute vor einem Jahr bis zwei Wochen danach – Jährliches, das bald wieder kommt.
- week: die letzten 7 Tage: neu abgelegt, erledigte Fristen, Fristen der nächsten 7 Tage.
- system (nur Admins, nur bei Problemen): Sicherung überfällig, KI-Erkennung fehlgeschlagen, Dokumente mit Fehler.
„Zuletzt angesehen“ merkt sich jedes Gerät selbst (Web localStorage, App UserDefaults) – der Server weiß davon nichts.
"""
import datetime as dt

from fastapi import APIRouter, Depends

from . import auth, extras
from . import backup as backupmod
from .auth import User, current_user
from .db import connect

router = APIRouter()

CANCEL_DAYS = 56
YEAR_AGO_AHEAD = 14


def _marks(ids) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def tax_year(today: dt.date) -> int:
    return today.year - 1 if today.month <= 7 else today.year


def build(con, user: User, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    owners = auth.visible_owner_ids(con, user, "all")
    m = _marks(owners)
    base = f"deleted_at IS NULL AND ext_pending=0 AND owner_id IN ({m})"
    out: dict = {}

    cancel = []
    for c in extras.contracts(con, owners, today) if owners else []:
        cb = c.get("cancel_by")
        if cb and cb["date"] <= (today + dt.timedelta(days=CANCEL_DAYS)).isoformat():
            cancel.append({"sender": c["sender"], "date": cb["date"], "note": cb["note"], "document_id": cb["document_id"],
                           "amount": c["amount"], "interval": c["interval"], "per_year": round(extras.PER_MONTH[c["interval"]] * 12 * c["amount"], 2),
                           "in_days": (dt.date.fromisoformat(cb["date"]) - today).days})
    out["cancel"] = sorted(cancel, key=lambda x: x["date"])

    y = tax_year(today)
    r = con.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(tax_35a), 0) AS s FROM documents WHERE {base} "
                    "AND tax_35a > 0 AND substr(document_date, 1, 4) = ?", (*owners, str(y))).fetchone()
    don = con.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN currency IS NULL OR currency='EUR' THEN amount END), 0) AS s "
                      f"FROM documents WHERE {base} AND doc_kind='spende' AND substr(document_date, 1, 4) = ?",
                      (*owners, str(y))).fetchone()
    out["tax"] = {"year": y, "craft_count": r["n"], "craft_labor": round(r["s"], 2),
                  "donation_count": don["n"], "donations": round(don["s"], 2)} if r["n"] or don["n"] else None

    own = [user.id] if user.id else []
    rows = con.execute(f"SELECT d.id, d.title, d.ext_from FROM documents d WHERE d.deleted_at IS NULL AND d.ext_pending=1 "
                       f"AND d.owner_id IN ({_marks(own)}) ORDER BY d.created_at DESC, d.id DESC", own).fetchall()
    out["inbox"] = {"count": len(rows), "from": sorted({x["ext_from"] for x in rows if x["ext_from"]})[:3],
                    "document_id": rows[0]["id"]} if rows else None

    lo = (today - dt.timedelta(days=365)).isoformat()
    hi = (today - dt.timedelta(days=365 - YEAR_AGO_AHEAD)).isoformat()
    out["year_ago"] = [dict(x) for x in con.execute(
        f"SELECT id, title, sender, document_date AS date FROM documents WHERE {base} AND document_date BETWEEN ? AND ? "
        "ORDER BY document_date, id LIMIT 4", (*owners, lo, hi))]

    week_ago = (today - dt.timedelta(days=7)).isoformat()
    out["week"] = {
        "new": con.execute(f"SELECT COUNT(*) FROM documents WHERE {base} AND date(created_at, 'localtime') > ?",
                           (*owners, week_ago)).fetchone()[0],
        "done": con.execute(f"SELECT COUNT(*) FROM documents WHERE {base} AND due_done=1 AND due_date IS NOT NULL "
                            "AND date(updated_at, 'localtime') > ?", (*owners, week_ago)).fetchone()[0],
        "next7": con.execute(f"SELECT COUNT(*) FROM documents WHERE {base} AND due_done=0 AND due_date IS NOT NULL "
                             "AND due_date BETWEEN ? AND ?", (*owners, today.isoformat(),
                                                              (today + dt.timedelta(days=7)).isoformat())).fetchone()[0],
    }

    problems = []
    if user.is_admin:
        bs = backupmod.settings()
        if bs["dir"] and backupmod.is_stale(bs):
            problems.append({"kind": "backup", "since": bs["last"]})
        failed = con.execute("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL AND llm_status='failed'").fetchone()[0]
        if failed:
            problems.append({"kind": "ai", "count": failed})
        errors = con.execute("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL AND status='error'").fetchone()[0]
        if errors:
            problems.append({"kind": "errors", "count": errors})
    out["system"] = problems
    return out


@router.get("/today/extras")
def today_extras(user: User = Depends(current_user)):
    if user.id == 0:
        return {"cancel": [], "tax": None, "inbox": None, "year_ago": [], "week": None, "system": []}
    with connect() as con:
        return build(con, user)
