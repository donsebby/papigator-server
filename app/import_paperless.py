"""Übernehmen aus Paperless-ngx (26.11.2026): ein Export von `document_exporter` (Ordner oder ZIP) wird Dokument für
Dokument in Papigator abgelegt.

    python -m app.import_paperless <exportordner|export.zip> --user <name> [--dry-run]
    POST /import/paperless   (nur Admin, ZIP-Upload, in der Demo gesperrt)

Aufbau des Exports (Paperless-ngx-Doku „Document exporter“, Quelltext document_exporter.py): `manifest.json` ist eine
Liste von Django-Datensätzen {"model", "pk", "fields"}; Dokumente (`documents.document`) tragen zusätzlich
`__exported_file_name__` (Original) und, falls vorhanden, `__exported_archive_name__` (PDF/A-Archivfassung). Mit
`--split-manifest` stehen die Dokumente in eigenen `*-manifest.json`, mit `--use-folder-prefix` liegen die Dateien in
`originals/`, `archive/` und `json/`.

Übernommen wird: Datei (Original, wenn es ein PDF ist, sonst die Archiv-PDF, sonst ein Original-Bild als PDF), Titel,
Erstelldatum → Dokumentdatum, Korrespondent → Absender, Tags → Tags (mit Ober-Tags als Ordner, ohne Posteingangs-Tags),
Dokumenttyp → Papigator-Art, wenn eindeutig (sonst als Tag), Inhalt (OCR-Text) → Volltext, damit keine neue Texterkennung
nötig ist. Danach läuft die KI nur für fehlende Felder (`llm_status='refill_p'`, processing.apply_missing): Betrag,
Frist, Zusammenfassung … – Titel, Datum, Absender, Art und Tags aus Paperless bleiben. Gleiche Dateien (SHA-256) werden
übersprungen, gelöschte Paperless-Dokumente (Papierkorb) und ältere Versionen eines Dokuments ebenso.

Pfade aus dem Manifest und ZIP-Einträge sind fremde Daten: nur Dateien innerhalb des Exports, nie Verweise, ZIP nie
komplett entpacken, Größen begrenzt.
"""
import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from . import auth, config, processing
from .auth import User, require_admin
from .db import connect

log = logging.getLogger("papigator.import_paperless")
router = APIRouter()

MAX_ZIP_MB = int(os.environ.get("PAPIGATOR_IMPORT_MAX_MB", "4096"))   # Upload über die Web-App
MAX_FILE = 512 * 1024 * 1024        # eine Datei im Export
MAX_MANIFEST = 256 * 1024 * 1024    # manifest.json
MAX_MEMBERS = 500_000               # Einträge im ZIP
MAX_RATIO = 200                     # Packrate eines ZIP-Eintrags (ZIP-Bombe)

FILE_KEY, ARCHIVE_KEY = "__exported_file_name__", "__exported_archive_name__"
IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff"}
# Dokumenttypen, die Papigator-Arten entsprechen (zusätzlich zu den deutschen/englischen Bezeichnungen in KIND_LABELS)
TYPE_ALIASES = {"receipt": "rechnung", "bill": "rechnung", "salary": "gehalt", "salary statement": "gehalt",
                "policy": "police", "letter": "info", "brief": "info", "quittung": "rechnung", "beleg": "rechnung",
                "lohnzettel": "gehalt", "gehaltsabrechnung": "gehalt", "versicherung": "police", "steuerbescheid": "bescheid"}


class ExportError(Exception):
    """Export nicht lesbar – Meldung für Nutzer."""


# ---------- Quelle: Ordner oder ZIP ----------

class _Source:
    def names(self) -> list[str]: ...
    def read(self, name: str, limit: int) -> bytes: ...
    def copy(self, name: str, dest: Path) -> None: ...


class _DirSource(_Source):
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, name: str) -> Optional[Path]:
        p = (self.root / name)
        try:
            r = p.resolve()
        except OSError:
            return None
        if self.root not in r.parents or p.is_symlink() or not r.is_file():
            return None
        return r

    def names(self) -> list[str]:
        out = []
        for p in self.root.rglob("*.json"):
            if not p.is_symlink() and p.is_file():
                out.append(p.relative_to(self.root).as_posix())
        return out

    def exists(self, name: str) -> bool:
        return self._path(name) is not None

    def read(self, name: str, limit: int) -> bytes:
        p = self._path(name)
        if not p or p.stat().st_size > limit:
            raise ExportError(f"{name} fehlt oder ist zu groß")
        return p.read_bytes()

    def copy(self, name: str, dest: Path) -> None:
        p = self._path(name)
        if not p or p.stat().st_size > MAX_FILE:
            raise ExportError("Datei fehlt oder ist zu groß")
        shutil.copyfile(p, dest)


