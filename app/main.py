import datetime as _dt
import hashlib
import json as _json
import logging
import math
import re
import zipfile
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from starlette.concurrency import run_in_threadpool
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import threading

from . import ask as askmod, auth, backup as backupmod, config, demo, due as duemod, extras, folders, inbox, llm, mail as mailmod, processing, security, smartsearch, worker
from .auth import User, current_user, require_admin
from .db import connect, refresh_parent_tags, get_setting, init_db, search_where, set_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# API-Doku (/docs, /redoc, /openapi.json) nur auf Wunsch – sonst sieht sie jeder ohne Anmeldung (PAPIGATOR_API_DOCS=1)
from .version import VERSION  # noqa: E402
app = FastAPI(title="Papigator", version=VERSION, **({} if config.API_DOCS else {"docs_url": None, "redoc_url": None, "openapi_url": None}))

@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """CSRF-Schutz: Ändernde Anfragen mit Anmelde-Cookie nur von der eigenen Seite. SameSite=Lax hält fremde
    Seiten ab, aber nicht andere Subdomains derselben Domain (z. B. fotos.… → papigator.…). Bearer-Token
    (App) und öffentliche /s/-Seiten sind nicht betroffen; Browser ohne Sec-Fetch-Site lassen wir durch."""
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.startswith("/s/")
            and "authorization" not in request.headers and "papigator_token" in request.cookies
            and request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Anfrage von einer anderen Seite abgelehnt"}, status_code=403)
    return await call_next(request)


# Öffentliche Demo (PAPIGATOR_DEMO=1): Sperrliste, Upload-Größe und Tageslimit an einer Stelle (app/demo.py)
app.middleware("http")(demo.guard)


from . import share  # noqa: E402  Freigabe-Links (/links, öffentlich /s/<token>)
app.include_router(share.router)
app.include_router(share.public)
from . import duplicates  # noqa: E402  Dubletten-Suche (/duplicates)
app.include_router(duplicates.router)
from . import family, cleanup  # noqa: E402  Personen, Notfallmappe (/persons, /emergency), Aufräumen (/cleanup)
app.include_router(family.router)
app.include_router(cleanup.router)
from . import bank  # noqa: E402  Geld: Kontoumsätze aus CSV (/bank)
app.include_router(bank.router)
from . import invites  # noqa: E402  Einladungen (/invites) – nur im Hauptdienst, nie in share_app
app.include_router(invites.router)
from . import yearly  # noqa: E402  Jährliche Kosten und Rücklagen-Topf (/bank/yearly)
app.include_router(yearly.router)
from . import avatars  # noqa: E402  Profilbilder für Konten und Personen ohne Konto (25.11.2026)
app.include_router(avatars.router)
from . import updates  # noqa: E402  Hinweis auf neue Server-Versionen (25.11.2026)
app.include_router(updates.router)
from . import notes  # noqa: E402  Notizen am Dokument (25.11.2026)
app.include_router(notes.router)
from . import calfeed  # noqa: E402  Kalender-Abo für alle Fristen (25.11.2026)
app.include_router(calfeed.router)
from . import cancel  # noqa: E402  Kündigungshelfer (25.11.2026)
app.include_router(cancel.router)
from . import whatsnew  # noqa: E402  Willkommen und „Was ist neu“ (25.11.2026)
app.include_router(whatsnew.router)


@app.on_event("startup")
def startup() -> None:
    config.ensure_dirs()
    init_db()
    with connect() as con:
        security.migrate(con)
    security.harden_files()
    worker.start()
    inbox.start()


# ---------- Helpers ----------

def serialize(con, row, user: User) -> dict:
    tags = [
        {"id": t["id"], "name": t["name"], "color": t["color"], "source": t["source"], "kind": t["kind"]}
        for t in con.execute(
            "SELECT t.id, t.name, t.color, t.kind, dt.source FROM document_tags dt "
            "JOIN tags t ON t.id = dt.tag_id WHERE dt.document_id=? ORDER BY t.name",
            (row["id"],),
        )
    ]
    d = dict(row)
    d.pop("text", None)
    d["tags"] = tags
    try:   # Nummern (Vertrag, Kunde, Aktenzeichen …) als Objekt, gespeichert als JSON
        d["refs"] = _json.loads(row["refs"]) if "refs" in row.keys() and row["refs"] else {}
    except ValueError:
        d["refs"] = {}
    d["thumbnail_url"] = f"/documents/{row['id']}/thumbnail"
    d["file_url"] = f"/documents/{row['id']}/file"
    d["is_own"] = row["owner_id"] == user.id
    d["can_edit"] = auth.can_edit(con, user, row["owner_id"])
    owner = con.execute("SELECT display_name FROM users WHERE id=?", (row["owner_id"],)).fetchone()
    d["owner_name"] = owner["display_name"] if owner else None
    d.update(duemod.assignee_fields(con, user, row))
    d["review_reasons"] = review_reasons(d)
    return d


def review_reasons(d: dict) -> list[str]:
    """Warum ein Dokument unter „Zu prüfen“ steht (gleiche Regeln wie REVIEW_SQL), für die Anzeige in App und Web."""
    if d.get("deleted_at") or d.get("status") in ("pending", "processing", "tagging"):
        return []
    out = []
    if d.get("status") == "error":
        out.append("error")
    elif d.get("llm_status") == "failed":
        out.append("llm_failed")
    if not d.get("document_date"):
        if not d.get("no_date"):
            out.append("no_date")
    elif d.get("date_check"):
        out.append("date_check")
    if not d.get("tags"):
        out.append("no_tags")
    if d.get("ext_pending"):
        out.append("external")
    return out


def get_doc(con, doc_id: int, user: User, edit: bool = False):
    row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row or not auth.can_view(con, user, row["owner_id"]):
        raise HTTPException(404, "Dokument nicht gefunden")
    if edit and not auth.can_edit(con, user, row["owner_id"]):
        raise HTTPException(403, "Keine Bearbeitungsrechte")
    return row


def set_tags(con, doc_id: int, names: list[str], source: str = "user") -> None:
    for name in names:
        name = name.strip()
        if name:
            con.execute("INSERT OR IGNORE INTO tags(name, created_by) "
                        "VALUES (?, (SELECT owner_id FROM documents WHERE id=?))", (name, doc_id))
            tid = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]
            con.execute("INSERT OR IGNORE INTO document_tags VALUES (?,?,?)", (doc_id, tid, source))
            # War der Tag schon als Oberordner automatisch da, gilt er jetzt als selbst gesetzt
            con.execute("UPDATE document_tags SET source=? WHERE document_id=? AND tag_id=? AND source='parent'",
                        (source, doc_id, tid))


# ---------- Auth ----------

class LoginIn(BaseModel):
    username: str
    password: str
    label: Optional[str] = "Web"


def client_ip(request: Request) -> str:
    """Besucher-IP; weitergeleitete Angaben nur von vertrauten Proxys (siehe app/clientip.py)."""
    from .clientip import client_ip as _ip
    return _ip(request)


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@app.post("/auth/login")
def do_login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    with connect() as con:
        wait = auth.login_locked(con, ip, body.username)
        if wait:
            raise HTTPException(429, f"Zu viele Fehlversuche – bitte {wait // 60 + 1} Minute(n) warten")
        token = auth.login(con, body.username, body.password, body.label or "Web", ip)
    if not token:
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    response.set_cookie("papigator_token", token, max_age=365 * 86400, httponly=True, samesite="lax",
                        secure=is_https(request))
    return {"token": token}


@app.get("/auth/sessions")
def list_sessions(request: Request, user: User = Depends(current_user)):
    """Angemeldete Geräte des Nutzers."""
    cur = security.token_hash(_current_token(request))
    with connect() as con:
        rows = con.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY last_used DESC", (user.id,)).fetchall()
    if config.DEMO:   # gemeinsames Konto: Geräte und IP-Adressen anderer Besucher gehen niemanden etwas an
        rows = [r for r in rows if r["token"] == cur]
    return [{"id": r["token"][:8], "label": r["label"], "ip": r["ip"], "created_at": r["created_at"],
             "last_used": r["last_used"], "current": r["token"] == cur} for r in rows]


@app.delete("/auth/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, user: User = Depends(current_user)):
    with connect() as con:
        con.execute("DELETE FROM sessions WHERE user_id=? AND substr(token,1,8)=?", (user.id, session_id))


@app.delete("/auth/sessions", status_code=204)
def revoke_other_sessions(request: Request, user: User = Depends(current_user)):
    cur = security.token_hash(_current_token(request))
    with connect() as con:
        con.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user.id, cur))


@app.post("/auth/logout")
def do_logout(request: Request, response: Response, user: User = Depends(current_user)):
    # Web schickt das Cookie, die App den Bearer-Token – beide Sitzungen beenden
    auth_header = request.headers.get("authorization") or ""
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else request.cookies.get("papigator_token")
    if token:
        with connect() as con:
            con.execute("DELETE FROM sessions WHERE token=?", (security.token_hash(token),))
    response.delete_cookie("papigator_token")
    return {"ok": True}


@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    with connect() as con:
        partners_in = [
            {"id": r["id"], "display_name": r["display_name"], "can_edit": bool(r["can_edit"]), "avatar": avatars.version("u", r["id"])}
            for r in con.execute(
                "SELECT u.id, u.display_name, p.can_edit FROM partner_shares p JOIN users u ON u.id=p.from_user "
                "WHERE p.to_user=?", (user.id,))
        ]
        partners_out = [
            {"id": r["id"], "display_name": r["display_name"], "can_edit": bool(r["can_edit"]), "avatar": avatars.version("u", r["id"])}
            for r in con.execute(
                "SELECT u.id, u.display_name, p.can_edit FROM partner_shares p JOIN users u ON u.id=p.to_user "
                "WHERE p.from_user=?", (user.id,))
        ]
        needs_setup = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "is_admin": user.is_admin, "show_partner_in_overview": user.show_partner_in_overview,
        "avatar": avatars.version("u", user.id) if user.id else None,
        "shared_with_me": partners_in, "shared_by_me": partners_out, "needs_setup": needs_setup,
        "demo": config.DEMO,
    }


class SettingsIn(BaseModel):
    display_name: Optional[str] = None
    show_partner_in_overview: Optional[bool] = None
    password: Optional[str] = None
    current_password: Optional[str] = None   # Pflicht beim Passwortwechsel, außer für Admins


MIN_PASSWORD = auth.MIN_PASSWORD


def _current_token(request: Request) -> str:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else (request.cookies.get("papigator_token") or "")


@app.patch("/auth/me")
def update_me(body: SettingsIn, request: Request, user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, "Bootstrap-Nutzer hat keine Einstellungen")
    if body.password is not None and len(body.password) < MIN_PASSWORD:
        raise HTTPException(400, f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen")
    with connect() as con:
        if body.password and not user.is_admin:
            row = con.execute("SELECT password_hash FROM users WHERE id=?", (user.id,)).fetchone()
            if not body.current_password or not auth.verify_password(body.current_password, row["password_hash"]):
                raise HTTPException(403, "Das aktuelle Passwort stimmt nicht")
        if body.display_name:
            con.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), user.id))
        if body.show_partner_in_overview is not None:
            con.execute("UPDATE users SET show_partner_in_overview=? WHERE id=?",
                        (int(body.show_partner_in_overview), user.id))
        if body.password:
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.password), user.id))
            # Andere Geräte mit dem alten Passwort abmelden, dieses bleibt angemeldet
            con.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user.id, security.token_hash(_current_token(request))))
    return {"ok": True}


class DeleteMeIn(BaseModel):
    password: str


@app.delete("/auth/me", status_code=204)
def delete_me(body: DeleteMeIn, user: User = Depends(current_user)):
    """Eigenes Konto löschen (App Store verlangt das in der App): mit Passwort, endgültig – Dokumente, Konten und
    Umsätze, Regeln, Sitzungen. Der letzte Admin geht erst, wenn jemand anderes Admin ist (sonst verwaiste der
    Server); ist er ganz allein, darf er – dann wartet der Server wieder auf die Ersteinrichtung."""
    if user.id == 0:
        raise HTTPException(400, "Der Einrichtungszugang hat kein Konto")
    with connect() as con:
        row = con.execute("SELECT username, password_hash, is_admin FROM users WHERE id=?", (user.id,)).fetchone()
        if not row or not auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(403, "Das Passwort stimmt nicht")
        if row["is_admin"]:
            admins = con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
            others = con.execute("SELECT COUNT(*) FROM users WHERE id<>?", (user.id,)).fetchone()[0]
            if admins == 1 and others:
                raise HTTPException(400, "Du bist der einzige Admin – gib erst jemand anderem Admin-Rechte")
        ids = _remove_user(con, user.id)
    _remove_files(ids, row["username"])


