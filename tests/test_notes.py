"""Notizen am Dokument: sehen und schreiben, wer das Dokument sieht; löschen nur eigene oder mit Bearbeitungsrecht;
fremde Namen bleiben verborgen; weg mit dem Dokument."""


def test_notes_rights_and_names(admin, sam, make_user):
    jo = make_user("jo")
    d = admin.upload("Versicherung Schaden", "Schadensmeldung").json()
    url = f"/documents/{d['id']}/notes"
    r = admin.post(url, json={"text": "  Mit Versicherung telefoniert, Rückruf abwarten  "})
    assert r.status_code == 201 and r.json()["text"] == "Mit Versicherung telefoniert, Rückruf abwarten" and r.json()["mine"]
    assert admin.post(url, json={"text": "   "}).status_code == 400
    assert admin.post(url, json={"text": "x" * 2001}).status_code == 422
    # ohne Freigabe: nichts
    assert sam.get(url).status_code == 404 and sam.post(url, json={"text": "hallo"}).status_code == 404
    # nur lesen: sehen und selbst notieren ja, fremde Notiz löschen nein
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    notes = sam.get(url).json()
    assert len(notes) == 1 and notes[0]["author"] == "Alex" and not notes[0]["mine"]
    mine = sam.post(url, json={"text": "Rückruf kam, Gutachter am Montag"}).json()
    assert sam.delete(f"{url}/{notes[0]['id']}").status_code == 403
    # Jo sieht das Dokument, kennt Sam aber nicht → kein Name
    admin.put("/shares", json={"user_id": jo.id, "can_edit": True})
    by_jo = {n["id"]: n for n in jo.get(url).json()}
    assert by_jo[mine["id"]]["author"] is None and by_jo[notes[0]["id"]]["author"] == "Alex"
    # mit Bearbeitungsrecht darf Jo jede Notiz löschen; Sam die eigene
    assert jo.delete(f"{url}/{notes[0]['id']}").status_code == 204
    assert sam.delete(f"{url}/{mine['id']}").status_code == 204
    assert admin.get(url).json() == []


def test_notes_go_with_the_document(admin):
    from app.db import connect
    d = admin.upload("Weg damit", "Weg").json()
    admin.post(f"/documents/{d['id']}/notes", json={"text": "Notiz"})
    admin.delete(f"/documents/{d['id']}")                       # Papierkorb: Notizen bleiben
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM document_notes").fetchone()[0] == 1
    admin.delete("/trash")                                       # endgültig: Notizen mit weg
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM document_notes").fetchone()[0] == 0
