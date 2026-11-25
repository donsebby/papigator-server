"""Update-Meldung (25.11.2026): Gibt es eine neuere Papigator-Version als die laufende, sehen Admins einen Hinweis in
Web und App (Nicht-Admins können ohnehin nichts aktualisieren).

Woher: das jüngste GitHub-Release von donsebby/papigator-server (Tag v<Version>, angelegt von tools/publish-server.sh).
Höchstens zweimal am Tag eine einzige anonyme Anfrage an api.github.com – ohne Kennung, ohne Daten über diesen
Server; das Ergebnis liegt in der Datenbank. Abschaltbar in den Einstellungen (Admin) oder per PAPIGATOR_UPDATE_CHECK=0;
in der Demo aus.
"""
import datetime as dt
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends

from . import config
from .auth import User, require_admin
from .db import connect, get_setting, set_setting
from .version import VERSION

log = logging.getLogger(__name__)
router = APIRouter()

REPO = "donsebby/papigator-server"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
MAX_AGE = dt.timedelta(hours=12)


def enabled(con) -> bool:
    if config.DEMO:
        return False
    default = "0" if config.env("UPDATE_CHECK", "1") == "0" else "1"
    return get_setting(con, "update_check", default) == "1"


def _key(v: Optional[str]) -> tuple:
    """„v2026.11.25.1“ → (2026, 11, 25, 1); Unlesbares ist nie neuer."""
    nums = re.findall(r"\d+", (v or "").lstrip("vV"))
    return tuple(int(n) for n in nums) if nums else ()


def newer(latest: Optional[str], current: str = VERSION) -> bool:
    return bool(_key(latest)) and _key(latest) > _key(current)


def _fetch() -> Optional[dict]:
    import httpx
    try:
        r = httpx.get(LATEST, timeout=5, headers={"Accept": "application/vnd.github+json", "User-Agent": "Papigator"})
        if r.status_code == 404:
            return {}                       # noch kein Release
        r.raise_for_status()
        j = r.json()
        return {"version": (j.get("tag_name") or "").lstrip("vV"), "url": j.get("html_url"),
                "notes": (j.get("body") or "")[:2000], "published": j.get("published_at")}
    except Exception as e:                  # offline, GitHub nicht erreichbar: still, nächstes Mal wieder
        log.info("Update-Prüfung: %s", e)
        return None


def status(force: bool = False) -> dict:
    with connect() as con:
        out = {"current": VERSION, "enabled": enabled(con), "latest": None, "newer": False, "url": None, "notes": None,
               "checked": None}
        if not out["enabled"]:
            return out
        checked = get_setting(con, "update_checked")
        cached = get_setting(con, "update_latest")
    stale = force or not checked or dt.datetime.now() - dt.datetime.fromisoformat(checked) > MAX_AGE
    info = json.loads(cached) if cached else {}
    if stale:
        got = _fetch()
        if got is not None:
            info = got
            checked = dt.datetime.now().isoformat(timespec="seconds")
            with connect() as con:
                set_setting(con, "update_latest", json.dumps(info))
                set_setting(con, "update_checked", checked)
    latest = info.get("version") or None
    # checked: wann GitHub zuletzt erreichbar war – None heißt „noch nie“ (offline, gesperrt)
    return {**out, "latest": latest, "newer": newer(latest), "url": info.get("url"), "notes": info.get("notes"),
            "checked": checked}


@router.get("/updates")
def get_updates(force: bool = False, admin: User = Depends(require_admin)):
    """Für Admins: laufende und neueste Version; `newer` = Hinweis zeigen. force=true: gleich bei GitHub nachsehen
    („Jetzt prüfen“ in den Einstellungen)."""
    return status(force=force)