# ---------- Users (Admin) ----------

class UserIn(BaseModel):
    username: str
    display_name: Optional[str] = None
    password: str
    is_admin: bool = False


@app.get("/users")
def list_users(user: User = Depends(current_user)):
    """Alle Konten (zum Freigeben braucht man die Namen). Benutzernamen – die halbe Anmeldung – und wer Admin ist,
    sehen nur Admins; die Felder bleiben für ältere Apps da, aber leer."""
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users ORDER BY id")]
    if not (user.is_admin or user.id == 0):
        for r in rows:
            r.update(username="", is_admin=0, created_at=None)
    return rows


@app.post("/users", status_code=201)
def create_user(body: UserIn, admin: User = Depends(require_admin)):
    with connect() as con:
        auth.check_new_user(con, body.username, body.password)
        first = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        u = auth.create_user(con, body.username, body.display_name or body.username, body.password,
                             is_admin=body.is_admin or first)
        if first:
            # Dokumente aus der Zeit vor der Nutzerverwaltung gehören dem ersten Nutzer
            con.execute("UPDATE documents SET owner_id=? WHERE owner_id IS NULL", (u.id,))
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "is_admin": u.is_admin}


class UserPatch(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None     # Admin setzt zurück, ohne das alte zu kennen
    is_admin: Optional[bool] = None


@app.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, admin: User = Depends(require_admin)):
    """Admin: Anzeigename, Admin-Recht oder Passwort eines Nutzers ändern. Ein neues Passwort meldet
    alle Geräte dieses Nutzers ab (außer dem eigenen, wenn der Admin sich selbst ändert)."""
    if body.password is not None and len(body.password) < MIN_PASSWORD:
        raise HTTPException(400, f"Das Passwort braucht mindestens {MIN_PASSWORD} Zeichen")
    if body.is_admin is False and user_id == admin.id:
        raise HTTPException(400, "Eigene Admin-Rechte entziehen geht nicht")
    with connect() as con:
        if not con.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "Nutzer nicht gefunden")
        if body.display_name and body.display_name.strip():
            con.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), user_id))
        if body.is_admin is not None:
            con.execute("UPDATE users SET is_admin=? WHERE id=?", (int(body.is_admin), user_id))
        if body.password:
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body.password), user_id))
            con.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, security.token_hash(_current_token(request))))
        r = con.execute("SELECT id, username, display_name, is_admin, created_at FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r)


def _remove_user(con, user_id: int) -> list[int]:
    """Konto samt allem, was ihm gehört: Dokumente, Konten mit Umsätzen, Regeln, Sitzungen, Freigaben (über die
    Fremdschlüssel). Fristen anderer, für die es zuständig war, gelten wieder für alle. → Dokument-IDs (Dateien)."""
    ids = [r["id"] for r in con.execute("SELECT id FROM documents WHERE owner_id=?", (user_id,))]
    con.execute("UPDATE documents SET due_assignee=NULL WHERE due_assignee=?", (user_id,))
    con.execute("DELETE FROM users WHERE id=?", (user_id,))
    avatars.remove("u", user_id)
    return ids


def _remove_files(doc_ids: list[int], username: Optional[str]) -> None:
    for i in doc_ids:
        processing.doc_path(i).unlink(missing_ok=True)
        processing.thumb_path(i).unlink(missing_ok=True)
    # Scanner-Eingang des Kontos (inbox/<Benutzername>/) – nur ein direkter Unterordner, nie mehr
    if username:
        box = (config.INBOX_DIR / username).resolve()
        if box.parent == config.INBOX_DIR.resolve() and box.is_dir():
            shutil.rmtree(box, ignore_errors=True)


@app.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(400, "Sich selbst löschen geht nicht")
    with connect() as con:
        row = con.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        ids = _remove_user(con, user_id)
    _remove_files(ids, row["username"] if row else None)


# ---------- Partner-Freigabe ----------

class ShareIn(BaseModel):
    user_id: int          # wem ich meinen Pot freigebe
    can_edit: bool = True


@app.put("/shares")
def set_share(body: ShareIn, user: User = Depends(current_user)):
    if body.user_id == user.id:
        raise HTTPException(400, "Sich selbst freigeben ist sinnlos")
    with connect() as con:
        if not con.execute("SELECT 1 FROM users WHERE id=?", (body.user_id,)).fetchone():
            raise HTTPException(404, "Nutzer nicht gefunden")
        con.execute(
            "INSERT INTO partner_shares(from_user, to_user, can_edit) VALUES (?,?,?) "
            "ON CONFLICT(from_user, to_user) DO UPDATE SET can_edit=excluded.can_edit",
            (user.id, body.user_id, int(body.can_edit)),
        )
    return {"ok": True}


@app.delete("/shares/{user_id}", status_code=204)
def remove_share(user_id: int, user: User = Depends(current_user)):
    with connect() as con:
        con.execute("DELETE FROM partner_shares WHERE from_user=? AND to_user=?", (user.id, user_id))
        # Wer meine Dokumente nicht mehr sieht, ist auch nicht mehr zuständig – sonst lebte die alte Zuteilung
        # bei einer späteren neuen Freigabe still wieder auf
        con.execute("UPDATE documents SET due_assignee=NULL WHERE owner_id=? AND due_assignee=?", (user.id, user_id))


# ---------- Documents ----------

def import_pdf(pdf_path: Path, owner_id: int, original_name: str, title: Optional[str] = None,
               text: Optional[str] = None, tags: Optional[list[str]] = None,
               ext_link_id: Optional[int] = None, ext_from: Optional[str] = None) -> int:
    """Legt ein PDF als Dokument an (verschiebt die Datei). Raises HTTPException(409) bei Duplikat,
    422 bei kaputtem oder passwortgeschütztem PDF.

    Mit ext_link_id (Briefkasten): Dokument ist „von außen, zu prüfen“. Eine schon vorhandene gleiche Datei
    wird dann still als angekommen gezählt – kein 409, kein Wiederherstellen aus dem Papierkorb –, damit
    niemand von außen abfragen kann, was im Archiv liegt."""
    problem = processing.pdf_problem(pdf_path)
    if problem:
        raise HTTPException(422, problem)
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    allow_dup = config.llm_config()["duplicate_mode"] == "allow"
    with connect() as con:
        dup = con.execute("SELECT id, owner_id, deleted_at FROM documents WHERE sha256=?", (sha,)).fetchone()
        if dup and ext_link_id is not None and auth.can_view(con, User(owner_id, "", "", False, False), dup["owner_id"]):
            return dup["id"]
        if dup and not allow_dup and not auth.can_view(con, User(owner_id, "", "", False, False), dup["owner_id"]):
            # Gleiche Datei liegt in einem Pot, den der Hochladende nicht sieht: als eigenes Dokument
            # anlegen (ohne Prüfsumme, die ist schon vergeben) – sonst würde er fremde Daten übernehmen
            # oder über die Fehlermeldung von dem fremden Dokument erfahren.
            allow_dup = True
        if dup and not allow_dup:
            if dup["deleted_at"]:
                # Liegt im Papierkorb: wiederherstellen statt ablehnen (Besitzer bleibt)
                con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (dup["id"],))
                return dup["id"]
            raise HTTPException(409, f"Dokument existiert bereits (id={dup['id']})")
        cur = con.execute(
            "INSERT INTO documents(owner_id, title, original_name, sha256, text, text_source, ext_link_id, ext_from, ext_pending) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (owner_id, title or Path(original_name or "Dokument").stem, original_name, None if (allow_dup and dup) else sha,
             text or None, "client" if text else None, ext_link_id, ext_from, 1 if ext_link_id is not None else 0),
        )
        doc_id = cur.lastrowid
        shutil.move(str(pdf_path), processing.doc_path(doc_id))
        set_tags(con, doc_id, tags or [])
    worker.wake()
    return doc_id


@app.post("/documents", status_code=201)
async def upload(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    text: Optional[str] = Form(None),          # OCR-Text vom Client (Vision), optional
    tags: Optional[str] = Form(None),          # Komma-getrennt
    user: User = Depends(current_user),
):
    if user.id == 0:
        raise HTTPException(400, "Erst einen Nutzer anlegen (POST /users)")
    if not (file.filename or "").lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(415, "Nur PDF. Bilder bitte clientseitig zu PDF wandeln.")

    # Kopieren, Seiten zählen, Datenbank: im Thread – sonst stünde während eines großen Uploads alles andere
    def work():
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = Path(tmp.name)
        if demo.too_big(tmp_path.stat().st_size):
            tmp_path.unlink(missing_ok=True)
            raise demo.too_big_error()
        try:
            doc_id = import_pdf(tmp_path, user.id, file.filename or "Dokument.pdf", title, text,
                                [t for t in (tags or "").split(",") if t.strip()])
        finally:
            tmp_path.unlink(missing_ok=True)
        with connect() as con:
            return serialize(con, get_doc(con, doc_id, user), user)
    return await run_in_threadpool(work)


# „Zu prüfen“: Fehler, fehlgeschlagenes Tagging, kein Datum (außer bestätigt: „Hat kein Datum“), keine Tags oder von außen (Briefkasten) und noch nicht übernommen
REVIEW_SQL = ("(d.status='error' OR d.llm_status='failed' OR (d.document_date IS NULL AND d.no_date=0) OR d.ext_pending=1 OR d.date_check=1 "
              "OR NOT EXISTS (SELECT 1 FROM document_tags dt WHERE dt.document_id=d.id))")


