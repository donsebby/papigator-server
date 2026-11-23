"""Dubletten-Suche: gleiche Inhalte finden, Monatsabrechnungen nicht, auflösen, „beide behalten“."""
import sqlite3

from app import config

LETTER = ("Stadtwerke Musterstadt GmbH Energieweg 12 Rechnung Nr 2025 4471 vom 14 03 2025 Kundennummer 8812233 "
          "Verbrauchsstelle Gartenstrasse 7 Abrechnungszeitraum 01 01 2024 bis 31 12 2024 Stromverbrauch 3412 kWh "
          "Arbeitspreis 0 3184 EUR kWh Grundpreis 118 80 EUR Gesamtbetrag 1205 13 EUR zahlbar bis 30 03 2025 "
          "Bitte ueberweisen Sie den Betrag auf das unten genannte Konto Vielen Dank fuer Ihr Vertrauen")


def _done(doc_id, **fields):
    """Tests haben keinen Worker: Dokument als fertig verarbeitet markieren und Felder setzen."""
    fields = {"status": "done", **fields}
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), doc_id))


def _doc(api, title, text, **fields):
    d = api.upload(text, title).json()
    _done(d["id"], **fields)
    return d


def test_finds_rescanned_letter(admin):
    a = _doc(admin, "Stromrechnung", LETTER, document_date="2025-03-14", amount=1205.13, sender="Stadtwerke")
    # zweiter Scan: OCR liest ein paar Wörter anders
    b = _doc(admin, "Scan 2", LETTER.replace("Vertrauen", "Vertrauen.").replace("Konto", "Kont0"),
             document_date="2025-03-14", amount=1205.13, sender="Stadtwerke")
    _doc(admin, "Arzt", "Praxis Dr Beispiel Rechnung fuer Behandlung am 02 02 2025 Betrag 80 EUR " * 3)
    pairs = admin.get("/duplicates").json()
    assert len(pairs) == 1
    p = pairs[0]
    assert {p["a"]["id"], p["b"]["id"]} == {a["id"], b["id"]} and p["a"]["id"] < p["b"]["id"]
    assert p["score"] >= 0.85 and "gleiches Datum" in p["reasons"] and "gleicher Betrag" in p["reasons"]


def test_monthly_statements_are_not_duplicates(admin):
    base = "Gehaltsabrechnung Firma Beispiel GmbH Personalnummer 4711 Steuerklasse 3 Krankenversicherung AOK "
    for m, amount in [("01", 3100.10), ("02", 3120.55), ("03", 3099.80)]:
        _doc(admin, f"Gehalt {m}", (base + f"Abrechnungsmonat {m} 2025 Auszahlungsbetrag {amount} EUR ") * 4,
             document_date=f"2025-{m}-28", amount=amount)
    assert admin.get("/duplicates").json() == []


def test_resolve_keeps_one_and_merges_tags_and_fields(admin):
    a = _doc(admin, "Original", LETTER, document_date="2025-03-14")
    b = _doc(admin, "Kopie", LETTER, sender="Stadtwerke", amount=1205.13)
    admin.patch(f"/documents/{b['id']}", json={"tags": ["Strom"]})
    _done(b["id"])
    r = admin.post("/duplicates/resolve", json={"keep": a["id"], "drop": b["id"]}).json()
    assert r["trashed"] == b["id"] and "sender" in r["filled"]
    kept = admin.get(f"/documents/{a['id']}").json()
    assert kept["sender"] == "Stadtwerke" and kept["amount"] == 1205.13 and kept["document_date"] == "2025-03-14"
    assert [t["name"] for t in kept["tags"]] == ["Strom"]
    assert admin.get(f"/documents/{b['id']}").json()["deleted_at"]
    assert admin.get("/duplicates").json() == []


def test_ignore_hides_pair(admin):
    a = _doc(admin, "A", LETTER)
    b = _doc(admin, "B", LETTER)
    assert len(admin.get("/duplicates").json()) == 1
    assert admin.post("/duplicates/ignore", json={"a": b["id"], "b": a["id"]}).status_code == 204
    assert admin.get("/duplicates").json() == []


def test_rights(admin, sam):
    a = _doc(admin, "A", LETTER)
    b = _doc(admin, "B", LETTER)
    j = _doc(sam, "J", LETTER)
    # Sam sieht Alex' Dokumente nicht und kann sie weder auflösen noch ignorieren
    assert all(j["id"] in (p["a"]["id"], p["b"]["id"]) for p in sam.get("/duplicates").json()) or sam.get("/duplicates").json() == []
    assert sam.post("/duplicates/resolve", json={"keep": j["id"], "drop": a["id"]}).status_code == 404
    assert sam.post("/duplicates/ignore", json={"a": a["id"], "b": b["id"]}).status_code == 404
    # Nur lesende Partner-Freigabe: nicht in der Suche
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert all(admin.id not in (p["a"]["owner_id"], p["b"]["owner_id"]) for p in sam.get("/duplicates").json())
    # Mit Schreibrecht: Paare über beide Pots
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    ids = {(p["a"]["id"], p["b"]["id"]) for p in sam.get("/duplicates").json()}
    assert (a["id"], j["id"]) in ids and (a["id"], b["id"]) in ids


def test_finds_rescan_without_date(admin):
    """Zweiter Scan, bei dem die KI kein Datum erkannt hat und die OCR ein paar Wörter verliest."""
    a = _doc(admin, "Stromrechnung", LETTER, document_date="2025-03-14", amount=1205.13)
    noisy = LETTER.replace("Energieweg", "Encrgieweg").replace("Grundpreis", "Grundprels")
    b = _doc(admin, "Scan", noisy)
    pairs = admin.get("/duplicates").json()
    assert [(p["a"]["id"], p["b"]["id"]) for p in pairs] == [(a["id"], b["id"])]
    assert 0.75 <= pairs[0]["score"] < 0.85


def test_unreviewed_external_docs_are_not_compared(admin):
    a = _doc(admin, "A", LETTER)
    b = _doc(admin, "B", LETTER, ext_pending=1)
    assert admin.get("/duplicates").json() == []
    assert admin.post(f"/documents/{b['id']}/accept").status_code == 200
    assert len(admin.get("/duplicates").json()) == 1


def test_finds_poor_rescan_with_hyphenation_and_ocr_errors(admin):
    """Schlechter zweiter Scan (Demo-Haushalt): Silbentrennung am Zeilenende, Umlaute/„rn“ falsch gelesen."""
    letter = LETTER.replace("ueberweisen", "überweisen").replace("fuer", "für")
    _doc(admin, "Strom", letter, document_date="2025-03-14", amount=1205.13)
    bad = (letter.replace("Abrechnungszeitraum", "Abrech-\nnungszeitraum").replace("Verbrauchsstelle", "Verbrauchs-\nstelle")
           .replace("überweisen", "uberweisen").replace("Vertrauen", "Vertrauen,").replace("Arbeitspreis", "Arbeitsprei")
           .replace("Grundpreis", "Grundprels").replace("Gesamtbetrag", "Gesarntbetrag").replace("Kundennummer", "Kundennumrner")
           .replace("Stromverbrauch", "Stromverbrauoh").replace("genannte", "genarmte"))
    _doc(admin, "Strom Scan", bad, document_date="2025-03-14", amount=1205.13)
    assert len(admin.get("/duplicates").json()) == 1
