"""Fundstücke aus dem QA-Durchgang (16.11.2026): Fristen, Datums- und Betragsprüfung, verborgene Ordner."""
import datetime as dt

from app import due as duemod


def _doc(api, title="Beleg"):
    return api.upload("Zahlungsziel Rechnung", title).json()


def test_new_due_date_reopens_done_deadline(admin):
    doc = _doc(admin)
    soon = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    later = (dt.date.today() + dt.timedelta(days=12)).isoformat()
    admin.patch(f"/documents/{doc['id']}", json={"due_date": soon, "due_done": True})
    # iOS schickt beim Ändern nur das Datum → die neue Frist ist offen
    r = admin.patch(f"/documents/{doc['id']}", json={"due_date": later}).json()
    assert r["due_done"] in (0, False) and r["due_date"] == later
    ids = [d["id"] for d in admin.get("/documents", params={"due": True}).json()]
    assert doc["id"] in ids
    # gleiches Datum noch einmal gesendet → Erledigt-Status bleibt, wie er ist
    admin.patch(f"/documents/{doc['id']}", json={"due_done": True})
    r = admin.patch(f"/documents/{doc['id']}", json={"due_date": later}).json()
    assert r["due_done"] in (1, True)


def test_invalid_dates_rejected(admin):
    doc = _doc(admin)
    for bad in ("garbage", "14.03.2025", "2025-02-30"):
        assert admin.patch(f"/documents/{doc['id']}", json={"document_date": bad}).status_code == 400, bad
    assert admin.patch(f"/documents/{doc['id']}", json={"due_date": "2026-02-30"}).status_code == 400
    # leer = kein Datum (NULL, nicht ""), damit „Zu prüfen“ es findet
    r = admin.patch(f"/documents/{doc['id']}", json={"document_date": ""}).json()
    assert r["document_date"] is None
    review = [d["id"] for d in admin.get("/documents", params={"review": True}).json()]
    assert doc["id"] in review
    assert admin.get("/documents/years").status_code == 200


def test_amount_validation(admin):
    doc = _doc(admin)
    url = f"/documents/{doc['id']}"
    for bad in ('{"amount": 1e999}', '{"amount": -12.5}', '{"amount": 1e308}'):
        r = admin.patch(url, content=bad, headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), bad
    assert admin.get(url).status_code == 200
    assert admin.patch(url, json={"amount": 12.345}).json()["amount"] in (12.34, 12.35)
    assert admin.patch(url, json={"direction": "out"}).status_code == 200
    r = admin.patch(url, json={"direction": None, "currency": ""})
    assert r.status_code == 200 and r.json()["direction"] is None and r.json()["currency"] is None


def test_repeat_keeps_month_end():
    assert duemod.next_due("2026-01-31", "monthly") == "2026-02-28"
    assert duemod.next_due("2026-02-28", "monthly", 31) == "2026-03-31"
    assert duemod.next_due("2027-02-28", "quarterly", 30) == "2027-05-30"


def test_repeat_month_end_via_api(admin):
    doc = _doc(admin)
    url = f"/documents/{doc['id']}"
    admin.patch(url, json={"due_date": "2026-01-31", "due_repeat": "monthly"})
    seen = [admin.patch(url, json={"due_done": True}).json()["due_date"] for _ in range(3)]
    assert seen == ["2026-02-28", "2026-03-31", "2026-04-30"]
    ics = admin.get(f"{url}/due.ics").text
    assert "BYMONTHDAY=28,29,30,31;BYSETPOS=-1" in ics
    # Datum von Hand neu gesetzt → neuer Wunschtag
    admin.patch(url, json={"due_date": "2026-06-15"})
    assert admin.patch(url, json={"due_done": True}).json()["due_date"] == "2026-07-15"