def _doc_filter(con, user: User, q=None, tag=None, status=None, year=None, review=False, due=False,
                sender=None, trash=False, scope=None, date_from=None, date_to=None, owner=None, mine_due=False,
                warranty=False, kind=None):
    """WHERE-Teile für die Dokumentliste: (where, params, fts_query) oder None, wenn nichts sichtbar ist."""
    where, params = [], []
    if user.id != 0:
        if scope not in ("mine", "partner", "all"):
            scope = "all" if user.show_partner_in_overview else "mine"
        owners = auth.visible_owner_ids(con, user, scope)
        if owner is not None:   # eine oder mehrere Personen der Familie („2“ oder „2,3“) – nur sichtbare
            wanted = {int(x) for x in str(owner).split(",") if x.strip().isdigit()}
            owners = [o for o in owners if o in wanted]
        if not owners:
            return None
        where.append(f"d.owner_id IN ({','.join('?' * len(owners))})")
        params += owners
    fq = None
    if q and q.strip():
        w, p, snip_w, snip_t = search_where(con, q)
        where += w
        params += p
        fq = (snip_w, snip_t) if snip_w else None
    if tag:
        where.append("d.id IN (SELECT document_id FROM document_tags dt JOIN tags t ON t.id=dt.tag_id "
                     "WHERE t.name=? COLLATE NOCASE)")
        params.append(tag)
    if status:
        where.append("d.status=?")
        params.append(status)
    if year:
        where.append("substr(d.document_date, 1, 4) = ?")   # Undatierte in keinem Jahr (siehe date_from)
        params.append(str(year))
    # Zeiträume nur über das echte Dokumentdatum – das Hochladedatum sagt nichts darüber, aus welcher Zeit ein
    # Brief stammt (ein Vertrag von 2015 gehört nicht in „dieses Jahr“). Undatierte stehen unter „Zu prüfen“.
    if date_from:
        where.append("substr(d.document_date, 1, 10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("substr(d.document_date, 1, 10) <= ?")
        params.append(str(date_to)[:10])
    if review:
        where.append(REVIEW_SQL)
    if due:
        where.append("d.due_date IS NOT NULL AND COALESCE(d.due_done, 0) = 0 AND d.ext_pending=0")
    if warranty:   # Gewährleistung oder Garantie läuft noch
        where.append("d.warranty_until >= date('now', 'localtime')")
    if mine_due and user.id != 0:   # Fristen, um die ich mich kümmere oder um die sich niemand kümmert
        where.append(duemod.MINE_DUE_SQL)
        params.append(user.id)
    if sender:
        where.append("d.sender = ? COLLATE NOCASE")
        params.append(sender)
    if kind:   # Dokumentart (Codes aus processing.KIND_LABELS, mehrere mit Komma)
        kinds = [k for k in str(kind).split(",") if k in processing.KIND_LABELS]
        where.append(f"d.doc_kind IN ({','.join('?' * len(kinds))})" if kinds else "0")
        params += kinds
    where.append("d.deleted_at IS NOT NULL" if trash else "d.deleted_at IS NULL")
    return where, params, fq


@app.get("/documents")
def list_documents(
    q: Optional[str] = Query(None, description="Volltextsuche"),
    tag: Optional[str] = None,
    status: Optional[str] = None,
    year: Optional[int] = Query(None, description="Jahr des Dokumentdatums (undatierte fallen heraus)"),
    review: bool = Query(False, description="Nur Dokumente ohne Tag/Datum oder mit fehlgeschlagenem Tagging"),
    due: bool = Query(False, description="Nur Dokumente mit offener Frist, nach Frist sortiert"),
    warranty: bool = Query(False, description="Nur Käufe mit laufender Gewährleistung/Garantie, nach Ende sortiert"),
    sender: Optional[str] = Query(None, description="Nur Dokumente dieses Absenders"),
    kind: Optional[str] = Query(None, max_length=200, description="Nur diese Dokumentart(en), Codes aus /documents/kinds"),
    trash: bool = Query(False, description="Papierkorb anzeigen"),
    sort: str = Query("date", description="date | added | title | sender | amount | due"),
    dir: str = Query("desc", description="asc | desc"),
    scope: Optional[str] = Query(None, description="mine | partner | all (Standard: Nutzereinstellung)"),
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = Query(None, description="Dokumentdatum ab (JJJJ-MM-TT; undatierte fallen heraus)"),
    date_to: Optional[str] = Query(None, description="Dokumentdatum bis (JJJJ-MM-TT)"),
    owner: Optional[str] = Query(None, pattern=r"^\d+(,\d+)*$",
                                 description="Nur Dokumente dieser Person(en), z. B. 2 oder 2,3 (müssen sichtbar sein)"),
    mine_due: bool = Query(False, description="Nur Fristen, die mir oder niemandem zugeteilt sind"),
    with_total: bool = Query(False, description="Gesamtzahl der Treffer im Header X-Total-Count (für Seiten)"),
    response: Response = None,
    user: User = Depends(current_user),
):
    for v in (date_from, date_to):
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise HTTPException(400, "Datum als JJJJ-MM-TT angeben")
    with connect() as con:
        f = _doc_filter(con, user, q=q, tag=tag, status=status, year=year, review=review, due=due, sender=sender, trash=trash,
                        scope=scope, date_from=date_from, date_to=date_to, owner=owner, mine_due=mine_due,
                        warranty=warranty, kind=kind)
        if f is None:
            if with_total and response is not None:
                response.headers["X-Total-Count"] = "0"
            return []
        where, params, fq = f
        if with_total and response is not None:
            n = con.execute("SELECT COUNT(*) FROM documents d" + (" WHERE " + " AND ".join(where) if where else ""),
                            params).fetchone()[0]
            response.headers["X-Total-Count"] = str(n)
        if due:
            sort = "due"
            dir = "asc"
        elif warranty and sort == "date":
            sort = "warranty"
            dir = "asc"
        # Bei Volltextsuche einen Textausschnitt mit markiertem Treffer mitliefern
        select = "SELECT d.*"
        if fq:
            # Treffer am Wortanfang zuerst, sonst die Stelle mitten im Wort (Teilwort-Index)
            snip = "(SELECT snippet(documents_fts, 1, '[[', ']]', '…', 18) FROM documents_fts WHERE rowid = d.id AND documents_fts MATCH ?)"
            pre = [fq[0]]
            if fq[1]:
                snip = (f"COALESCE({snip}, (SELECT snippet(documents_tri, 1, '[[', ']]', '…', 18) FROM documents_tri "
                        "WHERE rowid = d.id AND documents_tri MATCH ?))")
                pre.append(fq[1])
            select += f", {snip} AS snippet"
            params[:0] = pre
        sql = f"{select} FROM documents d"
        if where:
            sql += " WHERE " + " AND ".join(where)
        d = "ASC" if dir == "asc" else "DESC"
        orders = {
            "due": f"d.due_date IS NULL, d.due_date {d}, d.id {d}",
            "warranty": f"d.warranty_until IS NULL, d.warranty_until {d}, d.id {d}",
            "date": f"COALESCE(d.document_date, d.created_at) {d}, d.id {d}",
            "added": f"d.created_at {d}, d.id {d}",
            "title": f"d.title COLLATE NOCASE {d}, d.id DESC",
            "sender": f"d.sender IS NULL, d.sender COLLATE NOCASE {d}, COALESCE(d.document_date, d.created_at) DESC",
            "amount": f"d.amount IS NULL, d.amount {d}, d.id DESC",
        }
        sql += " ORDER BY " + orders.get(sort, orders["date"]) + " LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [serialize(con, r, user) for r in con.execute(sql, params)]


@app.get("/search")
def smart_search(q: str = Query(..., min_length=1, max_length=300), limit: int = Query(50, ge=1, le=200),
                 user: User = Depends(current_user)):
    """Suche in ganzen Sätzen („Gehalt letzte 6 Monate“): Filter als Chips plus Treffer, ohne KI.
    Findet sich nichts, werden Person, Zeitraum und Fristen nacheinander gelockert (in `dropped`), nie die Suchwörter."""
    with connect() as con:
        r = smartsearch.interpret(con, user, q)
    p = dict(r["params"])

    def run(p):
        return list_documents(q=p.get("q"), tag=None, status=None, year=None, review=False, due=bool(p.get("due")), sender=None, kind=None,
                              trash=False, sort="date", dir="desc", scope="all", limit=limit, offset=0,
                              date_from=p.get("date_from"), date_to=p.get("date_to"), owner=p.get("owner"), mine_due=False, warranty=False,
                              user=user)

    docs, dropped = run(p), []
    chips = list(r["chips"])
    for kind, keys in (("owner", ("owner",)), ("range", ("date_from", "date_to")), ("due", ("due",))):
        if docs or not any(k in p for k in keys):
            continue
        rest = [k for k in ("q", "owner", "date_from", "due") if k in p and k not in keys]
        if not rest:
            break   # ohne jeden übrigen Filter käme einfach alles – dann lieber „nichts gefunden“
        for k in keys:
            p.pop(k, None)
        dropped += [c["label"] for c in chips if c["kind"] == kind]
        chips = [c for c in chips if c["kind"] != kind]
        docs = run(p)
    return {"query": r["query"], "chips": chips, "params": p, "documents": docs, "dropped": dropped}


class AskIn(BaseModel):
    question: str
    scope: Optional[str] = None


@app.post("/ask")
def ask_documents(body: AskIn, user: User = Depends(current_user)):
    """Frage über alle sichtbaren Dokumente (Light-RAG: LLM-Suchbegriffe -> FTS5 -> LLM-Antwort mit Quellen)."""
    q = body.question.strip()
    if len(q) < 3:
        raise HTTPException(400, "Frage zu kurz")
    with connect() as con:
        scope = body.scope if body.scope in ("mine", "partner", "all") else ("all" if user.show_partner_in_overview else "mine")
        owners = auth.visible_owner_ids(con, user, scope) if user.id != 0 else []
    with worker.interactive_llm():
        return askmod.ask(q, owners, extra_facts=(lambda span: bank.ask_facts(user, q, span) + yearly.ask_facts(user, q))
                          if user.id != 0 else None)


def assignable_users(con, user: User) -> list[dict]:
    """Nutzer, denen man Dokumente zuordnen darf: sich selbst und Partner mit Schreibrecht; Admins alle."""
    if user.is_admin or user.id == 0:
        return [dict(r) for r in con.execute("SELECT id, display_name FROM users ORDER BY display_name")]
    ids = [user.id, *[p for p, edit in auth.partner_ids(con, user.id).items() if edit]]
    marks = ",".join("?" * len(ids))
    return [dict(r) for r in con.execute(
        f"SELECT id, display_name FROM users WHERE id IN ({marks}) ORDER BY display_name", ids)]


@app.get("/documents/owners")
def list_owners(user: User = Depends(current_user)):
    """Vor `/documents/{doc_id}` deklariert, sonst fängt die Route mit der ID den Pfad ab."""
    with connect() as con:
        return assignable_users(con, user)


class OwnerRequest(BaseModel):
    document_ids: list[int]
    owner_id: int


@app.post("/documents/owner")
def set_owner(body: OwnerRequest, user: User = Depends(current_user)):
    """Dokumente einem anderen Nutzer zuordnen – etwa einen Sammel-Import nachträglich aufteilen."""
    if not body.document_ids:
        raise HTTPException(400, "Keine Dokumente")
    with connect() as con:
        if body.owner_id not in {u["id"] for u in assignable_users(con, user)}:
            raise HTTPException(403, "Diesem Nutzer darfst du keine Dokumente zuordnen")
        for i in body.document_ids:
            get_doc(con, i, user, edit=True)
        marks = ",".join("?" * len(body.document_ids))
        con.execute(f"UPDATE documents SET owner_id=?, updated_at=datetime('now') WHERE id IN ({marks})",
                    (body.owner_id, *body.document_ids))
    return {"ok": True, "count": len(body.document_ids)}


@app.get("/documents/due_count")
def due_count(scope: Optional[str] = None, days: int = Query(30, ge=0, le=3650), user: User = Depends(current_user)):
    """Offene Fristen: überfällig plus fällig in den nächsten `days` Tagen."""
    with connect() as con:
        if user.id == 0:
            return {"count": 0, "overdue": 0}
        if scope not in ("mine", "partner", "all"):
            scope = "all" if user.show_partner_in_overview else "mine"
        owners = auth.visible_owner_ids(con, user, scope)
        if not owners:
            return {"count": 0, "overdue": 0}
        marks = ",".join("?" * len(owners))
        base = (f"FROM documents d WHERE d.deleted_at IS NULL AND d.owner_id IN ({marks}) "
                "AND d.due_date IS NOT NULL AND COALESCE(d.due_done, 0) = 0 AND d.ext_pending=0")
        n = con.execute(f"SELECT COUNT(*) {base} AND d.due_date <= date('now', ?)", (*owners, f"+{days} days")).fetchone()[0]
        over = con.execute(f"SELECT COUNT(*) {base} AND d.due_date < date('now')", owners).fetchone()[0]
        return {"count": n, "overdue": over}


@app.get("/documents/warranty_count")
def warranty_count(scope: Optional[str] = None, user: User = Depends(current_user)):
    """Käufe mit laufender Gewährleistung/Garantie; soon = endet in den nächsten 60 Tagen."""
    with connect() as con:
        if user.id == 0:
            return {"count": 0, "soon": 0}
        if scope not in ("mine", "partner", "all"):
            scope = "all" if user.show_partner_in_overview else "mine"
        owners = auth.visible_owner_ids(con, user, scope)
        if not owners:
            return {"count": 0, "soon": 0}
        marks = ",".join("?" * len(owners))
        base = (f"FROM documents d WHERE d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks}) "
                "AND d.warranty_until >= date('now', 'localtime')")
        n = con.execute(f"SELECT COUNT(*) {base}", owners).fetchone()[0]
        soon = con.execute(f"SELECT COUNT(*) {base} AND d.warranty_until <= date('now', 'localtime', '+60 days')",
                           owners).fetchone()[0]
        return {"count": n, "soon": soon}


@app.get("/upcoming")
def upcoming(scope: Optional[str] = None, user: User = Depends(current_user)):
    """„Im Blick“ für Heute (extras.upcoming): bald ablaufende Ausweise/Karten, fällige Prüfungen/Wartungen, offene
    Erstattungen, Preiserhöhungen bei Verträgen – erst, wenn es näher rückt."""
    with connect() as con:
        if user.id == 0:
            return {"items": []}
        if scope not in ("mine", "partner", "all"):
            scope = "all" if user.show_partner_in_overview else "mine"
        owners = auth.visible_owner_ids(con, user, scope)
        items = extras.upcoming(con, owners) if owners else []
        # Preiserhöhungen aus dem Konto dazu – je Absender nur einmal (aus Belegen erkannt geht vor)
        seen = " ".join((i.get("what") or "").lower() for i in items if i["type"] == "price")
        for b in bank.insights(con, user):
            if b["type"] == "price" and b["key"].split()[0] not in seen:
                items.append({"type": "price", "document_id": None, "title": b["name"], "what": b["name"],
                              "date": b["since"], "amount": b["to"], "from": b["from"], "percent": b["percent"],
                              "cancel_by": None, "source": "bank"})
        return {"items": items}


@app.get("/documents/review_count")
def review_count(scope: Optional[str] = None, user: User = Depends(current_user)):
    with connect() as con:
        if user.id == 0:
            return {"count": 0}
        if scope not in ("mine", "partner", "all"):
            scope = "all" if user.show_partner_in_overview else "mine"
        owners = auth.visible_owner_ids(con, user, scope)
        if not owners:
            return {"count": 0}
        n = con.execute(
            f"SELECT COUNT(*) FROM documents d WHERE d.deleted_at IS NULL AND d.owner_id IN ({','.join('?' * len(owners))}) AND "
            + REVIEW_SQL, owners).fetchone()[0]
        return {"count": n}


@app.get("/documents/years")
def list_years(scope: Optional[str] = None, user: User = Depends(current_user)):
    """Jahre mit Anzahl, über die sichtbaren Dokumente."""
    with connect() as con:
        # Nur echte Dokumentdaten – Undatierte gehören in kein Jahr (sie stehen unter „Zu prüfen“)
        sql = ("SELECT substr(document_date, 1, 4) AS y, COUNT(*) AS n "
               "FROM documents d WHERE d.deleted_at IS NULL AND d.document_date IS NOT NULL")
        params: list = []
        if user.id != 0:
            if scope not in ("mine", "partner", "all"):
                scope = "all" if user.show_partner_in_overview else "mine"
            owners = auth.visible_owner_ids(con, user, scope)
            if not owners:
                return []
            sql += f" AND d.owner_id IN ({','.join('?' * len(owners))})"
            params = owners
        sql += " GROUP BY y ORDER BY y DESC"
        return [{"year": int(r["y"]), "count": r["n"]} for r in con.execute(sql, params)
                if r["y"] and r["y"].isdigit()]   # kaputte Altwerte bringen die Liste nicht zu Fall


@app.get("/documents/owner_counts")
def owner_counts(user: User = Depends(current_user)):
    """Dokumente je sichtbarem Konto (Familie-Tab) – ohne Papierkorb und ungeprüfte Briefkasten-Einwürfe."""
    if user.id == 0:
        return []
    with connect() as con:
        owners = auth.visible_owner_ids(con, user, "all")
        if not owners:
            return []
        return [{"owner_id": r[0], "count": r[1]} for r in con.execute(
            "SELECT owner_id, COUNT(*) FROM documents WHERE deleted_at IS NULL AND ext_pending=0 "
            f"AND owner_id IN ({','.join('?' * len(owners))}) GROUP BY owner_id", owners)]


@app.get("/documents/kinds")
def document_kinds(lang: str = "de", scope: Optional[str] = None, user: User = Depends(current_user)):
    """Dokumentarten zum Auswählen und Filtern (Code → Bezeichnung), in der Reihenfolge der Auswahl; `count` = sichtbare
    Dokumente dieser Art (für den Filter nur Arten mit Treffern zeigen)."""
    i = 1 if lang.lower().startswith("en") else 0
    counts = {}
    with connect() as con:
        f = _doc_filter(con, user, scope=scope)
        if f:
            where, params, _ = f
            counts = {r[0]: r[1] for r in con.execute(
                "SELECT d.doc_kind, COUNT(*) FROM documents d WHERE " + " AND ".join(where) + " GROUP BY d.doc_kind", params)}
    return [{"code": c, "label": labels[i], "count": counts.get(c, 0)} for c, labels in processing.KIND_LABELS.items()]


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, include_text: bool = False, user: User = Depends(current_user)):
    with connect() as con:
        row = get_doc(con, doc_id, user)
        d = serialize(con, row, user)
        if include_text:
            d["text"] = row["text"]
        return d


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    document_date: Optional[str] = None
    sender: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    due_date: Optional[str] = None     # Frist als YYYY-MM-DD, "" löscht sie
    due_note: Optional[str] = None
    due_done: Optional[bool] = None
    due_assignee: Optional[int] = None  # wer sich kümmert (Nutzer-ID, muss das Dokument sehen), null = niemand
    due_repeat: Optional[str] = None    # monthly | quarterly | yearly, null/"" = einmalig
    direction: Optional[str] = None    # out (Ausgabe) | in (Einnahme) | none (bewegt kein Geld)
    tags: Optional[list[str]] = None   # ersetzt die komplette Tag-Liste
    no_date: Optional[bool] = None     # true: „Hat kein Datum“ – Datum leer, nicht mehr unter „Zu prüfen“
    warranty_months: Optional[int] = None  # Gewährleistung/Garantie in Monaten, 0 = keine
    warranty_kind: Optional[str] = None    # legal (gesetzliche Gewährleistung) | garantie
    expires_on: Optional[str] = None       # Ausweis/Karte gültig bis (YYYY-MM-DD), "" löscht
    service_next: Optional[str] = None     # nächste Prüfung/Wartung (YYYY-MM-DD), "" = erledigt/kein Termin
    refund_state: Optional[str] = None     # open | done | none (nicht einreichen)
    doc_kind: Optional[str] = None         # Dokumentart (Code aus /documents/kinds), "" = offen
    recipient: Optional[str] = Field(None, max_length=120)   # an wen, "" löscht
    refs: Optional[dict[str, str]] = None  # Nummern: contract/customer/case/invoice/other, leere Werte fallen weg