class _ZipSource(_Source):
    def __init__(self, path: Path):
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise ExportError("Keine gültige ZIP-Datei")
        infos = self.zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ExportError("Zu viele Dateien im ZIP")
        self.info = {}
        for i in infos:
            n = i.filename.replace("\\", "/")
            parts = PurePosixPath(n).parts
            if i.is_dir() or n.startswith("/") or ".." in parts or not parts:
                continue
            self.info[n] = i

    def names(self) -> list[str]:
        return list(self.info)

    def exists(self, name: str) -> bool:
        return name in self.info

    def _open(self, name: str, limit: int):
        i = self.info.get(name)
        if i is None:
            raise ExportError(f"{name} fehlt im ZIP")
        if i.file_size > limit or (i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 10 * 1024 * 1024):
            raise ExportError(f"{name} ist zu groß")
        try:
            return self.zf.open(i), i.file_size
        except NotImplementedError:   # z. B. zstd (Paperless --zip-compression zstd) vor Python 3.14
            raise ExportError("Diese ZIP-Kompression wird nicht unterstützt – bitte mit deflated exportieren")

    def read(self, name: str, limit: int) -> bytes:
        f, _ = self._open(name, limit)
        with f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ExportError(f"{name} ist zu groß")
        return data

    def copy(self, name: str, dest: Path) -> None:
        f, _ = self._open(name, MAX_FILE)
        n = 0
        with f, dest.open("wb") as out:
            while chunk := f.read(1024 * 1024):
                n += len(chunk)
                if n > MAX_FILE:
                    raise ExportError("Datei ist zu groß")
                out.write(chunk)

    def close(self) -> None:
        self.zf.close()


# ---------- Manifest lesen ----------

def _load_json(src: _Source, name: str) -> list:
    try:
        data = json.loads(src.read(name, MAX_MANIFEST))
    except (ValueError, UnicodeDecodeError):
        raise ExportError(f"{name} ist kein gültiges JSON")
    if not isinstance(data, list):
        raise ExportError(f"{name} hat nicht das Format eines Paperless-Exports")
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("fields"), dict)]


def read_export(src: _Source) -> tuple[str, list[dict]]:
    """(Wurzel im Export, alle Datensätze). manifest.json liegt in der Wurzel oder (ZIP) einen Ordner tiefer."""
    names = src.names()
    roots = sorted((n for n in names if PurePosixPath(n).name == "manifest.json" and len(PurePosixPath(n).parts) <= 2),
                   key=lambda n: len(n))
    if not roots:
        raise ExportError("Keine manifest.json gefunden – ist das ein Export von Paperless-ngx (document_exporter)?")
    manifest = roots[0]
    base = str(PurePosixPath(manifest).parent)
    base = "" if base == "." else base + "/"
    records = _load_json(src, manifest)
    # --split-manifest: je Dokument eine <Name>-manifest.json (mit --use-folder-prefix unter json/)
    for n in names:
        if n.startswith(base) and n.endswith("-manifest.json") and n != manifest:
            records.extend(_load_json(src, n))
    return base, records


