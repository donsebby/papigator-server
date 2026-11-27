"""Kalender-Abo (25.11.2026): alle offenen Fristen, die ich sehe, als ein Kalender – in Apple Kalender, Google & Co.
einmal abonnieren, danach kommen neue Fristen von selbst dazu und erledigte verschwinden.

Der Link enthält ein zufälliges Token (≥ 256 Bit) statt einer Anmeldung, weil Kalender-Apps keine Anmeldung
mitschicken. In der Datenbank liegt es nur als SHA-256 (Abfrage) und verschlüsselt (zum erneuten Anzeigen in den
Einstellungen). Neuer Link = alter ungültig; abschalten jederzeit. Der Abruf läuft über den Hauptdienst – von
unterwegs also nur im Heimnetz oder per VPN, nie über den öffentlichen Freigabe-Dienst. In der Demo gesperrt.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import auth, security
from . import due as duemod
from .auth import User, current_user
from .db import connect

router = APIRouter()


def _path(token: str) -> str:
    return f"/calendar/{token}.ics"


@router.get("/calendar/feed")
def feed_info(user: User = Depends(current_user)):
    """Mein Abo-Link (Pfad) oder null, wenn keiner angelegt ist."""
    with connect() as con:
        r = con.execute("SELECT token_enc FROM calendar_feeds WHERE user_id=?", (user.id,)).fetchone()
    token = security.decrypt(r["token_enc"]) if r else None
    return {"path": _path(token) if token else None}


@router.post("/calendar/feed")
def feed_create(user: User = Depends(current_user)):
    """Neuen Abo-Link anlegen – ein vorhandener wird damit ungültig."""
    if user.id == 0:
        raise HTTPException(400, "Erst anmelden")
    token = secrets.token_urlsafe(32)
    with connect() as con:
        con.execute("INSERT INTO calendar_feeds(user_id, token_hash, token_enc) VALUES (?, ?, ?) ON CONFLICT(user_id) "
                    "DO UPDATE SET token_hash=excluded.token_hash, token_enc=excluded.token_enc, created_at=datetime('now')",
                    (user.id, security.token_hash(token), security.encrypt(token)))
    return {"path": _path(token)}


@router.delete("/calendar/feed", status_code=204)
def feed_delete(user: User = Depends(current_user)):
    with connect() as con:
        con.execute("DELETE FROM calendar_feeds WHERE user_id=?", (user.id,))


def _feed_user(con, token: str) -> User:
    r = con.execute("SELECT u.id, u.username, u.display_name, u.is_admin, u.show_partner_in_overview FROM calendar_feeds f "
                    "JOIN users u ON u.id=f.user_id WHERE f.token_hash=?", (security.token_hash(token),)).fetchone()
    if not r:
        raise HTTPException(404, "Unbekannter Kalender")
    return User(r["id"], r["username"], r["display_name"], bool(r["is_admin"]), bool(r["show_partner_in_overview"]))


def _open_dues(con, user: User) -> tuple[str, list]:
    owners = auth.visible_owner_ids(con, user, "all")
    marks = ",".join("?" * len(owners)) or "NULL"
    return (f"FROM documents WHERE deleted_at IS NULL AND ext_pending=0 AND due_date IS NOT NULL AND due_done=0 "
            f"AND owner_id IN ({marks})", list(owners))


@router.get("/calendar/{token}.ics", include_in_schema=False)
def feed(token: str, request: Request):
    with connect() as con:
        user = _feed_user(con, token)
        base, params = _open_dues(con, user)
        rows = con.execute(f"SELECT * {base} ORDER BY due_date LIMIT 1000", params).fetchall()
        who = {}
        for row in rows:
            a = duemod.assignee_fields(con, user, row)
            if a["due_assignee_name"]:
                who[row["id"]] = a["due_assignee_name"]
    body = duemod.feed_calendar(rows, request.url.hostname or "papigator", who)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="papigator-fristen.ics"'})


@router.get("/calendar/{token}/summary.json", include_in_schema=False)
def feed_summary(token: str):
    """Dieselben Fristen kurz gezählt (27.11.2026) – für einen REST-Sensor in Home Assistant & Co.: offen, überfällig,
    in den nächsten 7 Tagen und die nächste. Gleicher Link, gleiche Sichtbarkeit wie das Kalender-Abo (der Titel der
    nächsten Frist steht dort ohnehin)."""
    with connect() as con:
        user = _feed_user(con, token)
        base, params = _open_dues(con, user)
        today = con.execute("SELECT date('now', 'localtime')").fetchone()[0]
        n = lambda extra, *p: con.execute(f"SELECT COUNT(*) {base} {extra}", (*params, *p)).fetchone()[0]
        out = {"open": n(""), "overdue": n("AND due_date < ?", today),
               "due_7_days": n("AND due_date >= ? AND due_date <= date(?, '+7 days')", today, today),
               "next": None, "today": today}
        r = con.execute(f"SELECT id, title, due_date, due_note, julianday(due_date) - julianday(?) AS days {base} "
                        "ORDER BY due_date, id LIMIT 1", (today, *params)).fetchone()
        if r:
            out["next"] = {"id": r["id"], "title": r["title"], "date": r["due_date"], "note": r["due_note"],
                           "in_days": int(r["days"]) if r["days"] is not None else None}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