@app.patch("/documents/{doc_id}")
def update_document(doc_id: int, body: DocumentUpdate, user: User = Depends(current_user)):
    with connect() as con:
        row = get_doc(con, doc_id, user, edit=True)
        fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "tags"}
        if "due_repeat" in fields:
            rep = (fields["due_repeat"] or "").strip() or None
            if rep is not None and rep not in duemod.REPEATS:
                raise HTTPException(400, "Wiederholung: monthly, quarterly, yearly oder leer")
            fields["due_repeat"] = rep
        if fields.get("due_assignee") == duemod.UNKNOWN_ASSIGNEE:
            fields.pop("due_assignee")   # Platzhalter zurückgeschickt (Web speichert alles) → unverändert
        if fields.get("due_assignee") is not None:
            if fields["due_assignee"] not in {u["id"] for u in duemod.candidates(con, user, row["owner_id"])}:
                raise HTTPException(400, "Diese Person sieht das Dokument nicht")
        if "due_date" in fields:
            due = (fields["due_date"] or "").strip()
            if due and not duemod.valid_date(due):
                raise HTTPException(400, "Frist als gültiges Datum YYYY-MM-DD angeben")
            fields["due_date"] = due or None
            if not due:  # Frist entfernt → Vermerk, Erledigt-Status, Zuständige und Wiederholung mit weg
                fields.update(due_note=None, due_done=0, due_assignee=None, due_repeat=None, due_last_done=None)
            elif due != (row["due_date"] or "")[:10]:
                fields["due_day"] = None          # neuer Termin von Hand → sein Tag ist der Wunschtag
                if "due_done" not in fields:      # neuer Termin ist offen (iOS schickt nur das Datum)
                    fields["due_done"] = False
        if "due_done" in fields:
            fields["due_done"] = 1 if fields["due_done"] else 0
            # Wiederkehrende Frist erledigt → gleich der nächste Termin, offen (etwa Kfz-Steuer im nächsten Jahr)
            due_eff = fields["due_date"] if "due_date" in fields else row["due_date"]
            rep_eff = fields["due_repeat"] if "due_repeat" in fields else row["due_repeat"]
            if fields["due_done"] and due_eff and rep_eff in duemod.REPEATS:
                want = duemod.wish_day(due_eff, fields["due_day"] if "due_day" in fields else row["due_day"])
                nxt = duemod.next_due(due_eff, rep_eff, want)
                fields.update(due_date=nxt, due_done=0, due_last_done=_dt.date.today().isoformat(),
                              due_day=want if int(nxt[8:10]) != want else None)
        if "document_date" in fields:
            ddate = (fields["document_date"] or "").strip()
            if ddate and not duemod.valid_date(ddate[:10]):
                raise HTTPException(400, "Datum als gültiges Datum YYYY-MM-DD angeben")
            fields["document_date"] = ddate[:10] or None
            fields["date_check"] = 0   # Datum von Hand gesetzt oder bestätigt
            if fields["document_date"]:
                fields["no_date"] = 0  # doch ein Datum
        if "no_date" in fields:
            fields["no_date"] = 1 if fields["no_date"] and not fields.get("document_date") else 0
            if fields["no_date"]:
                fields.update(document_date=None, date_check=0)
        # Gewährleistung/Garantie: das Web schickt beim Speichern alles mit – Unverändertes zählt nicht als Korrektur
        if "warranty_kind" in fields and fields["warranty_kind"] not in ("legal", "garantie", None):
            raise HTTPException(400, "Art: legal, garantie oder leer")
        if "warranty_months" in fields:
            m = fields["warranty_months"]
            if m is None or (m == row["warranty_months"] and fields.get("warranty_kind", row["warranty_kind"]) == row["warranty_kind"]):
                fields.pop("warranty_months"); fields.pop("warranty_kind", None)
            elif not 0 <= m <= 360:
                raise HTTPException(400, "Garantie: 0 bis 360 Monate")
            else:
                fields["warranty_kind"] = None if m == 0 else (fields.get("warranty_kind") or
                                                               ("legal" if m == processing.WARRANTY_LEGAL else "garantie"))
        else:
            fields.pop("warranty_kind", None)
        for k in ("expires_on", "service_next"):
            if k in fields:
                v = (fields[k] or "").strip()
                if v and not duemod.valid_date(v):
                    raise HTTPException(400, "Datum als gültiges Datum YYYY-MM-DD angeben")
                fields[k] = v or None
                if k == "expires_on" and not v:
                    fields["expires_what"] = None
        if "doc_kind" in fields:
            k = (fields["doc_kind"] or "").strip() or None
            if k is not None and k not in processing.KIND_LABELS:
                raise HTTPException(400, "Unbekannte Dokumentart")
            fields["doc_kind"] = k
        if "recipient" in fields:
            fields["recipient"] = re.sub(r"\s+", " ", fields["recipient"] or "").strip() or None
        if "refs" in fields:
            refs = {k: re.sub(r"\s+", " ", str(v)).strip()[:60] for k, v in (fields["refs"] or {}).items()
                    if k in processing.REF_KEYS and str(v or "").strip()}
            fields["refs"] = _json.dumps(refs, ensure_ascii=False) if refs else None
        if "refund_state" in fields and fields["refund_state"] not in ("open", "done", "none"):
            raise HTTPException(400, "Erstattung: open, done oder none")
        if "warranty_months" in fields or "document_date" in fields:
            fields["warranty_until"] = processing.warranty_end(
                fields["document_date"] if "document_date" in fields else row["document_date"],
                fields["warranty_months"] if "warranty_months" in fields else row["warranty_months"])
        if "amount" in fields and fields["amount"] is not None and fields["amount"] != row["amount"]:
            # nur neue Werte prüfen: die Web-App schickt den Betrag bei jedem Speichern mit
            a = fields["amount"]
            if not math.isfinite(a) or a < 0 or a >= 1e9:
                raise HTTPException(400, "Betrag: Zahl ab 0 und unter einer Milliarde")
            fields["amount"] = round(a, 2)
        if "direction" in fields and fields["direction"] not in ("out", "in", "none", None):
            raise HTTPException(400, "Richtung: out, in, none oder leer")
        if "currency" in fields:
            if not (fields["currency"] or "").strip():
                fields["currency"] = None
            else:
                cur = processing._currency(fields["currency"])
                if not cur:
                    raise HTTPException(400, "Währung: dreistelliger Code wie EUR")
                fields["currency"] = cur
        if fields or body.tags is not None:
            fields["ext_pending"] = 0  # wer ein Briefkasten-Dokument bearbeitet, hat es angesehen
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE documents SET {sets}, updated_at=datetime('now') WHERE id=?",
                        (*fields.values(), doc_id))
        if body.tags is not None:
            con.execute("DELETE FROM document_tags WHERE document_id=?", (doc_id,))
            set_tags(con, doc_id, body.tags)
        return serialize(con, get_doc(con, doc_id, user), user)


@app.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, permanent: bool = False, user: User = Depends(current_user)):
    """Standard: in den Papierkorb. permanent=true: sofort endgültig."""
    with connect() as con:
        get_doc(con, doc_id, user, edit=True)
        if permanent:
            con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        else:
            con.execute("UPDATE documents SET deleted_at=datetime('now') WHERE id=?", (doc_id,))
    if permanent:
        processing.doc_path(doc_id).unlink(missing_ok=True)
        processing.thumb_path(doc_id).unlink(missing_ok=True)


