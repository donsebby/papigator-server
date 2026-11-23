"""Eingangsordner: <INBOX_DIR>/<username>/ wird überwacht, neue PDFs/Bilder werden importiert.

Gedacht für Scanner (Scan-to-FTP/SMB), Mail-Weiterleitungen o. ä. Eine Datei gilt als fertig
geschrieben, wenn sich ihre Größe über zwei Durchläufe nicht ändert. Fehlgeschlagene Importe
landen in <INBOX_DIR>/<username>/_fehler/.

Unterordner werden zu Tags (09.11.2026): <username>/Haus/Strom/scan.pdf bekommt den Tag „Strom“;
fehlt er, wird er im Ordner „Haus“ angelegt. So kann man am Scanner in Ordnern denken.
"""
import logging
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


def _image_to_pdf(src: Path) -> Path:
    from PIL import Image
    out = Path(tempfile.mkstemp(suffix=".pdf")[1])
    with Image.open(src) as im:
        frames = []
        try:
            while True:
                frames.append(im.convert("RGB").copy())
                im.seek(im.tell() + 1)
        except EOFError:
            pass
    frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=300)
    return out


def _files(user_dir: Path, recursive: bool):
    """(Datei, Unterordner-Teile) – Ordner mit _ oder . am Anfang (z. B. _fehler) bleiben außen vor."""
    stack = [(user_dir, [])]
    while stack:
        d, parts = stack.pop()
        for f in d.iterdir():
            if f.name.startswith(".") or f.name.startswith("_"):
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
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        shutil.copy2(path, tmp)
        src = tmp
    try:
        doc_id = import_pdf(src, owner_id, path.name, tags=_folder_tag(parts or []))
        log.info("Inbox: %s importiert als Dokument %s", path.name, doc_id)
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
                if not user_dir.is_dir():
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
                        log.warning("Inbox: %s nicht importiert: %s", f.name, detail)
                        err_dir = user_dir / "_fehler"
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
