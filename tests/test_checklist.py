"""Checkliste der Notfallmappe: was drin ist, Vorschläge nur aus dem eigenen Bestand, lieber übersehen als falsch abhaken."""
from app import checklist
from app.db import connect


def _doc(api, title, summary=None, kind=None, text=""):
    d = api.upload(text=text or title, title=title).json()
    with connect() as con:
        con.execute("UPDATE documents SET summary=?, doc_kind=?, status='done' WHERE id=?", (summary, kind, d["id"]))
    return d["id"]


def _items(api, lang="de"):
    r = api.get(f"/emergency/checklist?lang={lang}")
    assert r.status_code == 200, r.text
    return r.json(), {i["key"]: i for g in r.json()["groups"] for i in g["items"]}


def test_matching_words():
    assert checklist.matches(["haftpflichtversicherung"], "Privathaftpflichtversicherung 2026")
    assert checklist.matches(["riester"], "Riester-Rente Jahresmitteilung")
    assert checklist.matches(["zulassungsbescheinigung teil ii"], "Zulassungsbescheinigung Teil II VW Golf")
    assert not checklist.matches(["zulassungsbescheinigung teil ii"], "Zulassungsbescheinigung Teil I")
    assert not checklist.matches(["testament"], "Testamentsvollstreckung Info")      # nur ganzes Wort oder Wortende
    assert not checklist.matches(["vorsorgevollmacht"], "Vollmacht für das Girokonto")


def test_empty_list_and_languages(admin):
    data, items = _items(admin)
    assert data["present"] == 0 and data["total"] == len(items) > 15
    assert items["living"]["label"] == "Patientenverfügung" and data["note"].startswith("Nicht als Dokument")
    _, en = _items(admin, "en")
    assert en["living"]["label"] == "Living will"


def test_present_and_suggestions(admin):
    pv = _doc(admin, "Patientenverfügung Alex")
    _doc(admin, "Brief der Bank", summary="Neue Vollmacht für das Girokonto")       # keine Vorsorgevollmacht
    _doc(admin, "Beitragsrechnung", summary="Beitrag zur Krankenversicherung", kind="rechnung")   # keine Police
    police = _doc(admin, "Versicherungsschein Lebensversicherung", kind="police")
    passport = _doc(admin, "Scan", kind="ausweis")
    _, items = _items(admin)
    assert [s["id"] for s in items["living"]["suggestions"]] == [pv]
    assert items["poa"]["suggestions"] == []
    assert [s["id"] for s in items["ins_person"]["suggestions"]] == [police]
    assert [s["id"] for s in items["id"]["suggestions"]] == [passport]
    admin.post("/emergency/documents", json={"document_id": pv})
    data, items = _items(admin)
    assert items["living"]["present"] == [{"id": pv, "title": "Patientenverfügung Alex"}]
    assert items["living"]["suggestions"] == [] and data["present"] == 1


def test_only_what_the_user_may_see(admin, make_user):
    """Der Ordner ist ein gemeinsamer Tag – fremde Dokumente darin zählen nicht, fremde werden nicht vorgeschlagen."""
    other = make_user("kim")
    mine = _doc(admin, "Vorsorgevollmacht Alex")
    admin.post("/emergency/documents", json={"document_id": mine})
    _doc(admin, "Testament Alex")
    data, items = _items(other)
    assert data["present"] == 0 and items["poa"]["present"] == []
    assert items["testament"]["suggestions"] == []
    theirs = _doc(other, "Testament Kim")
    _, items = _items(other)
    assert [s["id"] for s in items["testament"]["suggestions"]] == [theirs]


def test_suggestions_capped_and_without_deleted(admin):
    ids = [_doc(admin, f"Renteninformation {y}") for y in (2022, 2023, 2024, 2025)]
    admin.delete(f"/documents/{ids[-1]}")
    _, items = _items(admin)
    sugg = items["pension"]["suggestions"]
    assert len(sugg) == checklist.SUGGESTIONS and ids[-1] not in [s["id"] for s in sugg]
