"""Ordner (Tags in Tags), gespeicherte Suchen, Scanner-Unterordner und Ordner-Export."""
import io
import json
import time
import zipfile

from app import config, inbox
from conftest import make_pdf


def _tag(api, name):
    return next(t for t in api.get("/tags").json() if t["name"] == name)


def test_tag_in_folder_and_cycles(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    strom = admin.post("/tags", json={"name": "Strom", "parent_id": haus["id"]}).json()
    assert strom["parent_id"] == haus["id"]
    # Haus kann nicht in Strom liegen (Kreis), und nichts in sich selbst
    assert admin.patch(f"/tags/{haus['id']}", json={"name": "Haus", "parent_id": strom["id"]}).status_code == 400
    assert admin.patch(f"/tags/{haus['id']}", json={"name": "Haus", "parent_id": haus["id"]}).status_code == 400
    assert admin.patch(f"/tags/{strom['id']}", json={"name": "Strom", "parent_id": 9999}).status_code == 404
    # Umbenennen ohne parent_id lässt den Ordner in Ruhe; 0 holt ihn nach oben
    assert admin.patch(f"/tags/{strom['id']}", json={"name": "Strom & Gas"}).json()["parent_id"] == haus["id"]
    assert admin.patch(f"/tags/{strom['id']}", json={"name": "Strom", "parent_id": 0}).json()["parent_id"] is None


def test_only_admins_move_tags(admin, sam):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    strom = admin.post("/tags", json={"name": "Strom"}).json()
    assert sam.patch(f"/tags/{strom['id']}", json={"name": "Strom", "parent_id": haus["id"]}).status_code == 403


def test_delete_folder_lifts_children(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    strom = admin.post("/tags", json={"name": "Strom", "parent_id": haus["id"]}).json()
    admin.delete(f"/tags/{haus['id']}")
    assert _tag(admin, "Strom")["parent_id"] is None


def test_merge_moves_subfolders(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    wohnung = admin.post("/tags", json={"name": "Wohnung"}).json()
    strom = admin.post("/tags", json={"name": "Strom", "parent_id": wohnung["id"]}).json()
    admin.post("/tags/merge", json={"source_ids": [wohnung["id"]], "target_id": haus["id"]})
    assert _tag(admin, "Strom")["parent_id"] == haus["id"]
    # Lag das Ziel in der Quelle, rückt es hoch statt auf sich selbst zu zeigen
    a = admin.post("/tags", json={"name": "A"}).json()
    b = admin.post("/tags", json={"name": "B", "parent_id": a["id"]}).json()
    admin.post("/tags/merge", json={"source_ids": [a["id"]], "target_id": b["id"]})
    assert _tag(admin, "B")["parent_id"] is None


def test_saved_views(admin, sam):
    d = admin.upload("Spende", "Spende", tags="Steuer").json()
    admin.patch(f"/documents/{d['id']}", json={"document_date": "2025-04-01"})
    admin.upload("Andere", "Andere", tags="Steuer")
    assert admin.post("/views", json={"name": "Leer", "params": {}}).status_code == 400
    v = admin.post("/views", json={"name": "Steuer 2025", "params": {"tag": "Steuer", "year": "2025", "evil": 1}})
    assert v.status_code == 201
    assert v.json()["params"] == {"tag": "Steuer", "year": 2025} and v.json()["count"] == 1
    assert [x["name"] for x in admin.get("/views").json()] == ["Steuer 2025"]
    # Fremde sehen und löschen sie nicht
    assert sam.get("/views").json() == []
    sam.delete(f"/views/{v.json()['id']}")
    assert len(admin.get("/views").json()) == 1
    r = admin.patch(f"/views/{v.json()['id']}", json={"name": "Steuer"})
    assert r.json()["name"] == "Steuer" and r.json()["params"]["year"] == 2025
    admin.delete(f"/views/{v.json()['id']}")
    assert admin.get("/views").json() == []


def test_saved_view_count_respects_visibility(admin, sam):
    sam.upload("x", "Fremd", tags="Steuer")
    v = admin.post("/views", json={"name": "Steuer", "params": {"tag": "Steuer", "scope": "all"}}).json()
    assert v["count"] == 0


def test_scanner_subfolders_become_tags(admin, monkeypatch):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    d = config.INBOX_DIR / "alex" / "Haus" / "Strom"
    d.mkdir(parents=True)
    (d / "scan.pdf").write_bytes(make_pdf("Scanner Strom"))
    (config.INBOX_DIR / "alex" / "_fehler").mkdir()
    (config.INBOX_DIR / "alex" / "_fehler" / "kaputt.pdf").write_bytes(b"x")
    files = list(inbox._files(config.INBOX_DIR / "alex", True))
    assert [(f.name, parts) for f, parts in files] == [("scan.pdf", ["Haus", "Strom"])]
    for f, parts in files:
        inbox._import(f, admin.id, parts)
    doc = admin.get("/documents").json()[0]
    assert [t["name"] for t in doc["tags"]] == ["Haus", "Strom"]   # Oberordner kommt seit 11.11.2026 automatisch mit
    assert _tag(admin, "Strom")["parent_id"] == haus["id"]
    assert not (d / "scan.pdf").exists() and d.is_dir()   # Datei weg, Ordner bleibt für den Scanner


def test_export_as_folders(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    admin.post("/tags", json={"name": "Strom", "parent_id": haus["id"]})
    a = admin.upload("a", "Stromrechnung", tags="Strom,Rechnung").json()
    admin.patch(f"/documents/{a['id']}", json={"document_date": "2026-11-01"})
    b = admin.upload("b", "Stromrechnung", tags="Strom").json()
    admin.patch(f"/documents/{b['id']}", json={"document_date": "2026-11-01"})
    admin.upload("c", "Notiz")
    r = admin.get("/export.zip", params={"layout": "folders"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(z.namelist())
    assert "Haus/Strom/2026/2026-11-01 Stromrechnung.pdf" in names
    assert any(n.startswith("Haus/Strom/2026/2026-11-01 Stromrechnung (") for n in names)
    assert any(n.startswith("Ohne Ordner/") for n in names)
    meta = json.loads(z.read("metadata.json"))
    assert all(m["file"] in names for m in meta)


def test_new_empty_folder_is_marked_unused(admin, sam):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    neu = admin.post("/tags", json={"name": "Garten", "parent_id": haus["id"]}).json()
    assert neu["parent_id"] == haus["id"]
    tags = {t["name"]: t for t in admin.get("/tags").json()}
    assert tags["Garten"]["unused"] == 1 and tags["Garten"]["count"] == 0
    # Sobald irgendwer ein Dokument darin hat, ist er nicht mehr „unbenutzt“ – auch nicht für andere,
    # die ihn dann nur bei eigenen Dokumenten sehen
    sam.upload("x", "Rosen", tags="Garten")
    tags = {t["name"]: t for t in admin.get("/tags").json()}
    assert tags["Garten"]["unused"] == 0 and tags["Garten"]["count"] == 0


def _tags(api, doc_id):
    return sorted(t["name"] for t in api.get(f"/documents/{doc_id}").json()["tags"])


def test_parent_folders_are_tagged_automatically(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    energie = admin.post("/tags", json={"name": "Energie", "parent_id": haus["id"]}).json()
    admin.post("/tags", json={"name": "Strom", "parent_id": energie["id"]})
    doc = admin.upload("x", "Abschlag", tags="Strom").json()
    assert _tags(admin, doc["id"]) == ["Energie", "Haus", "Strom"]          # über zwei Ebenen
    # auch beim Bearbeiten: Oberordner lassen sich nicht wegnehmen, solange der Unterordner bleibt
    admin.patch(f"/documents/{doc['id']}", json={"tags": ["Strom"]})
    assert _tags(admin, doc["id"]) == ["Energie", "Haus", "Strom"]
    # Ordner verschieben: Dokumente darin bekommen die neuen Oberordner
    garten = admin.upload("y", "Rasen", tags="Garten").json()
    g = next(t for t in admin.get("/tags").json() if t["name"] == "Garten")
    admin.patch(f"/tags/{g['id']}", json={"name": "Garten", "parent_id": haus["id"]})
    assert _tags(admin, garten["id"]) == ["Garten", "Haus"]


def test_deleting_a_folder_keeps_documents_and_lifts_subfolders(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    energie = admin.post("/tags", json={"name": "Energie", "parent_id": haus["id"]}).json()
    strom = admin.post("/tags", json={"name": "Strom", "parent_id": energie["id"]}).json()
    doc = admin.upload("x", "Abschlag", tags="Strom").json()
    assert admin.delete(f"/tags/{energie['id']}").status_code == 204
    tags = {t["name"]: t for t in admin.get("/tags").json()}
    assert "Energie" not in tags and tags["Strom"]["parent_id"] == haus["id"]   # eine Ebene hoch, nicht ganz nach oben
    assert _tags(admin, doc["id"]) == ["Haus", "Strom"]                         # Dokument bleibt, nur der Tag ist weg
    assert admin.get(f"/documents/{doc['id']}").status_code == 200
    assert strom["id"] == tags["Strom"]["id"]


def test_undoing_a_move_removes_the_automatic_parent(admin):
    haus = admin.post("/tags", json={"name": "Haus"}).json()
    doc = admin.upload("y", "Rasen", tags="Garten").json()
    both = admin.upload("z", "Hecke", tags="Garten,Haus").json()        # Haus hier selbst gesetzt
    g = next(t for t in admin.get("/tags").json() if t["name"] == "Garten")
    admin.patch(f"/tags/{g['id']}", json={"name": "Garten", "parent_id": haus["id"]})
    assert _tags(admin, doc["id"]) == ["Garten", "Haus"]
    admin.patch(f"/tags/{g['id']}", json={"name": "Garten", "parent_id": 0})   # Rückgängig
    assert _tags(admin, doc["id"]) == ["Garten"]                                 # automatisches „Haus“ wieder weg
    assert _tags(admin, both["id"]) == ["Garten", "Haus"]                        # selbst gesetztes bleibt
