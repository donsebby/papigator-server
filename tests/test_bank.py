"""Geld: CSV-Import (Kopfzeile finden, Soll/Haben, Kodierung), Doppelte, Einsortieren mit Regeln, Summen ohne
Umbuchungen, Rechte pro Konto, Import zurücknehmen, Beleg zur Buchung nur bei eindeutigem Treffer."""
from app import bank
from app.db import connect

# wie ein Bankexport: Vorspann, Semikolon, Dezimalkomma, Windows-1252 (Umlaute)
CSV = """Umsatzanzeige C24 Girokonto;;;;
Kontoinhaber: Alex Beispiel;;;;
;;;;
Buchungsdatum;Wertstellung;Zahlungsempfänger;Verwendungszweck;Betrag
14.11.2026;14.11.2026;ZALANDO SE;Bestellung 1030 Winterjacke;-89,95
09.11.2026;09.11.2026;PayPal Europe;PP.4711.PP . VINTED, Ihr Einkauf bei VINTED;-24,50
28.10.2026;28.10.2026;H&M 101 Musterstadt;Kartenzahlung;-29,95
21.09.2026;21.09.2026;ZALANDO SE;Rücksendung Gutschrift;49,95
31.10.2026;31.10.2026;Arbeitgeber GmbH;Gehalt Oktober;2.950,00
01.11.2026;01.11.2026;Alex Beispiel;Umbuchung Tagesgeld;-500,00
13.11.2026;13.11.2026;REWE Markt;Kartenzahlung;-12,00
13.11.2026;13.11.2026;REWE Markt;Kartenzahlung;-12,00
10.11.2026;10.11.2026;Flohmarkt Verein;Stand 12;-18,00
""".encode("cp1252")


def _imp(api, content=CSV, name="c24_umsaetze.csv", **form):
    data = {k: str(v) for k, v in form.items()}
    return api.post("/bank/import", files={"file": (name, content, "text/csv")}, data=data)


def test_parse_header_after_preamble_and_soll_haben():
    p = bank.parse_csv(CSV, "c24.csv")
    assert p["error"] is None and p["bank"] == "C24 Bank" and len(p["rows"]) == 9
    assert p["mapping"]["date"] == "Buchungsdatum" and p["mapping"]["counterparty"] == "Zahlungsempfänger"
    assert p["rows"][0] == {"booked": "2026-11-14", "cents": -8995, "currency": "EUR", "counterparty": "ZALANDO SE",
                            "purpose": "Bestellung 1030 Winterjacke", "bank_category": "", "kind": "", "acc_no": "", "acc_name": ""}
    assert p["rows"][4]["cents"] == 295000
    sh = "Datum,Empfänger,Soll,Haben\n2026-11-02,Netflix,\"17,99\",\n2026-11-03,Arbeitgeber,,\"100,00\"\n".encode()
    q = bank.parse_csv(sh)
    assert [r["cents"] for r in q["rows"]] == [-1799, 10000] and q["mapping"]["amount"] == "Soll / Haben"
    assert bank.parse_csv(b"Name;Wert\nx;1\n")["error"]


def test_import_twice_adds_nothing_identical_bookings_stay(admin):
    prev = _imp(admin, preview="true").json()
    assert prev["new"] == 9 and prev["duplicates"] == 0 and "rows" in prev
    assert admin.get("/bank/accounts").json()["accounts"] == []          # Vorschau legt nichts an
    r = _imp(admin, account_name="Girokonto").json()
    acc = r["account_id"]
    assert r["new"] == 9
    again = _imp(admin, account_id=acc).json()
    assert again["new"] == 0 and again["duplicates"] == 9
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM bank_tx WHERE counterparty='REWE Markt'").fetchone()[0] == 2