@app.post("/documents/{doc_id}/accept")
def accept_document(doc_id: int, user: User = Depends(current_user)):
    """Briefkasten: von außen hochgeladenes Dokument übernehmen (zählt dann nicht mehr als „zu prüfen“)."""
    with connect() as con:
        get_doc(con, doc_id, user, edit=True)
        con.execute("UPDATE documents SET ext_pending=0 WHERE id=?", (doc_id,))
        return serialize(con, get_doc(con, doc_id, user), user)


@app.post("/documents/{doc_id}/restore")
def restore_document(doc_id: int, user: User = Depends(current_user)):
    with connect() as con:
        get_doc(con, doc_id, user, edit=True)
        con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (doc_id,))
        return serialize(con, get_doc(con, doc_id, user), user)


@app.delete("/trash", status_code=204)
def empty_trash(user: User = Depends(current_user)):
    """Papierkorb leeren: eigene Dokumente plus Partner-Pots mit Bearbeitungsrecht."""
    with connect() as con:
        rows = [r for r in con.execute("SELECT id, owner_id FROM documents WHERE deleted_at IS NOT NULL")
                if auth.can_edit(con, user, r["owner_id"])]
        for r in rows:
            con.execute("DELETE FROM documents WHERE id=?", (r["id"],))
    for r in rows:
        processing.doc_path(r["id"]).unlink(missing_ok=True)
        processing.thumb_path(r["id"]).unlink(missing_ok=True)


@app.get("/trash/count")
def trash_count(user: User = Depends(current_user)):
    with connect() as con:
        if user.id == 0:
            return {"count": 0, "days": 30}
        owners = auth.visible_owner_ids(con, user, "all")
        marks = ",".join("?" * len(owners)) or "NULL"
        n = con.execute(f"SELECT COUNT(*) FROM documents WHERE deleted_at IS NOT NULL AND owner_id IN ({marks})", owners).fetchone()[0]
        return {"count": n, "days": config.llm_config()["trash_days"]}



@app.get("/documents/{doc_id}/assignees")
def due_assignees(doc_id: int, user: User = Depends(current_user)):
    """Wer sich um die Frist kümmern kann: alle, die das Dokument sehen und die ich kenne."""
    with connect() as con:
        row = get_doc(con, doc_id, user)
        return duemod.candidates(con, user, row["owner_id"])


class DueAssignIn(BaseModel):
    ids: list[int]
    assignee: Optional[int] = None    # null = niemand


def _due_docs(con, user: User, ids: list[int], edit: bool) -> list:
    """Offene Fristen aus der Auswahl, die `user` sieht (bzw. bearbeiten darf); der Rest fällt still weg."""
    rows = []
    for i in dict.fromkeys(ids):
        r = con.execute("SELECT * FROM documents WHERE id=? AND deleted_at IS NULL", (i,)).fetchone()
        if not r or not r["due_date"] or r["due_done"] or not auth.can_view(con, user, r["owner_id"]):
            continue
        if edit and not auth.can_edit(con, user, r["owner_id"]):
            continue
        rows.append(r)
    return rows


@app.get("/due/assignees")
def due_assignees_many(ids: str = Query(..., max_length=20000), user: User = Depends(current_user)):
    """Mehrfachauswahl in „Wer kümmert sich?“: wen ich für die gewählten Fristen eintragen kann – je Person, für wie
    viele davon (nur, wer das jeweilige Dokument sieht; nur Dokumente, die ich bearbeiten darf)."""
    try:
        want = [int(x) for x in ids.split(",") if x.strip()][:500]
    except ValueError:
        raise HTTPException(400, "ids: Zahlen, durch Komma getrennt")
    with connect() as con:
        rows = _due_docs(con, user, want, edit=True)
        people: dict[int, dict] = {}
        for r in rows:
            for c in duemod.candidates(con, user, r["owner_id"]):
                people.setdefault(c["id"], {**c, "count": 0})["count"] += 1
        return {"total": len(rows), "people": sorted(people.values(), key=lambda p: p["display_name"].lower())}


@app.post("/due/assign")
def due_assign_many(body: DueAssignIn, user: User = Depends(current_user)):
    """Mehrere offene Fristen auf einmal einer Person geben (oder niemandem). Übersprungen wird, was ich nicht
    bearbeiten darf oder was die Person nicht sieht – das Ergebnis sagt, wie viele."""
    if len(body.ids) > 500:
        raise HTTPException(400, "Höchstens 500 auf einmal")
    with connect() as con:
        rows = _due_docs(con, user, body.ids, edit=True)
        done = 0
        for r in rows:
            if body.assignee is not None and body.assignee not in {c["id"] for c in duemod.candidates(con, user, r["owner_id"])}:
                continue
            con.execute("UPDATE documents SET due_assignee=?, updated_at=datetime('now') WHERE id=?", (body.assignee, r["id"]))
            done += 1
        return {"updated": done, "skipped": len(set(body.ids)) - done}


@app.get("/documents/{doc_id}/due.ics")
def due_ics(doc_id: int, user: User = Depends(current_user)):
    """Frist als Kalender-Termin (ganztägig, Erinnerung drei Tage vorher; wiederkehrend mit RRULE)."""
    with connect() as con:
        row = get_doc(con, doc_id, user)
    if not row["due_date"]:
        raise HTTPException(404, "Keine Frist")
    return Response(duemod.ics(row), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="frist-{doc_id}.ics"'})


@app.get("/documents/{doc_id}/file")
def get_file(doc_id: int, download: bool = False, user: User = Depends(current_user)):
    """Standard: inline anzeigen (Browser-Viewer). Mit ?download=1 als Datei speichern."""
    with connect() as con:
        row = get_doc(con, doc_id, user)
    p = processing.doc_path(doc_id)
    if not p.exists():
        raise HTTPException(404)
    if download:
        safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (row["title"] or f"dokument-{doc_id}"))
        return FileResponse(p, media_type="application/pdf", filename=f"{safe.strip()}.pdf")
    return FileResponse(p, media_type="application/pdf", headers={"Content-Disposition": "inline"})


@app.get("/documents/{doc_id}/thumbnail")
def get_thumbnail(doc_id: int, user: User = Depends(current_user)):
    with connect() as con:
        get_doc(con, doc_id, user)
    p = processing.thumb_path(doc_id)
    if not p.exists():
        raise HTTPException(404, "Thumbnail noch nicht erzeugt")
    return FileResponse(p, media_type="image/jpeg")


@app.post("/documents/{doc_id}/reprocess")
def reprocess(doc_id: int, rerun_ocr: bool = False, user: User = Depends(current_user)):
    with connect() as con:
        get_doc(con, doc_id, user, edit=True)
        if rerun_ocr:
            con.execute("UPDATE documents SET text=NULL, text_source=NULL WHERE id=?", (doc_id,))
        con.execute("UPDATE documents SET status='pending', llm_status=NULL WHERE id=?", (doc_id,))
    worker.wake()
    return {"ok": True}


class MergeRequest(BaseModel):
    document_ids: list[int]
    title: Optional[str] = None
    delete_sources: bool = True


@app.post("/documents/merge", status_code=201)
def merge(body: MergeRequest, user: User = Depends(current_user)):
    if len(body.document_ids) < 2:
        raise HTTPException(400, "Mindestens zwei Dokumente")
    with connect() as con:
        rows = [get_doc(con, i, user, edit=True) for i in body.document_ids]
        if any(r["deleted_at"] for r in rows):
            raise HTTPException(400, "Dokumente im Papierkorb lassen sich nicht zusammenführen")
        owners = {r["owner_id"] for r in rows}
        if len(owners) > 1:
            raise HTTPException(400, "Nur Dokumente desselben Besitzers lassen sich zusammenführen")
        if demo.too_big(sum(processing.doc_path(i).stat().st_size for i in body.document_ids
                            if processing.doc_path(i).exists())):
            raise demo.too_big_error()
        texts = "\n\n".join(r["text"] or "" for r in rows)
        cur = con.execute(
            "INSERT INTO documents(owner_id, title, original_name, text, text_source) VALUES (?,?,?,?,?)",
            (rows[0]["owner_id"], body.title or rows[0]["title"], "merged.pdf",
             texts or None, "pdf" if texts else None),
        )
        new_id = cur.lastrowid
        processing.merge_pdfs([processing.doc_path(i) for i in body.document_ids],
                              processing.doc_path(new_id))
        sha = hashlib.sha256(processing.doc_path(new_id).read_bytes()).hexdigest()
        if not con.execute("SELECT 1 FROM documents WHERE sha256=?", (sha,)).fetchone():
            con.execute("UPDATE documents SET sha256=? WHERE id=?", (sha, new_id))
        con.execute(
            "INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) "
            "SELECT ?, tag_id, 'user' FROM document_tags WHERE document_id IN (%s)"
            % ",".join("?" * len(body.document_ids)),
            (new_id, *body.document_ids),
        )
        if body.delete_sources:
            for i in body.document_ids:
                con.execute("UPDATE documents SET deleted_at=datetime('now') WHERE id=?", (i,))
        result = serialize(con, get_doc(con, new_id, user), user)
    worker.wake()
    return result


# ---------- Gespeicherte Suchen („intelligente Ordner“) ----------

VIEW_KEYS = {"q", "tag", "year", "sender", "kind", "due", "warranty", "review", "scope", "sort", "dir"}


class ViewIn(BaseModel):
    name: str
    params: dict = {}


def _clean_params(p: dict) -> dict:
    out = {k: v for k, v in (p or {}).items() if k in VIEW_KEYS and v not in (None, "", False)}
    if "year" in out:
        try:
            out["year"] = int(out["year"])
        except (TypeError, ValueError):
            out.pop("year")
    if not any(k in out for k in ("q", "tag", "year", "sender", "kind", "due", "warranty", "review")):
        raise HTTPException(400, "Eine gespeicherte Suche braucht mindestens einen Filter oder Suchbegriff")
    return out


def _view_out(con, user: User, r) -> dict:
    p = _json.loads(r["params"] or "{}")
    f = _doc_filter(con, user, q=p.get("q"), tag=p.get("tag"), year=p.get("year"), review=bool(p.get("review")),
                    due=bool(p.get("due")), sender=p.get("sender"), scope=p.get("scope"), warranty=bool(p.get("warranty")),
                    kind=p.get("kind"))
    n = 0
    if f:
        where, params, _ = f
        n = con.execute("SELECT COUNT(*) FROM documents d WHERE " + " AND ".join(where), params).fetchone()[0]
    return {"id": r["id"], "name": r["name"], "params": p, "count": n}


@app.get("/views")
def list_views(user: User = Depends(current_user)):
    with connect() as con:
        rows = con.execute("SELECT * FROM saved_views WHERE user_id=? ORDER BY position, id", (user.id,)).fetchall()
        return [_view_out(con, user, r) for r in rows]


@app.post("/views", status_code=201)
def create_view(body: ViewIn, user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, "Erst einen Nutzer anlegen")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name fehlt")
    params = _clean_params(body.params)
    with connect() as con:
        pos = con.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM saved_views WHERE user_id=?", (user.id,)).fetchone()[0]
        vid = con.execute("INSERT INTO saved_views(user_id, name, params, position) VALUES (?,?,?,?)",
                          (user.id, name[:60], _json.dumps(params, ensure_ascii=False), pos)).lastrowid
        return _view_out(con, user, con.execute("SELECT * FROM saved_views WHERE id=?", (vid,)).fetchone())


@app.patch("/views/{view_id}")
def rename_view(view_id: int, body: ViewIn, user: User = Depends(current_user)):
    with connect() as con:
        r = con.execute("SELECT * FROM saved_views WHERE id=? AND user_id=?", (view_id, user.id)).fetchone()
        if not r:
            raise HTTPException(404, "Gespeicherte Suche nicht gefunden")
        name = body.name.strip() or r["name"]
        params = _clean_params(body.params) if body.params else _json.loads(r["params"])
        con.execute("UPDATE saved_views SET name=?, params=? WHERE id=?", (name[:60], _json.dumps(params, ensure_ascii=False), view_id))
        return _view_out(con, user, con.execute("SELECT * FROM saved_views WHERE id=?", (view_id,)).fetchone())


@app.delete("/views/{view_id}", status_code=204)
def delete_view(view_id: int, user: User = Depends(current_user)):
    with connect() as con:
        con.execute("DELETE FROM saved_views WHERE id=? AND user_id=?", (view_id, user.id))


