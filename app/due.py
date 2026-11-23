"""Fristen für die Familie: wer kümmert sich, wiederkehrende Fristen, Kalender-Eintrag (.ics).

Sichtbarkeit: Zuständig sein kann nur, wer das Dokument sieht (Besitzer oder jemand, dem der Besitzer
seine Dokumente freigegeben hat). Namen gibt es nur für Personen, die der Fragende ohnehin kennt
(er selbst, Freigaben in beide Richtungen, Admins alle) – sonst bleibt der Name leer.
"""
import calendar
import re
import datetime as dt
from typing import Optional

from .auth import User

REPEATS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def known_user_ids(con, user: User) -> Optional[set[int]]:
    """Nutzer, deren Namen `user` sehen darf; None = alle (Admin, Bootstrap)."""
    if user.is_admin or user.id == 0:
        return None
    ids = {user.id}
    ids |= {r[0] for r in con.execute("SELECT from_user FROM partner_shares WHERE to_user=?", (user.id,))}
    ids |= {r[0] for r in con.execute("SELECT to_user FROM partner_shares WHERE from_user=?", (user.id,))}
    return ids


def viewer_ids(con, owner_id: Optional[int]) -> set[int]:
    """Wer Dokumente dieses Besitzers sieht: er selbst und alle, denen er freigegeben hat."""
    if owner_id is None:
        return set()
    return {owner_id, *(r[0] for r in con.execute("SELECT to_user FROM partner_shares WHERE from_user=?", (owner_id,)))}


def candidates(con, user: User, owner_id: Optional[int]) -> list[dict]:
    """Wen `user` für ein Dokument dieses Besitzers als zuständig eintragen darf."""
    ids = viewer_ids(con, owner_id)
    known = known_user_ids(con, user)
    if known is not None:
        ids &= known
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [{"id": r["id"], "display_name": r["display_name"]} for r in con.execute(
        f"SELECT id, display_name FROM users WHERE id IN ({marks}) ORDER BY display_name COLLATE NOCASE", tuple(ids))]


def effective_assignee(con, row) -> Optional[int]:
    """Eingetragene Person, solange sie das Dokument noch sieht (Freigabe entzogen, Besitzer gewechselt → niemand)."""
    a = row["due_assignee"] if "due_assignee" in row.keys() else None
    if a is None or a not in viewer_ids(con, row["owner_id"]):
        return None
    return a


# Platzhalter für „jemand, den du nicht kennst“: weder Name noch Nutzer-ID verlassen den Server.
# Schickt ein Client ihn beim Speichern zurück, bleibt die Zuteilung, wie sie ist.
UNKNOWN_ASSIGNEE = -1


def assignee_fields(con, user: User, row) -> dict:
    a = effective_assignee(con, row)
    if a is None:
        return {"due_assignee": None, "due_assignee_name": None}
    known = known_user_ids(con, user)
    if known is not None and a not in known:
        return {"due_assignee": UNKNOWN_ASSIGNEE, "due_assignee_name": None}
    r = con.execute("SELECT display_name FROM users WHERE id=?", (a,)).fetchone()
    return {"due_assignee": a, "due_assignee_name": r["display_name"] if r else None}


# SQL-Bedingung „mir zugeteilt oder niemandem“ – eine Person, die das Dokument nicht mehr sieht, zählt als niemand
MINE_DUE_SQL = ("(d.due_assignee IS NULL OR d.due_assignee = ? OR NOT (d.due_assignee = d.owner_id OR EXISTS "
                "(SELECT 1 FROM partner_shares ps WHERE ps.from_user = d.owner_id AND ps.to_user = d.due_assignee)))")


def add_months(day: dt.date, months: int, want_day: int | None = None) -> dt.date:
    """Monate addieren; am Monatsende auf den letzten Tag kürzen (31.01. + 1 Monat = 28./29.02.).
    want_day: der eigentlich gewünschte Tag, falls day schon gekürzt war (28.02. mit Wunsch 31 → 31.03.)."""
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(max(day.day, want_day or 0), calendar.monthrange(y, m)[1]))


def next_due(due: str, repeat: str, want_day: int | None = None) -> str:
    """Nächster Termin einer wiederkehrenden Frist: genau ein Intervall weiter. Liegt er noch in der Vergangenheit,
    bleibt er überfällig – verpasste Termine gehen so nicht still verloren, jeder Haken rückt eins weiter.
    Ein am Monatsende gekürzter Termin springt danach wieder auf den Wunschtag (31.01. → 28.02. → 31.03.)."""
    return add_months(dt.date.fromisoformat(due[:10]), REPEATS[repeat], want_day).isoformat()


def wish_day(due: str, want_day: int | None) -> int:
    """Wunschtag einer Wiederholung: gespeicherter Tag oder der Tag der Frist selbst."""
    return max(dt.date.fromisoformat(due[:10]).day, want_day or 0)


def valid_date(s: str) -> bool:
    """Echtes Datum im Format YYYY-MM-DD (kein 30.02.)."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        return False
    return True


# ---------- Kalender ----------

_RRULE = {"monthly": "FREQ=MONTHLY", "quarterly": "FREQ=MONTHLY;INTERVAL=3", "yearly": "FREQ=YEARLY"}


def _esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _fold(line: str) -> str:
    """Zeilen nach RFC 5545 bei 75 Oktetten umbrechen (Fortsetzung beginnt mit Leerzeichen)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def ics(row, host: str = "papigator") -> str:
    """Ganztägiger Termin am Tag der Frist mit Erinnerung drei Tage vorher; wiederkehrend mit RRULE."""
    day = dt.date.fromisoformat(row["due_date"][:10])
    title = row["title"] or "Dokument"
    desc = "\n".join(x for x in (row["due_note"], row["sender"]) if x)
    repeat = row["due_repeat"] if "due_repeat" in row.keys() else None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Papigator//Fristen//DE", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:papigator-doc-{row['id']}-due@{host}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_esc('Frist: ' + title)}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{_esc(desc)}")
    if repeat in _RRULE:
        rule = _RRULE[repeat]
        want = wish_day(row["due_date"], row["due_day"] if "due_day" in row.keys() else None)
        if want > 28:   # sonst fallen Monate ohne diesen Tag im Kalender ganz aus (RFC 5545)
            rule += (f";BYMONTH={day.month}" if repeat == "yearly" else "") + \
                f";BYMONTHDAY={','.join(str(d) for d in range(28, want + 1))};BYSETPOS=-1"
        lines.append(f"RRULE:{rule}")
    lines += ["TRANSP:TRANSPARENT",
              "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc('Frist: ' + title)}", "TRIGGER:-P3D", "END:VALARM",
              "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