def test_categories_and_sums_exclude_transfers(admin):
    _imp(admin, account_name="Girokonto")
    s = admin.get("/bank/summary").json()
    cats = {c["name"]: c["cents"] for c in s["categories"]}
    assert cats["Kleidung"] == 8995 + 2995 - 4995 + 2450          # Vinted über PayPal, Rücksendung abgezogen
    assert cats["Lebensmittel"] == 2400 and "Umbuchung" not in cats and s["income"] == 295000
    assert s["covered"] == {"first": "2026-09-21", "last": "2026-11-14"}
    assert s["to_sort"] == 1 and cats["Nicht einsortiert"] == 1800
    assert s["spent"] == sum(c for n, c in cats.items())
    k = admin.get("/bank/category", params={"name": "Kleidung", "from": "2026-10-01"}).json()
    assert k["cents"] == 8995 + 2995 + 2450 and [m["month"] for m in k["months"]] == ["2026-10", "2026-11"]


def test_sorting_with_rule_applies_to_open_and_future(admin):
    acc = _imp(admin, account_name="Girokonto").json()["account_id"]
    open_ = admin.get("/bank/to_sort").json()["transactions"]
    assert [t["counterparty"] for t in open_] == ["Flohmarkt Verein"] and open_[0]["key"] == "flohmarkt verein"
    r = admin.patch(f"/bank/tx/{open_[0]['id']}", json={"category": "Kinder", "remember": "flohmarkt"}).json()
    assert r["category"] == "Kinder"
    assert admin.patch(f"/bank/tx/{open_[0]['id']}", json={"category": "Quatsch"}).status_code == 400
    more = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n15.11.2026;Flohmarkt Musterstadt;Stand;-7,00\n".encode()
    _imp(admin, more, account_id=acc)
    assert admin.get("/bank/to_sort").json()["transactions"] == []
    assert [x["pattern"] for x in admin.get("/bank/rules").json()] == ["flohmarkt"]


def test_merchant_key_behind_paypal():
    assert bank.merchant_key("PayPal Europe", "PP.4711.PP . VINTED, Ihr Einkauf bei VINTED") == "vinted"
    assert bank.merchant_key("ZALANDO SE", "") == "zalando"


def test_account_visibility_is_its_own_setting(admin, sam, make_user):
    # Sam teilt Dokumente mit Alex – das Konto sieht Alex trotzdem erst, wenn Sam es freigibt
    me = admin.id
    sam.put("/shares", json={"user_id": me, "can_edit": True})              # Sam teilt Dokumente mit Alex
    acc = _imp(sam, account_name="Sams Konto").json()["account_id"]
    assert admin.get("/bank/accounts").json()["accounts"] == [] and admin.get("/bank/summary").json()["count"] == 0
    assert admin.get("/bank/category", params={"name": "Kleidung"}).json()["transactions"] == []
    tx = sam.get("/bank/to_sort").json()["transactions"][0]["id"]
    assert admin.patch(f"/bank/tx/{tx}", json={"category": "Kinder"}).status_code == 404
    assert admin.delete(f"/bank/accounts/{acc}").status_code == 404
    assert me in [p["id"] for p in sam.get("/bank/accounts").json()["people"]]
    assert sam.patch(f"/bank/accounts/{acc}", json={"visible_to": [me]}).status_code == 200
    assert admin.get("/bank/summary").json()["count"] == 9
    assert admin.patch(f"/bank/tx/{tx}", json={"category": "Kinder"}).status_code == 200   # mitsortieren geht
    assert admin.delete(f"/bank/accounts/{acc}").status_code == 403          # mitsehen, nicht löschen
    assert _imp(admin, account_id=acc).status_code == 403
    # nachträglich wieder privat: Alex sieht nichts mehr
    assert sam.patch(f"/bank/accounts/{acc}", json={"visible_to": []}).status_code == 200
    assert admin.get("/bank/accounts").json()["accounts"] == [] and admin.get("/bank/summary").json()["count"] == 0
    assert admin.patch(f"/bank/accounts/{acc}", json={"visible_to": [me]}).status_code == 404   # nur wer es angelegt hat
    other = make_user("fremd")
    assert sam.patch(f"/bank/accounts/{acc}", json={"visible_to": [other.id]}).status_code == 200
    assert other.get("/bank/summary").json()["count"] == 0                     # nur Leute, mit denen Sam teilt


