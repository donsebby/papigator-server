"""Dokumentart, Empfänger und Nummern im Einsatz (26.11.2026): Empfänger → Person als Vorschlag, Kündigungshelfer nimmt
erkannte Nummern zuerst, Satz-Suche kennt Arten, Frag Papigator sieht Art/Empfänger/Nummern, Export-Metadaten."""
import io
import json
import zipfile

from app import ask, processing
from app.db import connect


def _set(doc_id, **fields):
    with connect() as con:
        con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), doc_id))


def _doc(api, text, title, **fields):
    i = api.upload(text, title).json()["id"]
    if fields:
        _set(i, **fields)
    return i


# ---------- Empfänger → Person ----------

def test_no_hint_where_the_name_rule_already_tagged(admin):
    """Der Normalfall: Vorname im Text → die Regel hat die Person schon eingetragen, der Empfänger bringt nichts."""
    admin.post("/persons", json={"name": "Mia"})
    i = _doc(admin, "Zeugnis für Mia Muster, Klasse 2b", "Zeugnis")
    admin.post("/persons", json={"name": "Mia"})      # Regel wirkt auch auf Vorhandenes
    _set(i, recipient="Mia Muster")
    d = admin.get(f"/documents/{i}").json()
    assert "Mia" in {t["name"] for t in d["tags"]} and d["person_hints"] == []


