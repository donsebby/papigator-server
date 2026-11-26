"""Öffentliche Demo (PAPIGATOR_DEMO=1): erfundene Daten, gemeinsame Konten ohne Admin-Recht, täglich neu.

Ein Besucher darf alles ausprobieren, was nur die Demo-Daten betrifft (hochladen, bearbeiten, löschen in den
Papierkorb, Fristen, Personen, Suche, Fragen). Gesperrt ist, womit er andere aussperren (Passwort, Geräte
abmelden, Freigaben), etwas öffentlich machen (Freigabe-Links, Briefkasten, Einladungen) oder Daten endgültig
löschen könnte – und als zweite Sicherung alles Admin-Zeug, das nach draußen greift (KI-Server, Mail, Sicherung,
öffentliche Adresse, Nutzer). Die Sperren gelten auch für Admins: die Demo wird mit PAPIGATOR_DEMO aus befüllt.

Die einzige Stelle, die das durchsetzt, ist `guard` (Middleware in main.py). Neue ändernde Routen muss
tests/test_demo.py einordnen (Sperrliste hier, nur Admin oder geprüft erlaubt), sonst schlägt der Test fehl.
"""
import asyncio
import json
import re
from typing import Callable, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import auth, config
from .db import connect

DENIED = "In der Demo nicht möglich"
_export_lock = asyncio.Lock()


def _permanent(request: Request, _body) -> bool:
    v = (request.query_params.get("permanent") or "").strip().lower()
    return v not in ("", "0", "false", "no", "off", "f", "n")


# Beim eigenen Konto nur Anzeige-Einstellungen – Passwort und Name gehören allen, die das Konto benutzen
ME_FIELDS = {"show_partner_in_overview", "pay_qr"}


def _me_change(_request: Request, body) -> bool:
    if not isinstance(body, dict):
        return False   # kaputtes JSON: das lehnt die Route selbst ab
    return any(v is not None for k, v in body.items() if k not in ME_FIELDS)


def _shared_account(request: Request, _body) -> bool:
    """Konto löschen: die geteilten Demo-Konten (alex, sam, verwaltung) bleiben – das Testkonto zum Löschen nicht."""
    user = _user(request)
    return user is None or user.username in config.DEMO_KEEP_USERS


# (Methode, Pfad wie in der Route, Bedingung oder None = immer). "*" = jede Methode.
DENY: list[tuple[str, str, Optional[Callable]]] = [
    # Andere aussperren
    ("PATCH", "/auth/me", _me_change),
    # Profilbilder: in einer öffentlichen Demo sähen alle Besucher, was einer hochlädt
    ("PUT", "/auth/me/avatar", None),
    ("DELETE", "/auth/me/avatar", None),
    ("PUT", "/persons/{tag_id}/avatar", None),
    ("DELETE", "/persons/{tag_id}/avatar", None),
    ("DELETE", "/auth/me", _shared_account),
    ("DELETE", "/auth/sessions", None),
    ("DELETE", "/auth/sessions/{session_id}", None),
    ("POST", "/users", None),
    ("PATCH", "/users/{user_id}", None),
    ("DELETE", "/users/{user_id}", None),
    # Familien-Freigaben bleiben wie angelegt
    ("PUT", "/shares", None),
    ("DELETE", "/shares/{user_id}", None),
    # Nichts öffentlich machen: Freigabe-Links, Briefkasten, Einladungen, öffentliche Seiten
    ("POST", "/links", None),
    ("DELETE", "/links/{link_id}", None),
    ("POST", "/invites", None),
    ("DELETE", "/invites/{invite_id}", None),
    ("POST", "/invites/{token}/redeem", None),
    ("*", "/s/{rest:path}", None),
    # Nichts endgültig löschen (Papierkorb bleibt wiederherstellbar)
    ("DELETE", "/trash", None),
    ("DELETE", "/documents/{doc_id}", _permanent),
    # Admin-Einstellungen, die nach draußen greifen (sind ohnehin nur für Admins)
    ("PATCH", "/settings", None),
    ("POST", "/settings/backup/run", None),
    ("POST", "/settings/backup/restore", None),
    ("PATCH", "/settings/mail", None),
    ("POST", "/settings/mail/test", None),
    ("POST", "/settings/mail/check", None),
    ("POST", "/settings/llm/test", None),
    ("POST", "/settings/llm/retag", None),
    ("POST", "/import/paperless", None),          # Übernahme aus Paperless-ngx: großer Upload, viele Dokumente
    # Geld: die erfundenen Konten bleiben – ein Besucher leerte sonst den Reiter für alle bis zum Zurücksetzen
    ("DELETE", "/bank/accounts/{account_id}", None),
    ("DELETE", "/bank/imports/{import_id}", None),
    # Jährliche Kosten: Topf und Markierungen gelten für das geteilte Demo-Konto – „kein Topf“ eines Besuchers
    # leerte die Karte sonst für alle bis zum Zurücksetzen
    ("PUT", "/bank/yearly/pot", None),
    ("PUT", "/bank/yearly/mark", None),
    # Kalender-Abo: ein Link ohne Anmeldung – in der Demo nicht
    ("POST", "/calendar/feed", None),
    ("DELETE", "/calendar/feed", None),
    # Notizen: in der öffentlichen Demo sähen alle Besucher, was einer schreibt
    ("POST", "/documents/{doc_id}/notes", None),
    ("DELETE", "/documents/{doc_id}/notes/{note_id}", None),
    # Wochenübersicht und Mail-Versand: schickt Mails nach draußen (app/digest.py) – in der Demo gar nicht
    ("PUT", "/digest", None),
    ("POST", "/digest/test", None),
    ("PATCH", "/settings/smtp", None),
    ("POST", "/settings/smtp/test", None),
]

