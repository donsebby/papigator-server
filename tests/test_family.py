"""Familie: Personen ohne Konto, Aufbewahren & aufräumen, Notfallmappe."""
from urllib.parse import urlparse

from app import cleanup, processing
from app.db import connect


def _tags(api, doc_id):
    return {t["name"] for t in api.get(f"/documents/{doc_id}").json()["tags"]}


def _worker_rules(doc_id, text):
    """Wie der Worker nach der Texterkennung eines neuen Dokuments."""
    with connect() as con:
        processing.apply_rules(con, doc_id, text)


# ---------- Personen ----------

def test_person_in_one_step_whole_words_only(admin, sam):
    a = admin.upload("Zeugnis von Mia Muster, Klasse 3", "Zeugnis").json()
    b = admin.upload("Mias Impfpass", "Impfpass").json()
    c = admin.upload("Urlaub in Miami", "Urlaub").json()
    theirs = sam.upload("Mia war heute da", "Sam Notiz").json()      # Sam teilt nicht mit Alex
    r = admin.post("/persons", json={"name": " Mia "})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["name"] == "Mia" and p["tagged"] == 2 and p["auto"] and p["can_remove"]
    assert "Mia" in _tags(admin, a["id"]) and "Mia" in _tags(admin, b["id"])
    assert "Mia" not in _tags(admin, c["id"])                       # „Miami“ ist kein ganzes Wort
    assert "Mia" not in _tags(sam, theirs["id"])                     # fremder Bestand bleibt unberührt
    # Dokumente tragen die Art des Tags, damit die Apps Personen eigens zeigen
    assert {t["name"]: t["kind"] for t in admin.get(f"/documents/{a['id']}").json()["tags"]}["Mia"] == "person"
    # Neue Dokumente: die Regel wirkt nur im Bestand der Anlegenden
    new_own = admin.upload("Elternbrief für Mia", "Elternbrief").json()
    new_sam = sam.upload("Mia Rechnung", "Sam Rechnung").json()
    _worker_rules(new_own["id"], "Elternbrief für Mia")
    _worker_rules(new_sam["id"], "Mia Rechnung")
    assert "Mia" in _tags(admin, new_own["id"]) and "Mia" not in _tags(sam, new_sam["id"])
    # Auch „Auf vorhandene anwenden“ (Admin) trägt die Person nicht in fremde Bestände
    admin.post("/rules/apply")
    assert "Mia" not in _tags(sam, theirs["id"])


def test_person_names_stay_private(admin, sam):
    admin.post("/persons", json={"name": "Lotta"})                   # noch ohne Dokument
    assert [p["name"] for p in admin.get("/persons").json()] == ["Lotta"]
    assert sam.get("/persons").json() == []
    assert "Lotta" not in {t["name"] for t in sam.get("/tags", params={"scope": "all"}).json()}
    assert not any(c["kind"] == "person" for c in sam.get("/search", params={"q": "Lotta Zeugnis"}).json()["chips"])
    # Admin sieht in den Regeln keine Personen-Regeln von Leuten, deren Bestand er nicht sieht
    sam.post("/persons", json={"name": "Jonas"})
    assert "Jonas" not in {r["tag"] for r in admin.get("/rules").json()}
    # Mit Freigabe sieht Sam die Person – entfernen darf er sie nur mit Schreibrecht
    sam.put("/shares", json={"user_id": admin.id, "can_edit": False})
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    lotta = next(p for p in sam.get("/persons").json() if p["name"] == "Lotta")
    assert not lotta["can_remove"]
    assert sam.delete(f"/persons/{lotta['id']}").status_code == 403


def test_person_in_search_and_removal_keeps_folder(admin, sam):
    doc = admin.upload("Zeugnis Paul, Schuljahr 2025", "Zeugnis Paul").json()
    pid = admin.post("/persons", json={"name": "Paul"}).json()["id"]
    r = admin.get("/search", params={"q": "Zeugnis von Paul"}).json()
    person = [c for c in r["chips"] if c["kind"] == "person"]
    assert person and person[0]["label"] == "Paul" and "Paul" in r["params"]["q"].split()
    assert [d["id"] for d in r["documents"]] == [doc["id"]]
    # Fremde können eine Person nicht entfernen (und erfahren nicht, dass es sie gibt)
    assert sam.delete(f"/persons/{pid}").status_code == 404
    assert admin.delete(f"/persons/{pid}").status_code == 204
    assert admin.get("/persons").json() == []
    assert "Paul" in _tags(admin, doc["id"])                         # Ordner und Dokumente bleiben
    assert not any(r["tag"] == "Paul" for r in admin.get("/rules").json())
    later = admin.upload("Paul hat Geburtstag", "Einladung").json()
    _worker_rules(later["id"], "Paul hat Geburtstag")
    assert "Paul" not in _tags(admin, later["id"])
    assert admin.get(f"/documents/{doc['id']}").json()["tags"][0]["kind"] is None


