"""Einladungen: Familie und Freunde ohne Server-Adresse und Passwort abtippen dazuholen.

Ein Admin legt eine Einladung an (Anzeigename, optional Admin-Recht und eine Freigabe seines eigenen Pots für
die neue Person). Heraus kommt ein Link `<öffentliche Adresse>/app/#invite=<token>` – in der Weboberfläche auch
als QR-Code, den die App scannt. Wer ihn öffnet, sieht nur den Anzeigenamen und den Servernamen, wählt
Benutzername und Passwort und ist danach angemeldet.

- Der Token steht hinter `#`: Browser schicken ihn beim Laden der Seite nicht an den Server (kein Access-Log,
  kein Referer). Die Abfrage `GET /invites/<token>` taucht im Access-Log nur geschwärzt auf (Filter unten).
- In der Datenbank liegt nur die SHA-256-Prüfsumme; der Link wird genau einmal angezeigt.
- Einmal einlösbar, 7 Tage gültig, jederzeit zurückziehbar. Unbekannt/zurückgezogen → 404, abgelaufen oder
  schon benutzt → 410. Fehlversuche pro IP werden gezählt (wie bei Freigabe-Links).
- Nur im Hauptdienst eingehängt, nie in share_app: Konten anlegen gehört nicht auf den öffentlichen Port.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import auth, security
from .auth import User, require_admin
from .db import connect

router = APIRouter()
INVITE_DAYS = 7
MAX_MISSES = 20          # unbekannte/abgelaufene Tokens pro IP in 15 Minuten
SHARE_MODES = ("none", "read", "edit")


# ---------- Access-Log: Token schwärzen ----------

from . import logredact  # noqa: F401  – kürzt Tokens (auch /invites/<token>) im Zugriffs-Log



# ---------- Hilfen ----------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _base(con, request: Request) -> str:
    from .share import public_base   # öffentliche Adresse wie bei Freigabe-Links
    return public_base(con, request)


def _server_name(base: str) -> str:
    return urlsplit(base).hostname or "Papigator"


def _ip(request: Request) -> str:
    from .main import client_ip
    return client_ip(request)[:64] or "?"


def _check_rate(con, request: Request) -> str:
    key = f"invite-ip:{_ip(request)}"
    con.execute("DELETE FROM login_attempts WHERE key LIKE 'invite-ip:%' AND at < datetime('now', '-15 minutes')")
    if con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (key,)).fetchone()[0] >= MAX_MISSES:
        raise HTTPException(429, "Zu viele Versuche – bitte in einer Viertelstunde noch einmal")
    return key


def _miss(key: str, status: int, detail: str):
    """Fehlversuch zählen (eigene Verbindung, damit ein Rollback ihn nicht verschluckt) und abbrechen."""
    with connect() as con:
        con.execute("INSERT INTO login_attempts(key) VALUES (?)", (key,))
    raise HTTPException(status, detail)


def _load(con, token: str, key: str):
    """Einladung zum Token – oder 404 (unbekannt/zurückgezogen) bzw. 410 (abgelaufen/benutzt)."""
    row = con.execute("SELECT * FROM invites WHERE token=?", (security.token_hash(token),)).fetchone() \
        if 20 <= len(token) <= 200 else None
    if not row:
        _miss(key, 404, "Einladung nicht gefunden")
    if row["used_at"]:
        _miss(key, 410, "Diese Einladung wurde schon benutzt")
    if row["expires_at"] <= _now():
        _miss(key, 410, "Diese Einladung ist abgelaufen")
    return row


def _out(r, url: Optional[str] = None) -> dict:
    out = {"id": r["id"], "display_name": r["display_name"], "is_admin": bool(r["is_admin"]),
           "share_with_me": r["share_mode"], "expires_at": r["expires_at"], "created_at": r["created_at"],
           "expired": r["expires_at"] <= _now()}
    if "inviter" in r.keys():
        out["invited_by"] = r["inviter"]
    if url:
        out["url"] = url
    return out


# ---------- Admin: anlegen, auflisten, zurückziehen ----------

class InviteIn(BaseModel):
    display_name: str = Field(..., max_length=80)
    is_admin: bool = False
    share_with_me: str = "none"   # none | read | edit: Pot des Einladenden für die neue Person


@router.post("/invites", status_code=201)
def create_invite(body: InviteIn, request: Request, admin: User = Depends(require_admin)):
    if admin.id == 0:
        raise HTTPException(400, "Erst einen Nutzer anlegen")
    name = body.display_name.strip()
    if not name:
        raise HTTPException(400, "Name fehlt")
    if body.share_with_me not in SHARE_MODES:
        raise HTTPException(400, "share_with_me muss none, read oder edit sein")
    token = secrets.token_urlsafe(32)   # 256 Bit
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as con:
        iid = con.execute(
            "INSERT INTO invites(token, created_by, display_name, is_admin, share_mode, expires_at) VALUES (?,?,?,?,?,?)",
            (security.token_hash(token), admin.id, name, int(body.is_admin), body.share_with_me, expires)).lastrowid
        row = con.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone()
        url = f"{_base(con, request)}/app/#invite={token}"
    return _out(row, url)


@router.get("/invites")
def list_invites(admin: User = Depends(require_admin)):
    """Offene Einladungen (noch nicht eingelöst) aller Admins; der Link selbst ist nicht mehr abrufbar."""
    with connect() as con:
        # Lange abgelaufene räumen sich selbst weg
        con.execute("DELETE FROM invites WHERE expires_at < datetime('now', '-30 days')")
        rows = con.execute(
            "SELECT i.*, u.display_name AS inviter FROM invites i JOIN users u ON u.id=i.created_by "
            "WHERE i.used_at IS NULL ORDER BY i.created_at DESC, i.id DESC").fetchall()
    return [_out(r) for r in rows]


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_invite(invite_id: int, admin: User = Depends(require_admin)):
    with connect() as con:
        con.execute("DELETE FROM invites WHERE id=? AND used_at IS NULL", (invite_id,))


# ---------- Öffentlich: ansehen und einlösen ----------

@router.get("/invites/{token}")
def invite_info(token: str, request: Request, response: Response):
    """Ohne Anmeldung: nur Anzeigename und Servername – sonst nichts über den Server oder andere Nutzer."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    with connect() as con:
        key = _check_rate(con, request)
    with connect() as con:
        row = _load(con, token, key)
        return {"display_name": row["display_name"], "server_name": _server_name(_base(con, request))}


