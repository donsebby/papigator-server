"""Eingangsordner: <INBOX_DIR>/<username>/ wird überwacht, neue PDFs/Bilder werden importiert.

Gedacht für Scanner (Scan-to-FTP/SMB), Mail-Weiterleitungen o. ä. Eine Datei gilt als fertig
geschrieben, wenn sich ihre Größe über zwei Durchläufe nicht ändert. Fehlgeschlagene Importe
landen in <INBOX_DIR>/<username>/_fehler/.

Unterordner werden zu Tags (09.11.2026): <username>/Haus/Strom/scan.pdf bekommt den Tag „Strom“;
fehlt er, wird er im Ordner „Haus“ angelegt. So kann man am Scanner in Ordnern denken.
"""
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import config
from .db import connect

log = logging.getLogger("papigator.inbox")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def _users(con) -> dict[str, int]:
    return {r["username"].lower(): r["id"] for r in con.execute("SELECT id, username FROM users")}


def _fallback_owner(con) -> int | None:
    """Dateien direkt im Inbox-Wurzelordner gehen an den ersten Admin."""
    r = con.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    return r["id"] if r else None


def temp_path(suffix: str) -> Path:
    """Leere Temp-Datei; den Dateideskriptor von mkstemp gleich schließen – sonst läuft bei vielen Dateien
    (Import, Fotos) das Limit offener Dateien voll."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)


def _rgb(im):
    """Für das PDF: Handyfotos aufrecht (EXIF-Drehung), Transparenz auf weißem Grund statt schwarz."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(im) or im
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


# Alle Seiten zusammen höchstens so viele Pixel (≈ 25 Handyfotos à 12 MP) – entpackt liegen sie im Speicher
MAX_TOTAL_PIXELS = 300_000_000


MAX_FRAMES = 200


class TooManyPixels(ValueError):
    pass


def images_to_pdf(srcs: list[Path], max_pixels: int | None = None) -> Path:
    """Bilder (JPEG, PNG, TIFF – auch mehrseitig) als eine PDF, eine Seite je Bild bzw. TIFF-Seite. Die Größe kommt
    vorher aus den Dateiköpfen; zu viele Pixel zusammen: TooManyPixels, bevor irgendetwas entpackt wird."""
    from PIL import Image
    total, frames_n = 0, 0
    limit = max_pixels or MAX_TOTAL_PIXELS
    for src in srcs:
        with Image.open(src) as im:
            # Jede Seite einzeln zählen (Kopf lesen, nichts entpacken): eine winzige erste Seite durfte sonst eine
            # riesige zweite verstecken (TIFF 10×10 + 60000×60000 → Speicher voll, 27.11.2026)
            n = getattr(im, "n_frames", 1)
            frames_n += n
            if frames_n > MAX_FRAMES:
                raise TooManyPixels(f"mehr als {MAX_FRAMES} Seiten")
            for k in range(n):
                if k:
                    im.seek(k)
                total += im.size[0] * im.size[1]
                if total > limit:
                    raise TooManyPixels(f"{total} Pixel")
    frames = []
    for src in srcs:
        with Image.open(src) as im:
            try:
                while True:
                    frames.append(_rgb(im).copy())
                    im.seek(im.tell() + 1)
            except EOFError:
                pass
    out = temp_path(".pdf")
    # Feste Zeitstempel und kein Titel: dasselbe Foto ergibt dieselbe PDF – sonst erkennte die Dubletten-Prüfung (SHA-256) es nicht
    fixed = time.gmtime(0)
    frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=300,
                   creationDate=fixed, modDate=fixed, title="")
    return out


def _image_to_pdf(src: Path) -> Path:
    return images_to_pdf([src])


def _files(user_dir: Path, recursive: bool):
    """(Datei, Unterordner-Teile) – Ordner mit _ oder . am Anfang (z. B. _fehler) bleiben außen vor."""
    stack = [(user_dir, [])]
    while stack:
        d, parts = stack.pop()
        for f in d.iterdir():
            if f.name.startswith(".") or f.name.startswith("_"):
                continue
            if f.is_symlink():      # nie Verweisen folgen: der Dienst liest sonst Dateien außerhalb des Eingangs (Add-on: als root)
                continue
            if f.is_dir():
                if recursive:
                    stack.append((f, [*parts, f.name]))
            elif f.is_file() and f.suffix.lower() in {".pdf", *IMAGE_EXT}:
                yield f, parts


