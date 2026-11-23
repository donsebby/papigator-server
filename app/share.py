"""Freigabe-Links: Dokumente für Leute ohne Konto (Steuerberater, Vermieter, Familie).

Ein Link zeigt ein Dokument, eine feste Auswahl, einen Ordner (Tag samt Unterordnern) oder eine gespeicherte Suche –
immer nur eigene Dokumente des Erstellers, nie Partner-Dokumente oder den Papierkorb. Ordner und
Suchen werden bei jedem Aufruf neu ausgewertet, neue Belege erscheinen also von selbst.
Optional mit Ablaufdatum und Passwort; widerrufen = löschen.

Briefkasten (kind „upload“, 10.11.2026): umgekehrte Richtung – wer den Link hat, kann PDFs/Fotos
*einwerfen*, sieht aber nichts. Eingeworfenes landet beim Ersteller als „von außen, zu prüfen“
(ext_pending) und erscheint erst nach „Übernehmen“ in anderen Freigaben. Grenzen: Dateigröße, Dateien
pro Sendung, pro 15 Minuten und insgesamt; nur PDF/JPEG/PNG nach Dateiinhalt.

Öffentlich sind nur die Pfade unter /s/<token>. Wer den Server ins Internet stellt, kann im
Reverse-Proxy genau diese Pfade freigeben und den Rest im Heimnetz lassen.
"""
import hmac
import html
import json
import os
import re
import secrets
import tempfile
import warnings
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import auth, config, folders, processing, security
from .auth import User, current_user
from .db import connect, get_setting

router = APIRouter()   # Verwaltung, nur angemeldet
public = APIRouter()   # öffentlich: alles unter /s/ – läuft auch allein als share_app auf eigenem Port
MAX_PW_FAILS = 10
MAX_SELECTION = 500
UPLOAD_MAX_MB = 20          # pro Datei
UPLOAD_MAX_REQUEST_MB = 50  # pro Sendung (der Reverse-Proxy muss mindestens so viel durchlassen)
UPLOAD_MAX_FILES = 10       # pro Sendung
UPLOAD_MAX_PER_15MIN = 30   # Dateien bzw. Sendungen pro Briefkasten
UPLOAD_MAX_PER_IP = 40      # Sendungen pro Absender-IP in 15 Minuten (über alle Briefkästen)
UPLOAD_MAX_TOTAL = 200      # pro Briefkasten insgesamt (gezählt wird jede angenommene Datei, auch Dubletten)
UPLOAD_MAX_PAGES = 50       # Seiten pro PDF – mehr würde die OCR für alle anderen blockieren
UPLOAD_MAX_PIXELS = 40_000_000  # pro Bild (40 MP); mehr Bilder in einer Datei (APNG, MPO) gar nicht
UPLOAD_MAX_PARALLEL = 2     # gleichzeitige Sendungen pro Briefkasten
_uploading: Counter = Counter()  # laufende Sendungen je Link (nur im Event-Loop verändert)


# ---------- Hilfen ----------

def _cookie_name(link_id: int) -> str:
    return f"pg_share_{link_id}"


def _cookie_value(con, token: str) -> str:
    """Nach richtigem Passwort: HMAC über den Token mit einem aus dem Hauptschlüssel abgeleiteten Schlüssel."""
    return security.mac("share-cookie", token)


def _expired(row) -> bool:
    if not row["expires_at"]:
        return False
    return row["expires_at"] < datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def public_base(con, request: Request) -> str:
    """Adresse für Links: Einstellung „öffentliche Adresse“, sonst PAPIGATOR_PUBLIC_URL, sonst die, über die
    gerade zugegriffen wird."""
    return (get_setting(con, "public_url") or config.PUBLIC_URL or str(request.base_url)).rstrip("/")


def _owner(con, row) -> User | None:
    u = con.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    return User.from_row(u) if u else None