def test_hidden_folders_stay_hidden(admin, make_user):
    kim = make_user("kim")
    doc = _doc(admin, "Gehaltsabrechnung")
    admin.patch(f"/documents/{doc['id']}", json={"tags": ["Gehalt"]})
    gid = next(t["id"] for t in admin.get("/tags").json() if t["name"] == "Gehalt")
    # Person mit gleichem Namen macht Alex' Ordner nicht zur Person
    assert kim.post("/persons", json={"name": "Gehalt"}).status_code == 201
    assert kim.post("/persons", json={"name": "Gehalt"}).status_code == 201   # zweiter Aufruf genauso
    kinds = {t["name"]: t.get("kind") for t in admin.get("/tags").json()}
    assert kinds["Gehalt"] is None
    # eigener, unbenutzter Name wird Person
    assert kim.post("/persons", json={"name": "Lotta"}).status_code == 201
    assert {t["name"]: t.get("kind") for t in kim.get("/tags").json()}.get("Lotta") == "person"
    kim.delete(f"/persons/{gid}")
    # POST /tags verrät nicht, wem der Ordner gehört
    r = kim.post("/tags", json={"name": "Gehalt"}).json()
    assert r["created_by"] is None
    # Export: gleiche Meldung für „gibt es nicht“ und „fremd“
    a = kim.get("/export/tag.zip", params={"tag": "Gehalt"})
    b = kim.get("/export/tag.zip", params={"tag": "GibtEsNicht"})
    assert a.status_code == b.status_code == 404 and a.json() == b.json()


def test_owner_counts_only_visible(admin, sam, make_user):
    kim = make_user("kim")
    _doc(admin, "A1"); _doc(admin, "A2"); _doc(sam, "S1"); _doc(kim, "K1")
    assert admin.get("/documents/owner_counts").json() == [{"owner_id": admin.id, "count": 2}]
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    got = {r["owner_id"]: r["count"] for r in sam.get("/documents/owner_counts").json()}
    assert got == {admin.id: 2, sam.id: 1}
    assert kim.id not in got


def test_unchanged_odd_amount_still_saves(admin):
    """Ein von der Erkennung gespeicherter negativer Betrag sperrt das Speichern im Web nicht (es schickt ihn mit)."""
    from app.db import connect
    doc = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET amount=-12.5 WHERE id=?", (doc["id"],))
    r = admin.patch(f"/documents/{doc['id']}", json={"title": "Neu", "amount": -12.5})
    assert r.status_code == 200 and r.json()["title"] == "Neu"
    assert admin.patch(f"/documents/{doc['id']}", json={"amount": -13}).status_code == 400


def test_unknown_assignee_hides_id_and_is_kept(admin, sam, make_user):
    jo = make_user("jo")
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    admin.put("/shares", json={"user_id": jo.id, "can_edit": True})
    doc = _doc(admin)
    url = f"/documents/{doc['id']}"
    soon = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    admin.patch(url, json={"due_date": soon, "due_assignee": sam.id})
    seen = jo.get(url).json()   # Jo kennt Sam nicht: weder Name noch ID
    assert seen["due_assignee"] == duemod.UNKNOWN_ASSIGNEE and seen["due_assignee_name"] is None
    assert f'"due_assignee":{sam.id}' not in jo.get(url).text.replace(" ", "")
    # Jo speichert (Web schickt den Platzhalter mit) → Sam bleibt zuständig
    assert jo.patch(url, json={"title": "Neu", "due_assignee": duemod.UNKNOWN_ASSIGNEE}).status_code == 200
    assert admin.get(url).json()["due_assignee"] == sam.id


def test_revoked_share_clears_assignee(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    doc = _doc(admin)
    url = f"/documents/{doc['id']}"
    admin.patch(url, json={"due_date": (dt.date.today() + dt.timedelta(days=5)).isoformat(), "due_assignee": sam.id})
    admin.delete(f"/shares/{sam.id}")
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})   # neue Freigabe: alte Zuteilung lebt nicht auf
    assert admin.get(url).json()["due_assignee"] is None


def test_user_list_hides_logins_from_non_admins(admin, sam):
    rows = sam.get("/users").json()
    assert {r["display_name"] for r in rows} >= {"sam"} and all(r["username"] == "" and not r["is_admin"] for r in rows)
    assert any(r["username"] == "sam" for r in admin.get("/users").json())
    assert any(r["is_admin"] for r in admin.get("/users").json())