def test_undo_import(admin):
    r = _imp(admin, account_name="Girokonto").json()
    assert admin.delete(f"/bank/imports/{r['import_id']}").json()["removed"] == 9
    assert admin.get("/bank/summary").json()["count"] == 0


def test_receipt_linked_only_when_unique(admin):
    d = admin.upload("Rechnung Zalando 89,95 €", "Zalando Rechnung").json()["id"]
    other = admin.upload("Rechnung Otto 89,95 €", "Otto Rechnung").json()["id"]   # gleicher Betrag, anderer Absender
    with connect() as con:
        con.execute("UPDATE documents SET amount=89.95, document_date='2026-11-12', sender='Zalando' WHERE id=?", (d,))
        con.execute("UPDATE documents SET amount=89.95, document_date='2026-11-13', sender='Otto' WHERE id=?", (other,))
    _imp(admin, account_name="Girokonto")
    tx = admin.get("/bank/category", params={"name": "Kleidung"}).json()["transactions"]
    assert {t["counterparty"]: t["document_id"] for t in tx if t["cents"] == -8995} == {"ZALANDO SE": d}
    s = admin.get("/bank/summary").json()
    assert s["with_doc"] == 1


def test_bad_file_and_size(admin):
    assert _imp(admin, b"kein csv", account_name="x").status_code == 400


def test_insights_price_and_new_and_ask(admin, monkeypatch):
    import datetime as dt
    today = dt.date.today()
    lines = ["Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag"]
    for i, a in enumerate([13.99, 13.99, 13.99, 17.99, 17.99]):
        d = today - dt.timedelta(days=30 * (4 - i) + 2)
        lines.append(f"{d.strftime('%d.%m.%Y')};Netflix International;Abo;-" + f"{a:.2f}".replace(".", ","))
    for i in range(3):
        d = today - dt.timedelta(days=30 * (2 - i) + 1)
        lines.append(f"{d.strftime('%d.%m.%Y')};Disney Plus;Abo;-9,99")
    _imp(admin, "\n".join(lines).encode(), account_name="Giro")
    items = {i["key"]: i for i in admin.get("/bank/insights").json()["items"]}
    assert items["netflix international"]["type"] == "price" and items["netflix international"]["percent"] == 29
    assert items["disney plus"]["type"] == "new"
    up = [i for i in admin.get("/upcoming").json()["items"] if i["type"] == "price"]
    assert [i["what"] for i in up] == ["Netflix International"] and up[0]["document_id"] is None
    from app.auth import User
    with connect() as con:
        user = User.from_row(con.execute("SELECT * FROM users WHERE id=?", (admin.id,)).fetchone())
    txt = bank.ask_facts(user, "Was haben wir für Abos ausgegeben?", None)
    assert "- Abos: 107.92 €" in txt and "abgedeckt" in txt and "Abos nach Händler" in txt
    assert bank.ask_facts(user, "Wann läuft mein Ausweis ab?", None) == ""


def test_insights_ignore_one_off_payment_to_same_payee(admin):
    """Miete monatlich, dazwischen einmal die Nebenkosten-Nachzahlung an dieselbe Hausverwaltung: keine Preiserhöhung."""
    import datetime as dt
    today = dt.date.today()
    lines = ["Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag"]
    for i in range(6):
        d = today - dt.timedelta(days=30 * (5 - i) + 3)
        lines.append(f"{d.strftime('%d.%m.%Y')};Hausverwaltung Beispiel;Miete;-1150,00")
    lines.append(f"{(today - dt.timedelta(days=100)).strftime('%d.%m.%Y')};Hausverwaltung Beispiel;Nachzahlung Nebenkosten;-162,80")
    _imp(admin, "\n".join(lines).encode(), account_name="Giro")
    assert admin.get("/bank/insights").json()["items"] == []


