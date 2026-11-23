"""Dokumente: Sichtbarkeit zwischen Nutzern, Partner-Freigabe, Bearbeiten, Zuordnen, Papierkorb, Doppel."""
from conftest import make_pdf


def test_upload_requires_pdf(admin):
    r = admin.post("/documents", files={"file": ("bild.png", b"\x89PNG", "image/png")})
    assert r.status_code == 415


def test_documents_private_without_share(admin, sam):
    doc = admin.upload("Steuerbescheid Alex", "Bescheid").json()
    assert sam.get(f"/documents/{doc['id']}").status_code == 404
    assert sam.get(f"/documents/{doc['id']}/file").status_code == 404
    assert sam.get(f"/documents/{doc['id']}/thumbnail").status_code == 404
    assert sam.patch(f"/documents/{doc['id']}", json={"title": "x"}).status_code == 404
    assert sam.delete(f"/documents/{doc['id']}").status_code == 404
    assert sam.post(f"/documents/{doc['id']}/reprocess").status_code == 404
    assert sam.get("/documents", params={"scope": "all"}).json() == []
    assert sam.get("/documents", params={"q": "Steuerbescheid", "scope": "all"}).json() == []


def test_partner_share_read_only(admin, sam):
    doc = admin.upload("Kfz-Versicherung", "Versicherung").json()
    assert admin.put("/shares", json={"user_id": sam.id, "can_edit": False}).status_code == 200
    r = sam.get(f"/documents/{doc['id']}")
    assert r.status_code == 200 and r.json()["can_edit"] is False
    assert sam.get(f"/documents/{doc['id']}/file").status_code == 200
    assert sam.patch(f"/documents/{doc['id']}", json={"title": "x"}).status_code == 403
    assert sam.delete(f"/documents/{doc['id']}").status_code == 403
    ids = [d["id"] for d in sam.get("/documents", params={"scope": "partner"}).json()]
    assert ids == [doc["id"]]
    assert sam.get("/documents", params={"scope": "mine"}).json() == []


def test_partner_share_with_edit_and_revoke(admin, sam):
    doc = admin.upload("Mietvertrag", "Miete").json()
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    assert sam.patch(f"/documents/{doc['id']}", json={"title": "Mietvertrag 2026"}).status_code == 200
    assert admin.delete(f"/shares/{sam.id}").status_code == 204
    assert sam.get(f"/documents/{doc['id']}").status_code == 404


def test_share_is_one_directional(admin, sam):
    mine = sam.upload("Arztrechnung Sam", "Arzt").json()
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    # Alex hat freigegeben, Sam nicht – also sieht Alex Sams Dokumente nicht
    assert admin.get(f"/documents/{mine['id']}").status_code == 404


def test_update_fields_and_tags(admin):
    doc = admin.upload("Rechnung", "Rechnung").json()
    r = admin.patch(f"/documents/{doc['id']}", json={
        "title": "Stromrechnung", "sender": "Stadtwerke", "amount": 42.5, "currency": "EUR",
        "document_date": "2026-10-01", "tags": ["Strom", "Rechnung"]})
    assert r.status_code == 200
    d = r.json()
    assert d["title"] == "Stromrechnung" and d["sender"] == "Stadtwerke" and d["amount"] == 42.5
    assert sorted(t["name"] for t in d["tags"]) == ["Rechnung", "Strom"]


def test_assign_owner(admin, sam, make_user):
    doc = admin.upload("Zeugnis Sam", "Zeugnis").json()
    r = admin.post("/documents/owner", json={"document_ids": [doc["id"]], "owner_id": sam.id})
    assert r.status_code == 200
    assert sam.get(f"/documents/{doc['id']}").json()["is_own"] is True
    assert admin.get(f"/documents/{doc['id']}").status_code == 404


