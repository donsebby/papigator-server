"""Willkommen/„Was ist neu“: neue Konten sehen die Tour, danach nichts bis zur nächsten Ausgabe; bestehende Konten beim
Einführen die Neuerungen; App-Karten nur in der App; in der Demo nur die Tour."""
from app import whatsnew


def test_tour_then_nothing(admin, sam):
    w = admin.get("/whatsnew").json()
    assert w["mode"] == "tour" and len(w["cards"]) == len(whatsnew.TOUR)
    assert all(c["title"] and c["text"] and c["anim"] for c in w["cards"])
    assert admin.post("/whatsnew/seen").status_code == 204
    assert admin.get("/whatsnew").json()["mode"] is None
    assert sam.get("/whatsnew").json()["mode"] == "tour"          # pro Konto, nicht global
    # auf Wunsch (Einstellungen) trotzdem
    again = admin.get("/whatsnew?show=news&client=web").json()
    assert again["mode"] == "news" and again["label"]
    assert admin.get("/whatsnew?show=tour").json()["mode"] == "tour"


def test_existing_accounts_get_news_and_app_only_cards(admin):
    from app.db import connect, init_db
    with connect() as con:
        con.execute("DROP TABLE whatsnew_seen")
    init_db()                                                   # wie beim Update: Konto gab es schon
    web = admin.get("/whatsnew?client=web").json()
    ios = admin.get("/whatsnew?client=ios&lang=en").json()
    assert web["mode"] == ios["mode"] == "news"
    assert "appicon" not in {c["anim"] for c in web["cards"]} and "appicon" in {c["anim"] for c in ios["cards"]}
    assert ios["cards"][0]["title"] != web["cards"][0]["title"]   # englisch
    admin.post("/whatsnew/seen")
    assert admin.get("/whatsnew?client=ios").json()["mode"] is None


def test_news_includes_everything_since_last_seen(admin, monkeypatch):
    admin.post("/whatsnew/seen")
    newer = {"id": "2999-01-01", "label": {"de": "Neu", "en": "New"},
             "cards": [{"anim": "x", "de": ("Neu", "Neu."), "en": ("New", "New.")}]}
    monkeypatch.setattr(whatsnew, "RELEASES", [newer] + whatsnew.RELEASES)
    monkeypatch.setattr(whatsnew, "LATEST", newer["id"])
    w = admin.get("/whatsnew").json()
    assert w["mode"] == "news" and [c["anim"] for c in w["cards"]] == ["x"]


def test_demo_only_tour(admin, monkeypatch):
    from app import config
    from app.db import connect
    monkeypatch.setattr(config, "DEMO", True)
    assert admin.get("/whatsnew").json()["mode"] == "tour"
    assert admin.post("/whatsnew/seen").status_code == 204
    with connect() as con:
        con.execute("UPDATE whatsnew_seen SET seen='0'")
    assert admin.get("/whatsnew").json()["mode"] is None
