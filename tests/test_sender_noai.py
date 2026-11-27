"""Absender und Titel ohne KI (27.11.2026): nur Namen aus dem eigenen Bestand, im Briefkopf oder in der Fußzeile.
Alle Texte erfunden."""
from app import worker
from app.db import connect, init_db
from app.processing import plain_title, sender_from_known


def _doc(api, title, text, sender=None, skipped=True, recipient=None):
    i = api.upload(text=text, title=title).json()["id"]
    with connect() as con:
        if skipped:
            con.execute("UPDATE documents SET status='tagging', llm_status='pending' WHERE id=?", (i,))
        else:
            con.execute("UPDATE documents SET status='done', llm_status='done', sender=?, recipient=? WHERE id=?",
                        (sender, recipient, i))
    return i


def _known(api, *names, recipient=None):
    for n in names:
        _doc(api, f"Alt {n}", f"Brief von {n}", sender=n, skipped=False, recipient=recipient)


def _owner(i):
    with connect() as con:
        return con.execute("SELECT owner_id FROM documents WHERE id=?", (i,)).fetchone()[0]


LETTER = ("Stadtwerke Musterstadt · Postfach 12 · 12345 Musterstadt\nSam Muster\nLindenweg 3\n12345 Musterstadt\n"
          "Rechnungsdatum: 14.05.2025\nIhre Jahresabrechnung\n" + "Verbrauch Zeile\n" * 30 +
          "Bankverbindung: Sparkasse Musterstadt IBAN DE00 0000 0000 0000 0000 00\nStadtwerke Musterstadt GmbH")


def test_plain_title():
    assert plain_title("Scan 27.11.2026, 08:12", "Scan 27.11.2026, 08:12.pdf")
    assert plain_title("2026, 8:12 AM", "Scan 11/27/2026, 8:12 AM.pdf")   # englisch: Schrägstriche im Namen
    assert plain_title("IMG_1234", "IMG_1234.jpg") and plain_title("scan_0001", "scan_0001.pdf")
    assert plain_title("Dokument", None) and plain_title("", "x.pdf")
    assert not plain_title("Rechnung_2025_05", "Rechnung_2025_05.pdf")       # Dateiname mit Aussage
    assert not plain_title("Strom 2025", "scan_0001.pdf")                     # von Hand vergeben


def test_known_sender_head_and_foot(admin):
    _known(admin, "Stadtwerke Musterstadt", "Sparkasse Musterstadt", "Telekom", "Deutsche Telekom")
    o = _owner(_doc(admin, "x", "x", skipped=False))
    with connect() as con:
        assert sender_from_known(con, o, LETTER) == "Stadtwerke Musterstadt"      # Bankzeile zählt nicht
        assert sender_from_known(con, o, "DEUTSCHE   TELEKOM GmbH\nRechnung") == "Deutsche Telekom"
        assert sender_from_known(con, o, "Stadtwerke Musterstadt\nDeutsche Telekom") is None   # zwei: keiner
        body = "Versicherung AG\n" + "Text\n" * 20 + "wie die Stadtwerke Musterstadt mitteilen\n" + "Text\n" * 20
        assert sender_from_known(con, o, body) is None                            # nur im Fließtext
        assert sender_from_known(con, o, "Stadtwerkestraße 4") is None            # kein ganzes Wort


def test_household_and_other_pots(admin, sam):
    _known(admin, "Sam Muster", recipient="Sam Muster")    # eigener Brief (Kündigung) – Name ist Empfänger
    _known(sam, "Kita Sonnenschein")                       # fremder Pot
    o = _owner(_doc(admin, "x", "x", skipped=False))
    with connect() as con:
        assert sender_from_known(con, o, "Sam Muster\nLindenweg 3") is None
        assert sender_from_known(con, o, "Kita Sonnenschein\nBeitrag") is None