def test_insights_intro_price_ending_is_an_increase(admin):
    """Einführungspreis vorbei: 6 × 19,99, dann 3 × 39,99 – das ist eine Preiserhöhung, kein Ausreißer."""
    import datetime as dt
    today = dt.date.today()
    lines = ["Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag"]
    for i, a in enumerate([19.99] * 6 + [39.99] * 3):
        d = today - dt.timedelta(days=30 * (8 - i) + 2)
        lines.append(f"{d.strftime('%d.%m.%Y')};Streamdienst Beispiel;Abo;-" + f"{a:.2f}".replace(".", ","))
    _imp(admin, "\n".join(lines).encode(), account_name="Giro")
    items = admin.get("/bank/insights").json()["items"]
    assert [(i["type"], i["to"]) for i in items] == [("price", 39.99)]


def test_household_rules_and_ask_wording(admin, sam):
    """Sam sortiert, Alex importiert den nächsten Monat – Sams Regel greift; Frag Papigator nur bei Geldfragen."""
    import datetime as dt
    from app import ask
    from app.auth import User
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    acc = _imp(admin, account_name="Giro", visible_to=sam.id).json()["account_id"]
    open_ = sam.get("/bank/to_sort").json()["transactions"]
    sam.patch(f"/bank/tx/{open_[0]['id']}", json={"category": "Kinder", "remember": "flohmarkt"})
    more = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n15.11.2026;Flohmarkt Musterstadt;Stand;-7,00\n".encode()
    _imp(admin, more, account_id=acc)
    assert admin.get("/bank/to_sort").json()["transactions"] == []
    with connect() as con:
        user = User.from_row(con.execute("SELECT * FROM users WHERE id=?", (admin.id,)).fetchone())
    q = "Was haben wir seit Juni für Kleidung ausgegeben?"
    span = ask.time_range(q, dt.date(2026, 11, 18))
    txt = bank.ask_facts(user, q, span)
    assert "Kleidung" in txt and "Kleidung nach Händler" in txt
    assert bank.ask_facts(user, "Welche Versicherungen haben wir?", None) == ""
    assert bank.merchant_key("PayPal Europe", "Zahlung an privat") is None


def test_receipt_used_once(admin):
    d = admin.upload("Rechnung REWE 12,00 €", "Bon").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET amount=12.0, document_date='2026-11-13' WHERE id=?", (d,))
    _imp(admin, account_name="Giro")
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM bank_tx WHERE document_id=?", (d,)).fetchone()[0] <= 1


def test_suggestions_never_use_cloud(monkeypatch):
    from app import llm
    called = []
    monkeypatch.setattr(llm, "usable", lambda purpose: called.append(purpose) or [])
    assert bank.suggest_categories(1) == 0 and called == ["bank"]


def test_coverage_is_all_bookings_not_the_window(admin):
    _imp(admin, account_name="Giro")
    s = admin.get("/bank/summary", params={"from": "2026-10-01"}).json()
    assert s["covered"]["first"] == "2026-09-21"            # ältere Umsätze sind da – kein „es fehlen welche“
    e = admin.get("/bank/summary", params={"from": "2030-01-01"}).json()
    assert e["count"] == 0 and e["out_count"] == 0 and e["covered"]["first"] == "2026-09-21"


# wie der C24-Export: Buchungsart vorne, Kategorie und Unterkategorie hinten (erfundene Zeilen)
C24_LIKE = ("Transaktionstyp;Buchungsdatum;Wertstellung;Betrag;Zahlungsempfänger;IBAN;BIC;Verwendungszweck;"
            "Kontoinhaber;Kontonummer;Kategorie;Unterkategorie\n"
            "Pocket-Umbuchung;17.11.2026;;186,00 €;Urlaub Alex;;;Schuhe Kinder;Alex Beispiel;1234567890;Kleidung;Umbuchung\n"
            "Pocket-Umbuchung;16.11.2026;;-50,00 €;Urlaub Alex;;;Sparen;Alex Beispiel;1234567890;Umbuchung;Umbuchung\n"
            "Gutschrift;15.11.2026;;25,00 €;Sam Muster;;;Danke fürs Auslegen;Alex Beispiel;1234567890;;\n"
            "Lastschrift;14.11.2026;;-89,95 €;ZALANDO SE;;;Winterjacke;Alex Beispiel;1234567890;Kleidung;Bekleidung\n"
            "Gutschrift;31.10.2026;;2.950,00 €;Arbeitgeber GmbH;;;Gehalt Oktober;Alex Beispiel;1234567890;Einkommen;Gehalt\n")


