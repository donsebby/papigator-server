"""Demo-Daten mitwandern lassen: alle Datumsangaben um dieselbe Anzahl Tage verschieben, damit offene Fristen in der
Zukunft liegen und „die letzten 6 Monate“ etwas finden, egal wann der Demoserver neu befüllt wird.

Bezugstag der Demo-Daten ist BASE (Stand der Texte in make_docs.py). Verschoben werden „19.11.2026“, „2026-11-19“
und Monatsangaben wie „Oktober 2026“ (über den 15. des Monats, damit sie zum Rest passen). Bloße Jahreszahlen
(„Steuerbescheid 2025“) bleiben – bei ein paar Monaten Abstand stört das nicht.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

BASE = date(2026, 11, 16)
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]
# Ein Durchgang mit einer Alternation – sonst würde ein schon verschobenes Datum noch einmal verschoben
_ANY = re.compile(
    r"\b(?P<r1>\d{1,2})\.\s*(?P<dash>[–-])\s*(?P<r2>\d{1,2})\.(?P<rm>\d{1,2})\.(?P<ry>\d{4})\b"   # 01.–03.12.2026
    r"|\b(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\b"                                      # 19.11.2026
    r"|\b(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})\b"                                           # 2026-11-19
    r"|\b(?P<mon>" + "|".join(MONTHS) + r")\s+(?P<my>\d{4})\b")                                # Oktober 2026


def today() -> date:
    """Heute – zum Ausprobieren überschreibbar mit DEMO_TODAY=JJJJ-MM-TT."""
    t = os.environ.get("DEMO_TODAY")
    return date.fromisoformat(t) if t else date.today()


def delta_days(day: date | None = None) -> int:
    return ((day or today()) - BASE).days


def shift_date(s: str | None, days: int) -> str | None:
    if not s or not days:
        return s
    try:
        return (date.fromisoformat(s[:10]) + timedelta(days=days)).isoformat()
    except ValueError:
        return s


def shift_text(text: str | None, days: int) -> str | None:
    if not text or not days:
        return text
    dd = timedelta(days=days)

    def one(m):
        try:
            if m["r1"]:
                y, mo = int(m["ry"]), int(m["rm"])
                a, b = date(y, mo, int(m["r1"])) + dd, date(y, mo, int(m["r2"])) + dd
                return f"{a.day:02d}.{a.month:02d}.{a.year}{m['dash']}{b.day:02d}.{b.month:02d}.{b.year}"
            if m["d"]:
                x = date(int(m["y"]), int(m["m"]), int(m["d"])) + dd
                return f"{x.day:02d}.{x.month:02d}.{x.year}" if len(m["d"]) == 2 else f"{x.day}.{x.month}.{x.year}"
            if m["iy"]:
                return (date(int(m["iy"]), int(m["im"]), int(m["id"])) + dd).isoformat()
            x = date(int(m["my"]), MONTHS.index(m["mon"]) + 1, 15) + dd
            return f"{MONTHS[x.month - 1]} {x.year}"
        except ValueError:
            return m[0]

    return _ANY.sub(one, text)