# ---------- Tags & Rules (gemeinsamer Pool) ----------
# Vergeben und neu anlegen darf jeder; umbenennen, umfärben, zusammenführen, löschen und Regeln
# nur Admins – das wirkt auf die Dokumente aller Nutzer.

class TagIn(BaseModel):
    name: str
    color: Optional[str] = None
    parent_id: Optional[int] = None   # Ordner, in dem der Tag liegt; None/0 = oberste Ebene


def _check_parent(con, tag_id: Optional[int], parent_id: Optional[int]) -> Optional[int]:
    if not parent_id:
        return None
    if not con.execute("SELECT 1 FROM tags WHERE id=?", (parent_id,)).fetchone():
        raise HTTPException(404, "Ordner nicht gefunden")
    if tag_id is not None and folders.would_cycle(con, tag_id, parent_id):
        raise HTTPException(400, "Ein Ordner kann nicht in sich selbst oder seinen Unterordnern liegen")
    return parent_id


def _tag_color(c: Optional[str]) -> Optional[str]:
    """Nur #RRGGBB zulassen – die Farbe landet im Web direkt im style-Attribut."""
    c = (c or "").strip()
    return c if re.fullmatch(r"#[0-9a-fA-F]{6}", c) else None


def _visible_owners(con, user: User, scope: Optional[str]) -> list[int]:
    if scope not in ("mine", "partner", "all"):
        scope = "all" if user.show_partner_in_overview else "mine"
    return auth.visible_owner_ids(con, user, scope)


def _editable_owners(con, user: User) -> list[int]:
    """Eigener Pot plus Partner mit Schreibrecht."""
    return [user.id, *[p for p, edit in auth.partner_ids(con, user.id).items() if edit]]


# ---------- Absender ----------

@app.get("/senders")
def list_senders(scope: Optional[str] = None, user: User = Depends(current_user)):
    """Absender der sichtbaren Dokumente mit Anzahl und Summe."""
    with connect() as con:
        owners = _visible_owners(con, user, scope)
        return extras.senders(con, owners) if owners else []


class MergeSendersIn(BaseModel):
    sources: list[str]
    target: str


@app.post("/senders/merge")
def merge_senders(body: MergeSendersIn, user: User = Depends(require_admin)):
    """Absender-Schreibweisen vereinheitlichen – nur in Pots, die ich bearbeiten darf."""
    if not body.target.strip():
        raise HTTPException(400, "Ziel fehlt")
    with connect() as con:
        moved = extras.merge_senders(con, _editable_owners(con, user), body.sources, body.target)
    return {"ok": True, "moved": moved}


@app.post("/senders/suggest-merges")
def suggest_sender_merges(scope: Optional[str] = None, user: User = Depends(require_admin)):
    """LLM gruppiert Schreibweisen desselben Absenders; geändert wird erst per POST /senders/merge."""
    with connect() as con:
        items = extras.senders(con, _visible_owners(con, user, scope))
    try:
        with worker.interactive_llm():
            return {"groups": askmod.suggest_sender_merges(items)}
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"LLM nicht erreichbar: {e.__class__.__name__}")


# ---------- Steuer-Paket & Verträge ----------