def test_pocket_transfers_are_not_income(admin):
    p = bank.parse_csv(C24_LIKE.encode(), "Umsaetze.csv")
    assert p["mapping"]["kind"] == "Transaktionstyp" and p["rows"][0]["kind"] == "Pocket-Umbuchung"
    assert p["rows"][0]["bank_category"] == "Kleidung / Umbuchung"
    _imp(admin, C24_LIKE.encode(), account_name="C24")
    s = admin.get("/bank/summary").json()
    cats = {c["name"]: c["cents"] for c in s["categories"]}
    assert s["income"] == 295000                                   # nur das Gehalt, nicht die 186 € aus dem Pocket
    assert cats == {"Kleidung": 8995, "Nicht einsortiert": -2500}  # Gutschrift von Sam: zum Einsortieren
    open_ = admin.get("/bank/to_sort").json()["transactions"]
    assert [t["counterparty"] for t in open_] == ["Sam Muster"]


def test_back_and_forth_between_own_accounts(admin):
    a = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n10.11.2026;Alex Beispiel;Ferien;-300,00\n".encode()
    b = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n11.11.2026;Alex Beispiel;Ferien;300,00\n".encode()
    _imp(admin, a, account_name="Giro")
    r = _imp(admin, b, account_name="Tagesgeld").json()
    assert r["transfers"] == 2
    s = admin.get("/bank/summary").json()
    assert s["income"] == 0 and s["spent"] == 0


def test_rule_on_bank_category(admin):
    csv_ = ("Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kategorie;Unterkategorie\n"
            "Lastschrift;10.11.2026;-23,10 €;Hofladen Huber;Einkauf;Einkaufen;Wochenmarkt\n"
            "Lastschrift;11.11.2026;-9,80 €;Bauer Schmid;Eier;Einkaufen;Wochenmarkt\n"
            "Lastschrift;12.11.2026;-15,00 €;Verein XY;Beitrag;Mitgliedschaften;Vereine\n").encode()
    acc = _imp(admin, csv_, account_name="C24").json()["account_id"]
    open_ = {t["counterparty"]: t for t in admin.get("/bank/to_sort").json()["transactions"]}
    assert open_["Hofladen Huber"]["bank_category"] == "Einkaufen / Wochenmarkt"
    r = admin.patch(f"/bank/tx/{open_['Hofladen Huber']['id']}",
                    json={"category": "Lebensmittel", "remember_bank": True}).json()
    assert r["applied"] == 1                                       # Bauer Schmid gleich mit
    assert [t["counterparty"] for t in admin.get("/bank/to_sort").json()["transactions"]] == ["Verein XY"]
    more = "Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kategorie;Unterkategorie\n" \
           "Lastschrift;15.11.2026;-4,20 €;Gärtnerei;Salat;Einkaufen;Wochenmarkt\n"
    _imp(admin, more.encode(), account_id=acc)
    assert [t["counterparty"] for t in admin.get("/bank/to_sort").json()["transactions"]] == ["Verein XY"]
    assert any(x["pattern"] == "kat:einkaufen / wochenmarkt" for x in admin.get("/bank/rules").json())


