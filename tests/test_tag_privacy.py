"""Verborgene Ordner- und Personennamen (27.11.2026): eng gefasste Korrekturen nach der Server-Prüfung, dazu
festgehalten, was sichtbar bleiben muss (alte Ordner ohne Ersteller, leere Admin-Ordner, Partner-Ordner, eigene)."""
import pytest

from app import processing, worker
from app.db import connect


def _names(api):
    return {t["name"] for t in api.get("/tags").json()}


@pytest.fixture
def secret(sam):
    """Sams privater Ordner „Scheidung“ an einem eigenen Dokument – kim sieht Sams Bestand nicht."""
    f = sam.post("/tags", json={"name": "Scheidung"}).json()
    sam.upload(text="Termin beim Anwalt", title="Anwalt", tags="Scheidung")
    return f


def test_no_planted_child_under_hidden_folder(make_user, secret):
    kim = make_user("kim")
    assert "Scheidung" not in _names(kim)
    assert kim.post("/tags", json={"name": "pX", "parent_id": secret["id"]}).status_code == 404
    with connect() as con:   # schon vorhandener leerer Unterordner (älterer Stand) verrät ihn auch nicht
        con.execute("INSERT INTO tags(name, parent_id, created_by) VALUES ('alt', ?, ?)", (secret["id"], kim.id))
    assert "Scheidung" not in _names(kim)


def test_existing_hidden_name_tells_nothing(make_user, secret):
    kim = make_user("kim")
    with connect() as con:   # Ordner liegt in Sams „Privat“ und hat eine Farbe
        priv = con.execute("SELECT id FROM tags WHERE name='Privat'").fetchone()
        pid = priv[0] if priv else con.execute("INSERT INTO tags(name, created_by) VALUES ('Privat', 2)").lastrowid
        con.execute("UPDATE tags SET parent_id=?, color='#aa0000' WHERE id=?", (pid, secret["id"]))
    r = kim.post("/tags", json={"name": "Scheidung"}).json()
    assert r["parent_id"] is None and r["color"] is None and r["kind"] is None and r["created_by"] is None


def test_what_stays_visible(admin, sam, make_user, secret):
    kim = make_user("kim")
    with connect() as con:   # alter Ordner ohne Ersteller, nur als Oberordner benutzt
        haus = con.execute("INSERT INTO tags(name) VALUES ('Haus')").lastrowid
        con.execute("INSERT INTO tags(name, parent_id) VALUES ('Strom', ?)", (haus,))
    kim.upload(text="Abschlag", title="Strom", tags="Strom")
    assert {"Haus", "Strom"} <= _names(kim)
    admin.post("/tags", json={"name": "Archiv"})                 # leerer Admin-Ordner
    assert "Archiv" in _names(sam)
    kim.post("/tags", json={"name": "Meins"})                    # eigener leerer Ordner
    assert "Meins" in _names(kim)
    sam.put("/shares", json={"user_id": kim.id, "can_edit": False})   # Partner-Ordner, sobald geteilt
    assert "Scheidung" in _names(kim)


def test_person_rules_need_the_owner_to_see_the_creator(sam, make_user):
    kim = make_user("kim")
    sam.post("/persons", json={"name": "Mia"})
    kim.put("/shares", json={"user_id": sam.id, "can_edit": True})   # ungefragt Schreibrecht verschenkt
    d = kim.upload(text="Mia war krank", title="Attest").json()["id"]
    with connect() as con:
        processing.apply_rules(con, d, "Mia war krank")
    assert "Mia" not in {t["name"] for t in kim.get(f"/documents/{d}").json()["tags"]}
    sam.put("/shares", json={"user_id": kim.id, "can_edit": False})   # kim sieht Sams Bestand: Person ist bekannt
    with connect() as con:
        processing.apply_rules(con, d, "Mia war krank")
    assert "Mia" in {t["name"] for t in kim.get(f"/documents/{d}").json()["tags"]}


def test_ai_gets_only_tags_the_owner_may_know(sam, make_user, secret):
    kim = make_user("kim")
    sam.post("/persons", json={"name": "Mia"})
    d = kim.upload(text="Brief", title="Brief").json()["id"]
    with connect() as con:
        names = worker.tag_names_for(con, d)
    assert "Scheidung" not in names and "Mia" not in names


def test_empty_foreign_tag_stays_hidden(admin, sam, make_user):
    """Letztes Dokument endgültig gelöscht: Sams leerer Tag „Psychotherapie“ taucht nicht bei anderen auf (27.11.2026)."""
    kim = make_user("kim")
    d = sam.upload(text="Termin", title="Sitzung", tags="Psychotherapie").json()["id"]
    assert sam.delete(f"/documents/{d}", params={"permanent": True}).status_code == 204
    assert "Psychotherapie" in _names(sam)            # Sam selbst sieht ihn weiter
    assert "Psychotherapie" not in _names(kim)


def test_guessed_child_does_not_reveal_parent(sam, make_user):
    """kim errät Sams Unterordner „Anwalt“ (liegt in „Scheidung“) und hängt ihn an: „Scheidung“ bleibt verborgen (27.11.2026)."""
    kim = make_user("kim")
    with connect() as con:
        sid = con.execute("INSERT INTO tags(name, created_by) VALUES ('Scheidung', ?)", (sam.id,)).lastrowid
        con.execute("INSERT INTO tags(name, parent_id, created_by) VALUES ('Anwalt', ?, ?)", (sid, sam.id))
    sam.upload(text="Termin", title="Anwalt", tags="Anwalt")
    d = kim.upload(text="Brief", title="Brief", tags="Anwalt").json()["id"]
    tags = {t["name"]: t for t in kim.get("/tags").json()}
    assert "Scheidung" not in tags and tags["Anwalt"]["parent_id"] is None
    assert "Scheidung" not in {t["name"] for t in kim.get(f"/documents/{d}").json()["tags"]}
    assert "Scheidung" not in str(kim.get("/documents").json())