class RedeemIn(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)
    label: Optional[str] = Field("Web", max_length=80)


@router.post("/invites/{token}/redeem")
def redeem_invite(token: str, body: RedeemIn, request: Request, response: Response):
    """Konto anlegen, gewählte Freigabe eintragen, Einladung verbrauchen, anmelden – alles oder nichts."""
    from .main import is_https
    with connect() as con:
        key = _check_rate(con, request)
    with connect() as con:
        row = _load(con, token, key)
        auth.check_new_user(con, body.username, body.password)
        # Einladung beanspruchen, bevor das Konto entsteht: zwei gleichzeitige Versuche bekommen nur ein Konto
        claimed = con.execute("UPDATE invites SET used_at=datetime('now') WHERE id=? AND used_at IS NULL "
                              "AND expires_at > ?", (row["id"], _now())).rowcount
        if claimed != 1:
            raise HTTPException(410, "Diese Einladung wurde schon benutzt")
        u = auth.create_user(con, body.username, row["display_name"], body.password, is_admin=bool(row["is_admin"]))
        con.execute("UPDATE invites SET used_by=? WHERE id=?", (u.id, row["id"]))
        if row["share_mode"] in ("read", "edit"):
            con.execute("INSERT OR REPLACE INTO partner_shares(from_user, to_user, can_edit) VALUES (?,?,?)",
                        (row["created_by"], u.id, int(row["share_mode"] == "edit")))
        session = auth.new_session(con, u.id, (body.label or "Web").strip()[:80] or "Web", _ip(request))
    response.set_cookie("papigator_token", session, max_age=365 * 86400, httponly=True, samesite="lax",
                        secure=is_https(request))
    return {"token": session, "username": u.username, "display_name": u.display_name}