def link_docs(con, row) -> list:
    """Dokumente eines Links, ausgewertet mit den Rechten des Erstellers (nur eigener Pot)."""
    owner = _owner(con, row)
    if not owner:
        return []
    base = "d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id=?"
    if row["kind"] == "upload":
        return []  # Briefkasten: von außen ist nichts zu sehen
    if row["kind"] == "doc":
        return con.execute(f"SELECT d.* FROM documents d WHERE {base} AND d.id=?", (owner.id, int(row["target"]))).fetchall()
    if row["kind"] == "docs":  # feste Auswahl: Liste von IDs
        ids = [int(x) for x in (row["target"] or "").split(",") if x.isdigit()]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT d.* FROM documents d WHERE {base} AND d.id IN ({marks}) "
            "ORDER BY COALESCE(d.document_date, d.created_at) DESC, d.id DESC", (owner.id, *ids)).fetchall()
    if row["kind"] == "tag":
        tree = folders.tag_tree(con)
        root = int(row["target"])
        if root not in tree:
            return []
        ids, todo = {root}, [root]
        while todo:  # Unterordner einsammeln
            cur = todo.pop()
            for tid, (_, parent) in tree.items():
                if parent == cur and tid not in ids:
                    ids.add(tid); todo.append(tid)
        marks = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT d.* FROM documents d WHERE {base} AND d.id IN "
            f"(SELECT document_id FROM document_tags WHERE tag_id IN ({marks})) "
            "ORDER BY COALESCE(d.document_date, d.created_at) DESC, d.id DESC", (owner.id, *ids)).fetchall()
    if row["kind"] == "view":
        from .main import _doc_filter  # späte Bindung gegen Zirkel
        p = json.loads(row["target"] or "{}")
        f = _doc_filter(con, owner, q=p.get("q"), tag=p.get("tag"), year=p.get("year"), review=bool(p.get("review")),
                        due=bool(p.get("due")), sender=p.get("sender"), scope="mine", warranty=bool(p.get("warranty")))
        if not f:
            return []
        where, params, _ = f
        where = [*where, "d.ext_pending=0"]  # ungeprüfte Briefkasten-Dokumente nie öffentlich
        return con.execute("SELECT d.* FROM documents d WHERE " + " AND ".join(where) +
                           " ORDER BY COALESCE(d.document_date, d.created_at) DESC, d.id DESC LIMIT 2000", params).fetchall()
    return []


def _out(con, request: Request, r) -> dict:
    # In der DB liegt nur die Prüfsumme des Tokens; die Adresse zum Kopieren steht verschlüsselt daneben
    tok = security.decrypt(r["token_enc"])
    return {"id": r["id"], "kind": r["kind"], "name": r["name"],
            "url": f"{public_base(con, request)}/s/{tok}" if tok else "",
            "has_password": bool(r["password_hash"]), "expires_at": r["expires_at"], "expired": _expired(r),
            "created_at": r["created_at"], "last_access": r["last_access"], "access_count": r["access_count"],
            "documents": _upload_count(con, r["id"]) if r["kind"] == "upload" else len(link_docs(con, r))}


def _upload_count(con, link_id: int) -> int:
    """Über einen Briefkasten eingegangene Dokumente (ohne Papierkorb)."""
    return con.execute("SELECT COUNT(*) FROM documents WHERE ext_link_id=? AND deleted_at IS NULL", (link_id,)).fetchone()[0]


# ---------- Verwaltung (angemeldet) ----------

class LinkIn(BaseModel):
    kind: str                       # doc | docs | tag | view | upload
    target: int | str | dict | list[int] | None = None  # Dokument-ID, Liste, Tag-ID/-Name, Filter; bei upload optional Ziel-Ordner
    name: str | None = None
    expires_days: int | None = 30   # None/0 = unbefristet
    password: str | None = None