def _date(v) -> Optional[str]:
    """created: Datum („2023-05-01“) oder in älteren Versionen Zeitpunkt („2023-05-01T00:00:00+02:00“)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    if not m:
        return None
    try:
        d = datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return d.isoformat() if 1900 <= d.year <= 2100 else None


def kind_for_type(name: str) -> Optional[str]:
    """Paperless-Dokumenttyp → Papigator-Art, nur bei eindeutiger Bezeichnung („Rechnung“, „Invoice“ …)."""
    key = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not key:
        return None
    if key in processing.KINDS:
        return processing.KINDS[key]
    for code, (_, en) in processing.KIND_LABELS.items():
        if key == en.lower():
            return code
    return TYPE_ALIASES.get(key)


def _clean(s, limit: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


class Plan:
    """Was aus den Datensätzen wird: Tags (mit Pfad), Absender, Arten – und die Dokumente."""

    def __init__(self, records: list[dict]):
        by_model: dict[str, dict] = {}
        for r in records:
            by_model.setdefault(str(r.get("model") or ""), {})[r.get("pk")] = r
        self.tags = by_model.get("documents.tag", {})
        self.correspondents = {pk: _clean(r["fields"].get("name"), 120)
                               for pk, r in by_model.get("documents.correspondent", {}).items()}
        self.types = {pk: _clean(r["fields"].get("name"), 60) for pk, r in by_model.get("documents.documenttype", {}).items()}
        self.documents = sorted(by_model.get("documents.document", {}).values(), key=lambda r: str(r.get("pk")))

    def tag_path(self, pk) -> list[str]:
        """Tag samt Ober-Tags (tn_parent in neueren Versionen, parent in älteren), oberster zuerst."""
        out, seen = [], set()
        while pk is not None and pk in self.tags and pk not in seen and len(out) < 10:
            seen.add(pk)
            f = self.tags[pk]["fields"]
            name = _clean(f.get("name"), 60).replace(",", " ")
            if name:
                out.insert(0, name)
            pk = f.get("tn_parent", f.get("parent"))
        return out

    def is_inbox_tag(self, pk) -> bool:
        return bool(self.tags.get(pk, {}).get("fields", {}).get("is_inbox_tag"))


def _pick_file(src: _Source, base: str, rec: dict) -> tuple[Optional[str], str]:
    """(Name im Export, "pdf" | "image") oder (None, Grund)."""
    f = rec["fields"]
    orig, arch = rec.get(FILE_KEY), rec.get(ARCHIVE_KEY)
    mime = str(f.get("mime_type") or "").lower()

    def find(name, folder):
        if not isinstance(name, str) or not name:
            return None
        for cand in (base + name, base + folder + "/" + name):
            if src.exists(cand):
                return cand
        return None
    o = find(orig, "originals")
    if o and (mime == "application/pdf" or (not mime and o.lower().endswith(".pdf"))):
        return o, "pdf"
    a = find(arch, "archive")
    if a:
        return a, "pdf"
    if o and mime in IMAGE_MIMES:
        return o, "image"
    if not o and not a:
        return None, "Datei fehlt im Export"
    return None, f"Dateityp {mime or '?'} ohne Archiv-PDF"


def run(src: _Source, username: str, dry_run: bool = False) -> dict:
    """Export übernehmen; Ergebnis: Zahlen und (höchstens 50) Probleme mit Titel und Grund."""
    from .folders import ensure_path
    from .inbox import images_to_pdf, temp_path
    from .main import import_pdf

    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row:
        raise ExportError(f"Nutzer „{username}“ gibt es nicht")
    user = User.from_row(row)
    base, records = read_export(src)
    plan = Plan(records)
    res = {"dry_run": dry_run, "total": 0, "imported": 0, "duplicates": 0, "skipped": 0, "problems": [],
           "document_ids": []}

    def problem(title, reason):
        res["skipped"] += 1
        if len(res["problems"]) < 50:
            res["problems"].append({"title": title, "reason": reason})

    for rec in plan.documents:
        f = rec["fields"]
        if f.get("deleted_at") or f.get("root_document"):
            continue   # im Paperless-Papierkorb bzw. ältere Fassung eines Dokuments
        res["total"] += 1
        title = _clean(f.get("title"), 120) or _clean(Path(str(f.get("original_filename") or "Dokument")).stem, 120) or "Dokument"
        name, kind = _pick_file(src, base, rec)
        if not name:
            problem(title, kind)
            continue
        tmp = temp_path(Path(name).suffix[:10] or ".bin")
        pdf = tmp
        try:
            src.copy(name, tmp)
            if kind == "image":
                pdf = images_to_pdf([tmp])
            sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            with connect() as con:
                dup = con.execute("SELECT owner_id FROM documents WHERE sha256=?", (sha,)).fetchone()
                if dup and auth.can_view(con, user, dup["owner_id"]):
                    res["duplicates"] += 1
                    continue
            if dry_run:
                res["imported"] += 1
                continue
            tag_paths = [plan.tag_path(pk) for pk in (f.get("tags") or []) if not plan.is_inbox_tag(pk)]
            doc_type = plan.types.get(f.get("document_type"))
            doc_kind = kind_for_type(doc_type) if doc_type else None
            if doc_type and not doc_kind:
                tag_paths.append([doc_type])
            tags = []
            with connect() as con:
                for path in tag_paths:
                    if path:
                        leaf = ensure_path(con, path)
                        if leaf:
                            tags.append(leaf)
            sender = plan.correspondents.get(f.get("correspondent")) or None
            if sender:
                from .extras import canonical_sender
                with connect() as con:
                    sender = canonical_sender(con, sender)
            text = str(f.get("content") or "").strip()
            fields = {"document_date": _date(f.get("created")), "sender": sender, "doc_kind": doc_kind,
                      "llm_status": "refill_p", "text_source": "paperless" if text else None}
            doc_id = import_pdf(pdf, user.id, str(f.get("original_filename") or Path(name).name)[:200], title,
                                text or None, tags, fields=fields)
            res["imported"] += 1
            res["document_ids"].append(doc_id)
        except ExportError as e:
            problem(title, str(e))
        except HTTPException as e:
            if e.status_code == 409:
                res["duplicates"] += 1
            else:
                problem(title, str(e.detail))
        except Exception as e:   # ein kaputtes Dokument hält den Rest nicht auf
            log.warning("Paperless-Import: %s nicht übernommen: %s", title, e)
            problem(title, "Datei nicht lesbar")
        finally:
            tmp.unlink(missing_ok=True)
            if pdf != tmp:
                pdf.unlink(missing_ok=True)
    log.info("Paperless-Import%s: %d Dokumente, %d übernommen, %d Dubletten, %d übersprungen",
             " (Probelauf)" if dry_run else "", res["total"], res["imported"], res["duplicates"], res["skipped"])
    return res


def run_path(path: Path, username: str, dry_run: bool = False) -> dict:
    if path.is_dir():
        return run(_DirSource(path), username, dry_run)
    src = _ZipSource(path)
    try:
        return run(src, username, dry_run)
    finally:
        src.close()


# ---------- Web/App: ZIP hochladen (nur Admin) ----------

@router.post("/import/paperless")
async def import_upload(file: UploadFile = File(...), user: Optional[str] = Form(None), dry_run: bool = Form(False),
                        admin: User = Depends(require_admin)):
    """ZIP eines Paperless-ngx-Exports übernehmen (`document_exporter … --zip` oder den Export-Ordner gezippt).
    user = Benutzername, dem die Dokumente gehören (Standard: der Admin selbst); dry_run = nur zählen."""
    target = (user or admin.username).strip()

    def work():
        limit = MAX_ZIP_MB * 1024 * 1024
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            path = Path(tmp.name)
            n = 0
            while chunk := file.file.read(1024 * 1024):
                n += len(chunk)
                if n > limit:
                    break
                tmp.write(chunk)
        try:
            if n > limit:
                raise HTTPException(413, f"Höchstens {MAX_ZIP_MB} MB – größere Exporte bitte auf dem Server mit "
                                         "python -m app.import_paperless übernehmen")
            try:
                return run_path(path, target, dry_run)
            except ExportError as e:
                raise HTTPException(400, str(e))
        finally:
            path.unlink(missing_ok=True)
    return await run_in_threadpool(work)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.import_paperless",
                                 description="Export von Paperless-ngx (document_exporter) in Papigator übernehmen")
    ap.add_argument("export", type=Path, help="Export-Ordner oder ZIP")
    ap.add_argument("--user", required=True, help="Benutzername, dem die Dokumente gehören")
    ap.add_argument("--dry-run", action="store_true", help="nur prüfen und zählen, nichts ablegen")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.export.exists():
        print(f"{args.export} gibt es nicht", file=sys.stderr)
        return 2
    from .db import init_db
    config.ensure_dirs()
    init_db()
    try:
        res = run_path(args.export, args.user, args.dry_run)
    except ExportError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in res.items() if k != "document_ids"}, ensure_ascii=False, indent=2))
    if not args.dry_run and res["imported"]:
        print("Der laufende Dienst erzeugt Vorschaubilder und ergänzt fehlende Felder per KI im Hintergrund.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