# Zählen zum Tageslimit pro Nutzer: alles, was ein neues PDF anlegt oder OCR/KI erneut laufen lässt
QUOTA = {("POST", "/documents"), ("POST", "/documents/merge"), ("POST", "/documents/{doc_id}/reprocess")}


def _regex(path: str) -> re.Pattern:
    out = re.sub(r"\{[^}:]+:path\}", "\x00", path)
    out = re.sub(r"\{[^}]+\}", "\x01", out)
    out = re.escape(out).replace("\x00", ".*").replace("\x01", "[^/]+")
    return re.compile(f"^{out}$")


_DENY = [(m, p, _regex(p), cond) for m, p, cond in DENY]
_QUOTA = [(m, p, _regex(p)) for m, p in QUOTA]


def _match(method: str, path: str, table) -> list:
    return [row for row in table if row[0] in ("*", method) and row[2].match(path)]


def _user(request: Request) -> Optional[auth.User]:
    try:
        return auth.current_user(request.headers.get("authorization"), request.cookies.get("papigator_token"))
    except HTTPException:
        return None   # nicht angemeldet: die Route antwortet selbst mit 401


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def max_bytes() -> int:
    return config.DEMO_MAX_UPLOAD_MB * 1024 * 1024


def too_big(size: int) -> bool:
    """Für Routen, die ein PDF aus vorhandenen bauen (Zusammenfügen): in der Demo höchstens so groß wie ein Upload."""
    return config.DEMO and size > max_bytes()


def too_big_error() -> HTTPException:
    return HTTPException(413, f"In der Demo höchstens {config.DEMO_MAX_UPLOAD_MB} MB pro Datei")


async def guard(request: Request, call_next):
    """Middleware: Sperrliste, Upload-Größe (vor dem Einlesen) und Tageslimit – nur mit PAPIGATOR_DEMO."""
    if not config.DEMO:
        return await call_next(request)
    # scope["path"], nicht request.url: die URL baut Starlette aus dem Host-Kopf – ein krummer Host machte
    # sonst den Pfad leer, und die Sperrliste griffe nicht
    method, path = request.method, request.scope.get("path", "")
    hits = _match(method, path, _DENY)
    if hits:
        body = None
        if any(c is _me_change for *_, c in hits):
            try:
                body = json.loads(await request.body() or b"null")
            except ValueError:
                body = None
        if any(c is None or c(request, body) for *_, c in hits):
            return _deny(403, DENIED)

    if method in ("POST", "PUT", "PATCH", "DELETE"):
        # Starlette/FastAPI lesen den ganzen Body ein, bevor die Route (und die Anmeldung) läuft – deshalb hier an
        # der Länge abweisen. Browser und App schicken sie immer mit; ohne (chunked) wäre die Größe offen.
        length = request.headers.get("content-length")
        upload = method == "POST" and path == "/documents"
        if length is None and (upload or "transfer-encoding" in request.headers):
            return _deny(411, "In der Demo nur Anfragen mit Längenangabe")
        if length is not None and not length.isdigit():
            return _deny(400, "Ungültige Längenangabe")
        # Upload: Luft für die Formularfelder (OCR-Text vom Gerät); alles andere ist kleines JSON
        if length and int(length) > (max_bytes() + 1024 * 1024 if upload else 1024 * 1024):
            return _deny(413, too_big_error().detail if upload else "Anfrage zu groß")

    if method == "GET" and path.startswith("/export"):
        # Exporte bauen ZIPs und PDFs aus allem: in der Demo einer nach dem anderen, sonst reichen ein paar
        # gleichzeitige für den Speicher
        if _export_lock.locked():
            return _deny(429, "In der Demo läuft gerade schon ein Export – bitte gleich noch einmal")
        async with _export_lock:
            return await call_next(request)

    if not _match(method, path, _QUOTA):
        return await call_next(request)
    user = _user(request)
    if user is None:
        return await call_next(request)
    # Erst den Platz reservieren (Zählen und Eintragen in einem Schritt unter der DB-Sperre), dann arbeiten –
    # sonst kämen viele gleichzeitige Uploads alle am Limit vorbei. Abgelehnte geben den Platz wieder frei.
    with connect() as con:
        con.execute("DELETE FROM demo_uploads WHERE at < datetime('now', '-1 day')")
        cur = con.execute("INSERT INTO demo_uploads(user_id) SELECT ? WHERE "
                          "(SELECT COUNT(*) FROM demo_uploads WHERE user_id=?) < ?",
                          (user.id, user.id, config.DEMO_MAX_UPLOADS_PER_DAY))
        slot = cur.lastrowid if cur.rowcount else None
    if slot is None:
        return _deny(429, f"Tageslimit der Demo erreicht ({config.DEMO_MAX_UPLOADS_PER_DAY} Uploads und "
                          "Neuverarbeitungen am Tag) – morgen geht es weiter")
    try:
        response = await call_next(request)
    except BaseException:
        _release(slot)
        raise
    if response.status_code >= 400:
        _release(slot)
    return response


def _release(slot: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM demo_uploads WHERE rowid=?", (slot,))
