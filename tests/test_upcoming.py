"""„Im Blick“: Ablaufdaten von Ausweisen und Karten, Prüfungen/Wartungen (nur der jüngste Beleg), offene Erstattungen
(GOÄ) und Preiserhöhungen bei Verträgen; Rücksendefrist nur mit Einstellung."""
import datetime as dt

from app import extras, processing
from app.db import connect

TODAY = dt.date.today()


def _iso(days: int) -> str:
    return (TODAY + dt.timedelta(days=days)).isoformat()


def _de(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def _apply(doc_id, data):
    with connect() as con:
        processing.apply_llm(con, doc_id, data)
        return dict(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def _items(admin, kind=None):
    items = admin.get("/upcoming").json()["items"]
    return [i for i in items if kind is None or i["type"] == kind]


def test_month_in_text():
    f = processing.month_in_text
    assert f("2028-03-31", "Gültig bis 03/28") and f("2028-03-15", "valid thru 03/2028") and f("2028-03-01", "März 2028")
    assert f("2031-08-20", "Gültig bis 20.08.2031")
    assert not f("2028-03-31", "Kundennummer 103/284") and not f("2028-04-30", "Gültig bis 03/28")


def test_id_card_expiry_not_birth_or_issue_date(admin):
    exp = _iso(90)
    text = f"Personalausweis Geburtsdatum 03.03.1977 Ausgestellt am 01.02.2016 Gültig bis {_de(exp)}"
    d = admin.upload(text, "Ausweis").json()["id"]
    r = _apply(d, {"document_date": "2016-02-01", "expires_what": "Personalausweis", "expires_on": "1977-03-03"})
    assert r["expires_on"] is None                                   # Geburtsdatum: vor der Ausstellung
    r = _apply(d, {"document_date": "2016-02-01", "expires_what": "Personalausweis", "expires_on": exp})
    assert (r["expires_on"], r["expires_what"]) == (exp, "Personalausweis")
    assert [i["what"] for i in _items(admin, "expiry")] == ["Personalausweis"]
    from app import ask
    assert "Personalausweis gültig bis" in ask.facts([admin.get("/auth/me").json()["id"]], TODAY)
    # nicht aus der Liste (Gutschein) und nicht im Text: nichts
    g = admin.upload(f"Gutschein gültig bis {_de(exp)}", "Gutschein").json()["id"]
    assert _apply(g, {"expires_what": "Gutschein", "expires_on": exp})["expires_on"] is None


def test_model_shifted_day_uses_date_from_text(admin):
    """gemma4:e4b machte aus „Gültig bis 01.02.2031“ den 2031-02-28 – das Datum aus dem Text gilt."""
    d = admin.upload("PERSONALAUSWEIS Geburtstag 09.09.1979 Gültig bis 01.02.2031 Ausgestellt am 02.02.2021", "Ausweis").json()["id"]
    r = _apply(d, {"document_date": "2021-02-02", "expires_what": "Personalausweis", "expires_on": "2031-02-28"})
    assert r["expires_on"] == "2031-02-01"


def test_card_month_year_and_new_card_replaces_old(admin):
    old = admin.upload("Kreditkarte Gültig bis 03/24", "Alte Karte").json()["id"]
    r = _apply(old, {"expires_what": "Kreditkarte", "expires_on": "2024-03-31"})
    assert r["expires_on"] == "2024-03-31"
    soon = _iso(30)
    y, m = soon[:4], soon[5:7]
    new = admin.upload(f"Kreditkarte valid thru {m}/{y[2:]}", "Neue Karte").json()["id"]
    r = _apply(new, {"expires_what": "Kreditkarte", "expires_on": f"{y}-{m}-01"})
    assert r["expires_on"] == processing._month_end(f"{y}-{m}-01")
    assert [i["document_id"] for i in _items(admin, "expiry")] == [new]
    # weit weg: nicht in „Im Blick“
    far = admin.upload("Reisepass gültig bis 01.01.2033", "Pass").json()["id"]
    _apply(far, {"expires_what": "Reisepass", "expires_on": "2033-01-01"})
    assert all(i["what"] != "Reisepass" for i in _items(admin, "expiry"))


def test_service_newest_report_only(admin):
    # 2020er Bericht (nächste HU längst vorbei) und ein neuer mit „nächste HU“ in 30 Tagen
    old = admin.upload("Prüfbericht Hauptuntersuchung 10.03.2020 nächste HU 03/2022", "HU 2020").json()["id"]
    _apply(old, {"document_date": "2020-03-10", "service_what": "Hauptuntersuchung", "service_next": "2022-03-31"})
    nxt = _iso(30)
    new = admin.upload(f"Prüfbericht Hauptuntersuchung {_de(_iso(-700))} nächste HU {nxt[5:7]}/{nxt[:4]}", "HU neu").json()["id"]
    r = _apply(new, {"document_date": _iso(-700), "service_what": "Hauptuntersuchung", "service_next": nxt})
    assert r["service_next"] == processing._month_end(nxt)
    assert [i["document_id"] for i in _items(admin, "service")] == [new]
    # Heizungswartung ohne Termin im Text: Belegdatum + 12 Monate
    h = admin.upload(f"Rechnung Heizungswartung {_de(_iso(-350))}", "Heizung").json()["id"]
    r = _apply(h, {"document_date": _iso(-350), "service_what": "Heizungswartung"})
    assert r["service_next"] == processing.warranty_end(_iso(-350), 12)
    assert {i["what"] for i in _items(admin, "service")} == {"Hauptuntersuchung", "Heizungswartung"}
    # erledigt (Termin geleert): weg
    admin.patch(f"/documents/{h}", json={"service_next": ""})
    assert {i["what"] for i in _items(admin, "service")} == {"Hauptuntersuchung"}


def test_refund_open_until_statement_contains_amount(admin):
    bill = admin.upload("Liquidation nach GOÄ, Dr. Muster, Betrag 123,45 €", "Arztrechnung").json()["id"]
    _apply(bill, {"kind": "Rechnung", "amount": 123.45})
    assert [i["document_id"] for i in _items(admin, "refund")] == [bill]
    other = admin.upload("Leistungsabrechnung: wir erstatten 98,00 € für Rechnung vom …", "Abrechnung 1").json()["id"]
    _apply(other, {"kind": "Abrechnung"})
    assert [i["document_id"] for i in _items(admin, "refund")] == [bill]          # anderer Betrag
    stmt = admin.upload("Leistungsabrechnung Ihrer Krankenversicherung: Rechnung 123,45 € erstattet", "Abrechnung 2").json()["id"]
    _apply(stmt, {"kind": "Abrechnung"})
    assert _items(admin, "refund") == []
    with connect() as con:
        assert con.execute("SELECT refund_state, refund_doc FROM documents WHERE id=?", (bill,)).fetchone()[:] == ("done", stmt)
    # von Hand: nicht einreichen
    b2 = admin.upload("Rechnung GOZ 55,00 €", "Zahnarzt").json()["id"]
    _apply(b2, {"kind": "Rechnung", "amount": 55.0})
    assert admin.patch(f"/documents/{b2}", json={"refund_state": "none"}).json()["refund_state"] == "none"
    assert admin.patch(f"/documents/{b2}", json={"refund_state": "x"}).status_code == 400
    # Kassenpatient (keine GOÄ): nichts
    k = admin.upload("Rechnung Physiotherapie 40,00 €", "Physio").json()["id"]
    assert _apply(k, {"kind": "Rechnung", "amount": 40.0})["refund_state"] is None


def test_price_increase_needs_stable_new_amount():
    def pays(amounts, kind="rechnung"):
        start = dt.date(2025, 1, 15)
        return [{"id": i + 1, "amount": a, "d": (start + dt.timedelta(days=30 * i)).isoformat(), "doc_kind": kind}
                for i, a in enumerate(amounts)]
    inc = extras._increase(pays([40, 40, 40, 44, 44]))
    assert inc and (inc["from"], inc["to"], inc["percent"], inc["document_id"]) == (40, 44, 10, 4)
    assert extras._increase(pays([40, 40, 40, 44])) is None                        # einmal teurer: Aufschlag?
    assert extras._increase(pays([40, 40, 44]), "jährlich")["percent"] == 10      # Jahresbeitrag: einmal reicht
    assert extras._increase(pays([40, 40, 40, 40.5, 40.5])) is None               # unter 3 %
    assert extras._increase(pays([80, 120, 120], "abrechnung")) is None           # Nachzahlung ist kein Preis


def test_return_window_only_with_setting(admin):
    bought = _iso(-3)
    d = admin.upload(f"Bestellung Online-Shop, Rechnung vom {_de(bought)}, Toaster 39,99 €", "Toaster").json()["id"]
    data = {"kind": "Rechnung", "document_date": bought, "goods": True, "online": True}
    assert _apply(d, data)["due_date"] is None
    assert admin.patch("/settings", json={"return_window": True}).status_code == 200
    assert admin.get("/settings").json()["return_window"] is True
    d2 = admin.upload(f"Bestellung Online-Shop, Rechnung vom {_de(bought)}, Mixer 59,99 €", "Mixer").json()["id"]
    r = _apply(d2, data)
    assert (r["due_date"], r["due_note"]) == (_iso(11), "Rücksendefrist")


def test_upcoming_respects_visibility(admin, sam):
    exp = _iso(20)
    d = sam.upload(f"Reisepass gültig bis {_de(exp)}", "Sams Pass").json()["id"]
    _apply(d, {"expires_what": "Reisepass", "expires_on": exp})
    assert _items(admin) == [] and [i["document_id"] for i in sam.get("/upcoming").json()["items"]] == [d]


def test_two_passports_and_two_cars_both_show(admin):
    a = admin.upload(f"Reisepass Mia gültig bis {_de(_iso(40))}", "Pass Mia").json()["id"]
    _apply(a, {"expires_what": "Reisepass", "expires_on": _iso(40)})
    b = admin.upload(f"Reisepass Alex gültig bis {_de(_iso(90))}", "Pass Alex").json()["id"]
    _apply(b, {"expires_what": "Reisepass", "expires_on": _iso(90)})
    assert [i["document_id"] for i in _items(admin, "expiry")] == [a, b]
    nxt = _iso(20)
    for plate, title in (("KA-AB 123", "HU Kombi"), ("KAR-X 77", "HU Kleinwagen")):
        d = admin.upload(f"Prüfbericht Hauptuntersuchung Kennzeichen {plate} {_de(_iso(-700))} nächste HU {nxt[5:7]}/{nxt[:4]}",
                         title).json()["id"]
        _apply(d, {"document_date": _iso(-700), "service_what": "Hauptuntersuchung", "service_next": nxt})
    assert {i["title"] for i in _items(admin, "service")} == {"HU Kombi", "HU Kleinwagen"}


def test_many_open_refunds_collapse(admin):
    for n in range(5):
        d = admin.upload(f"Liquidation GOÄ Nr {n} Betrag 1{n},50 €", f"Arzt {n}").json()["id"]
        _apply(d, {"kind": "Rechnung", "amount": float(f"1{n}.5")})
    r = _items(admin, "refund")
    assert len(r) == 1 and r[0]["count"] == 5 and r[0]["title"] == "5 Arztrechnungen"


def test_extras_migration_runs_on_startup(tmp_path, monkeypatch):
    """Beim Start: GOÄ-Rechnung wird geöffnet und von der späteren Abrechnung geschlossen; Ausweis wird vorgemerkt."""
    import sqlite3 as sq
    from app import db
    path = tmp_path / "alt.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sq.connect(path)
    con.execute("INSERT INTO users(username, display_name, password_hash) VALUES ('x','X','h')")
    rows = [("Arzt", "Liquidation nach GOÄ 80,00 €", "rechnung", 80.0, _iso(-30)),
            ("Abrechnung", "Leistungsabrechnung: wir erstatten 80,00 €", "abrechnung", None, _iso(-5)),
            ("Ausweis", "Personalausweis gültig bis 01.02.2031", "info", None, "2021-02-02")]
    for title, text, kind, amount, d in rows:
        con.execute("INSERT INTO documents(owner_id, title, text, status, llm_status, doc_kind, amount, document_date) "
                    "VALUES (1, ?, ?, 'done', 'done', ?, ?, ?)", (title, text, kind, amount, d))
    con.execute("DELETE FROM settings WHERE key='extras_v1'")
    con.commit(); con.close()
    db.init_db()
    con = sq.connect(path)
    got = con.execute("SELECT title, refund_state, llm_status FROM documents ORDER BY id").fetchall()
    assert got[0][1] == "done" and got[2][2] == "refill_w" and got[1][2] == "done"


def test_warranty_ending_soon(admin):
    """Gewährleistung/Garantie endet in den nächsten vier Wochen → „Im Blick“; Kleinkram unter 50 € und spätere nicht."""
    def doc(title, until, amount, kind="legal"):
        d = admin.upload(f"Rechnung {title}", title).json()
        with connect() as con:
            con.execute("UPDATE documents SET warranty_months=24, warranty_kind=?, warranty_until=?, amount=? WHERE id=?",
                        (kind, until, amount, d["id"]))
        return d["id"]
    tv = doc("Fernseher", _iso(20), 899.0)
    doc("Kabel", _iso(10), 12.99)
    doc("Waschmaschine", _iso(90), 650.0)
    dryer = doc("Trockner", _iso(5), 700.0, kind="garantie")
    w = _items(admin, "warranty")
    assert [i["document_id"] for i in w] == [dryer, tv]
    assert w[0]["what"] == "Garantie" and w[1]["what"] == "Gewährleistung" and w[1]["date"] == _iso(20)
