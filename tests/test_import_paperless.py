"""Übernahme aus Paperless-ngx: selbst gebauter Export nach der Doku/dem Quelltext von document_exporter (manifest.json mit
Django-Datensätzen, __exported_file_name__/__exported_archive_name__, --split-manifest, --use-folder-prefix, --zip).
Nicht mit einem echten Export getestet."""
import io
import json
import zipfile

import pytest
from PIL import Image

from app import import_paperless as ip
from app import processing, worker
from app.db import connect
from conftest import make_pdf


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "orange").save(buf, "PNG")
    return buf.getvalue()


def _doc(pk, title, fname, *, archive=None, mime="application/pdf", tags=(), corr=None, dtype=None, created="2023-05-01",
         content="", deleted=None, extra=None):
    rec = {"model": "documents.document", "pk": pk,
           "fields": {"correspondent": corr, "storage_path": None, "title": title, "document_type": dtype,
                      "content": content, "mime_type": mime, "checksum": "x" * 64, "archive_checksum": None,
                      "page_count": 1, "created": created, "modified": "2024-01-01T10:00:00Z",
                      "added": "2024-01-01T10:00:00Z", "filename": fname, "archive_filename": archive,
                      "original_filename": fname.split("/")[-1], "archive_serial_number": None, "owner": 1,
                      "tags": list(tags), "deleted_at": deleted, "restored_at": None, "transaction_id": None},
           "__exported_file_name__": fname, "__exported_thumbnail_name__": fname.rsplit(".", 1)[0] + "-thumbnail.webp"}
    if archive:
        rec["__exported_archive_name__"] = archive
    rec["fields"].update(extra or {})
    return rec


def _records():
    meta = [
        {"model": "documents.correspondent", "pk": 1, "fields": {"name": "Stadtwerke Musterstadt", "match": "",
                                                                 "matching_algorithm": 6, "is_insensitive": True, "owner": 1}},
        {"model": "documents.documenttype", "pk": 1, "fields": {"name": "Rechnung", "match": "", "matching_algorithm": 6,
                                                                "is_insensitive": True, "owner": 1}},
        {"model": "documents.documenttype", "pk": 2, "fields": {"name": "Invoice", "match": "", "matching_algorithm": 6,
                                                                "is_insensitive": True, "owner": 1}},
        {"model": "documents.documenttype", "pk": 3, "fields": {"name": "Steuerunterlagen", "match": "",
                                                                "matching_algorithm": 0, "is_insensitive": True, "owner": 1}},
        {"model": "documents.tag", "pk": 1, "fields": {"name": "Haus", "color": "#a6cee3", "is_inbox_tag": False,
                                                       "match": "", "matching_algorithm": 0, "is_insensitive": True,
                                                       "owner": 1, "tn_parent": None}},
        {"model": "documents.tag", "pk": 2, "fields": {"name": "Strom", "color": "#a6cee3", "is_inbox_tag": False,
                                                       "match": "", "matching_algorithm": 0, "is_insensitive": True,
                                                       "owner": 1, "tn_parent": 1}},
        {"model": "documents.tag", "pk": 3, "fields": {"name": "Posteingang", "color": "#a6cee3", "is_inbox_tag": True,
                                                       "match": "", "matching_algorithm": 0, "is_insensitive": True,
                                                       "owner": 1, "tn_parent": None}},
        {"model": "auth.user", "pk": 1, "fields": {"username": "admin", "password": "nicht-übernehmen"}},
    ]
    docs = [
        _doc(1, "Stromrechnung Mai", "2023-05-01 Stadtwerke Musterstadt Stromrechnung Mai.pdf", tags=[2, 3], corr=1,
             dtype=1, content="Stadtwerke Musterstadt\nRechnung Nr. 4711\nBetrag 49,90 EUR",
             archive="2023-05-01 Stadtwerke Musterstadt Stromrechnung Mai-archive.pdf"),
        # Word-Datei: nur die Archiv-PDF taugt; created als Zeitpunkt (ältere Versionen)
        _doc(2, "Mietvertrag", "2021-02-03 Mietvertrag.docx", archive="2021-02-03 Mietvertrag-archive.pdf",
             mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", dtype=3,
             created="2021-02-03T00:00:00+01:00", content="Mietvertrag zwischen …"),
        # Foto ohne Archiv
        _doc(3, "Kassenzettel", "2024-07-08 Kassenzettel.png", mime="image/png", dtype=2, content=""),
        # im Paperless-Papierkorb: bleibt draußen
        _doc(4, "Gelöscht", "2020-01-01 Gelöscht.pdf", deleted="2024-02-02T10:00:00Z"),
        # Datei fehlt / Pfad aus dem Export heraus
        _doc(5, "Fehlt", "../../outside.pdf"),
        # E-Mail ohne Archiv: nicht übernehmbar
        _doc(6, "Mail", "2024-01-01 Mail.eml", mime="message/rfc822"),
    ]
    return meta + docs