def test_worker_sets_sender_and_title(admin):
    _known(admin, "Stadtwerke Musterstadt")
    a = _doc(admin, "Scan 27.11.2026, 08:12", LETTER)
    b = _doc(admin, "Strom 2025", LETTER.replace("14.05.2025", "15.05.2025"))
    for i in (a, b):
        worker.llm_one(i)
    da, db_ = admin.get(f"/documents/{a}").json(), admin.get(f"/documents/{b}").json()
    assert da["sender"] == "Stadtwerke Musterstadt" and da["title"] == "Stadtwerke Musterstadt · 14.05.2025"
    assert db_["sender"] == "Stadtwerke Musterstadt" and db_["title"] == "Strom 2025"   # eigener Titel bleibt


def test_learn_from_manual_sender(admin):
    a = _doc(admin, "IMG_0001", "Kita Sonnenschein\nElternbeitrag Mai")
    b = _doc(admin, "IMG_0002", "Kita Sonnenschein\nElternbeitrag Juni")
    c = _doc(admin, "IMG_0003", "Kita Sonnenschein\nFotos", )
    for i in (a, b, c):
        worker.llm_one(i)
    assert admin.get(f"/documents/{b}").json()["sender"] is None           # noch unbekannt
    admin.patch(f"/documents/{c}", json={"title": "Fotos Sommerfest"})
    r = admin.patch(f"/documents/{a}", json={"sender": "Kita Sonnenschein"}).json()
    assert r["title"] == "Kita Sonnenschein"
    db_, dc = admin.get(f"/documents/{b}").json(), admin.get(f"/documents/{c}").json()
    assert db_["sender"] == "Kita Sonnenschein" and db_["title"] == "Kita Sonnenschein"
    assert dc["sender"] == "Kita Sonnenschein" and dc["title"] == "Fotos Sommerfest"


def test_bestand_once(admin):
    _known(admin, "Stadtwerke Musterstadt")
    a = _doc(admin, "scan_0001", LETTER)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='skipped', document_date='2025-05-14' WHERE id=?", (a,))
        con.execute("DELETE FROM settings WHERE key='sender_noai_v1'")
    init_db()
    assert admin.get(f"/documents/{a}").json()["title"] == "Stadtwerke Musterstadt · 14.05.2025"
    with connect() as con:
        assert con.execute("SELECT value FROM settings WHERE key='sender_noai_v1'").fetchone()[0] == "1"


def test_tags_from_same_sender(admin):
    """Ohne KI: Tags, die alle letzten Briefe desselben Absenders tragen – nicht, was nur einer hat, keine Person."""
    olds = []
    for n, tags in ((1, ["Haus", "Strom"]), (2, ["Haus", "Strom", "Steuer"]), (3, ["Strom", "Haus"])):
        i = _doc(admin, f"Alt {n}", f"Brief {n}", sender="Stadtwerke Musterstadt", skipped=False)
        admin.patch(f"/documents/{i}", json={"tags": tags})
        olds.append(i)
    p = admin.post("/persons", json={"name": "Mia"}).json()
    with connect() as con:
        for i in olds:   # Person an allen alten Briefen – hängt am Empfänger, nicht am Absender
            con.execute("INSERT INTO document_tags VALUES (?,?,'user')", (i, p["id"]))
    a = _doc(admin, "scan_0001", LETTER)
    worker.llm_one(a)
    d = admin.get(f"/documents/{a}").json()
    assert d["sender"] == "Stadtwerke Musterstadt"
    assert sorted(t["name"] for t in d["tags"]) == ["Haus", "Strom"]
    # Ein einziger früherer Brief reicht nicht
    _doc(admin, "Alt Kita", "Kita Sonnenschein", sender="Kita Sonnenschein", skipped=False)
    b = _doc(admin, "IMG_0001", "Kita Sonnenschein\nElternbeitrag")
    worker.llm_one(b)
    assert admin.get(f"/documents/{b}").json()["tags"] == []
    # Von Hand getaggt: nichts dazu
    c = _doc(admin, "scan_0002", LETTER.replace("14.05.2025", "16.05.2025"))
    admin.patch(f"/documents/{c}", json={"tags": ["Wichtig"]})
    worker.llm_one(c)
    assert [t["name"] for t in admin.get(f"/documents/{c}").json()["tags"]] == ["Wichtig"]