def _hidden_tags(con, user: User) -> set[int]:
    """Fremde Ordner, deren Namen der Nutzer nicht kennt – ein Link darauf würde den Namen verraten."""
    return folders.hidden_tag_ids(con, auth.visible_owner_ids(con, user, "all"))


@router.post("/links", status_code=201)
def create_link(body: LinkIn, request: Request, user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, "Erst einen Nutzer anlegen")
    with connect() as con:
        if body.kind == "doc":
            try:
                doc_id = int(body.target)
            except (TypeError, ValueError):
                raise HTTPException(400, "Dokument-ID fehlt")
            d = con.execute("SELECT * FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,)).fetchone()
            if not d or d["owner_id"] != user.id:
                raise HTTPException(404, "Teilen geht nur mit eigenen Dokumenten")
            target, name = str(doc_id), body.name or d["title"]
        elif body.kind == "docs":
            if not isinstance(body.target, list) or not body.target:
                raise HTTPException(400, "Keine Dokumente ausgewählt")
            ids = list(dict.fromkeys(int(i) for i in body.target))
            if len(ids) > MAX_SELECTION:
                raise HTTPException(400, f"Höchstens {MAX_SELECTION} Dokumente pro Link")
            marks = ",".join("?" * len(ids))
            own = {r[0] for r in con.execute(
                f"SELECT id FROM documents WHERE id IN ({marks}) AND owner_id=? AND deleted_at IS NULL", (*ids, user.id))}
            if len(own) != len(ids):
                n = len(ids) - len(own)
                raise HTTPException(404, f"Teilen per Link geht nur mit eigenen Dokumenten – {n} der Auswahl "
                                         f"{'gehört' if n == 1 else 'gehören'} jemand anderem oder {'liegt' if n == 1 else 'liegen'} im Papierkorb")
            target, name = ",".join(map(str, ids)), body.name or f"{len(ids)} Dokument{'' if len(ids) == 1 else 'e'}"
        elif body.kind == "tag":
            t = None
            if isinstance(body.target, int) or str(body.target).isdigit():
                t = con.execute("SELECT * FROM tags WHERE id=?", (int(body.target),)).fetchone()
            if not t:
                t = con.execute("SELECT * FROM tags WHERE name=?", (str(body.target),)).fetchone()
            if not t or t["id"] in _hidden_tags(con, user):
                raise HTTPException(404, "Ordner nicht gefunden")
            target, name = str(t["id"]), body.name or t["name"]
        elif body.kind == "view":
            if isinstance(body.target, dict):
                params = body.target
            else:
                v = con.execute("SELECT * FROM saved_views WHERE id=? AND user_id=?", (int(body.target), user.id)).fetchone()
                if not v:
                    raise HTTPException(404, "Gespeicherte Suche nicht gefunden")
                params = json.loads(v["params"]); body.name = body.name or v["name"]
            from .main import _clean_params
            target, name = json.dumps(_clean_params(params), ensure_ascii=False), body.name or "Auswahl"
        elif body.kind == "upload":
            target = ""
            if body.target not in (None, "", 0):
                t = None
                if isinstance(body.target, int) or str(body.target).isdigit():
                    t = con.execute("SELECT * FROM tags WHERE id=?", (int(body.target),)).fetchone()
                if not t and isinstance(body.target, str):
                    t = con.execute("SELECT * FROM tags WHERE name=?", (body.target,)).fetchone()
                if not t or t["id"] in _hidden_tags(con, user):
                    raise HTTPException(404, "Ordner nicht gefunden")
                target = str(t["id"])
            name = body.name or "Briefkasten"
        else:
            raise HTTPException(400, "kind muss doc, docs, tag, view oder upload sein")
        if body.password is not None and 0 < len(body.password) < 4:
            raise HTTPException(400, "Das Passwort braucht mindestens 4 Zeichen")
        days = body.expires_days or 0
        if days < 0 or days > 3650:
            raise HTTPException(400, "Ablauf zwischen 1 und 3650 Tagen")
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else None
        token = secrets.token_urlsafe(24)
        lid = con.execute(
            "INSERT INTO share_links(token, token_enc, user_id, kind, target, name, password_hash, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (security.token_hash(token), security.encrypt(token), user.id, body.kind, target, name.strip()[:120] or "Freigabe",
             auth.hash_password(body.password) if body.password else None, expires)).lastrowid
        return _out(con, request, con.execute("SELECT * FROM share_links WHERE id=?", (lid,)).fetchone())


