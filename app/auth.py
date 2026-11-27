"""Nutzer, Sessions, Partner-Freigaben."""
import secrets
from dataclasses import dataclass
from typing import Optional

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException

from . import config, security
from .db import connect

# Anmeldungen, die so lange nicht benutzt wurden, verfallen (verlorenes Gerät, alter Browser)
SESSION_IDLE_DAYS = 90


@dataclass
class User:
    id: int
    username: str
    display_name: str
    is_admin: bool
    show_partner_in_overview: bool

    @classmethod
    def from_row(cls, r) -> "User":
        return cls(r["id"], r["username"], r["display_name"], bool(r["is_admin"]),
                   bool(r["show_partner_in_overview"]))


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except ValueError:
        return False


MIN_PASSWORD = 8


def check_new_user(con, username: str, password: str) -> None:
    """Gleiche Regeln für „Nutzer anlegen“ (Admin) und „Einladung einlösen“: 400 bzw. 409."""
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen")
    if not username.strip() or "/" in username or username.strip().startswith("."):
        raise HTTPException(400, "Ungültiger Benutzername")
    if con.execute("SELECT 1 FROM users WHERE username=?", (username.strip(),)).fetchone():
        raise HTTPException(409, "Benutzername existiert bereits")


def create_user(con, username: str, display_name: str, password: str, is_admin: bool = False) -> User:
    cur = con.execute(
        "INSERT INTO users(username, display_name, password_hash, is_admin) VALUES (?,?,?,?)",
        (username.strip(), display_name.strip() or username.strip(), hash_password(password), int(is_admin)),
    )
    return User.from_row(con.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())


LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_MIN = 10


def _attempt_keys(ip: str, username: str) -> list[str]:
    """Sperr-Schlüssel für Fehlversuche: IP und Benutzername. In der öffentlichen Demo nur die IP – sonst könnte
    jeder im Internet das gemeinsame Konto (z. B. das der App-Prüfer) mit fünf falschen Passwörtern sperren."""
    keys = [f"ip:{ip}"]
    if not config.DEMO:
        keys.append(f"user:{username.strip().lower()}")
    return keys


def login_locked(con, ip: str, username: str) -> int:
    """Sekunden bis zur Freigabe, 0 wenn nicht gesperrt."""
    # Nur die eigenen Schlüssel – Freigabe- und Briefkasten-Zähler haben ein eigenes 15-Minuten-Fenster
    con.execute("DELETE FROM login_attempts WHERE at < datetime('now', ?) AND (key LIKE 'ip:%' OR key LIKE 'user:%')",
                (f"-{LOGIN_WINDOW_MIN} minutes",))
    for key in _attempt_keys(ip, username):
        row = con.execute(
            "SELECT COUNT(*) AS n, MAX(at) AS last FROM login_attempts WHERE key=?", (key,)
        ).fetchone()
        if row["n"] >= LOGIN_MAX_FAILS:
            rem = con.execute("SELECT CAST((julianday(?) + ?/1440.0 - julianday('now')) * 86400 AS INT) AS s",
                              (row["last"], LOGIN_WINDOW_MIN)).fetchone()["s"]
            return max(int(rem or 0), 1)
    return 0


def login(con, username: str, password: str, label: str, ip: str = "") -> Optional[str]:
    row = con.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        con.executemany("INSERT INTO login_attempts(key) VALUES (?)", [(k,) for k in _attempt_keys(ip, username)])
        return None
    con.execute("DELETE FROM login_attempts WHERE key IN (?, ?)", (f"ip:{ip}", f"user:{username.strip().lower()}"))
    return new_session(con, row["id"], label, ip)


def new_session(con, user_id: int, label: str, ip: str = "") -> str:
    """Neue Anmeldung anlegen; zurück kommt der Token (nur der Client hat ihn)."""
    token = secrets.token_urlsafe(32)
    # In der Datenbank liegt nur die Prüfsumme – eine kopierte DB taugt nicht zum Anmelden
    con.execute("INSERT INTO sessions(token, user_id, label, ip, last_used) VALUES (?,?,?,?,datetime('now'))",
                (security.token_hash(token), user_id, label, ip))
    return token


def user_for_token(con, token: str) -> Optional[User]:
    h = security.token_hash(token)
    con.execute("DELETE FROM sessions WHERE COALESCE(last_used, created_at) < datetime('now', ?)",
                (f"-{SESSION_IDLE_DAYS} days",))
    row = con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (h,)
    ).fetchone()
    if row:
        con.execute("UPDATE sessions SET last_used=datetime('now') WHERE token=?", (h,))
        return User.from_row(row)
    return None


def _bootstrap_user(con) -> Optional[User]:
    """Solange es keine Nutzer gibt, gilt der statische PAPIGATOR_TOKEN als Admin-Bootstrap."""
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return User(0, "bootstrap", "Bootstrap", True, False)
    return None


def current_user(authorization: Optional[str] = Header(None),
                 papigator_token: Optional[str] = Cookie(None)) -> User:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif papigator_token:
        token = papigator_token
    if not token:
        raise HTTPException(401, "Nicht angemeldet")
    with connect() as con:
        if config.API_TOKEN and secrets.compare_digest(token, config.API_TOKEN):   # zeitkonstant
            u = _bootstrap_user(con)
            if u:
                return u
            raise HTTPException(401, "Bootstrap-Token ist nach dem ersten Nutzer deaktiviert")
        u = user_for_token(con, token)
    if not u:
        raise HTTPException(401, "Ungültiger Token")
    return u


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Nur für Admins")
    return user


# ---------- Sichtbarkeit ----------

def partner_ids(con, user_id: int) -> dict[int, bool]:
    """Nutzer, die ihren Pot für user_id freigegeben haben -> can_edit."""
    return {r["from_user"]: bool(r["can_edit"])
            for r in con.execute("SELECT from_user, can_edit FROM partner_shares WHERE to_user=?", (user_id,))}


def visible_owner_ids(con, user: User, scope: str) -> list[int]:
    """scope: mine | partner | all"""
    partners = list(partner_ids(con, user.id).keys())
    if scope == "mine":
        return [user.id]
    if scope == "partner":
        return partners
    return [user.id, *partners]


def can_view(con, user: User, owner_id: Optional[int]) -> bool:
    if user.id == 0:  # Bootstrap
        return True
    return owner_id == user.id or owner_id in partner_ids(con, user.id)


def can_edit(con, user: User, owner_id: Optional[int]) -> bool:
    if user.id == 0:
        return True
    if owner_id == user.id:
        return True
    return partner_ids(con, user.id).get(owner_id, False)