def _files():
    return {
        "2023-05-01 Stadtwerke Musterstadt Stromrechnung Mai.pdf": make_pdf("paperless strom original"),
        "2023-05-01 Stadtwerke Musterstadt Stromrechnung Mai-archive.pdf": make_pdf("paperless strom archiv"),
        "2021-02-03 Mietvertrag.docx": b"PK\x03\x04 kein PDF",
        "2021-02-03 Mietvertrag-archive.pdf": make_pdf("paperless miete archiv"),
        "2024-07-08 Kassenzettel.png": _png(),
        "2020-01-01 Gelöscht.pdf": make_pdf("paperless gelöscht"),
        "2024-01-01 Mail.eml": b"From: a@example.org\n\nHallo",
    }


@pytest.fixture
def export_dir(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(_records()), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({"version": "2.18.0"}), encoding="utf-8")
    for name, data in _files().items():
        (root / name).write_bytes(data)
    (tmp_path / "outside.pdf").write_bytes(make_pdf("außerhalb"))
    return root


def _rows():
    with connect() as con:
        return {r["title"]: dict(r) for r in con.execute("SELECT * FROM documents")}


def _tags(doc_id):
    with connect() as con:
        return {r[0] for r in con.execute("SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id "
                                          "WHERE dt.document_id=?", (doc_id,))}


def test_import_from_folder(admin, export_dir):
    res = ip.run_path(export_dir, "alex")
    assert (res["total"], res["imported"], res["duplicates"], res["skipped"]) == (5, 3, 0, 2)
    assert {p["title"] for p in res["problems"]} == {"Fehlt", "Mail"}
    rows = _rows()
    strom = rows["Stromrechnung Mai"]
    assert strom["owner_id"] == admin.id and strom["document_date"] == "2023-05-01"
    assert strom["sender"] == "Stadtwerke Musterstadt" and strom["doc_kind"] == "rechnung"
    assert strom["text"].startswith("Stadtwerke") and strom["text_source"] == "paperless"
    assert strom["status"] == "pending" and strom["llm_status"] == "refill_p"
    assert processing.doc_path(strom["id"]).read_bytes() == make_pdf("paperless strom original")   # Original bevorzugt
    assert _tags(strom["id"]) == {"Strom", "Haus"}                       # Ober-Tag als Ordner, ohne Posteingang
    miete = rows["Mietvertrag"]
    assert processing.doc_path(miete["id"]).read_bytes() == make_pdf("paperless miete archiv")
    assert miete["document_date"] == "2021-02-03" and miete["doc_kind"] is None
    assert _tags(miete["id"]) == {"Steuerunterlagen"}                     # Typ ohne passende Art → Tag
    bon = rows["Kassenzettel"]
    assert processing.doc_path(bon["id"]).read_bytes()[:5] == b"%PDF-" and bon["doc_kind"] == "rechnung"
    assert bon["text"] is None                                            # kein Inhalt: OCR läuft wie üblich
    assert "Gelöscht" not in rows and "Fehlt" not in rows
    # Export bleibt unangetastet (import_pdf verschiebt nur die Kopie)
    assert (export_dir / "2023-05-01 Stadtwerke Musterstadt Stromrechnung Mai.pdf").exists()
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1     # Paperless-Nutzer kommen nicht mit


def test_second_run_skips_duplicates(admin, export_dir):
    ip.run_path(export_dir, "alex")
    res = ip.run_path(export_dir, "alex")
    assert res["imported"] == 0 and res["duplicates"] == 3


def test_dry_run_changes_nothing(admin, export_dir):
    res = ip.run_path(export_dir, "alex", dry_run=True)
    assert res["dry_run"] and res["imported"] == 3
    assert _rows() == {}


def test_unknown_user_and_no_manifest(admin, tmp_path, export_dir):
    with pytest.raises(ip.ExportError):
        ip.run_path(export_dir, "niemand")
    (tmp_path / "leer").mkdir()
    with pytest.raises(ip.ExportError):
        ip.run_path(tmp_path / "leer", "alex")


def test_split_manifest_and_folder_prefix(admin, tmp_path):
    """--split-manifest --use-folder-prefix: Dokumente in json/*-manifest.json, Dateien in originals/ und archive/."""
    root = tmp_path / "split"
    for d in ("json", "originals", "archive"):
        (root / d).mkdir(parents=True)
    recs = _records()
    meta = [r for r in recs if r["model"] != "documents.document"]
    (root / "manifest.json").write_text(json.dumps(meta), encoding="utf-8")
    strom = next(r for r in recs if r["pk"] == 1 and r["model"] == "documents.document")
    strom = {**strom, "__exported_file_name__": "originals/strom.pdf", "__exported_archive_name__": "archive/strom-archive.pdf"}
    (root / "json" / "strom-manifest.json").write_text(json.dumps([strom]), encoding="utf-8")
    (root / "originals" / "strom.pdf").write_bytes(make_pdf("split original"))
    (root / "archive" / "strom-archive.pdf").write_bytes(make_pdf("split archiv"))
    res = ip.run_path(root, "alex")
    assert res["imported"] == 1
    assert _rows()["Stromrechnung Mai"]["sender"] == "Stadtwerke Musterstadt"


