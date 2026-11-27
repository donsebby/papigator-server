"""Export, Steuer-Paket, Absender, Verträge, Regeln – jeweils nur mit sichtbaren Dokumenten."""
import io
import json
import zipfile


def _zip(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_export_only_own_documents(admin, sam):
    admin.upload("a", "Alex A")
    sam.upload("b", "Sam B")
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    meta = json.loads(_zip(sam.get("/export.zip", params={"scope": "all"})).read("metadata.json"))
    assert [m["title"] for m in meta] == ["Sam B"]           # kein Admin: nur der eigene Pot
    # Admin: nur, was er sieht (27.11.2026) – Sams Bestand erst, wenn Sam teilt
    meta = json.loads(_zip(admin.get("/export.zip", params={"scope": "all"})).read("metadata.json"))
    assert sorted(m["title"] for m in meta) == ["Alex A"]
    sam.put("/shares", json={"user_id": admin.id, "can_edit": False})
    meta = json.loads(_zip(admin.get("/export.zip", params={"scope": "all"})).read("metadata.json"))
    assert sorted(m["title"] for m in meta) == ["Alex A", "Sam B"]


def test_tax_package_respects_visibility(admin, sam):
    d = admin.upload("Spende", "Spende Alex", tags="Steuer").json()
    admin.patch(f"/documents/{d['id']}", json={"document_date": "2025-05-01", "amount": 50, "sender": "Verein"})
    j = sam.upload("Spende", "Spende Sam", tags="Steuer").json()
    sam.patch(f"/documents/{j['id']}", json={"document_date": "2025-06-01", "amount": 20, "sender": "Verein"})
    z = _zip(sam.get("/export/package.zip", params={"year": 2025, "tags": "Steuer", "scope": "all"}))
    names = z.namelist()
    assert any("Spende Sam" in n for n in names)
    assert not any("Spende Alex" in n for n in names)
    assert sam.get("/export/package.zip", params={"year": 1999}).status_code == 404


def test_senders_and_years_respect_visibility(admin, sam):
    d = admin.upload("x", "X").json()
    admin.patch(f"/documents/{d['id']}", json={"sender": "Geheimbank", "document_date": "2020-01-01"})
    assert all(s.get("sender", s.get("name")) != "Geheimbank" for s in sam.get("/senders", params={"scope": "all"}).json())
    assert sam.get("/documents/years", params={"scope": "all"}).json() == []
    assert sam.get("/contracts", params={"scope": "all"}).json() == []


def test_rules_apply_tags_matching_documents(admin, sam):
    admin.upload("Ihre Telekom Rechnung", "Telefon")
    sam.upload("Telekom Vertrag von Sam", "SamTel")
    admin.post("/rules", json={"pattern": "Telekom", "tag": "Telefon"})
    r = admin.post("/rules/apply")
    assert r.json()["tagged"] == 1                    # nur, was der Admin bearbeiten darf
    tags = [t["name"] for t in admin.get("/documents").json()[0]["tags"]]
    assert tags == ["Telefon"]


def test_download_selection_as_zip(admin, sam):
    import io
    import zipfile
    a = admin.upload("Strom", "Stromrechnung").json()
    b = admin.upload("Gas", "Gasrechnung").json()
    r = admin.get("/export/selection.zip", params={"ids": f"{a['id']},{b['id']}"})
    assert r.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert len(names) == 2 and any("Stromrechnung" in n for n in names) and all(n.endswith(".pdf") for n in names)
    # Fremde Dokumente gibt es nicht – auch nicht versteckt in einer Auswahl
    assert sam.get("/export/selection.zip", params={"ids": f"{a['id']}"}).status_code == 404
    assert admin.get("/export/selection.zip", params={"ids": "x"}).status_code == 400


def _zip_names(resp):
    import io, zipfile
    assert resp.status_code == 200, resp.text
    return sorted(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())


def test_tag_export_builds_structure_from_other_tags(admin, sam):
    kfz = admin.post("/tags", json={"name": "KFZ"}).json()
    admin.post("/tags", json={"name": "Kombi", "parent_id": kfz["id"]})
    docs = [("Kaufvertrag", "Kombi,Vertrag", "2021-05-02"), ("Inspektion", "Kombi,Rechnung", "2024-05-02"),
            ("Reifen", "Kombi,Rechnung,Garantie", "2023-10-18"), ("HU-Bericht", "Kombi", "2025-03-20"),
            ("Stromrechnung", "Rechnung", "2024-01-01")]                          # gehört nicht zum Kombi
    for title, tags, d in docs:
        doc = admin.upload(title, title, tags=tags).json()
        admin.patch(f"/documents/{doc['id']}", json={"document_date": d, "amount": 100.5})
    sam.upload("x", "Sam Kombi", tags="Kombi")                                        # fremd, nicht sichtbar

    names = _zip_names(admin.get("/export/tag.zip", params={"tag": "kombi"}))
    assert names == sorted([
        "Kombi/Inhaltsverzeichnis.csv", "Kombi/Inhaltsverzeichnis.pdf",
        "Kombi/Garantie/2023-10-18 Reifen.pdf",                     # zwei andere Tags → in beiden Ordnern
        "Kombi/Rechnung/2023/2023-10-18 Reifen.pdf",                # „Rechnung“ hat zwei Jahre → Jahresordner
        "Kombi/Rechnung/2024/2024-05-02 Inspektion.pdf",
        "Kombi/Sonstiges/2025-03-20 HU-Bericht.pdf",                # kein anderer Tag
        "Kombi/Vertrag/2021-05-02 Kaufvertrag.pdf",                 # ein Jahr → kein Jahresordner
    ])
    # Oberordner: Unterordner bleiben als Ebene erhalten
    names = _zip_names(admin.get("/export/tag.zip", params={"tag": "KFZ"}))
    assert "KFZ/Kombi/Vertrag/2021-05-02 Kaufvertrag.pdf" in names
    assert not any("Sam" in n or "Stromrechnung" in n for n in names)


def test_tag_export_csv_and_rights(admin, sam):
    import io, zipfile
    doc = admin.upload("x", "=Formel", tags="Haus").json()
    admin.patch(f"/documents/{doc['id']}", json={"document_date": "2025-01-02", "amount": 12})
    z = zipfile.ZipFile(io.BytesIO(admin.get("/export/tag.zip", params={"tag": "Haus"}).content))
    csv_text = z.read("Haus/Inhaltsverzeichnis.csv").decode("utf-8-sig")
    assert "'=Formel" in csv_text and "12,00" in csv_text
    assert z.read("Haus/Inhaltsverzeichnis.pdf").startswith(b"%PDF")
    # Sam sieht Alex' Dokumente nicht, auch nicht über den gemeinsamen Tag-Namen
    assert sam.get("/export/tag.zip", params={"tag": "Haus"}).status_code == 404
    assert admin.get("/export/tag.zip", params={"tag": "gibtsnicht"}).status_code == 404
    # Mit Partner-Freigabe und scope=all sind die Dokumente für Sam drin
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert any(n.endswith("=Formel.pdf") or "Formel" in n for n in _zip_names(sam.get("/export/tag.zip", params={"tag": "Haus", "scope": "all"})))


def test_partner_moves_only_to_self(admin, sam, make_user):
    """Schreibrecht bei Anna und Carl: Annas Dokument darf Bert zu sich holen, aber nicht zu Carl schieben (27.11.2026)."""
    carl = make_user("carl")
    d = admin.upload("x", "Annas Brief").json()["id"]
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    carl.put("/shares", json={"user_id": sam.id, "can_edit": True})
    assert sam.post("/documents/owner", json={"document_ids": [d], "owner_id": carl.id}).status_code == 403
    assert admin.get(f"/documents/{d}").status_code == 200
    assert sam.post("/documents/owner", json={"document_ids": [d], "owner_id": sam.id}).status_code == 200