def _folder_tag(parts: list[str]) -> list[str]:
    if not parts:
        return []
    from . import folders
    with connect() as con:
        tag = folders.ensure_path(con, parts)
    return [tag] if tag else []


def _import(path: Path, owner_id: int, parts: list[str] | None = None) -> None:
    from .main import import_pdf  # späte Bindung, main importiert worker/inbox
    src = path
    tmp = None
    if path.suffix.lower() in IMAGE_EXT:
        tmp = _image_to_pdf(path)
        src = tmp
    else:
        tmp = temp_path(".pdf")
        shutil.copy2(path, tmp)
        src = tmp
    try:
        doc_id = import_pdf(src, owner_id, path.name, tags=_folder_tag(parts or []))
        log.info("Inbox: Datei importiert als Dokument %s", doc_id)   # ohne Dateinamen (oft der Titel)
        from .onboarding import mark_inbox_import
        with connect() as con:
            mark_inbox_import(con, owner_id)   # „Los geht’s“: Eingang läuft
        path.unlink(missing_ok=True)
    finally:
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)


def loop() -> None:
    seen: dict[Path, int] = {}  # Pfad -> Größe beim letzten Durchlauf
    while True:
        try:
            config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
            with connect() as con:
                users = _users(con)
                fallback = _fallback_owner(con)
            # Pro Nutzer einen Ordner anlegen, damit der Scanner ein Ziel hat
            for name in users:
                (config.INBOX_DIR / name).mkdir(exist_ok=True)
            current: dict[Path, int] = {}
            scan_dirs = [(config.INBOX_DIR / n, uid, True) for n, uid in users.items()]
            if fallback:
                scan_dirs.append((config.INBOX_DIR, fallback, False))  # Wurzel -> Admin, Unterordner sind Nutzer
            for user_dir, owner_id, recursive in scan_dirs:
                # Nie einem Symlink folgen (27.11.2026): im HA-Add-on läuft der Dienst als root und der Eingang liegt auf der
                # per Samba beschreibbaren /share – „<nutzer> -> /data/…/documents“ hätte dort importiert und gelöscht
                if user_dir.is_symlink() or not user_dir.is_dir():
                    continue
                for f, parts in list(_files(user_dir, recursive)):
                    size = f.stat().st_size
                    current[f] = size
                    if seen.get(f) != size or size == 0:
                        continue  # noch im Schreiben, nächsten Durchlauf abwarten
                    try:
                        _import(f, owner_id, parts)
                    except Exception as e:
                        detail = getattr(e, "detail", str(e))
                        log.warning("Inbox: eine Datei nicht importiert (%s) – liegt unter _fehler", type(e).__name__)
                        err_dir = user_dir / "_fehler"
                        if err_dir.is_symlink():   # „_fehler -> /etc“: nichts dorthin schieben oder schreiben
                            log.warning("Inbox: _fehler ist ein Symlink – Datei bleibt liegen")
                            continue
                        err_dir.mkdir(exist_ok=True)
                        if "existiert bereits" in str(detail):
                            f.unlink(missing_ok=True)  # Duplikat: einfach verwerfen
                        else:
                            target = err_dir / f.name
                            if target.exists():   # gleicher Name schon da (scan.pdf): nicht überschreiben
                                target = err_dir / f"{f.stem} {time.strftime('%Y-%m-%d %H%M%S')}{f.suffix}"
                            shutil.move(str(f), str(target))
                            # Grund daneben legen – wer am Scanner-Ordner nachsieht, soll wissen, was los ist
                            target.with_name(target.name + ".txt").write_text(f"{detail}\n", encoding="utf-8")
            seen = current
        except Exception:
            log.exception("Inbox-Durchlauf fehlgeschlagen")
        time.sleep(config.INBOX_POLL)


def start() -> None:
    threading.Thread(target=loop, name="papigator-inbox", daemon=True).start()
