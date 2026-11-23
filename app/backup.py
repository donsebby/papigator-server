"""Eingebaute Sicherung: täglich ein Snapshot von Datenbank und Dateien in einen Zielordner.

Jeder Snapshot ist ein vollständiger Ordner `JJJJ-MM-TT_HHMM/` mit `papigator.db`, `documents/`,
`thumbnails/` und `manifest.json`. Unveränderte Dateien sind Hardlinks auf den vorherigen Snapshot,
also schnell und platzsparend; die Datenbank wird über die SQLite-Backup-API konsistent kopiert,
auch während der Server läuft. Aufbewahrung: alle Snapshots der letzten `keep_days` Tage, danach
einer pro Woche für `keep_weeks` Wochen.

Kommandozeile (bei gestopptem oder laufendem Server):
    python -m app.backup run
    python -m app.backup restore 2026-11-08_0300
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .db import connect, get_setting, set_setting

log = logging.getLogger("papigator.backup")
_lock = threading.Lock()
SNAP_FMT = "%Y-%m-%d_%H%M"
STALE_DAYS = 2


def settings() -> dict:
    with connect() as con:
        return {
            "dir": get_setting(con, "backup_dir", config.BACKUP_DIR) or "",
            "hour": int(get_setting(con, "backup_hour", "3") or 3),
            "keep_days": int(get_setting(con, "backup_keep_days", "14") or 14),
            "keep_weeks": int(get_setting(con, "backup_keep_weeks", "8") or 8),
            "last": get_setting(con, "backup_last"),
            "last_ok": get_setting(con, "backup_last_ok") == "1",
            "last_error": get_setting(con, "backup_last_error"),
            "last_bytes": int(get_setting(con, "backup_last_bytes", "0") or 0),
            "last_seconds": float(get_setting(con, "backup_last_seconds", "0") or 0),
        }


def _record(ok: bool, error: str | None = None, size: int = 0, seconds: float = 0) -> None:
    with connect() as con:
        set_setting(con, "backup_last", datetime.now().isoformat(timespec="seconds"))
        set_setting(con, "backup_last_ok", "1" if ok else "0")
        set_setting(con, "backup_last_error", error)
        set_setting(con, "backup_last_bytes", str(size))
        set_setting(con, "backup_last_seconds", f"{seconds:.1f}")


def is_stale(s: dict) -> bool:
    """Zielordner gesetzt, aber keine erfolgreiche Sicherung in den letzten zwei Tagen."""
    if not s["dir"]:
        return False
    if not s["last"] or not s["last_ok"]:
        return True
    return datetime.fromisoformat(s["last"]) < datetime.now() - timedelta(days=STALE_DAYS)


def list_snapshots(root: Path) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        m = d / "manifest.json"
        # „.part“ = Sicherung läuft noch (Manifest steht schon drin, umbenannt wird zuletzt)
        if d.is_dir() and not d.name.endswith(".part") and m.exists():
            try:
                info = json.loads(m.read_text())
            except Exception:
                info = {}
            out.append({"name": d.name, "created": info.get("created"), "documents": info.get("documents"),
                        "bytes": info.get("bytes"), "files": info.get("files")})
    return out


def status() -> dict:
    s = settings()
    root = Path(s["dir"]) if s["dir"] else None
    return {**s, "running": _lock.locked(), "stale": is_stale(s),
            "snapshots": list_snapshots(root) if root else [],
            "dir_ok": bool(root and root.is_dir() and os.access(root, os.W_OK))}


def _copy_tree(src: Path, dest: Path, prev: Path | None) -> tuple[int, int]:
    """Dateien kopieren; unveränderte (Größe + mtime) als Hardlink aus dem vorherigen Snapshot."""
    dest.mkdir(parents=True, exist_ok=True)
    files = size = 0
    if not src.is_dir():
        return 0, 0
    for f in src.iterdir():
        if not f.is_file():
            continue
        st = f.stat()
        target = dest / f.name
        old = prev / f.name if prev else None
        linked = False
        if old and old.exists():
            ost = old.stat()
            if ost.st_size == st.st_size and int(ost.st_mtime) == int(st.st_mtime):
                try:
                    os.link(old, target)
                    linked = True
                except OSError:
                    linked = False  # Ziel-Dateisystem ohne Hardlinks (z. B. manche SMB-Mounts)
        if not linked:
            shutil.copy2(f, target)
        files += 1
        size += st.st_size
    return files, size


def run(reason: str = "manuell") -> dict:
    s = settings()
    if not s["dir"]:
        raise ValueError("Kein Zielordner für die Sicherung eingestellt")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Eine Sicherung läuft bereits")
    t0 = time.time()
    root = Path(s["dir"])
    name = datetime.now().strftime(SNAP_FMT)
    if (root / name).exists():  # zweiter Lauf in derselben Minute
        name = datetime.now().strftime(SNAP_FMT + "%S")
    dest = root / (name + ".part")
    try:
        root.mkdir(parents=True, exist_ok=True)
        snaps = list_snapshots(root)
        prev = root / snaps[0]["name"] if snaps else None
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(mode=0o700)   # Snapshots enthalten alle Dokumente – nur für den Dienst lesbar
        # Datenbank konsistent kopieren (auch bei laufendem Server)
        src = sqlite3.connect(config.DB_PATH, timeout=30)
        dst = sqlite3.connect(dest / "papigator.db")
        with dst:
            src.backup(dst)
        n_docs = dst.execute("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL").fetchone()[0]
        dst.close()
        src.close()
        # Ein alter Snapshot kann noch Klartext-Tokens oder ein unverschlüsseltes Mail-Passwort enthalten
        from . import security
        from .db import connect as _connect
        with _connect() as con:
            security.migrate(con)
        db_bytes = (dest / "papigator.db").stat().st_size
        files = size = 0
        for sub in ("documents", "thumbnails"):
            f, b = _copy_tree(config.DATA_DIR / sub, dest / sub, prev / sub if prev else None)
            files += f
            size += b
        total = size + db_bytes
        manifest = {
            "app": "papigator", "format": 1, "created": datetime.now().isoformat(timespec="seconds"),
            "reason": reason, "documents": n_docs, "files": files, "bytes": total,
            "db_sha256": hashlib.sha256((dest / "papigator.db").read_bytes()).hexdigest(),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
        dest.rename(root / name)
        removed = prune(root, s["keep_days"], s["keep_weeks"])
        secs = time.time() - t0
        _record(True, None, total, secs)
        log.info("Sicherung %s: %d Dokumente, %d Dateien, %.1f MB in %.1fs (%s), %d alte entfernt",
                 name, n_docs, files, total / 1e6, secs, reason, removed)
        return {"name": name, **manifest, "seconds": round(secs, 1), "removed": removed}
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        _record(False, f"{e.__class__.__name__}: {e}", 0, time.time() - t0)
        log.exception("Sicherung fehlgeschlagen")
        raise
    finally:
        _lock.release()


def prune(root: Path, keep_days: int, keep_weeks: int) -> int:
    """Jünger als keep_days: alles behalten. Älter: pro Kalenderwoche den neuesten, für keep_weeks Wochen."""
    now = datetime.now()
    keep: set[str] = set()
    weekly: dict[tuple[int, int], str] = {}
    snaps = list_snapshots(root)  # neueste zuerst
    for sn in snaps:
        try:
            ts = datetime.strptime(sn["name"], SNAP_FMT)
        except ValueError:
            keep.add(sn["name"])  # fremde Ordner nie anfassen
            continue
        if ts >= now - timedelta(days=keep_days):
            keep.add(sn["name"])
        elif ts >= now - timedelta(weeks=keep_weeks):
            weekly.setdefault(ts.isocalendar()[:2], sn["name"])
    keep.update(weekly.values())
    removed = 0
    for sn in snaps:
        if sn["name"] not in keep:
            shutil.rmtree(root / sn["name"], ignore_errors=True)
            removed += 1
    for part in root.glob("*.part"):
        shutil.rmtree(part, ignore_errors=True)
    return removed


def restore(name: str) -> dict:
    """Snapshot zurückspielen: Dateien überschreiben, Datenbank in die laufende DB kopieren."""
    s = settings()
    path = Path(name) if "/" in name else Path(s["dir"]) / name
    if not (path / "manifest.json").exists() or not (path / "papigator.db").exists():
        raise ValueError(f"Kein Snapshot: {path}")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Eine Sicherung läuft gerade – bitte kurz warten")
    try:
        files = 0
        for sub in ("documents", "thumbnails"):
            dest = config.DATA_DIR / sub
            dest.mkdir(parents=True, exist_ok=True)
            srcdir = path / sub
            if srcdir.is_dir():
                for f in srcdir.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dest / f.name)
                        files += 1
        src = sqlite3.connect(path / "papigator.db")
        dst = sqlite3.connect(config.DB_PATH, timeout=60)
        # Aktuelle Anmeldungen überleben das Zurückspielen – sonst fliegt der Admin mitten im Restore raus
        sessions = dst.execute("SELECT token, user_id, label, ip, created_at, last_used FROM sessions").fetchall()
        with dst:
            src.backup(dst)
        with dst:
            dst.executemany("INSERT OR IGNORE INTO sessions(token, user_id, label, ip, created_at, last_used) VALUES (?,?,?,?,?,?)",
                            [s for s in sessions if dst.execute("SELECT 1 FROM users WHERE id=?", (s[1],)).fetchone()])
        # PDFs von nach der Sicherung liegen noch im Ordner (ohne Eintrag): neue Dokumente bekommen höhere Nummern,
        # sonst überschriebe das nächste Dokument so eine Datei und zeigte kurz ihr altes Vorschaubild
        ids = [int(f.stem) for f in (config.DATA_DIR / "documents").glob("*.pdf") if f.stem.isdigit()]
        if ids:
            with dst:
                dst.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('documents', 0)")
                dst.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'documents'", (max(ids),))
        n_docs = dst.execute("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL").fetchone()[0]
        dst.close()
        src.close()
        # Ein älterer Snapshot kennt neuere Spalten und Tabellen noch nicht: Anpassungen wie beim Start nachziehen
        from . import db
        db._trigram = None
        db.init_db()
        log.warning("Wiederhergestellt aus %s: %d Dokumente, %d Dateien", path.name, n_docs, files)
        return {"name": path.name, "documents": n_docs, "files": files}
    finally:
        _lock.release()


def loop() -> None:
    """Jede Minute prüfen: Zielordner gesetzt, Stunde erreicht, heute noch nicht gesichert."""
    if config.DEMO:
        return   # Demo: wird täglich verworfen – Sicherungen hielten Hochgeladenes von Besuchern wochenlang fest
    while True:
        try:
            s = settings()
            if s["dir"] and datetime.now().hour == s["hour"]:
                last_ok_today = s["last_ok"] and (s["last"] or "")[:10] == datetime.now().date().isoformat()
                if not last_ok_today and not _lock.locked():
                    run("Zeitplan")
        except Exception:
            log.exception("Sicherungs-Zeitplan")
        time.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1:] or ["status"]
    if cmd[0] == "run":
        print(json.dumps(run("Kommandozeile"), indent=2))
    elif cmd[0] == "restore" and len(cmd) > 1:
        print(json.dumps(restore(cmd[1]), indent=2))
    else:
        print(json.dumps(status(), indent=2, default=str))