def test_person_name_checks_and_shared_names(admin, sam):
    assert admin.post("/persons", json={"name": "A"}).status_code == 400
    assert admin.post("/persons", json={"name": "Mia, Ben"}).status_code == 400
    short = admin.post("/persons", json={"name": "Jo"}).json()
    assert short["auto"] is False and short["tagged"] == 0           # zu kurz für eine Regel – nur Ordner
    # Zwei Nutzer ohne Freigabe legen dieselbe Person an: beide sehen nur ihre eigene
    admin.post("/persons", json={"name": "Emma"})
    r = sam.post("/persons", json={"name": "Emma"})
    assert r.status_code == 201 and r.json()["can_remove"]
    assert sam.delete(f"/persons/{r.json()['id']}").status_code == 204
    assert "Emma" in {p["name"] for p in admin.get("/persons").json()}
    assert sam.get("/persons").json() == []


# ---------- Aufräumen ----------

def _old(api, text, title, tags, date="2015-06-01"):
    doc = api.upload(text, title, tags=tags).json()
    api.patch(f"/documents/{doc['id']}", json={"document_date": date})
    return doc["id"]


def test_retention_defaults():
    assert cleanup.default_rule("Rechnungen")[2] == 3
    assert cleanup.default_rule("Stromrechnung")[2] == 3
    assert cleanup.default_rule("Handwerkerrechnungen")[2] == 5   # Gewährleistung am Bau
    assert cleanup.default_rule("Kontoauszüge")[2] == 3
    assert cleanup.default_rule("Steuerbescheide")[2] == 10
    for never in ("Gehaltsabrechnung", "Nebenkostenabrechnung", "Lohnsteuerbescheinigung", "Versicherungen",
                  "Mietvertrag", "Zeugnisse", "Geburtsurkunde", "Gesundheit", "Notfallmappe", "Arztrechnungen",
                  "Steuerberechnung", "Berechnung", "Verrechnung", "Unterhaltsberechnung"):
        assert cleanup.default_rule(never) == ("never",), never
    assert cleanup.default_rule("Urlaub") is None and cleanup.default_rule("Steuerberater") is None


def test_cleanup_suggestions(admin, sam, make_user):
    old_bill = _old(admin, "Rechnung Elektromarkt", "Toaster", "Rechnungen")
    old_salary = _old(admin, "Gehalt Juni", "Gehalt", "Rechnungen,Gehalt")           # „nie“ schlägt alles
    young_tax = _old(admin, "Bescheid", "Steuerbescheid 2020", "Steuer", "2020-05-01")  # 10 Jahre: noch nicht
    old_tax = _old(admin, "Bescheid", "Steuerbescheid 2012", "Steuer", "2012-05-01")
    recent = _old(admin, "Rechnung", "Neue Rechnung", "Rechnungen", "2026-01-10")
    no_rule = _old(admin, "Foto", "Urlaub", "Urlaub")
    open_due = _old(admin, "Mahnung", "Alte Mahnung", "Rechnungen")
    admin.patch(f"/documents/{open_due}", json={"due_date": "2030-01-01"})
    in_trash = _old(admin, "Bon", "Bon", "Rechnungen")
    admin.delete(f"/documents/{in_trash}")
    # Unterordner: „Strom“ liegt in „Rechnungen“ – zählt wie Rechnungen
    folder = {t["name"]: t["id"] for t in admin.get("/tags", params={"scope": "all"}).json()}["Rechnungen"]
    strom = admin.post("/tags", json={"name": "Strom", "parent_id": folder}).json()["id"]
    in_sub = _old(admin, "Strom 2014", "Strom", "Strom", "2014-01-01")
    theirs = _old(sam, "Rechnung", "Sams Rechnung", "Rechnungen")                     # nicht freigegeben

    r = admin.get("/cleanup").json()
    ids = {d["id"] for g in r["groups"] for d in g["documents"]}
    assert ids == {old_bill, old_tax, in_sub}, r
    assert not ids & {old_salary, young_tax, recent, no_rule, open_due, in_trash, theirs}
    groups = {g["label"]: g for g in r["groups"]}
    assert groups["Rechnungen & Quittungen"]["years"] == 3 and "BGB" in groups["Rechnungen & Quittungen"]["basis"]
    assert groups["Steuer"]["documents"][0]["keep_until"] == "2022-12-31"
    assert r["count"] == 3
    # Sam sieht nur Eigenes; mit Leserecht auf Alex kommen dessen Dokumente nicht dazu (nur bearbeitbare)
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert {d["id"] for g in sam.get("/cleanup").json()["groups"] for d in g["documents"]} == {theirs}

    # Admin stellt pro Ordner ein: „Strom“ nie vorschlagen, „Steuer“ 15 Jahre
    assert admin.put("/cleanup/rules", json={"tag": "Strom", "years": None}).status_code == 200
    admin.put("/cleanup/rules", json={"tag": "Steuer", "years": 15})
    assert admin.put("/cleanup/rules", json={"tag": "Steuer", "years": 0}).status_code == 400
    ids = {d["id"] for g in admin.get("/cleanup").json()["groups"] for d in g["documents"]}
    assert ids == {old_bill}
    rules = admin.get("/cleanup/rules").json()
    assert {c["tag"]: c["years"] for c in rules["custom"]} == {"Strom": None, "Steuer": 15}
    assert any(d["key"] == "never" for d in rules["defaults"])
    admin.delete(f"/cleanup/rules/{strom}")
    assert in_sub in {d["id"] for g in admin.get("/cleanup").json()["groups"] for d in g["documents"]}
    # Die Notfallmappe bleibt, auch umbenannt und mit Rechnung darin
    admin.post("/emergency/documents", json={"document_id": in_sub})
    tid = admin.get("/emergency").json()["tag_id"]
    admin.patch(f"/tags/{tid}", json={"name": "Wichtig"})
    assert in_sub not in {d["id"] for g in admin.get("/cleanup").json()["groups"] for d in g["documents"]}
    # Aufräumen = normaler Papierkorb
    admin.delete(f"/documents/{old_bill}")
    assert old_bill not in {d["id"] for g in admin.get("/cleanup").json()["groups"] for d in g["documents"]}