def _zip(files: dict, prefix="") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(prefix + name, data)
    return buf.getvalue()


def test_zip_upload_route(admin, sam):
    files = {"manifest.json": json.dumps(_records()), **_files(), "../evil.pdf": make_pdf("böse")}
    data = _zip(files, prefix="export-2026-11-26/")           # Ordner eine Ebene tiefer
    r = admin.post("/import/paperless", files={"file": ("export.zip", data, "application/zip")},
                   data={"user": "sam", "dry_run": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3 and _rows() == {}
    r = admin.post("/import/paperless", files={"file": ("export.zip", data, "application/zip")}, data={"user": "sam"})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3
    assert {row["owner_id"] for row in _rows().values()} == {sam.id}
    # nur Admins
    assert sam.post("/import/paperless", files={"file": ("export.zip", data, "application/zip")}).status_code == 403


def test_zip_errors(admin, monkeypatch):
    r = admin.post("/import/paperless", files={"file": ("x.zip", b"kein zip", "application/zip")})
    assert r.status_code == 400 and r.json()["detail"] == "Keine gültige ZIP-Datei"
    r = admin.post("/import/paperless", files={"file": ("x.zip", _zip({"a.txt": "x"}), "application/zip")})
    assert r.status_code == 400 and "manifest.json" in r.json()["detail"]
    r = admin.post("/import/paperless", files={"file": ("x.zip", _zip({"manifest.json": "{}"}), "application/zip")})
    assert r.status_code == 400
    r = admin.post("/import/paperless", files={"file": ("x.zip", _zip({"manifest.json": "[]"}), "application/zip")},
                   data={"user": "niemand"})
    assert r.status_code == 400
    monkeypatch.setattr(ip, "MAX_ZIP_MB", 0)
    r = admin.post("/import/paperless", files={"file": ("x.zip", _zip({"manifest.json": "[]"}), "application/zip")})
    assert r.status_code == 413


def test_type_mapping():
    assert ip.kind_for_type("Rechnung") == "rechnung"
    assert ip.kind_for_type("invoice") == "rechnung"
    assert ip.kind_for_type("Bank statement") == "kontoauszug"
    assert ip.kind_for_type("Kontoauszug") == "kontoauszug"
    assert ip.kind_for_type("Steuerunterlagen") is None


def test_cli(admin, export_dir, capsys):
    assert ip.main([str(export_dir), "--user", "alex", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["imported"] == 3 and out["dry_run"] is True
    assert ip.main([str(export_dir / "nicht-da"), "--user", "alex"]) == 2


def test_llm_fills_only_missing_fields(admin, export_dir, monkeypatch):
    """refill_p: Betrag, Zusammenfassung und Frist kommen von der KI – Titel, Datum, Absender, Art, Tags bleiben."""
    ip.run_path(export_dir, "alex")
    doc = _rows()["Stromrechnung Mai"]
    with connect() as con:
        con.execute("UPDATE documents SET status='tagging' WHERE id=?", (doc["id"],))   # wie nach der OCR-Stufe
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://llm:11434/v1"})
    monkeypatch.setattr(processing, "llm_extract", lambda *a, **k: (
        {"title": "KI-Titel", "sender": "Anders GmbH", "document_date": "2023-05-01", "kind": "Mahnung",
         "amount": 49.90, "currency": "EUR", "summary": "Strom für Mai.", "tags": ["Energie"],
         "direction": "Ausgabe"}, True))
    assert worker.llm_one(doc["id"]) is True
    row = _rows()["Stromrechnung Mai"]
    assert row["sender"] == "Stadtwerke Musterstadt" and row["doc_kind"] == "rechnung"
    assert row["amount"] == 49.9 and row["summary"] == "Strom für Mai." and row["direction"] == "out"
    assert row["status"] == "done" and row["llm_status"] == "done"
    assert _tags(doc["id"]) == {"Strom", "Haus"}


def test_ocr_stage_keeps_refill_p(admin, export_dir, monkeypatch):
    ip.run_path(export_dir, "alex")
    doc = _rows()["Stromrechnung Mai"]
    monkeypatch.setattr(processing, "make_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(processing, "run_ocr", lambda *a, **k: pytest.fail("keine OCR nötig"))
    worker.ocr_one(doc["id"])
    row = _rows()["Stromrechnung Mai"]
    assert row["status"] == "tagging" and row["llm_status"] == "refill_p" and row["text_source"] == "paperless"


def test_no_file_descriptor_leak(admin, tmp_path):
    import os
    root = tmp_path / "viele"
    root.mkdir()
    recs = []
    for i in range(40):
        recs.append(_doc(100 + i, f"Beleg {i}", f"beleg{i}.pdf"))
        (root / f"beleg{i}.pdf").write_bytes(make_pdf(f"viele {i}"))
    (root / "manifest.json").write_text(json.dumps(recs), encoding="utf-8")
    before = len(os.listdir("/dev/fd"))
    assert ip.run_path(root, "alex")["imported"] == 40
    assert len(os.listdir("/dev/fd")) - before < 5