@app.get("/export/package.zip")
def export_package(year: Optional[int] = None, tags: str = Query("", description="kommagetrennt"), sender: Optional[str] = None,
                   scope: Optional[str] = None, title: Optional[str] = None, user: User = Depends(current_user)):
    """Steuer-Paket: PDFs umbenannt, Gesamt-PDF mit Inhaltsverzeichnis, CSV-Übersicht, Summen."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    name = title or ("Unterlagen " + " ".join(filter(None, [str(year) if year else "", ", ".join(tag_list), sender or ""])).strip() or "Papigator-Paket")
    with connect() as con:
        owners = _visible_owners(con, user, scope)
        if not owners:
            raise HTTPException(404, "Keine Dokumente")
        try:
            path = extras.build_package(con, owners, year, tag_list, sender, name)
        except ValueError as e:
            raise HTTPException(404, str(e))
    return FileResponse(str(path), media_type="application/zip", filename=f"{extras._safe(name)}.zip",
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


@app.get("/export/tag.zip")
def export_tag(tag: str = Query(..., description="Name des Tags/Ordners"), scope: Optional[str] = None,
               user: User = Depends(current_user)):
    """Ordner zum Herausgeben: Struktur aus den übrigen Tags, Inhaltsverzeichnis (PDF, CSV). Nur sichtbare Dokumente."""
    with connect() as con:
        owners = _visible_owners(con, user, scope)
        t = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()
        if not owners or not t:
            raise HTTPException(404, "Ordner nicht gefunden oder leer")
        try:
            path, name = extras.build_tag_export(con, owners, t["id"])
        except ValueError:   # eine Meldung für „gibt es nicht“ und „leer“ – sonst verrät sie fremde Ordner
            raise HTTPException(404, "Ordner nicht gefunden oder leer")
    return FileResponse(str(path), media_type="application/zip", filename=f"{folders._safe(name)}.zip",
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


@app.get("/contracts")
def list_contracts(scope: Optional[str] = None, user: User = Depends(current_user)):
    """Wiederkehrende Zahlungen (Verträge, Abos) aus den sichtbaren Dokumenten abgeleitet."""
    with connect() as con:
        owners = _visible_owners(con, user, scope)
        return extras.contracts(con, owners) if owners else []


@app.get("/tags")
def list_tags(scope: Optional[str] = None, user: User = Depends(current_user)):
    """Tags mit Zähler, gezählt über die für den Nutzer sichtbaren Dokumente. `unused` = in keinem Bestand ein
    Dokument (z. B. gerade über „Neuer Ordner“ angelegt) – solche Ordner zeigt die Ordner-Ansicht auch leer."""
    with connect() as con:
        if user.id == 0:
            owners = None
        else:
            if scope not in ("mine", "partner", "all"):
                scope = "all" if user.show_partner_in_overview else "mine"
            owners = auth.visible_owner_ids(con, user, scope)
        sql = ("SELECT t.*, COUNT(d.id) AS count, NOT EXISTS (SELECT 1 FROM document_tags u JOIN documents ud ON ud.id=u.document_id "
               "WHERE u.tag_id=t.id AND ud.deleted_at IS NULL) AS unused FROM tags t "
               "LEFT JOIN document_tags dt ON dt.tag_id=t.id "
               "LEFT JOIN documents d ON d.id=dt.document_id AND d.deleted_at IS NULL")
        params: list = []
        if owners is not None:
            sql += f" AND d.owner_id IN ({','.join('?' * len(owners)) or 'NULL'})"
            params = owners
        sql += " GROUP BY t.id ORDER BY t.name"
        tags = [dict(r) for r in con.execute(sql, params)]
        if user.id == 0:
            return tags
        # Tag-Namen anderer verraten etwas („Psychotherapie“): nur Tags zeigen, die an einem sichtbaren Dokument hängen
        # (Papierkorb zählt mit), an gar keinem (leere Ordner) oder die man selbst (bzw. ein Partner) angelegt hat –
        # dazu die Oberordner der gezeigten.
        hidden = folders.hidden_tag_ids(con, auth.visible_owner_ids(con, user, "all"))
        return [t for t in tags if t["id"] not in hidden]


@app.post("/tags", status_code=201)
def create_tag(body: TagIn, user: User = Depends(current_user)):
    with connect() as con:
        if not body.name.strip():
            raise HTTPException(400, "Name fehlt")
        parent = _check_parent(con, None, body.parent_id)
        con.execute("INSERT OR IGNORE INTO tags(name, color, parent_id, created_by) VALUES (?,?,?,?)",
                    (body.name.strip(), _tag_color(body.color), parent, user.id or None))
        out = dict(con.execute("SELECT * FROM tags WHERE name=?", (body.name.strip(),)).fetchone())
        if out["created_by"] != user.id and out["id"] in folders.hidden_tag_ids(con, auth.visible_owner_ids(con, user, "all")):
            out["created_by"] = None   # fremder, verborgener Ordner: nicht verraten, wem er gehört
        return out


@app.patch("/tags/{tag_id}")
def update_tag(tag_id: int, body: TagIn, admin: User = Depends(require_admin)):
    with connect() as con:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Name fehlt")
        clash = con.execute("SELECT id FROM tags WHERE name=? AND id<>?", (name, tag_id)).fetchone()
        if clash:
            raise HTTPException(409, f"Tag „{name}“ gibt es schon – zum Zusammenführen beide auswählen")
        if not con.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone():
            raise HTTPException(404)
        con.execute("UPDATE tags SET name=?, color=? WHERE id=?", (name, _tag_color(body.color), tag_id))
        if "parent_id" in body.model_fields_set:
            con.execute("UPDATE tags SET parent_id=? WHERE id=?", (_check_parent(con, tag_id, body.parent_id), tag_id))
            refresh_parent_tags(con)   # Dokumente im verschobenen Ordner: neue Oberordner, alte weg
        row = con.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return dict(row)


@app.delete("/tags/{tag_id}", status_code=204)
def delete_tag(tag_id: int, admin: User = Depends(require_admin)):
    """Dokumente bleiben und verlieren nur diesen Tag; Unterordner rücken eine Ebene hoch (nicht ganz nach oben)."""
    with connect() as con:
        row = con.execute("SELECT parent_id FROM tags WHERE id=?", (tag_id,)).fetchone()
        if row:
            con.execute("UPDATE tags SET parent_id=? WHERE parent_id=?", (row["parent_id"], tag_id))
        con.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        refresh_parent_tags(con)


class MergeTagsIn(BaseModel):
    source_ids: list[int]
    target_id: int


@app.post("/tags/merge")
def merge_tags(body: MergeTagsIn, admin: User = Depends(require_admin)):
    """Quell-Tags in Ziel-Tag aufgehen lassen (Dokumente, Regeln umhängen, Quellen löschen)."""
    with connect() as con:
        if not con.execute("SELECT 1 FROM tags WHERE id=?", (body.target_id,)).fetchone():
            raise HTTPException(404, "Ziel-Tag nicht gefunden")
        moved = 0
        for sid in body.source_ids:
            if sid == body.target_id:
                continue
            con.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) "
                        "SELECT document_id, ?, source FROM document_tags WHERE tag_id=?", (body.target_id, sid))
            moved += con.execute("DELETE FROM document_tags WHERE tag_id=?", (sid,)).rowcount
            con.execute("UPDATE rules SET tag_id=? WHERE tag_id=?", (body.target_id, sid))
            con.execute("UPDATE OR IGNORE persons SET tag_id=? WHERE tag_id=?", (body.target_id, sid))
            con.execute("UPDATE OR IGNORE retention SET tag_id=? WHERE tag_id=?", (body.target_id, sid))
            # Unterordner der Quelle ins Ziel; lag das Ziel selbst in der Quelle, rückt es eine Ebene hoch
            src_parent = con.execute("SELECT parent_id FROM tags WHERE id=?", (sid,)).fetchone()
            con.execute("UPDATE tags SET parent_id=? WHERE parent_id=? AND id<>?", (body.target_id, sid, body.target_id))
            con.execute("UPDATE tags SET parent_id=? WHERE id=? AND parent_id=?",
                        (src_parent["parent_id"] if src_parent else None, body.target_id, sid))
            con.execute("DELETE FROM tags WHERE id=?", (sid,))
        return {"ok": True, "moved": moved}


@app.post("/tags/suggest-merges")
def suggest_tag_merges(admin: User = Depends(require_admin)):
    """LLM gruppiert ähnliche Tags (Plural, Schreibweise, Synonyme) und schlägt je Gruppe ein Ziel vor.
    Es wird nichts verändert – zusammengeführt wird erst per POST /tags/merge."""
    with connect() as con:
        tags = [{"id": r["id"], "name": r["name"], "count": r["n"]} for r in con.execute(
            "SELECT t.id, t.name, COUNT(d.id) AS n FROM tags t "
            "LEFT JOIN document_tags dt ON dt.tag_id=t.id "
            "LEFT JOIN documents d ON d.id=dt.document_id AND d.deleted_at IS NULL "
            "GROUP BY t.id ORDER BY t.name")]
    try:
        with worker.interactive_llm():
            return {"groups": askmod.suggest_tag_merges(tags)}
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"LLM nicht erreichbar: {e.__class__.__name__}")


class SettingsIn2(BaseModel):
    llm_tag_mode: Optional[str] = None   # free | restricted
    llm_enabled: Optional[bool] = None
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_url2: Optional[str] = None     # Ersatz-Server, wenn der erste nicht erreichbar ist
    llm_model2: Optional[str] = None
    llm_ask_model: Optional[str] = None    # Modell fürs Fragen (leer = wie fürs Erkennen)
    llm_ask_model2: Optional[str] = None
    llm_key: Optional[str] = None      # API-Schlüssel des Haupt-Servers; "" löscht, None lässt unverändert
    llm_key2: Optional[str] = None
    llm_cloud_ack: Optional[bool] = None   # Zustimmung: Dokumenttexte dürfen an die eingetragenen Cloud-Server
    llm_cloud_tag: Optional[bool] = None   # Cloud-KI fürs Erkennen nutzen
    llm_cloud_ask: Optional[bool] = None   # Cloud-KI fürs Fragen nutzen
    ocr_langs: Optional[str] = None
    trash_days: Optional[int] = None
    duplicate_mode: Optional[str] = None
    thumb_width: Optional[int] = None
    backup_dir: Optional[str] = None       # leer = Sicherung aus
    backup_hour: Optional[int] = None
    backup_keep_days: Optional[int] = None
    backup_keep_weeks: Optional[int] = None
    public_url: Optional[str] = None       # Adresse für Freigabe-Links, z. B. https://papigator.example.de
    return_window: Optional[bool] = None   # Online-Käufe: Rücksendefrist (14 Tage) als Frist eintragen
    update_check: Optional[bool] = None     # nach neuen Papigator-Versionen suchen (GitHub-Release)


@app.get("/settings")
def get_settings(user: User = Depends(require_admin)):
    """Nur Admins: enthält Server-Adressen (KI, Sicherung, öffentliche Adresse)."""
    cfg = config.llm_config()
    return {"llm_tag_mode": cfg["tag_mode"], "llm_enabled": cfg["enabled"], "llm_url": cfg["url"],
            "llm_model": cfg["model"], "llm_url2": cfg["url2"], "llm_model2": cfg["model2"],
            "llm_ask_model": cfg["ask_model"], "llm_ask_model2": cfg["ask_model2"],
            **llm.status(),
            "ocr_langs": cfg["ocr_langs"], "trash_days": cfg["trash_days"],
            "duplicate_mode": cfg["duplicate_mode"], "thumb_width": cfg["thumb_width"],
            **{f"backup_{k}": v for k, v in backupmod.settings().items() if k in ("dir", "hour", "keep_days", "keep_weeks")},
            "public_url": _public_url(), "llm_paused_until": worker.paused_until(),
            "return_window": _setting_on("return_window"), "update_check": _update_check()}


def _update_check() -> bool:
    from . import updates
    with connect() as con:
        return updates.enabled(con)


def _setting_on(key: str) -> bool:
    with connect() as con:
        return get_setting(con, key, "0") == "1"


def _public_url() -> str:
    with connect() as con:
        return get_setting(con, "public_url", "") or config.PUBLIC_URL


@app.patch("/settings")
def patch_settings(body: SettingsIn2, admin: User = Depends(require_admin)):
    with connect() as con:
        if body.llm_tag_mode in ("free", "restricted"):
            set_setting(con, "llm_tag_mode", body.llm_tag_mode)
        if body.llm_enabled is not None:
            set_setting(con, "llm_enabled", "1" if body.llm_enabled else "0")
        _patch_llm_servers(con, body)
        if body.return_window is not None:
            set_setting(con, "return_window", "1" if body.return_window else "0")
        if body.update_check is not None:
            set_setting(con, "update_check", "1" if body.update_check else "0")
            set_setting(con, "update_checked", None)       # beim nächsten Blick neu nachsehen
        if body.ocr_langs is not None and body.ocr_langs.strip():
            set_setting(con, "ocr_langs", body.ocr_langs.strip())
        if body.trash_days is not None:
            set_setting(con, "trash_days", str(max(1, min(body.trash_days, 365))))
        if body.duplicate_mode in ("reject", "allow"):
            set_setting(con, "duplicate_mode", body.duplicate_mode)
        if body.thumb_width in (300, 400, 600, 800):
            set_setting(con, "thumb_width", str(body.thumb_width))
        if body.backup_dir is not None:
            set_setting(con, "backup_dir", body.backup_dir.strip())
        if body.backup_hour is not None:
            set_setting(con, "backup_hour", str(max(0, min(body.backup_hour, 23))))
        if body.backup_keep_days is not None:
            set_setting(con, "backup_keep_days", str(max(1, min(body.backup_keep_days, 3650))))
        if body.public_url is not None:
            u = body.public_url.strip().rstrip("/")
            if u and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", u):
                raise HTTPException(400, "Adresse bitte mit http:// oder https:// angeben")
            set_setting(con, "public_url", u)
        if body.backup_keep_weeks is not None:
            set_setting(con, "backup_keep_weeks", str(max(0, min(body.backup_keep_weeks, 520))))
    return get_settings(admin)


def _patch_llm_servers(con, body: SettingsIn2) -> None:
    """KI-Server, Schlüssel und Cloud-Freigabe. Zuerst alles prüfen, dann schreiben.

    - Ein Cloud-Server (Adresse außerhalb des Heimnetzes) braucht die ausdrückliche Zustimmung (llm_cloud_ack),
      sonst 409 mit den betroffenen Namen – die Oberfläche fragt dann nach und schickt die Zustimmung mit.
    - Ein Schlüssel gehört zu seinem Server: Ändert sich der Servername ohne neuen Schlüssel, wird er gelöscht,
      damit er nie an einen anderen Server geht."""
    old = {1: get_setting(con, "llm_url", config.LLM_BASE_URL) or "", 2: get_setting(con, "llm_url2", "") or ""}
    new = {1: old[1] if body.llm_url is None else body.llm_url.strip(),
           2: old[2] if body.llm_url2 is None else body.llm_url2.strip()}
    for slot, url in new.items():
        if url and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", url):
            raise HTTPException(400, "KI-Server bitte mit http:// oder https:// angeben")
        if url and not llm.is_local(url) and url.startswith("http://"):
            raise HTTPException(400, "Server außerhalb des Heimnetzes nur über https:// – sonst gehen Schlüssel und Texte unverschlüsselt durchs Internet")
    keys = {1: body.llm_key, 2: body.llm_key2}
    for k in keys.values():
        if k is not None and len(k.strip()) > 500:
            raise HTTPException(400, "Schlüssel zu lang")
    approved = llm.cloud_hosts(con)
    cloud_now = {llm.host_of(u) for u in new.values() if u and not llm.is_local(u)}
    missing = sorted(cloud_now - approved)
    if missing and not body.llm_cloud_ack:
        raise HTTPException(409, {"cloud_confirm": missing,
                                  "message": "Dokumenttexte würden an einen Cloud-Anbieter gehen – bitte bestätigen"})
    for slot in (1, 2):
        if llm.host_of(new[slot]) != llm.host_of(old[slot]) and keys[slot] is None:
            llm.set_key(con, slot, "")
        if keys[slot] is not None:
            llm.set_key(con, slot, keys[slot])
    set_setting(con, "llm_url", new[1])
    set_setting(con, "llm_url2", new[2])
    if body.llm_model is not None:
        set_setting(con, "llm_model", body.llm_model.strip())
    if body.llm_model2 is not None:
        set_setting(con, "llm_model2", body.llm_model2.strip())
    if body.llm_ask_model is not None:
        set_setting(con, "llm_ask_model", body.llm_ask_model.strip())
    if body.llm_ask_model2 is not None:
        set_setting(con, "llm_ask_model2", body.llm_ask_model2.strip())
    # Freigaben gelten nur für eingetragene Server; wer einen entfernt und später wieder einträgt, wird neu gefragt
    set_setting(con, "llm_cloud_hosts", _json.dumps(sorted((approved | set(missing)) & cloud_now)))
    if body.llm_cloud_tag is not None:
        set_setting(con, "llm_cloud_tag", "1" if body.llm_cloud_tag else "0")
    if body.llm_cloud_ask is not None:
        set_setting(con, "llm_cloud_ask", "1" if body.llm_cloud_ask else "0")


@app.get("/settings/backup")
def backup_status(admin: User = Depends(require_admin)):
    """Einstellungen, letzter Lauf und vorhandene Snapshots."""
    return backupmod.status()


@app.post("/settings/backup/run")
def backup_run(admin: User = Depends(require_admin)):
    """Sicherung im Hintergrund starten; Fortschritt über GET /settings/backup (running)."""
    st = backupmod.status()
    if not st["dir"]:
        raise HTTPException(400, "Kein Zielordner eingestellt")
    if st["running"]:
        raise HTTPException(409, "Eine Sicherung läuft bereits")
    threading.Thread(target=lambda: _safe_backup("manuell"), name="papigator-backup-now", daemon=True).start()
    return {"started": True}


def _safe_backup(reason: str) -> None:
    try:
        backupmod.run(reason)
    except Exception:
        pass  # Fehler steht in den Einstellungen (backup_last_error)


class RestoreIn(BaseModel):
    name: str
    confirm: str = ""


@app.post("/settings/backup/restore")
def backup_restore(body: RestoreIn, admin: User = Depends(require_admin)):
    """Snapshot zurückspielen – überschreibt Datenbank und Dateien. Nur mit confirm="WIEDERHERSTELLEN"."""
    if body.confirm != "WIEDERHERSTELLEN":
        raise HTTPException(400, "Bestätigung fehlt")
    if "/" in body.name or body.name.startswith("."):
        raise HTTPException(400, "Ungültiger Name")
    try:
        return backupmod.restore(body.name)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


class MailSettingsIn(BaseModel):
    mail_enabled: Optional[bool] = None
    mail_host: Optional[str] = None
    mail_port: Optional[int] = None
    mail_user: Optional[str] = None
    mail_password: Optional[str] = None     # leer lassen = unverändert
    mail_folder: Optional[str] = None
    mail_done_folder: Optional[str] = None
    mail_owner: Optional[int] = None        # 0 = erster Admin
    mail_poll: Optional[int] = None
    mail_body_pdf: Optional[bool] = None


@app.get("/settings/mail")
def mail_status(admin: User = Depends(require_admin)):
    """E-Mail-Eingang: Einstellungen (ohne Passwort) und letzter Abruf."""
    return mailmod.settings()


@app.patch("/settings/mail")
def mail_patch(body: MailSettingsIn, admin: User = Depends(require_admin)):
    with connect() as con:
        for k, v in body.model_dump(exclude_unset=True).items():
            if k == "mail_password":
                if v:  # leeres Passwort heißt: so lassen; gespeichert wird nur verschlüsselt
                    set_setting(con, k, security.encrypt(v))
                continue
            if isinstance(v, bool):
                v = "1" if v else "0"
            set_setting(con, k, "" if v is None else str(v).strip())
    return mailmod.settings()


@app.post("/settings/mail/test")
def mail_test(body: MailSettingsIn, admin: User = Depends(require_admin)):
    """Verbindung mit den (ggf. noch ungespeicherten) Werten prüfen, ungelesene zählen, Ordner auflisten."""
    return mailmod.test(body.model_dump(exclude_unset=True))


@app.post("/settings/mail/check")
def mail_check(admin: User = Depends(require_admin)):
    """Postfach jetzt abholen."""
    try:
        return mailmod.check("manuell")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Abruf fehlgeschlagen: {e.__class__.__name__}: {e}")


@app.get("/settings/inbox")
def inbox_status(user: User = Depends(current_user)):
    """Scanner-Eingang: Ordner und wartende Dateien."""
    folders = []
    with connect() as con:
        names = [r["username"] for r in con.execute("SELECT username FROM users ORDER BY id")
                 if user.is_admin or r["username"] == user.username]
    for n in names:
        d = config.INBOX_DIR / n
        files = [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith("_")] if d.exists() else []
        errs = len(list((d / "_fehler").glob("*"))) if (d / "_fehler").exists() else 0
        folders.append({"username": n, "path": str(d), "waiting": len(files), "errors": errs})
    return {"inbox_dir": str(config.INBOX_DIR), "folders": folders, "poll_seconds": config.INBOX_POLL,
            "ftp": shutil.which("vsftpd") is not None}


@app.get("/export.zip")
def export_zip(scope: str = Query("mine", description="mine | all"),
               layout: str = Query("flat", description="flat | folders (Ordner/Jahr/Datum Titel.pdf)"),
               user: User = Depends(current_user)):
    """Alle PDFs + metadata.json als ZIP (eigener Pot; Admin mit scope=all: alle)."""
    with connect() as con:
        tree = folders.tag_tree(con)
        used: set[str] = set()
        if scope == "all" and user.is_admin:
            rows = con.execute("SELECT * FROM documents WHERE deleted_at IS NULL ORDER BY id").fetchall()
        else:
            rows = con.execute("SELECT * FROM documents WHERE deleted_at IS NULL AND owner_id=? ORDER BY id", (user.id,)).fetchall()
        meta = []
        for r in rows:
            trows = con.execute(
                "SELECT t.id, t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=?", (r["id"],)).fetchall()
            tags = [t["name"] for t in trows]
            owner = con.execute("SELECT username FROM users WHERE id=?", (r["owner_id"],)).fetchone()
            safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (r["title"] or ""))[:80].strip()
            meta.append({"id": r["id"], "title": r["title"], "document_date": r["document_date"], "sender": r["sender"],
                         "amount": r["amount"], "currency": r["currency"], "summary": r["summary"], "tags": tags,
                         "original_name": r["original_name"], "created_at": r["created_at"],
                         "owner": owner["username"] if owner else None,
                         "file": f"{r['id']} {safe}.pdf" if safe else f"{r['id']}.pdf"})
            if layout == "folders":
                path = folders.export_path(tree, [t["id"] for t in trows], r["document_date"], r["title"], r["id"])
                if path.lower() in used:   # gleicher Titel am gleichen Tag: ID anhängen
                    path = path[:-4] + f" ({r['id']}).pdf"
                used.add(path.lower())
                meta[-1]["file"] = path

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr("metadata.json", _json.dumps(meta, ensure_ascii=False, indent=1))
        for m in meta:
            p = processing.doc_path(m["id"])
            if p.exists():
                z.write(p, m["file"])
    return FileResponse(tmp.name, media_type="application/zip", filename="Papigator-export.zip",
                        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))



@app.get("/export/selection.zip")
def export_selection(ids: str = Query(..., description="Dokument-IDs, kommagetrennt"), user: User = Depends(current_user)):
    """Ausgewählte Dokumente als ZIP („Datum Titel.pdf“). Nur, was der Nutzer sehen darf – sonst 404."""
    try:
        wanted = list(dict.fromkeys(int(x) for x in ids.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(400, "Ungültige Auswahl")
    if not wanted or len(wanted) > 1000:
        raise HTTPException(400, "Bitte 1 bis 1000 Dokumente auswählen")
    files, used = [], set()
    with connect() as con:
        for doc_id in wanted:
            row = get_doc(con, doc_id, user)
            safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (row["title"] or ""))[:80].strip() or f"Dokument {doc_id}"
            name = f"{row['document_date']} {safe}.pdf" if row["document_date"] else f"{safe}.pdf"
            if name.lower() in used:
                name = name[:-4] + f" ({doc_id}).pdf"
            used.add(name.lower())
            files.append((processing.doc_path(doc_id), name))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for path, name in files:
            if path.exists():
                z.write(path, name)
    return FileResponse(tmp.name, media_type="application/zip", filename=f"Papigator-{len(files)}-Dokumente.zip",
                        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))


class LlmTestIn(BaseModel):
    url: str
    model: Optional[str] = None
    key: Optional[str] = None    # frisch eingegebener, noch nicht gespeicherter Schlüssel
    slot: Optional[int] = None   # 1 | 2: sonst den gespeicherten Schlüssel dieses Servers nehmen


@app.post("/settings/llm/test")
def test_llm(body: LlmTestIn, admin: User = Depends(require_admin)):
    url = body.url.strip()
    if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", url):
        raise HTTPException(400, "KI-Server bitte mit http:// oder https:// angeben")
    key = (body.key or "").strip() or None
    if key is None and body.slot in (1, 2):
        # Den gespeicherten Schlüssel nur an den Server schicken, für den er eingegeben wurde
        with connect() as con:
            saved_url = get_setting(con, "llm_url" if body.slot == 1 else "llm_url2", "") or ""
            if llm.host_of(saved_url) == llm.host_of(url):
                key = llm.stored_key(con, body.slot) or None
    if key and not llm.is_local(url) and url.startswith("http://"):
        raise HTTPException(400, "Schlüssel nur über https:// senden")
    with worker.interactive_llm():
        return processing.llm_test(url, body.model or "", key or config.LLM_API_KEY)


class LlmPauseIn(BaseModel):
    until: Optional[str] = None   # Zeitpunkt mit Zeitzone (ISO), "forever" = bis „Fortsetzen“, leer/None = fortsetzen


@app.post("/settings/llm/pause")
def pause_llm(body: LlmPauseIn, admin: User = Depends(require_admin)):
    """KI-Erkennung anhalten, ohne etwas zu überspringen; Fragen an die Dokumente gehen weiter."""
    from datetime import datetime, timedelta, timezone
    v = (body.until or "").strip()
    if v and v != "forever":
        try:
            until = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "Zeitpunkt als ISO-Datum mit Uhrzeit angeben")
        if until.tzinfo is None:
            raise HTTPException(400, "Zeitpunkt bitte mit Zeitzone")
        now = datetime.now(timezone.utc)
        if until <= now:
            v = ""
        else:
            v = min(until, now + timedelta(days=31)).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with connect() as con:
        if v:
            set_setting(con, "llm_paused_until", v)
        else:
            con.execute("DELETE FROM settings WHERE key='llm_paused_until'")
    worker.wake()
    return {"llm_paused_until": worker.paused_until()}


@app.post("/settings/llm/retag")
def retag_all(scope: str = Query("failed", description="failed | all"), admin: User = Depends(require_admin)):
    """Tagging erneut anstoßen: nur fehlgeschlagene oder alle fertigen Dokumente."""
    with connect() as con:
        if scope == "all":
            n = con.execute("UPDATE documents SET status='tagging' WHERE status='done'").rowcount
        else:
            n = con.execute("UPDATE documents SET status='tagging' WHERE status='done' AND llm_status IN ('failed','skipped')").rowcount
    worker.wake()
    return {"queued": n}


class RuleIn(BaseModel):
    pattern: str
    tag: str


def _rule_docs(con, user: User) -> list[tuple[str, str, int]]:
    """(Titel, Text klein, Besitzer) aller Dokumente, auf die „Auf vorhandene anwenden“ wirken würde."""
    editable: dict[int, bool] = {}
    out = []
    for d in con.execute("SELECT owner_id, title, text FROM documents "
                         "WHERE deleted_at IS NULL AND text IS NOT NULL ORDER BY COALESCE(document_date, created_at) DESC"):
        if d["owner_id"] not in editable:
            editable[d["owner_id"]] = auth.can_edit(con, user, d["owner_id"])
        if editable[d["owner_id"]]:
            out.append((d["title"], d["text"].lower(), d["owner_id"]))
    return out


@app.get("/rules")
def list_rules(user: User = Depends(require_admin)):
    """Mit `matches`: wie viele vorhandene Dokumente das Schlagwort enthalten – damit man sieht, wenn eine Regel
    zu breit ist („Steuer“ steckt in jeder Lohnsteuer, „Bank“ in jeder Bankverbindung)."""
    with connect() as con:
        # Personen-Regeln anderer (Familie) nur, wenn man deren Bestand sehen darf – der Name ist privat
        visible = set(auth.visible_owner_ids(con, user, "all"))
        rules = [dict(r) for r in con.execute(
            "SELECT r.id, r.pattern, r.word, r.created_by, t.name AS tag FROM rules r JOIN tags t ON t.id=r.tag_id ORDER BY r.id")
            if r["created_by"] is None or r["created_by"] in visible or user.id == 0]
        docs = _rule_docs(con, user) if rules else []
        for r in rules:
            r["matches"] = sum(1 for _, text, owner in docs if processing.rule_hit(r["pattern"], r["word"], text)
                               and processing.rule_allowed(con, r["created_by"], owner))
            r["person"] = r.pop("created_by") is not None
            r.pop("word")
    return rules


@app.get("/rules/preview")
def preview_rule(pattern: str, user: User = Depends(require_admin)):
    """Vor dem Anlegen: Anzahl und Beispiele der vorhandenen Dokumente, die das Schlagwort enthalten."""
    p = pattern.strip().lower()
    if len(p) < 3:
        return {"matches": 0, "examples": []}
    with connect() as con:
        hits = [title for title, text, _ in _rule_docs(con, user) if p in text]
    return {"matches": len(hits), "examples": hits[:5]}


@app.post("/rules", status_code=201)
def create_rule(body: RuleIn, admin: User = Depends(require_admin)):
    if len(body.pattern.strip()) < 3 or not body.tag.strip():
        raise HTTPException(400, "Schlagwort (mindestens 3 Zeichen) und Tag angeben")
    body.pattern = body.pattern.strip()
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO tags(name, created_by) VALUES (?, ?)", (body.tag.strip(), admin.id or None))
        tid = con.execute("SELECT id FROM tags WHERE name=?", (body.tag.strip(),)).fetchone()["id"]
        cur = con.execute("INSERT INTO rules(pattern, tag_id) VALUES (?,?)", (body.pattern, tid))
        return {"id": cur.lastrowid, "pattern": body.pattern, "tag": body.tag.strip()}


@app.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, admin: User = Depends(require_admin)):
    with connect() as con:
        con.execute("DELETE FROM rules WHERE id=?", (rule_id,))


@app.post("/rules/apply")
def apply_rules_now(user: User = Depends(require_admin)):
    """Regeln rückwirkend auf alle vorhandenen Dokumente anwenden (sonst greifen sie nur bei neuen).
    Nur Dokumente, die der Nutzer bearbeiten darf."""
    tagged = 0
    with connect() as con:
        rules = con.execute("SELECT r.pattern, r.word, r.created_by, t.id AS tag_id FROM rules r JOIN tags t ON t.id=r.tag_id").fetchall()
        if not rules:
            return {"tagged": 0}
        for d in con.execute("SELECT id, owner_id, text FROM documents WHERE deleted_at IS NULL AND text IS NOT NULL").fetchall():
            if not auth.can_edit(con, user, d["owner_id"]):
                continue
            lowered = d["text"].lower()
            for r in rules:
                if processing.rule_hit(r["pattern"], r["word"], lowered) and processing.rule_allowed(con, r["created_by"], d["owner_id"]):
                    cur = con.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) VALUES (?,?,'rule')",
                                      (d["id"], r["tag_id"]))
                    tagged += cur.rowcount
    return {"tagged": tagged}


@app.get("/ping")
def ping():
    """Ohne Anmeldung: lebt der Server, und braucht er die Ersteinrichtung? (Docker-Healthcheck, Web-Start)"""
    with connect() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"ok": True, "needs_setup": users == 0}


@app.get("/health")
def health(user: User = Depends(current_user)):
    """Zähler, KI- und Sicherungsstatus – nur für angemeldete Nutzer."""
    with connect() as con:
        # Papierkorb nicht mitzählen: dort bleibt ein Dokument im alten Zustand (z. B. „tagging“), bis es
        # wiederhergestellt wird – sonst hinge die Fortschrittsanzeige für immer
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) AS n FROM documents WHERE deleted_at IS NULL GROUP BY status")}
        llm_failed = con.execute("SELECT COUNT(*) FROM documents WHERE llm_status='failed' AND deleted_at IS NULL").fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        # Bestand still nachtragen (Art/Empfänger/Nummern, refill_k): Status bleibt „done“, zählt also nicht oben mit
        backfill = con.execute("SELECT COUNT(*) FROM documents WHERE llm_status='refill_k' AND status='done' "
                               "AND deleted_at IS NULL").fetchone()[0]
        backfill_total = max(backfill, int(get_setting(con, "kinds_v1", "0") or 0))
    bs = backupmod.settings()
    cfg = config.llm_config()
    return {"ok": True, "version": app.version, "documents": counts, "llm_failed": llm_failed, "users": users,
            "backfill": backfill, "backfill_total": backfill_total,
            "llm": cfg["enabled"] and bool(cfg["url"]), "llm_paused_until": worker.paused_until(),
            "backup": {"configured": bool(bs["dir"]), "stale": backupmod.is_stale(bs), "last": bs["last"], "ok": bs["last_ok"]}}


# ---------- Web-Frontend ----------
@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    """Browser sollen HTML/JS/CSS nach jedem Deploy neu prüfen: ein altes index.html mit neuem app.js
    (oder umgekehrt) lässt das Frontend beim Start abbrechen. Mit ETag kostet das nur ein 304."""
    response = await call_next(request)
    if request.url.path.startswith("/app"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/app", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/app/")
