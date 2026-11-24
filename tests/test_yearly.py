"""Jährliche Kosten: Reihen aus Kontoumsätzen (auch zwei Verträge beim selben Versicherer, Preiserhöhung, Abbuchung
über den Jahreswechsel), keine Abos/Einkäufe, von Hand markieren, Belege ohne Doppelzählung, Rücklagen-Topf mit
Monatsrate, Rechte und Aufräumen beim Löschen. Daten relativ zu heute, damit die Tests nicht altern."""
import datetime as dt

from app.db import connect

T = dt.date.today()


def day(days_ago: int) -> str:
    return (T - dt.timedelta(days=days_ago)).strftime("%d.%m.%Y")


def eur(cents: int) -> str:
    return f"{cents / 100:.2f}".replace(".", ",")


def _csv(rows) -> bytes:
    lines = ["Buchungsdatum;Wertstellung;Zahlungsempfänger;Verwendungszweck;Betrag"]
    lines += [f"{day(a)};{day(a)};{who};{why};{eur(c)}" for a, who, why, c in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _imp(api, rows, name):
    r = api.post("/bank/import", files={"file": ("x.csv", _csv(rows), "text/csv")}, data={"account_name": name})
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


GIRO = [
    # zwei Verträge beim selben Versicherer, je zweimal im Jahresabstand; Kfz teurer geworden
    (40 + 365, "Hanse Kraft Versicherung AG", "Kfz-Versicherung Beitrag", -65580),
    (40, "Hanse Kraft Versicherung AG", "Kfz-Versicherung Beitrag", -70120),
    (250 + 365, "Hanse Kraft Versicherung AG", "Privathaftpflicht", -6890),
    (250, "Hanse Kraft Versicherung AG", "Privathaftpflicht", -7410),
    # Jahresabo über PayPal
    (100 + 366, "PayPal Europe", "PP.1234.PP . Sony Interactive, Ihr Einkauf bei Sony", -7199),
    (100, "PayPal Europe", "PP.1234.PP . Sony Interactive, Ihr Einkauf bei Sony", -7999),
    # Monatsabo: kein Jahresposten, obwohl es ein Jahr vorher denselben Betrag gab
    *[(30 * k + 5, "Netflix", "Abo", -1399) for k in range(14)],
    # nur einmal abgebucht (neu): erst nach Markieren dabei
    (20, "ADAC e.V.", "Mitgliedsbeitrag", -9400),
]


def _by_name(items):
    return {(i["name"], i["amount"]): i for i in items}


def test_series_from_bank_and_monthly_rate(admin):
    _imp(admin, GIRO, "Girokonto")
    r = admin.get("/bank/yearly").json()
    items = _by_name(r["items"])
    kfz = items[("Hanse Kraft Versicherung AG", 70120)]
    haft = items[("Hanse Kraft Versicherung AG", 7410)]
    ps = next(i for i in r["items"] if i["mkey"] == "sony")
    assert kfz["previous"] == 65580 and kfz["change_pct"] == 7 and kfz["active"] and kfz["label"] == "Kfz-Versicherung Beitrag"
    assert haft["previous"] == 6890 and haft["label"] == "Privathaftpflicht"
    assert ps["amount"] == 7999 and ps["previous"] == 7199
    assert ps["name"] == "Sony"                                          # Händler statt „PayPal Europe“
    assert not any(i["name"] == "Netflix" for i in r["items"])          # Monatsabo
    assert not any(i["name"].startswith("ADAC") for i in r["items"])   # erst eine Abbuchung
    assert r["totals"]["per_year"] == 70120 + 7410 + 7999
    assert r["totals"]["monthly_needed"] == round((70120 + 7410 + 7999) / 12)
    # nächste Abbuchungen in den kommenden 12 Monaten
    assert sum(m["cents"] for m in r["schedule"]) == 70120 + 7410 + 7999


def test_turn_of_the_year_and_year_columns(admin):
    _imp(admin, [(371 + 30, "Bundeskasse", "Kfz-Steuer", -12000), (30, "Bundeskasse", "Kfz-Steuer", -12000)], "Giro")
    it = admin.get("/bank/yearly").json()["items"][0]
    first, last = (T - dt.timedelta(days=401)), (T - dt.timedelta(days=30))
    want = {}
    for d in (first, last):
        want[str(d.year)] = want.get(str(d.year), 0) + 12000
    assert it["years"] == want and it["per_year"] == 12000


def test_mark_yearly_and_not_yearly(admin):
    acc = _imp(admin, GIRO, "Girokonto")
    with connect() as con:
        adac = con.execute("SELECT id FROM bank_tx WHERE counterparty='ADAC e.V.'").fetchone()[0]
        netflix = con.execute("SELECT id FROM bank_tx WHERE counterparty='Netflix' ORDER BY booked DESC").fetchone()[0]
        kfz = con.execute("SELECT id FROM bank_tx WHERE cents=-70120").fetchone()[0]
    assert admin.put("/bank/yearly/mark", json={"tx_id": adac, "yearly": True}).status_code == 200
    r = admin.get("/bank/yearly").json()
    adac_item = next(i for i in r["items"] if i["name"] == "ADAC e.V.")
    assert adac_item["source"] == "marked" and adac_item["per_year"] == 9400
    # Fehltreffer ausblenden, dann zurücknehmen
    admin.put("/bank/yearly/mark", json={"tx_id": kfz, "yearly": False})
    assert not any(i["amount"] == 70120 for i in admin.get("/bank/yearly").json()["items"])
    admin.put("/bank/yearly/mark", json={"tx_id": kfz, "yearly": None})
    assert any(i["amount"] == 70120 for i in admin.get("/bank/yearly").json()["items"])
    # Monatsabo von Hand dazu: zählt so oft, wie es im letzten Jahr kam
    admin.put("/bank/yearly/mark", json={"tx_id": netflix, "yearly": True})
    nf = next(i for i in admin.get("/bank/yearly").json()["items"] if i["name"] == "Netflix")
    assert nf["per_year"] == 1399 * 12
    assert acc


def test_pot_suggested_by_name_monthly_and_chosen(admin):
    _imp(admin, GIRO, "Girokonto")
    # Pocket „Jährlich“: jeden Monat 75 € rein (letzte drei volle Monate), einmal 700 € zurück aufs Giro
    first = T.replace(day=1)
    rows = []
    for k in range(1, 7):
        m = (first.month - 1 - k) % 12 + 1
        y = first.year + (first.month - 1 - k) // 12
        d = dt.date(y, m, 2)
        rows.append(((T - d).days, "Alex Muster", "Umbuchung Jährlich", 7500 if k <= 3 else 6000))
    rows.append((40, "Alex Muster", "Umbuchung aufs Giro", -70000))
    pot = _imp(admin, rows, "Jährlich")
    r = admin.get("/bank/yearly").json()
    assert r["pot"]["id"] == pot and not r["pot"]["chosen"] and r["pot"]["monthly"] == 7500
    giro = next(a["id"] for a in r["accounts"] if a["name"] == "Girokonto")
    assert admin.put("/bank/yearly/pot", json={"account_id": giro}).status_code == 200
    r = admin.get("/bank/yearly").json()
    assert r["pot"]["id"] == giro and r["pot"]["chosen"]
    # Konto weg → Wahl weg (Fremdschlüssel), wieder der Vorschlag nach Namen
    admin.delete(f"/bank/accounts/{giro}")
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM bank_pot").fetchone()[0] == 0
    assert admin.get("/bank/yearly").json()["pot"]["id"] == pot


def test_rights_only_visible_accounts(admin, sam):
    acc = _imp(admin, GIRO, "Girokonto")
    with connect() as con:
        tx = con.execute("SELECT id FROM bank_tx WHERE cents=-70120").fetchone()[0]
    r = sam.get("/bank/yearly").json()
    assert r["items"] == [] and r["pot"] is None and r["accounts"] == []
    assert sam.put("/bank/yearly/mark", json={"tx_id": tx, "yearly": True}).status_code == 404
    assert sam.put("/bank/yearly/pot", json={"account_id": acc}).status_code == 404
    # Markierungen gelten je Person: Alex blendet aus, Sam (mit Zugriff) sieht es weiter
    sid = sam.get("/auth/me").json()["id"]
    admin.put("/shares", json={"user_id": sid, "can_edit": False})   # mitsehen nur unter Partnern
    assert admin.patch(f"/bank/accounts/{acc}", json={"visible_to": [sid]}).status_code == 200
    admin.put("/bank/yearly/mark", json={"tx_id": tx, "yearly": False})
    assert any(i["amount"] == 70120 for i in sam.get("/bank/yearly").json()["items"])
    assert not any(i["amount"] == 70120 for i in admin.get("/bank/yearly").json()["items"])


def test_documents_counted_once(admin):
    _imp(admin, GIRO, "Girokonto")
    # Haftpflicht auch als Belege (jährlich): nicht verknüpft → steht da, zählt aber nicht zur Summe
    ids = []
    for a, c in ((250 + 730, 6490), (250 + 365, 6890), (250, 7410)):
        d = admin.upload(f"Beitragsrechnung Privathaftpflicht {a}", "Beitragsrechnung").json()["id"]
        with connect() as con:
            con.execute("UPDATE documents SET sender='Lindwurm Versicherung AG', amount=?, document_date=?, direction='out', "
                        "doc_kind='rechnung' WHERE id=?", (c / 100, (T - dt.timedelta(days=a)).isoformat(), d))
        ids.append(d)
    r = admin.get("/bank/yearly").json()
    doc = next(i for i in r["items"] if i["source"] == "document")
    assert not doc["in_sum"] and r["totals"]["per_year"] == 70120 + 7410 + 7999
    # mit einer Abbuchung der Reihe verknüpft → nur noch die Abbuchung
    with connect() as con:
        con.execute("UPDATE bank_tx SET document_id=? WHERE cents=-7410", (ids[-1],))
    r = admin.get("/bank/yearly").json()
    assert not any(i["source"] == "document" for i in r["items"])
    haft = next(i for i in r["items"] if i["amount"] == 7410)
    assert haft["document_id"] == ids[-1] and haft["label"] == "Beitragsrechnung"


def test_deleting_user_removes_marks_and_pot(admin, sam):
    acc = _imp(sam, GIRO, "Giro Sam")
    with connect() as con:
        tx = con.execute("SELECT id FROM bank_tx WHERE counterparty='ADAC e.V.'").fetchone()[0]
    sam.put("/bank/yearly/mark", json={"tx_id": tx, "yearly": True})
    sam.put("/bank/yearly/pot", json={"account_id": acc})
    sid = sam.get("/auth/me").json()["id"]
    assert admin.delete(f"/users/{sid}").status_code == 204
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM bank_yearly_marks").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM bank_pot").fetchone()[0] == 0