# ---------- Notfallmappe ----------

def test_emergency_folder_and_link(client, admin, sam, make_user):
    empty = admin.get("/emergency").json()
    assert empty["tag_id"] is None and empty["documents"] == []
    mine = admin.upload("Vorsorgevollmacht", "Vollmacht").json()
    partner = sam.upload("Patientenverfügung Sam", "Patientenverfügung").json()
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    r = admin.post("/emergency/documents", json={"document_id": mine["id"]})
    assert r.status_code == 200
    tag_id = r.json()["tag_id"]
    admin.post("/emergency/documents", json={"document_id": partner["id"]})   # Partner mit Schreibrecht: erlaubt
    e = admin.get("/emergency").json()
    assert e["tag_id"] == tag_id and e["name"] == "Notfallmappe"
    assert {d["id"] for d in e["documents"]} == {mine["id"], partner["id"]} and e["own_count"] == 1
    # Link über den normalen Freigabe-Endpunkt: nur eigene Dokumente
    link = admin.post("/links", json={"kind": "tag", "target": tag_id, "expires_days": 365}).json()
    assert link["documents"] == 1 and link["expires_at"]
    assert [l["id"] for l in admin.get("/emergency").json()["links"]] == [link["id"]]
    client.cookies.clear()
    page = client.get(urlparse(link["url"]).path)
    assert "Vorsorgevollmacht" in page.text or "Vollmacht" in page.text
    assert "Patientenverfügung" not in page.text
    # Fremde Dokumente kann man nicht hineinlegen
    kim = make_user("kim")
    other = kim.upload("Kims Testament", "Testament").json()
    assert admin.post("/emergency/documents", json={"document_id": other["id"]}).status_code == 404
    # Kim (ohne Freigabe) sieht die Mappe leer und teilt erst, wenn eigene Dokumente drin sind
    k = kim.get("/emergency").json()
    assert k["documents"] == [] and k["own_count"] == 0
    kim.post("/emergency/documents", json={"document_id": other["id"]})
    k = kim.get("/emergency").json()
    assert [d["id"] for d in k["documents"]] == [other["id"]] and k["own_count"] == 1
    assert kim.post("/links", json={"kind": "tag", "target": k["tag_id"], "expires_days": 30}).json()["documents"] == 1
    # Herausnehmen lässt das Dokument stehen
    assert admin.delete(f"/emergency/documents/{mine['id']}").status_code == 204
    assert admin.get(f"/documents/{mine['id']}").status_code == 200
    assert mine["id"] not in {d["id"] for d in admin.get("/emergency").json()["documents"]}
    # Der Name ist reserviert
    assert admin.post("/persons", json={"name": "Notfallmappe"}).status_code == 400