# C24-Export mit allen Konten (erfunden): hinter dem Zweck Name, Kontonummer und Name des eigenen Kontos – die
# Überschriften kennen wir nicht sicher, deshalb hier absichtlich unbekannte („Feld 9“ …)
def _c24_all(rows):
    head = ("Transaktionstyp;Buchungsdatum;Wertstellung;Betrag;Zahlungsempfänger;IBAN;BIC;Verwendungszweck;"
            "Feld 9;Feld 10;Feld 11;Kategorie;Unterkategorie")
    return (head + "\n" + "\n".join(rows) + "\n").encode()


ALEX = _c24_all([
    "Lastschrift;12.11.26;;-40,00 €;REWE Markt;;;Karte;;1000000001;Gemeinschaftskonto;Lebensmittel;Supermarkt",
    "Pocket-Umbuchung;13.11.26;;-200,00 €;Gemeinschaftskonto;;;Reicht sonst nicht;;2000000002;Urlaub;Umbuchung;Umbuchung",
    "Pocket-Umbuchung;13.11.26;;200,00 €;Urlaub;;;Reicht sonst nicht;Alex Beispiel;1000000001;Gemeinschaftskonto;Umbuchung;Umbuchung",
])
SAM = _c24_all([
    "Lastschrift;12.11.26;;-40,00 €;REWE Markt;;;Karte;;1000000001;Gemeinschaftskonto;Lebensmittel;Supermarkt",
    "Lastschrift;10.11.26;;-15,00 €;Buchladen;;;Roman;;3000000003;Sams Sparkonto;Freizeit;Bücher",
    "Lastschrift;14.11.26;;-30,00 €;dm-drogerie;;;Karte;;1000000001;Gemeinschaftskonto;Drogerie;Drogerie",
])