def test_document_with_same_payee_and_amount_joins_the_series(admin):
    """Rechnung ohne verknüpfte Abbuchung, aber gleicher Absender und Betrag wie eine Reihe: kein zweiter Posten."""
    _imp(admin, GIRO, "Girokonto")
    for a, c in ((40 + 730, 61240), (40 + 365, 65580), (40, 70120)):
        d = admin.upload(f"Beitragsrechnung Kfz {a}", "Beitragsrechnung Kfz").json()["id"]
        with connect() as con:
            con.execute("UPDATE documents SET sender='Hanse Kraft Versicherung AG', amount=?, document_date=?, "
                        "direction='out', doc_kind='rechnung' WHERE id=?", (c / 100, (T - dt.timedelta(days=a + 3)).isoformat(), d))
    r = admin.get("/bank/yearly").json()
    assert not any(i["source"] == "document" for i in r["items"])
    kfz = next(i for i in r["items"] if i["amount"] == 70120)
    assert kfz["document_id"] and kfz["label"] == "Kfz-Versicherung Beitrag"


def test_withdrawals_from_pot_are_yearly_items(admin):
    """PS Plus läuft über ein Konto, das Papigator nicht kennt; ausgeglichen wird aus dem Topf „Jährlich“. Entnahmen
    zählen als Jahresposten – außer sie gehören zu einer schon erkannten Abbuchung (dann nur „aus dem Topf“)."""
    _imp(admin, GIRO, "Girokonto")
    pot_rows = [
        (170 + 365, "Alex Muster", "PS Plus", -7199), (170, "Alex Muster", "PS Plus", -7999),   # nicht beim Sony-Abo
        (38, "Alex Muster", "Umbuchung Kfz", -70120),                  # gehört zur Kfz-Abbuchung vom Giro
        (60, "Alex Muster", "Domain papigator", -1500),                # nur einmal entnommen
        (10, "Alex Muster", "Korrektur", -2000),                       # Fehltreffer, wird ausgeblendet
        *[(30 * k + 2, "Alex Muster", "Umbuchung Jährlich", 7500) for k in range(1, 14)],
    ]
    _imp(admin, pot_rows, "Jährlich")
    r = admin.get("/bank/yearly").json()
    ps = next(i for i in r["items"] if i["name"] == "PS Plus")
    assert ps["source"] == "pot" and ps["amount"] == 7999 and ps["previous"] == 7199 and ps["change_pct"] == 11
    assert next(i for i in r["items"] if i["name"] == "Domain papigator")["per_year"] == 1500
    kfz = [i for i in r["items"] if i["amount"] == 70120]
    assert len(kfz) == 1 and kfz[0]["source"] == "bank" and kfz[0]["from_pot"]
    with connect() as con:
        fix = con.execute("SELECT id FROM bank_tx WHERE purpose='Korrektur'").fetchone()[0]
    assert admin.put("/bank/yearly/mark", json={"tx_id": fix, "yearly": False}).status_code == 200
    r = admin.get("/bank/yearly").json()
    assert not any(i["name"] == "Korrektur" for i in r["items"])
    assert r["totals"]["per_year"] == 70120 + 7410 + 7999 + 7999 + 1500   # Sony über PayPal und PS Plus aus dem Topf