def test_hint_for_short_name_and_accept(admin, make_user):
    """Namen unter drei Buchstaben haben keine Regel – hier hilft der Empfänger."""
    pid = admin.post("/persons", json={"name": "Jo"}).json()["id"]
    i = _doc(admin, "Anmeldung Schwimmkurs für Jo Muster", "Anmeldung")
    assert admin.get(f"/documents/{i}").json()["person_hints"] == []      # ohne Empfänger kein Vorschlag
    r = admin.patch(f"/documents/{i}", json={"recipient": "Jo Muster"})   # von Hand eingetragen
    assert r.json()["person_hints"] == [{"id": pid, "name": "Jo"}]        # auch in der PATCH-Antwort (iOS)
    assert "person_hints" not in admin.get("/documents").json()[0]        # nur im Detail, nicht in Listen
    # „Jo“ steckt nicht in „Johanna“: nur ganze Wörter
    j = _doc(admin, "Brief an Johanna Muster", "Brief", recipient="Johanna Muster")
    assert admin.get(f"/documents/{j}").json()["person_hints"] == []
    # Annehmen: Tag gesetzt, Besitzer bleibt, Vorschlag weg
    r = admin.post(f"/documents/{i}/person", json={"tag_id": pid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert "Jo" in {t["name"] for t in out["tags"]} and out["person_hints"] == [] and out["owner_id"] == admin.id
    assert admin.post(f"/documents/{i}/person", json={"tag_id": pid}).status_code == 200   # zweimal schadet nicht


def test_hint_where_the_rule_may_not_reach(admin, sam):
    """Sam legt die Person an und darf Alex' Dokumente nur lesen – Sams Regel greift dort nicht. Alex sieht Sams
    Bestand (also die Person) und darf das eigene Dokument bearbeiten: Vorschlag für Alex, nicht für Sam."""
    sam.put("/shares", json={"user_id": admin.id, "can_edit": False})
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    pid = sam.post("/persons", json={"name": "Lina"}).json()["id"]
    i = _doc(admin, "Impfbescheinigung Lina Muster", "Impfbescheinigung", recipient="Lina Muster")
    with connect() as con:                                                   # wie der Worker nach der Texterkennung
        processing.apply_rules(con, i, "Impfbescheinigung Lina Muster")
    d = admin.get(f"/documents/{i}").json()
    assert "Lina" not in {t["name"] for t in d["tags"]} and d["person_hints"] == [{"id": pid, "name": "Lina"}]
    assert sam.get(f"/documents/{i}").json()["person_hints"] == []          # nur lesen: kein Vorschlag
    assert sam.post(f"/documents/{i}/person", json={"tag_id": pid}).status_code == 403
    assert admin.post(f"/documents/{i}/person", json={"tag_id": pid}).status_code == 200


def test_person_names_stay_private_in_hints(admin, sam, make_user):
    kim = make_user("kim")
    hidden = kim.post("/persons", json={"name": "Ole"}).json()["id"]        # Kim teilt mit niemandem
    plain = admin.post("/tags", json={"name": "Schule"}).json()["id"]
    i = _doc(admin, "Brief an Ole Muster", "Brief", recipient="Ole Muster")
    assert admin.get(f"/documents/{i}").json()["person_hints"] == []        # Kims Person kennt Alex nicht
    # Fremde Person und gewöhnlicher Ordner sehen aus wie eine, die es nicht gibt
    assert admin.post(f"/documents/{i}/person", json={"tag_id": hidden}).status_code == 404
    assert admin.post(f"/documents/{i}/person", json={"tag_id": plain}).status_code == 404
    assert admin.post(f"/documents/{i}/person", json={"tag_id": 99999}).status_code == 404
    assert "Ole" not in {t["name"] for t in admin.get(f"/documents/{i}").json()["tags"]}
    # Fremdes Dokument: wie nicht vorhanden
    own = kim.post("/persons", json={"name": "Pia"}).json()["id"]
    assert kim.post(f"/documents/{i}/person", json={"tag_id": own}).status_code == 404
    assert sam.post(f"/documents/{i}/person", json={"tag_id": own}).status_code == 404


# ---------- Kündigungshelfer ----------

def test_cancel_prefill_takes_recognized_numbers_first(admin):
    old = _doc(admin, "Netzfunk Beispiel GmbH Vertragsbestätigung Vertragsnummer: NF-2020-17 Kundennummer: KD 448812",
               "Vertrag Netzfunk 2020", sender="Netzfunk Beispiel GmbH",
               refs=json.dumps({"contract": "NF-2020-17", "customer": "KD 448812"}))
    young = _doc(admin, "Netzfunk Beispiel GmbH Rechnung Kundennummer: 0800-1234 Vertragskonto 55-66",
                 "Rechnung Netzfunk 2026", sender="Netzfunk Beispiel GmbH")
    r = admin.get("/contracts/cancel-prefill", params={"document_ids": f"{young},{old}"}).json()
    # Das Muster fände im jüngsten Brief „0800-1234“/„55-66“ – die erkannten Nummern aus dem Vertrag gehen vor
    assert r["contract_number"] == "NF-2020-17" and r["customer_number"] == "KD 448812"
    assert r["to_name"] == "Netzfunk Beispiel GmbH" and r["about"] == "Rechnung Netzfunk"
    # Ohne erkannte Nummern bleibt es beim Muster
    r = admin.get("/contracts/cancel-prefill", params={"document_ids": str(young)}).json()
    assert r["contract_number"] == "55-66" and r["customer_number"] == "0800-1234"


# ---------- Satz-Suche ----------

def test_sentence_search_kind_chip(admin, sam):
    z = _doc(admin, "Zeugnis Mia Muster Schuljahr 2025", "Zeugnis Mia", doc_kind="zeugnis", document_date="2025-07-20")
    b = _doc(admin, "Bescheinigung über das Zeugnis der Reife", "Bescheinigung", doc_kind="bescheinigung",
             document_date="2025-03-01")
    _doc(admin, "Zeugnis 2019", "Altes Zeugnis", doc_kind="zeugnis", document_date="2019-07-01")
    r = admin.get("/search", params={"q": "Zeugnisse aus 2025"}).json()
    chip = [c for c in r["chips"] if c["kind"] == "doc_kind"]
    assert chip and chip[0]["code"] == "zeugnis" and chip[0]["label"] == "Zeugnis"
    assert r["params"]["kind"] == "zeugnis" and "q" not in r["params"]
    assert [d["id"] for d in r["documents"]] == [z]                           # Bescheinigung mit dem Wort nicht
    # Englisch, mehrere Arten auf einmal
    r = admin.get("/search", params={"q": "Zeugnisse und Bescheinigungen aus 2025"}).json()
    assert r["params"]["kind"] == "zeugnis,bescheinigung" and {d["id"] for d in r["documents"]} == {z, b}
    r = admin.get("/search", params={"q": "certificates from 2025"}).json()
    assert r["params"].get("kind") == "bescheinigung"
    # Der Client baut die Liste nach dem Entfernen eines Chips über /documents?kind=… neu
    assert [d["id"] for d in admin.get("/documents", params={"kind": "zeugnis", "year": 2025}).json()] == [z]


def test_sentence_search_kind_falls_back_to_word(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    sam.put("/shares", json={"user_id": admin.id, "can_edit": False})
    _doc(admin, "Zeugnis Mia", "Zeugnis Mia", doc_kind="zeugnis")
    sams = _doc(sam, "Zeugnis von Ben (noch nicht nachgetragen)", "Ben Schule")   # ohne Art
    r = admin.get("/search", params={"q": "Zeugnisse von Sam"}).json()
    assert [d["id"] for d in r["documents"]] == [sams] and r["dropped"] == []
    assert "kind" not in r["params"] and "Zeugnis" in r["params"]["q"]
    assert not any(c["kind"] == "doc_kind" for c in r["chips"])
    assert any(c["kind"] == "word" and c["value"] == "Zeugnis" for c in r["chips"])


def test_sentence_search_kind_only_when_visible_and_no_tag(admin, sam, make_user):
    kim = make_user("kim")
    _doc(kim, "Geburtsurkunde", "Urkunde Kim", doc_kind="urkunde")
    r = admin.get("/search", params={"q": "Urkunden von 2025"}).json()
    assert not any(c["kind"] == "doc_kind" for c in r["chips"])               # Kims Arten verraten nichts
    # Ein eigener Ordner gleichen Namens geht vor (bisheriges Verhalten)
    admin.post("/tags", json={"name": "Rechnung"})
    _doc(admin, "Stromrechnung", "Stromrechnung", doc_kind="rechnung")
    r = admin.get("/search", params={"q": "Rechnungen von 2025"}).json()
    assert not any(c["kind"] == "doc_kind" for c in r["chips"])
    assert any(c["kind"] == "word" and c["value"] == "Rechnung" and c["tag"] for c in r["chips"])
    _doc(admin, "Mietvertrag", "Mietvertrag", doc_kind="vertrag")       # ohne Ordner „Vertrag“: die Art
    assert admin.get("/search", params={"q": "Alle Verträge"}).json()["params"].get("kind") == "vertrag"
    # „Gehalt“ und „Versicherungen“ bleiben Suchwörter, auch wenn es die Arten gibt
    _doc(admin, "Lohn", "Lohn", doc_kind="gehalt")
    _doc(admin, "Police", "Police", doc_kind="police")
    r = admin.get("/search", params={"q": "Gehalt und Versicherungen 2025"}).json()
    assert not any(c["kind"] == "doc_kind" for c in r["chips"])
    r = admin.get("/search", params={"q": "Alle Lohnabrechnungen"}).json()
    assert r["params"].get("kind") == "gehalt"


# ---------- Frag Papigator ----------

def test_ask_sees_kind_recipient_and_numbers(admin, monkeypatch):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "m"})
    _doc(admin, "Netzfunk Beispiel GmbH Vertragsbestätigung Ihr Tarif Kundennummer KD 448812 " + "Text " * 2000,
         "Vertrag Netzfunk", sender="Netzfunk Beispiel GmbH", doc_kind="vertrag", recipient="Alex Muster",
         refs=json.dumps({"customer": "KD 448812", "contract": "NF-17"}))
    prompts = []

    def fake(prompt, timeout=180, purpose="tag"):
        prompts.append(prompt)
        return '["Netzfunk", "Kundennummer"]' if len(prompts) == 1 else "KD 448812 [Dok 1]"

    monkeypatch.setattr(ask, "_chat", fake)
    r = admin.post("/ask", json={"question": "Wie ist meine Kundennummer bei Netzfunk?"}).json()
    assert r["sources"] and "[Art: Vertrag; an Alex Muster; Kundennummer KD 448812; Vertragsnummer NF-17]" in prompts[1]


# ---------- Export ----------

def test_export_metadata_has_kind_fields(admin):
    i = _doc(admin, "Zeugnis Mia", "Zeugnis", doc_kind="zeugnis", recipient="Mia Muster", refs=json.dumps({"other": "S-2231"}))
    z = zipfile.ZipFile(io.BytesIO(admin.get("/export.zip").content))
    meta = {m["id"]: m for m in json.loads(z.read("metadata.json"))}
    assert meta[i]["doc_kind"] == "zeugnis" and meta[i]["recipient"] == "Mia Muster" and meta[i]["refs"] == {"other": "S-2231"}
