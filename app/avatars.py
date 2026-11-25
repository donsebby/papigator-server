"""Profilbilder (25.11.2026): für Konten (selbst gewählt) und Personen ohne Konto (Kinder unter „Familie“).

Gespeichert als kleines JPEG (256 × 256, mittig quadratisch zugeschnitten, ohne Metadaten – also ohne Ort aus dem
Foto) im Datenordner unter avatars/u<Nutzer>.jpg bzw. avatars/p<Personen-Tag>.jpg; die Sicherung nimmt den Ordner mit.
Sehen darf ein Kontobild nur, wer den Namen ohnehin sieht (due.known_user_ids: man selbst, Freigaben in beide
Richtungen, Admins), ein Personenbild, wer die Person sieht (wie /persons). Ändern: das eigene Bild bzw. Personen,
die ich (oder ein Partner, dessen Bestand ich bearbeiten darf) angelegt habe. Nie in Freigabe-Links (der
Freigabe-Dienst bindet diesen Router nicht ein); in der öffentlichen Demo gesperrt (app/demo.py).

Clients bekommen in /auth/me, den Freigaben und /persons ein Feld `avatar` (Änderungszeit oder null) und hängen es
als ?v= an – so bleibt das Bild im Cache, bis es sich ändert.
"""
import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import auth, config
from . import due as duemod
from .auth import User, current_user
from .db import connect

router = APIRouter()

SIZE = 256
MAX_BYTES = 12 * 1024 * 1024      # ein Foto vom Telefon, nicht mehr
MAX_PIXELS = 50_000_000           # gegen Dekompressionsbomben


def avatar_dir() -> Path:
    return config.DATA_DIR / "avatars"


def _path(kind: str, ident: int) -> Path:
    return avatar_dir() / f"{kind}{int(ident)}.jpg"


def version(kind: str, ident: int) -> Optional[int]:
    """Änderungszeit des Bildes (für ?v=) oder None ohne Bild."""
    p = _path(kind, ident)
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return None


def remove(kind: str, ident: int) -> None:
    _path(kind, ident).unlink(missing_ok=True)


def _store(kind: str, ident: int, data: bytes) -> int:
    from PIL import Image, ImageOps
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Das Bild ist zu groß (höchstens 12 MB)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise HTTPException(413, "Das Bild ist zu groß")
            im = ImageOps.exif_transpose(im)          # hochkant fotografiert bleibt hochkant
            im = ImageOps.fit(im.convert("RGB"), (SIZE, SIZE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, optimize=True)   # ohne exif=… – keine Metadaten
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Das ist kein Bild, das sich lesen lässt (JPEG, PNG, WebP …)")
    avatar_dir().mkdir(parents=True, exist_ok=True)
    tmp = _path(kind, ident).with_suffix(".tmp")
    tmp.write_bytes(out.getvalue())
    tmp.replace(_path(kind, ident))
    return version(kind, ident) or 0


def _send(kind: str, ident: int) -> Response:
    p = _path(kind, ident)
    if not p.is_file():
        raise HTTPException(404, "Kein Bild")
    return Response(p.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


# ---------- Konten ----------

def _may_see_user(con, user: User, uid: int) -> bool:
    if uid == user.id:
        return True
    known = duemod.known_user_ids(con, user)
    return known is None or uid in known


@router.put("/auth/me/avatar")
async def set_my_avatar(file: UploadFile = File(...), user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, "Der Einrichtungszugang hat kein Konto")
    return {"avatar": _store("u", user.id, await file.read(MAX_BYTES + 1))}


@router.delete("/auth/me/avatar", status_code=204)
def delete_my_avatar(user: User = Depends(current_user)):
    remove("u", user.id)


@router.get("/users/{uid}/avatar")
def user_avatar(uid: int, user: User = Depends(current_user)):
    with connect() as con:
        if not _may_see_user(con, user, uid):
            raise HTTPException(404, "Kein Bild")
    return _send("u", uid)


# ---------- Personen ohne Konto ----------

def _person_owners(con, tag_id: int) -> list[int]:
    return [r[0] for r in con.execute("SELECT user_id FROM persons WHERE tag_id=?", (tag_id,))]


def _check_person(con, user: User, tag_id: int, edit: bool) -> None:
    """404, wenn ich die Person nicht sehe; 403, wenn ich sie sehe, aber nicht angelegt habe (bzw. ein Partner mit
    Schreibrecht)."""
    owners = _person_owners(con, tag_id)
    visible = auth.visible_owner_ids(con, user, "all") if user.id != 0 else []
    if not any(o in visible for o in owners):
        raise HTTPException(404, "Person nicht gefunden")
    if edit:
        editable = [user.id, *[p for p, e in auth.partner_ids(con, user.id).items() if e]]
        if not any(o in editable for o in owners):
            raise HTTPException(403, "Diese Person hat jemand anderes angelegt")


@router.put("/persons/{tag_id}/avatar")
async def set_person_avatar(tag_id: int, file: UploadFile = File(...), user: User = Depends(current_user)):
    with connect() as con:
        _check_person(con, user, tag_id, edit=True)
    return {"avatar": _store("p", tag_id, await file.read(MAX_BYTES + 1))}


@router.delete("/persons/{tag_id}/avatar", status_code=204)
def delete_person_avatar(tag_id: int, user: User = Depends(current_user)):
    with connect() as con:
        _check_person(con, user, tag_id, edit=True)
    remove("p", tag_id)


@router.get("/persons/{tag_id}/avatar")
def person_avatar(tag_id: int, user: User = Depends(current_user)):
    with connect() as con:
        _check_person(con, user, tag_id, edit=False)
    return _send("p", tag_id)
