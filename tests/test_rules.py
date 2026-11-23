"""Schlagwort-Regeln: Trefferzahl in der Liste und Vorschau vor dem Anlegen."""


def test_rule_counts_and_preview(admin, sam):
    admin.upload("Gehaltsabrechnung, Lohnsteuer 312,00", "Gehalt Mai")
    admin.upload("Finanzamt, Steuerbescheid 2025", "Bescheid")
    admin.upload("Telekom Rechnung", "Telekom")
    sam.upload("Sams Lohnsteuerbescheinigung", "Sam privat")   # nicht freigegeben: zählt für Alex nicht

    p = admin.get("/rules/preview", params={"pattern": "steuer"}).json()
    assert p["matches"] == 2 and set(p["examples"]) == {"Gehalt Mai", "Bescheid"}
    assert admin.get("/rules/preview", params={"pattern": "ab"}).json() == {"matches": 0, "examples": []}

    admin.post("/rules", json={"pattern": "Steuer", "tag": "Steuer"})
    admin.post("/rules", json={"pattern": "Vodafone", "tag": "Handy"})
    assert {r["pattern"]: r["matches"] for r in admin.get("/rules").json()} == {"Steuer": 2, "Vodafone": 0}


def test_tags_of_other_users_stay_hidden(admin, sam, make_user):
    kim = make_user("kim")
    kim.upload("Befund", "Befund", tags="Psychotherapie")
    admin.upload("Rechnung", "Rechnung", tags="Haushalt")
    admin.post("/tags", json={"name": "Leerer Ordner"})
    for api in (admin, sam):
        names = {t["name"] for t in api.get("/tags", params={"scope": "all"}).json()}
        assert "Psychotherapie" not in names and "Leerer Ordner" in names
    assert "Haushalt" not in {t["name"] for t in sam.get("/tags").json()}
    assert "Psychotherapie" in {t["name"] for t in kim.get("/tags").json()}
    # Teilt Kim mit Alex, sieht Alex den Tag
    admin_id = admin.get("/auth/me").json()["id"]
    kim.put("/shares", json={"user_id": admin_id, "can_edit": False})
    assert "Psychotherapie" in {t["name"] for t in admin.get("/tags", params={"scope": "all"}).json()}
