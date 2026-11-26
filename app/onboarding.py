"""„Los geht’s“ (26.11.2026): die ersten Schritte für neue Konten als Häkchen-Liste in App und Web.

`GET /onboarding` liefert die Schritte mit Häkchen und ob der Client die Karte zeigen soll. Gezeigt wird sie nur neuen
Konten: Konten, die es beim Einführen schon gab, stehen in `onboarding_dismissed` (wie „ausgeblendet“, Muster
`whatsnew_seen`). Nie in der Demo, nie nach „Ausblenden“ und nicht mehr, wenn alle Schritte erledigt sind.

Schritte (die Häkchen ermittelt der Server, der Client merkt sich nichts):
- first_document: das Konto hat ein Dokument abgelegt (auch eines im Papierkorb zählt – es war ja schon da)
- app: eine Anmeldung, die nicht aus dem Browser kommt. Die Web-App meldet sich mit dem Label „Web“ an, die iOS-App
  mit dem Gerätenamen („iPhone von Alex“) – alles andere als „Web“ gilt als App.
- invite: nur für Admins (nur sie dürfen einladen): Einladung erstellt, weitere Konten da oder eigener Bestand freigegeben
- persons: Personen ohne Konto, die das Konto kennt (selbst angelegt oder im sichtbaren Bestand)
- inbox: E-Mail-Eingang eingerichtet oder der Scanner-Eingang hat schon etwas abgelegt. Admins: irgendein Eingang des
  Servers; sonst nur der eigene Scanner-Ordner bzw. ein E-Mail-Eingang, der an dieses Konto liefert.
"""
from fastapi import APIRouter, Depends

from . import config
from .auth import User, current_user
from .db import connect, get_setting, set_setting

router = APIRouter()

STEPS = ("first_document", "app", "invite", "persons", "inbox")


def mark_inbox_import(con, owner_id: int) -> None:
    """Vom Scanner-Eingang aufgerufen: „Eingang einrichten“ gilt danach als erledigt (für den Besitzer und den Server)."""
    set_setting(con, f"inbox_used:{owner_id}", "1")
    set_setting(con, "inbox_used", "1")


def _mail_ready(con) -> tuple[bool, str]:
    on = (get_setting(con, "mail_enabled", "0") or "0") == "1" and bool(get_setting(con, "mail_host", "") or "")
    return on, get_setting(con, "mail_owner", "") or ""


def steps(con, user: User) -> list[dict]:
    uid = user.id
    out = [{"key": "first_document",
            "done": con.execute("SELECT 1 FROM documents WHERE owner_id=? LIMIT 1", (uid,)).fetchone() is not None},
           {"key": "app",
            "done": con.execute("SELECT 1 FROM sessions WHERE user_id=? AND TRIM(COALESCE(label, '')) NOT IN ('', 'Web') "
                                "LIMIT 1", (uid,)).fetchone() is not None}]
    if user.is_admin:
        invited = (con.execute("SELECT 1 FROM invites WHERE created_by=? LIMIT 1", (uid,)).fetchone()
                   or con.execute("SELECT 1 FROM users WHERE id<>? LIMIT 1", (uid,)).fetchone()
                   or con.execute("SELECT 1 FROM partner_shares WHERE from_user=? LIMIT 1", (uid,)).fetchone())
        out.append({"key": "invite", "done": invited is not None})
    from .family import _known_persons
    out.append({"key": "persons", "done": bool(_known_persons(con, user))})
    mail_on, mail_owner = _mail_ready(con)
    if user.is_admin:
        inbox = mail_on or get_setting(con, "inbox_used") == "1"
    else:
        inbox = (mail_on and mail_owner == str(uid)) or get_setting(con, f"inbox_used:{uid}") == "1"
    out.append({"key": "inbox", "done": bool(inbox)})
    return out


@router.get("/onboarding")
def get_onboarding(user: User = Depends(current_user)):
    """{"show", "steps": [{"key", "done"}], "dismissed"} – show nur für neue Konten mit offenen Schritten."""
    if user.id == 0:
        return {"show": False, "steps": [], "dismissed": False}
    with connect() as con:
        dismissed = con.execute("SELECT 1 FROM onboarding_dismissed WHERE user_id=?", (user.id,)).fetchone() is not None
        out = steps(con, user)
    show = not config.DEMO and not dismissed and not all(s["done"] for s in out)
    return {"show": show, "steps": out, "dismissed": dismissed}


@router.post("/onboarding/dismiss", status_code=204)
def dismiss_onboarding(user: User = Depends(current_user)):
    """„Ausblenden“: die Karte kommt für dieses Konto nicht wieder (auf allen Geräten)."""
    if user.id == 0:
        return
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO onboarding_dismissed(user_id) VALUES (?)", (user.id,))
