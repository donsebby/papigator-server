"""Fristen für die Familie: Zuständige (nur wer das Dokument sieht, keine fremden Namen),
wiederkehrende Fristen und der Kalender-Eintrag (.ics)."""
import datetime as dt

from app import due as duemod


def _doc_with_due(api, title="Rechnung", days=10, **extra):
    doc = api.upload("Zahlungsziel", title).json()
    soon = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    r = api.patch(f"/documents/{doc['id']}", json={"due_date": soon, "due_note": "Zahlen", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_assignee_needs_visibility_and_leaks_no_names(admin, sam, make_user):
    jo = make_user("jo")
    doc = _doc_with_due(admin)
    assert doc["due_assignee"] is None and doc["due_assignee_name"] is None
    # Sam sieht das Dokument (noch) nicht → nicht zuteilbar, keine Liste
    assert admin.patch(f"/documents/{doc['id']}", json={"due_assignee": sam.id}).status_code == 400
    assert admin.patch(f"/documents/{doc['id']}", json={"due_assignee": 9999}).status_code == 400
    assert [u["id"] for u in admin.get(f"/documents/{doc['id']}/assignees").json()] == [admin.id]
    # Ohne Freigabe: weder ändern noch Kandidaten sehen
    assert sam.patch(f"/documents/{doc['id']}", json={"due_assignee": sam.id}).status_code == 404
    assert sam.get(f"/documents/{doc['id']}/assignees").status_code == 404
    assert sam.get(f"/documents/{doc['id']}/due.ics").status_code == 404

    # Einseitige Freigabe Alex → Sam: Alex darf Sam eintragen, Sam sieht seinen Namen
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    ids = {u["id"] for u in admin.get(f"/documents/{doc['id']}/assignees").json()}
    assert ids == {admin.id, sam.id}
    r = admin.patch(f"/documents/{doc['id']}", json={"due_assignee": sam.id}).json()
    assert r["due_assignee"] == sam.id and r["due_assignee_name"] == "sam"
    assert sam.get(f"/documents/{doc['id']}").json()["due_assignee_name"] == "sam"

    # Alex gibt auch Jo frei: Jo sieht das Dokument, kennt aber Sam nicht → kein Name, nicht in der Liste
    admin.put("/shares", json={"user_id": jo.id, "can_edit": True})
    body = jo.get(f"/documents/{doc['id']}").text
    assert '"due_assignee_name":null' in body.replace(" ", "") and '"sam"' not in body
    jo_list = jo.get(f"/documents/{doc['id']}/assignees")
    assert {u["id"] for u in jo_list.json()} == {admin.id, jo.id} and "sam" not in jo_list.text
    assert jo.patch(f"/documents/{doc['id']}", json={"due_assignee": sam.id}).status_code == 400
    assert "sam" not in jo.get("/documents", params={"due": True, "scope": "all"}).text

    # Nur Admins kennen alle: Sam darf Jo nicht eintragen, obwohl Jo das Dokument sieht
    assert sam.patch(f"/documents/{doc['id']}", json={"due_assignee": jo.id}).status_code == 400
    # Zuständige löschen
    r = admin.patch(f"/documents/{doc['id']}", json={"due_assignee": None}).json()
    assert r["due_assignee"] is None


def test_mine_due_and_revoked_share(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    mine = _doc_with_due(admin, "Für Alex")
    theirs = _doc_with_due(admin, "Für Sam")
    nobody = _doc_with_due(admin, "Für alle")
    admin.patch(f"/documents/{mine['id']}", json={"due_assignee": admin.id})
    admin.patch(f"/documents/{theirs['id']}", json={"due_assignee": sam.id})

    def titles(api):
        return {d["title"] for d in api.get("/documents", params={"due": True, "mine_due": True, "scope": "all"}).json()}

    assert titles(admin) == {"Für Alex", "Für alle"}
    assert titles(sam) == {"Für Sam", "Für alle"}
    # ohne mine_due: alle (ältere App-Fassungen)
    assert len(admin.get("/documents", params={"due": True}).json()) == 3

    # Freigabe entzogen → Sam sieht das Dokument nicht mehr; die Frist darf nicht verloren gehen
    assert admin.delete(f"/shares/{sam.id}").status_code == 204
    assert titles(admin) == {"Für Alex", "Für Sam", "Für alle"}
    d = admin.get(f"/documents/{theirs['id']}").json()
    assert d["due_assignee"] is None and d["due_assignee_name"] is None
    assert nobody["id"] in {x["id"] for x in admin.get("/documents", params={"due": True, "mine_due": True}).json()}


def test_repeat_advances_on_done(admin):
    doc = _doc_with_due(admin, "Kfz-Steuer", days=3)
    assert admin.patch(f"/documents/{doc['id']}", json={"due_repeat": "weekly"}).status_code == 400
    r = admin.patch(f"/documents/{doc['id']}", json={"due_repeat": "yearly"}).json()
    assert r["due_repeat"] == "yearly"
    first = dt.date.fromisoformat(r["due_date"])
    r = admin.patch(f"/documents/{doc['id']}", json={"due_done": True}).json()
    assert r["due_done"] == 0
    assert r["due_date"] == duemod.add_months(first, 12).isoformat()
    assert r["due_last_done"] == dt.date.today().isoformat()
    assert r["due_note"] == "Zahlen"
    # bleibt in der Fristen-Liste
    assert doc["id"] in [d["id"] for d in admin.get("/documents", params={"due": True}).json()]

    # Web schickt Datum und Erledigt zusammen: vom mitgeschickten Datum aus weiterrechnen
    q = admin.patch(f"/documents/{doc['id']}", json={"due_date": "2026-12-31", "due_repeat": "quarterly", "due_done": True}).json()
    assert q["due_date"] == "2027-03-31" and q["due_done"] == 0
    # überfällig: ein Intervall weiter, bleibt (noch) überfällig in der Liste
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    o = admin.patch(f"/documents/{doc['id']}", json={"due_date": old, "due_repeat": "monthly", "due_done": True}).json()
    assert o["due_date"] == duemod.add_months(dt.date.fromisoformat(old), 1).isoformat() and o["due_done"] == 0

    # einmalige Frist bleibt erledigt
    once = _doc_with_due(admin, "Einmalig")
    assert admin.patch(f"/documents/{once['id']}", json={"due_done": True}).json()["due_done"] == 1

    # Frist entfernen räumt Wiederholung und Zuständige mit weg
    r = admin.patch(f"/documents/{doc['id']}", json={"due_date": ""}).json()
    assert r["due_repeat"] is None and r["due_assignee"] is None and r["due_last_done"] is None


def test_next_due_dates():
    assert duemod.add_months(dt.date(2027, 1, 31), 1) == dt.date(2027, 2, 28)
    assert duemod.add_months(dt.date(2027, 11, 30), 3) == dt.date(2028, 2, 29)
    assert duemod.next_due("2026-11-20", "monthly") == "2026-12-20"
    assert duemod.next_due("2026-11-30", "quarterly") == "2027-02-28"
    # lange überfällig: genau ein Intervall weiter, verpasste Termine bleiben sichtbar
    assert duemod.next_due("2026-01-31", "monthly") == "2026-02-28"
    assert duemod.next_due("2024-03-01", "yearly") == "2025-03-01"


def test_due_ics(admin):
    doc = admin.upload("x", "Kfz-Steuer").json()
    assert admin.get(f"/documents/{doc['id']}/due.ics").status_code == 404     # keine Frist
    admin.patch(f"/documents/{doc['id']}", json={"due_date": "2027-03-01", "due_note": "Zahlen; bitte, sofort",
                                                 "sender": "Hauptzollamt", "due_repeat": "yearly"})
    r = admin.get(f"/documents/{doc['id']}/due.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20270301\r\n" in body and "DTEND;VALUE=DATE:20270302\r\n" in body
    assert "SUMMARY:Frist: Kfz-Steuer\r\n" in body
    assert "DESCRIPTION:Zahlen\\; bitte\\, sofort\\nHauptzollamt\r\n" in body
    assert "TRIGGER:-P3D" in body and "BEGIN:VALARM" in body and "RRULE:FREQ=YEARLY" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    long = duemod.ics({"id": 1, "title": "Ä" * 100, "due_date": "2027-01-01", "due_note": None, "sender": None,
                       "due_repeat": None})
    assert all(len(line.encode()) <= 75 for line in long.split("\r\n"))
    assert "".join(x[1:] if x.startswith(" ") else x for x in long.split("\r\n")).count("Ä") == 200  # Titel + Alarm