def test_non_admin_assign_only_to_editable_partners(admin, sam, make_user):
    other = make_user("dritter")
    doc = sam.upload("Brief", "Brief").json()
    # Sam darf nicht an Fremde zuordnen …
    r = sam.post("/documents/owner", json={"document_ids": [doc["id"]], "owner_id": other.id})
    assert r.status_code == 403
    # … und keine fremden Dokumente umhängen
    foreign = other.upload("Fremd", "Fremd").json()
    r = sam.post("/documents/owner", json={"document_ids": [foreign["id"]], "owner_id": sam.id})
    assert r.status_code == 404
    # Mit Schreibfreigabe von Alex an Sam darf Sam ihm Dokumente geben
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    r = sam.post("/documents/owner", json={"document_ids": [doc["id"]], "owner_id": admin.id})
    assert r.status_code == 200


def test_trash_restore_and_empty(admin, sam):
    doc = admin.upload("Alt", "Alt").json()
    other = sam.upload("Sam alt", "SamAlt").json()
    sam.delete(f"/documents/{other['id']}")
    assert admin.delete(f"/documents/{doc['id']}").status_code == 204
    assert admin.get("/documents").json() == []
    assert [d["id"] for d in admin.get("/documents", params={"trash": True}).json()] == [doc["id"]]
    assert admin.get("/trash/count").json()["count"] == 1
    admin.post(f"/documents/{doc['id']}/restore")
    assert len(admin.get("/documents").json()) == 1
    admin.delete(f"/documents/{doc['id']}")
    assert admin.delete("/trash").status_code == 204
    assert admin.get(f"/documents/{doc['id']}").status_code == 404
    # Der Papierkorb von Sam bleibt unberührt
    assert sam.get("/trash/count").json()["count"] == 1


def test_duplicate_rejected_for_same_owner(admin):
    pdf = make_pdf("gleich")
    assert admin.upload(title="A", content=pdf).status_code == 201
    assert admin.upload(title="B", content=pdf).status_code == 409


def test_duplicate_in_trash_is_restored_for_same_owner(admin):
    pdf = make_pdf("gleich")
    doc = admin.upload(title="A", content=pdf).json()
    admin.delete(f"/documents/{doc['id']}")
    r = admin.upload(title="B", content=pdf)
    assert r.status_code == 201 and r.json()["id"] == doc["id"]


def test_duplicate_of_foreign_document_does_not_leak(admin, sam):
    """Lädt jemand dieselbe Datei hoch, die ein anderer (ohne Freigabe) hat, darf er weder
    dessen Dokument übernehmen noch etwas darüber erfahren."""
    pdf = make_pdf("gleiche Datei")
    doc = admin.upload("Notizen von Alex", title="Alex", tags="Privat", content=pdf).json()
    admin.delete(f"/documents/{doc['id']}")          # liegt im Papierkorb
    r = sam.upload(title="Sam", content=pdf)
    assert r.status_code == 201, r.text
    assert r.json()["id"] != doc["id"]
    assert r.json()["tags"] == []
    # Alex' Dokument gehört weiter ihm
    assert admin.get(f"/documents/{doc['id']}").status_code == 200
    # Auch ohne Papierkorb: kein 409 mit fremder ID
    pdf2 = make_pdf("zweite Datei")
    admin.upload(title="S2", content=pdf2)
    r = sam.upload(title="J2", content=pdf2)
    assert r.status_code == 201, r.text


def test_merge_requires_same_owner_and_edit(admin, sam):
    a = admin.upload("Seite eins", "S1").json()
    b = admin.upload("Seite zwei", "S2").json()
    c = sam.upload("Fremd", "F").json()
    assert sam.post("/documents/merge", json={"document_ids": [a["id"], b["id"]]}).status_code == 404
    assert admin.post("/documents/merge", json={"document_ids": [a["id"], c["id"]]}).status_code == 404
    r = admin.post("/documents/merge", json={"document_ids": [a["id"], b["id"]], "title": "Beide"})
    assert r.status_code == 201
    assert r.json()["title"] == "Beide"
    assert admin.get(f"/documents/{a['id']}").json()["deleted_at"] is not None


def test_list_limit_bounds(admin):
    assert admin.get("/documents", params={"limit": 0}).status_code == 422
    assert admin.get("/documents", params={"limit": 2001}).status_code == 422


def test_user_delete_removes_their_documents(admin, sam):
    doc = sam.upload("Weg damit", "Weg").json()
    assert admin.delete(f"/users/{sam.id}").status_code == 204
    assert admin.get(f"/documents/{doc['id']}").status_code == 404