def test_pot_spellings_and_double_withdrawal_count_once(admin):
    """„Sony PsPlus“ 2025 und 2026, dazu „Ps plus“ zwei Wochen nach der 2026er: ein Abo, einmal gezählt."""
    _imp(admin, [(170 + 365, "Sebbo", "Sony PsPlus", -12599), (170, "Sebbo", "Sony PsPlus", -12599),
                 (156, "Sebbo", "Ps plus", -12599),
                 *[(30 * k + 2, "Sebbo", "Umbuchung Jährlich", 9000) for k in range(1, 14)]], "Jährlich - PS5-Prime-ADAC")
    r = admin.get("/bank/yearly").json()
    ps = [i for i in r["items"] if "plus" in i["name"].lower()]
    assert len(ps) == 1 and ps[0]["previous"] == 12599 and r["totals"]["per_year"] == 12599


def test_ask_facts_only_for_yearly_questions(admin):
    from app import yearly
    from app.auth import User
    _imp(admin, GIRO, "Girokonto")
    me = User(admin.get("/auth/me").json()["id"], "alex", "Alex", True, False)
    f = yearly.ask_facts(me, "Wie viel müssen wir monatlich auf die Seite legen für jährliche Sachen?")
    assert "pro Jahr aktuell: 855,29 €" in f and "nötig im Monat: 71,27 €" in f
    assert "+7 % zum Vorjahr" in f and "[Übersicht]" in f
    assert yearly.ask_facts(me, "Wann war ich beim Zahnarzt?") == ""