@router.get("/links")
def list_links(request: Request, user: User = Depends(current_user)):
    with connect() as con:
        rows = con.execute("SELECT * FROM share_links WHERE user_id=? ORDER BY created_at DESC, id DESC", (user.id,)).fetchall()
        return [_out(con, request, r) for r in rows]


@router.delete("/links/{link_id}", status_code=204)
def revoke_link(link_id: int, user: User = Depends(current_user)):
    with connect() as con:
        con.execute("DELETE FROM share_links WHERE id=? AND user_id=?", (link_id, user.id))


# ---------- Öffentlich: /s/<token> ----------

def _public_headers(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _load(con, token: str):
    row = con.execute("SELECT * FROM share_links WHERE token=?", (security.token_hash(token),)).fetchone()
    if not row:
        raise HTTPException(404, "Link nicht gefunden")
    return row


def _unlocked(con, row, request: Request, token: str) -> bool:
    if not row["password_hash"]:
        return True
    got = request.cookies.get(_cookie_name(row["id"])) or ""
    return hmac.compare_digest(got, _cookie_value(con, token))


def _doc_of(con, row, request: Request, doc_id: int, token: str):
    if _expired(row) or not _unlocked(con, row, request, token):
        raise HTTPException(404)
    for d in link_docs(con, row):
        if d["id"] == doc_id:
            return d
    raise HTTPException(404)


PAGE_CSS = """
:root{--bg:#f3f3f6;--card:#fff;--text:#111118;--muted:#6b6b78;--border:rgba(0,0,0,.08);--accent:#2f8a5a}
@media (prefers-color-scheme:dark){:root{--bg:#0e1311;--card:#161e1a;--text:#e7eee9;--muted:#95a59b;--border:rgba(255,255,255,.08);--accent:#5fbe8c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:center;gap:10px;margin-bottom:22px}header img{width:34px;height:34px;border-radius:9px}
header b{font-size:18px}header span{color:var(--muted);font-size:13px;margin-left:auto}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px}
.doc{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:10px;text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:8px}
.doc img{width:100%;aspect-ratio:.72;object-fit:cover;object-position:top;border-radius:9px;background:#fbfbfa}
.doc .t{font-weight:600;font-size:14px;line-height:1.3}.doc .m{font-size:12px;color:var(--muted)}
.doc .dl{font-size:12.5px;font-weight:600;color:var(--accent);text-decoration:none}
.box{max-width:380px;margin:60px auto;background:var(--card);border:1px solid var(--border);border-radius:20px;padding:26px;text-align:center}
input{width:100%;padding:11px 13px;border-radius:12px;border:1px solid var(--border);background:var(--bg);color:var(--text);font:inherit;margin:14px 0 10px}
button{width:100%;padding:11px;border-radius:12px;border:0;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{color:#d9403b;font-size:14px}footer{margin-top:40px;color:var(--muted);font-size:12.5px;text-align:center}
.ok{color:var(--accent);font-size:15px;font-weight:600}.box.wide{max-width:460px;text-align:left}.box.wide h1{text-align:center}
input[type=file]{padding:14px;border-style:dashed;cursor:pointer}.hint{color:var(--muted);font-size:13px;margin:4px 0 0}
"""


def _page(title: str, body: str, upload: bool = False) -> HTMLResponse:
    label, foot = ("Briefkasten", "Papigator-Briefkasten · Eingeworfenes sieht nur der Empfänger") if upload else \
        ("Freigabe", "Geteilt über Papigator · nur lesen")
    doc = (f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
           f'<title>{html.escape(title)} · Papigator</title><link rel="icon" href="/s/_/icon.png"><style>{PAGE_CSS}</style></head>'
           f'<body><div class="wrap"><header><img src="/s/_/icon.png" alt=""><b>Papigator</b><span>{label}</span></header>{body}'
           f'<footer>{foot}</footer></div></body></html>')
    return _public_headers(HTMLResponse(doc))


def _fmt_date(s: str | None) -> str:
    if not s:
        return ""
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return s[:10]


@public.get("/s/_/icon.png", include_in_schema=False)
def public_icon():
    """Logo der öffentlichen Seite unter /s/…, damit ein Reverse-Proxy nur diesen Pfad freigeben muss."""
    from pathlib import Path
    return FileResponse(Path(__file__).parent / "static" / "icon.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


UPLOAD_ERRORS = {
    "none": "Bitte mindestens eine Datei auswählen.",
    "many": f"Höchstens {UPLOAD_MAX_FILES} Dateien auf einmal.",
    "type": "Nur PDF, JPEG oder PNG.",
    "size": f"Zu groß – höchstens {UPLOAD_MAX_MB} MB pro Datei und {UPLOAD_MAX_REQUEST_MB} MB pro Sendung.",
    "bad": "Eine Datei ließ sich nicht lesen.",
    "rate": "Gerade kamen sehr viele Dateien an – bitte in einer Viertelstunde noch einmal.",
    "full": "Dieser Briefkasten ist voll.",
}


def _upload_page(row, token: str, ok: int | None, error: str | None) -> HTMLResponse:
    until = f"<p class=\"hint\" style=\"text-align:center\">Offen bis {_fmt_date(row['expires_at'])}</p>" if row["expires_at"] else ""
    msg = ""
    if ok:
        msg = f'<p class="ok">Danke! {ok} Datei{"" if ok == 1 else "en"} {"ist" if ok == 1 else "sind"} angekommen.</p>'
    elif error in UPLOAD_ERRORS:
        msg = f'<p class="err">{html.escape(UPLOAD_ERRORS[error])}</p>'
    return _page(row["name"],
                 f'<form class="box wide" method="post" enctype="multipart/form-data" action="/s/{html.escape(token)}/upload">'
                 f'<h1 style="font-size:22px">{html.escape(row["name"])}</h1>'
                 '<p class="sub" style="text-align:center">Hier kannst du Dokumente einwerfen. Was schon im Briefkasten liegt, ist nicht zu sehen.</p>'
                 f'{msg}<input type="file" name="files" multiple required accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png">'
                 f'<p class="hint">PDF, JPEG oder PNG · bis {UPLOAD_MAX_MB} MB pro Datei, {UPLOAD_MAX_REQUEST_MB} MB pro Sendung · '
                 f'höchstens {UPLOAD_MAX_FILES} auf einmal</p>'
                 '<input name="sender" maxlength="80" placeholder="Dein Name (optional)" autocomplete="name">'
                 f'<button type="submit">Einwerfen</button>{until}</form>', upload=True)


@public.get("/s/{token}", include_in_schema=False)
def public_page(token: str, request: Request, error: str | None = None, ok: int | None = None):
    with connect() as con:
        row = con.execute("SELECT * FROM share_links WHERE token=?", (security.token_hash(token),)).fetchone()
        if not row or _expired(row):
            return _page("Link abgelaufen", '<div class="box"><h1>Link nicht mehr gültig</h1>'
                         '<p class="sub">Dieser Freigabe-Link ist abgelaufen oder wurde zurückgezogen.</p></div>')
        if not _unlocked(con, row, request, token):
            err = '<p class="err">Das Passwort stimmt nicht.</p>' if error == "pw" else (
                '<p class="err">Zu viele Versuche – bitte später noch einmal.</p>' if error == "locked" else "")
            return _page(row["name"], f'<form class="box" method="post" action="/s/{html.escape(token)}/unlock">'
                         f'<h1 style="font-size:22px">{html.escape(row["name"])}</h1><p class="sub">Diese Freigabe ist mit einem Passwort geschützt.</p>'
                         f'{err}<input type="password" name="password" placeholder="Passwort" autofocus required>'
                         '<button type="submit">Öffnen</button></form>', upload=row["kind"] == "upload")
        if row["kind"] == "upload":
            con.execute("UPDATE share_links SET access_count=access_count+1, last_access=datetime('now') WHERE id=?", (row["id"],))
            return _upload_page(row, token, ok, error)
        docs = link_docs(con, row)
        con.execute("UPDATE share_links SET access_count=access_count+1, last_access=datetime('now') WHERE id=?", (row["id"],))
        until = f" · gültig bis {_fmt_date(row['expires_at'])}" if row["expires_at"] else ""
        cards = "".join(
            f'<div class="doc"><a href="/s/{token}/file/{d["id"]}" target="_blank" rel="noopener">'
            f'<img src="/s/{token}/thumb/{d["id"]}" alt="" loading="lazy" onerror="this.style.visibility=\'hidden\'"></a>'
            f'<div class="t">{html.escape(d["title"] or "Dokument")}</div>'
            f'<div class="m">{html.escape(" · ".join(filter(None, [_fmt_date(d["document_date"]), d["sender"]])))}</div>'
            f'<a class="dl" href="/s/{token}/file/{d["id"]}?download=1">PDF herunterladen</a></div>'
            for d in docs)
        empty = '<p class="sub">Hier liegt gerade nichts.</p>' if not docs else ""
        return _page(row["name"], f'<h1>{html.escape(row["name"])}</h1>'
                     f'<p class="sub">{len(docs)} Dokument{"" if len(docs) == 1 else "e"}{until}</p>{empty}<div class="grid">{cards}</div>')


@public.post("/s/{token}/unlock", include_in_schema=False)
def public_unlock(token: str, request: Request, password: str = Form("")):
    with connect() as con:
        row = _load(con, token)
        key = f"share:{row['id']}"
        con.execute("DELETE FROM login_attempts WHERE at < datetime('now', '-15 minutes')")
        fails = con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
        if fails >= MAX_PW_FAILS:
            return RedirectResponse(f"/s/{token}?error=locked", status_code=303)
        if not row["password_hash"] or not auth.verify_password(password, row["password_hash"]):
            con.execute("INSERT INTO login_attempts(key) VALUES (?)", (key,))
            return RedirectResponse(f"/s/{token}?error=pw", status_code=303)
        resp = RedirectResponse(f"/s/{token}", status_code=303)
        resp.set_cookie(_cookie_name(row["id"]), _cookie_value(con, token), max_age=7 * 86400, httponly=True,
                        samesite="lax", secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
                        path=f"/s/{token}")
        return resp


# ---------- Briefkasten: Einwerfen ----------

def _save_limited(f: UploadFile, limit: int) -> Path | None:
    """Datei in eine Temp-Datei schreiben; None, wenn sie größer als limit ist."""
    fd, name = tempfile.mkstemp(suffix=".upload")
    path, total = Path(name), 0
    with os.fdopen(fd, "wb") as out:
        while chunk := f.file.read(1 << 20):
            total += len(chunk)
            if total > limit:
                break
            out.write(chunk)
    if total > limit or total == 0:
        path.unlink(missing_ok=True)
        return None
    return path


def _kind_of(path: Path) -> str | None:
    """Dateityp nach Inhalt, nicht nach Endung."""
    with path.open("rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def _image_upload_to_pdf(path: Path) -> Path | None:
    """Bild von außen → PDF. None bei mehreren Bildern in einer Datei oder zu vielen Pixeln – Größe
    und Bildzahl werden aus dem Dateikopf gelesen, *bevor* irgendetwas entpackt wird (Dekompressionsbombe)."""
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as im:
            if getattr(im, "n_frames", 1) > 1:
                return None
            w, h = im.size
            if w * h > UPLOAD_MAX_PIXELS:
                return None
            rgb = im.convert("RGB")
    out = Path(tempfile.mkstemp(suffix=".pdf")[1])
    rgb.save(out, "PDF", resolution=300)
    return out


def _client_ip(request: Request) -> str:
    """Besucher-IP: weitergeleitete Angaben nur von vertrauten Proxys (siehe app/clientip.py)."""
    from .clientip import client_ip
    return (client_ip(request) or "?").strip()[:64]


def _clean(s: str, limit: int) -> str:
    """Steuerzeichen raus, Leerraum zusammenfassen – der Text stammt von Unbekannten."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", s or "")).strip()[:limit]


@public.post("/s/{token}/upload", include_in_schema=False)
async def public_upload(token: str, request: Request):
    """Einwerfen. Token, Ablauf, Passwort, Tempo und Größe werden geprüft, *bevor* der Inhalt gelesen wird –
    sonst könnte jeder mit einer erfundenen Adresse beliebig viel auf die Platte des Servers schreiben.
    Jede Sendung zählt sofort (auch abgelehnte), dazu eine Grenze pro Absender-IP und höchstens
    UPLOAD_MAX_PARALLEL gleichzeitige Sendungen pro Briefkasten."""
    back = f"/s/{token}"
    ip_key = f"upload-ip:{_client_ip(request)}"
    with connect() as con:
        row = _load(con, token)
        if row["kind"] != "upload" or _expired(row) or not _unlocked(con, row, request, token):
            return RedirectResponse(back, status_code=303)
        owner = _owner(con, row)
        if not owner:
            raise HTTPException(404)
        key = f"upload:{row['id']}"
        con.execute("DELETE FROM login_attempts WHERE at < datetime('now', '-15 minutes')")
        count = lambda k: con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (k,)).fetchone()[0]
        if count(key) >= UPLOAD_MAX_PER_15MIN or count(ip_key) >= UPLOAD_MAX_PER_IP \
                or _uploading[row["id"]] >= UPLOAD_MAX_PARALLEL:
            return RedirectResponse(f"{back}?error=rate", status_code=303)
        if (row["upload_count"] if "upload_count" in row.keys() else 0) >= UPLOAD_MAX_TOTAL:
            return RedirectResponse(f"{back}?error=full", status_code=303)
        con.executemany("INSERT INTO login_attempts(key) VALUES (?)", [(key,), (ip_key,)])  # die Sendung zählt sofort
        folder = None
        if row["target"]:
            t = con.execute("SELECT name FROM tags WHERE id=?", (int(row["target"]),)).fetchone()
            folder = t["name"] if t else None
        link_id, link_name = row["id"], row["name"]
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if not size or size > UPLOAD_MAX_REQUEST_MB * 1024 * 1024:
        return RedirectResponse(f"{back}?error={'size' if size else 'none'}", status_code=303)
    _uploading[link_id] += 1
    form = None
    try:
        try:
            form = await request.form(max_files=UPLOAD_MAX_FILES + 1, max_fields=5)
        except Exception as e:  # Starlette: zu viele Dateien/Felder oder kaputte Sendung
            return RedirectResponse(f"{back}?error={'many' if 'Too many' in str(e) else 'bad'}", status_code=303)
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        sender = form.get("sender")
        sender = sender if isinstance(sender, str) else ""
        if not files:
            return RedirectResponse(f"{back}?error=none", status_code=303)
        if len(files) > UPLOAD_MAX_FILES:
            return RedirectResponse(f"{back}?error=many", status_code=303)
        with connect() as con:
            r = con.execute("SELECT upload_count FROM share_links WHERE id=?", (link_id,)).fetchone()
            if not r:
                raise HTTPException(404)
            recent = con.execute("SELECT COUNT(*) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
            if recent - 1 + len(files) > UPLOAD_MAX_PER_15MIN:  # -1: die Sendung selbst zählt schon
                return RedirectResponse(f"{back}?error=rate", status_code=303)
            if (r["upload_count"] or 0) + len(files) > UPLOAD_MAX_TOTAL:
                return RedirectResponse(f"{back}?error=full", status_code=303)
            con.executemany("INSERT INTO login_attempts(key) VALUES (?)", [(key,)] * (len(files) - 1))
        err, n = await run_in_threadpool(_take_files, files, owner.id, sender, folder, link_id, link_name)
    finally:
        _uploading[link_id] -= 1
        if _uploading[link_id] <= 0:
            del _uploading[link_id]
        if form is not None:
            await form.close()
    return RedirectResponse(f"{back}?error={err}" if err else f"{back}?ok={n}", status_code=303)


def _take_files(files: list[UploadFile], owner_id: int, sender: str, folder: str | None,
                link_id: int, link_name: str) -> tuple[str | None, int]:
    """Prüfen, umwandeln, importieren – (Fehlerschlüssel, Anzahl)."""
    from .main import import_pdf
    # Erst alles prüfen, dann importieren: eine kaputte Datei lässt die ganze Sendung zurückgehen
    ready, temps, err = [], [], None
    try:
        for f in files:
            raw = _save_limited(f, UPLOAD_MAX_MB * 1024 * 1024)
            if not raw:
                err = "size"; break
            temps.append(raw)
            kind = _kind_of(raw)
            if not kind:
                err = "type"; break
            pdf = raw
            if kind != "pdf":
                try:
                    pdf = _image_upload_to_pdf(raw)
                except Exception:
                    pdf = None
                if not pdf:
                    err = "bad"; break
                temps.append(pdf)
            pages = processing.page_count(pdf)
            if not 1 <= pages <= UPLOAD_MAX_PAGES:
                err = "bad"; break
            name = _clean(Path(f.filename.replace("\\", "/")).name, 120) or "Dokument"
            ready.append((pdf, Path(name).stem or "Dokument"))
        if err:
            return err, 0
        who = _clean(sender, 80)
        ext_from = f"{link_name} · {who}" if who else link_name
        for pdf, title in ready:
            import_pdf(pdf, owner_id, f"{title}.pdf", title=title, tags=[folder] if folder else None,
                       ext_link_id=link_id, ext_from=ext_from)
        # Jede angenommene Datei zählt, auch eine schon vorhandene – sonst verriete der Zähler, was im Archiv liegt
        with connect() as con:
            con.execute("UPDATE share_links SET upload_count=COALESCE(upload_count, 0)+? WHERE id=?", (len(ready), link_id))
    finally:
        for t in temps:
            t.unlink(missing_ok=True)
    return None, len(ready)


@public.get("/s/{token}/file/{doc_id}", include_in_schema=False)
def public_file(token: str, doc_id: int, request: Request, download: bool = Query(False)):
    with connect() as con:
        row = _load(con, token)
        d = _doc_of(con, row, request, doc_id, token)
    p = processing.doc_path(doc_id)
    if not p.exists():
        raise HTTPException(404)
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (d["title"] or f"dokument-{doc_id}")).strip()
    resp = FileResponse(p, media_type="application/pdf", filename=f"{safe}.pdf" if download else None,
                        headers=None if download else {"Content-Disposition": "inline"})
    return _public_headers(resp)


@public.get("/s/{token}/thumb/{doc_id}", include_in_schema=False)
def public_thumb(token: str, doc_id: int, request: Request):
    with connect() as con:
        row = _load(con, token)
        _doc_of(con, row, request, doc_id, token)
    p = processing.thumb_path(doc_id)
    if not p.exists():
        raise HTTPException(404)
    return _public_headers(FileResponse(p, media_type="image/jpeg"))