def test_accounts_from_columns_merge_between_partners_privately(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    p = bank.parse_csv(ALEX)
    assert p["rows"][1]["acc_no"] == "2000000002" and p["rows"][1]["acc_name"] == "Urlaub"
    prev = _imp(admin, ALEX, preview="true").json()
    keys = {a["name"]: a["key"] for a in prev["accounts"]}
    assert set(keys) == {"Gemeinschaftskonto", "Urlaub"} and not any(a["existing"] for a in prev["accounts"])
    import json as _j
    r = _imp(admin, ALEX, visible_to=sam.id, shares=_j.dumps({keys["Urlaub"]: False})).json()
    assert r["new"] == 3
    # Sam sieht das gemeinsame Konto, nicht „Urlaub“
    names = {a["name"] for a in sam.get("/bank/accounts").json()["accounts"]}
    assert names == {"Gemeinschaftskonto"}
    # Sams Export: das gemeinsame Konto wird ergänzt (REWE schon da), ihr Sparkonto neu und privat
    prev = _imp(sam, SAM, preview="true").json()
    acc = {a["name"]: a for a in prev["accounts"]}
    assert acc["Gemeinschaftskonto"]["existing"] and acc["Gemeinschaftskonto"]["new"] == 1
    assert not acc["Sams Sparkonto"]["existing"]
    r = _imp(sam, SAM, visible_to=admin.id, shares=_j.dumps({acc["Sams Sparkonto"]["key"]: False})).json()
    assert r["new"] == 2 and r["duplicates"] == 1
    assert {a["name"] for a in admin.get("/bank/accounts").json()["accounts"]} == {"Gemeinschaftskonto", "Urlaub"}
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM bank_tx WHERE counterparty='REWE Markt'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM bank_accounts WHERE number_hash IS NOT NULL").fetchone()[0] == 3
        assert "1000000001" not in str(con.execute("SELECT * FROM bank_accounts").fetchall())   # nur Hash + Endziffern
    # Rücknehmen nimmt den ganzen Import des Partners zurück, sein leeres Sparkonto auch
    assert sam.delete(f"/bank/imports/{r['import_id']}").json()["removed"] == 2
    assert {a["name"] for a in sam.get("/bank/accounts").json()["accounts"]} == {"Gemeinschaftskonto"}


def test_subscriptions_not_leisure_and_recategorize_all_from_merchant(admin):
    csv_ = ("Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kategorie;Unterkategorie\n"
            "Kartenzahlung;15.11.2026;-11,99 €;Adobe;Creative Cloud;Freizeit;Software\n"
            "Kartenzahlung;13.11.2026;-0,99 €;APPLE;App;Freizeit;Apps\n"
            "Kartenzahlung;04.11.2026;-3,99 €;WL *STEAM PURCHASE;Spiel;Freizeit;Spiele\n"
            "Kartenzahlung;09.11.2026;-24,00 €;Kino am Markt;Kino;Freizeit;Kino\n"
            "Kartenzahlung;08.11.2026;-12,00 €;Kiosk Müller;Zeitung;Freizeit;Sonstiges\n"
            "Kartenzahlung;07.11.2026;-8,00 €;Kiosk Müller;Zeitung;Freizeit;Sonstiges\n").encode()
    _imp(admin, csv_, account_name="C24")
    k = admin.get("/bank/category", params={"name": "Abos"}).json()
    assert {t["counterparty"] for t in k["transactions"]} == {"Adobe", "APPLE"}
    f = admin.get("/bank/category", params={"name": "Freizeit & Ausgehen"}).json()["transactions"]
    steam = next(t for t in f if "STEAM" in t["counterparty"])
    assert steam["key"] == "steam"
    kiosk = [t for t in f if t["counterparty"] == "Kiosk Müller"]
    r = admin.patch(f"/bank/tx/{kiosk[0]['id']}", json={"category": "Haushalt", "remember": kiosk[0]["key"]}).json()
    assert r["applied"] == 1                       # die zweite Kiosk-Buchung (von der Bank einsortiert) mit
    assert len(admin.get("/bank/category", params={"name": "Haushalt"}).json()["transactions"]) == 2


def test_recategorize_keeps_manual(admin):
    _imp(admin, account_name="Giro")
    with connect() as con:
        tid = con.execute("SELECT id FROM bank_tx WHERE counterparty='REWE Markt' LIMIT 1").fetchone()[0]
        con.execute("UPDATE bank_tx SET category='Kinder', cat_source='user' WHERE id=?", (tid,))
        con.execute("UPDATE bank_tx SET category='Sonstiges', cat_source='bank' WHERE counterparty='ZALANDO SE'")
        n = bank.recategorize(con, [r[0] for r in con.execute("SELECT id FROM bank_accounts")])
        assert n >= 2
        assert con.execute("SELECT category FROM bank_tx WHERE id=?", (tid,)).fetchone()[0] == "Kinder"
        assert {r[0] for r in con.execute("SELECT category FROM bank_tx WHERE counterparty='ZALANDO SE'")} == {"Kleidung"}


def test_recategorize_runs_on_startup(tmp_path, monkeypatch):
    import sqlite3 as sq
    from app import db
    path = tmp_path / "alt.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    con = sq.connect(path)
    con.execute("INSERT INTO users(username, display_name, password_hash) VALUES ('x','X','h')")
    con.execute("INSERT INTO bank_accounts(name, owner_id) VALUES ('C24', 1)")
    con.execute("INSERT INTO bank_tx(account_id, booked, cents, counterparty, category, cat_source, fp, bank_category) "
                "VALUES (1, '2026-11-15', -1199, 'Adobe', 'Freizeit & Ausgehen', 'bank', 'a', 'Freizeit')")
    con.execute("DELETE FROM settings WHERE key='bank_recat_v1'")
    con.commit(); con.close()
    db.init_db()
    con = sq.connect(path)
    assert con.execute("SELECT category, cat_source FROM bank_tx").fetchone() == ("Abos", "builtin")


def test_search_sentence_finds_bookings_with_total(admin, sam):
    _imp(admin, account_name="Giro")
    r = admin.get("/bank/search", params={"q": "Zalando November 2026"}).json()
    assert r["range"] == {"from": "2026-11-01", "to": "2026-11-30"} and r["words"] == ["zalando"]
    assert r["count"] == 1 and r["cents"] == 8995
    r = admin.get("/bank/search", params={"q": "zalando 2026"}).json()
    assert r["count"] == 2 and r["cents"] == 8995 - 4995               # Rücksendung zieht ab
    assert admin.get("/bank/search", params={"q": "Kleidung"}).json()["count"] == 4   # auch nach Kategorie
    assert admin.get("/bank/search", params={"q": "was haben wir bei REWE ausgegeben"}).json()["cents"] == 2400
    assert sam.get("/bank/search", params={"q": "zalando"}).json()["count"] == 0      # fremdes Konto: nichts
    assert admin.get("/bank/search", params={"q": "September 2026"}).json()["count"] == 0   # nur Zeitraum: keine Liste


def test_online_shopping_bucket():
    def cat(cp, purpose="", cents=-2000):
        return bank.categorize({"counterparty": cp, "purpose": purpose, "cents": cents}, [])[0]
    assert cat("AMAZON EU S.A R.L.", "305-1234567 AMZN Mktp DE") == "Online-Shopping"
    assert cat("PayPal Europe", "Ihr Einkauf bei eBay") == "Online-Shopping"
    assert cat("Otto GmbH") == "Online-Shopping" and cat("TEMU.COM") == "Online-Shopping"
    assert cat("Amazon Prime", "Mitgliedschaft") == "Abos"                       # Abo geht vor
    assert cat("ZALANDO SE") == "Kleidung"                                        # wofür, nicht wo
    assert bank.categorize({"counterparty": "Amazon", "purpose": "", "cents": -999},
                           [("amazon", "Haushalt")])[0] == "Haushalt"             # eigene Regel geht vor
    assert "Online-Shopping" in bank.CATEGORIES


def test_interest_credit_is_income():
    assert bank.categorize({"cents": 1840, "counterparty": "Beispielbank", "purpose": "Zinsen"}, []) == ("Einnahmen", "builtin")
    assert bank.categorize({"cents": -1840, "counterparty": "Beispielbank", "purpose": "Sollzinsen"}, [])[0] != "Einnahmen"


def test_transfer_pairing_stays_fast_with_many_equal_amounts(admin):
    """Präparierte Datei: Tausende gleiche Beträge hin und zurück am selben Tag – paart linear, nicht jeder mit jedem."""
    import time
    head = "Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kontonummer;Kontoname"
    rows = [f"Lastschrift;05.11.2026;-1,00 €;Wer;Zweck {i};1000000001;Giro" for i in range(3000)]
    rows += [f"Gutschrift;05.11.2026;1,00 €;Wer;Zweck {i};2000000002;Topf" for i in range(3000)]
    t = time.monotonic()
    r = _imp(admin, ("\n".join([head, *rows]) + "\n").encode(), shares="{}")
    assert r.status_code == 200, r.text
    assert time.monotonic() - t < 20
    assert r.json()["transfers"] == 6000
    s = admin.get("/bank/summary").json()
    assert s["spent"] == 0 and s["income"] == 0                           # alles Umbuchung


def test_import_row_and_account_limits(admin, monkeypatch):
    monkeypatch.setattr(bank, "MAX_ROWS", 5)
    lines = ["Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag"] + [f"0{i}.11.2026;A;B;-1,00" for i in range(1, 8)]
    r = _imp(admin, ("\n".join(lines) + "\n").encode(), account_name="Giro")
    assert r.status_code == 400 and "Mehr als 5" in r.json()["detail"]
    monkeypatch.setattr(bank, "MAX_ROWS", 20000)
    monkeypatch.setattr(bank, "MAX_FILE_ACCOUNTS", 2)
    head = "Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kontonummer;Kontoname"
    rows = [f"05.11.2026;-1,00 €;A;B;{i}00000000{i};Konto {i}" for i in range(1, 4)]
    r = _imp(admin, ("\n".join([head, *rows]) + "\n").encode(), shares="{}")
    assert r.status_code == 400 and "Konten" in r.json()["detail"]
