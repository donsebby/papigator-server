"""Aufbau wie die App (26.11.2026): Tab-Leiste am Handy, die Fristen-Zahl in „Dokumente“ und das Dokument als
Lesekarte mit direktem Bearbeiten. Läuft nach test_web.py (Dateiname), weil die Tests dort mit festen Dokumentzahlen
rechnen."""
import json
import re
from datetime import date, timedelta

from playwright.sync_api import expect

from e2e.test_web import _patch, _upload, hidden, shown

PHONE = {"width": 390, "height": 844}


def _no_sideways_scroll(page):
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_phone_tab_bar_switches_views(page):
    """390 px: Tab-Leiste unten statt Umschalter oben; Gruß nur auf „Heute“, sonst der Name des Tabs; „Dokumente“
    merkt sich Kacheln oder Ordner; kein seitliches Scrollen; breit keine Leiste."""
    hidden(page, "#tabbar")                                         # 1280 px: Navigation oben wie bisher
    shown(page, "#mode-seg")
    page.set_viewport_size(PHONE)
    page.reload()
    page.wait_for_selector(".card")
    bar = page.locator("#tabbar")
    shown(page, "#tabbar")
    hidden(page, "#mode-seg")
    hidden(page, '.topbar label[for="file-input"]')                  # Hochladen steckt in „Ablegen“
    expect(bar.locator('[data-tab="docs"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#hello-tab")).to_have_text("Dokumente")
    hidden(page, "#hello-title")
    _no_sideways_scroll(page)
    # Leiste liegt unten, ganz im Bild
    box = bar.bounding_box()
    assert box["y"] + box["height"] <= PHONE["height"] and box["y"] > PHONE["height"] - 120, box

    bar.locator('[data-tab="today"]').click()
    shown(page, "#todayview")
    shown(page, "#hello-title")
    hidden(page, "#hello-tab")
    expect(bar.locator('[data-tab="today"]')).to_have_attribute("aria-current", "page")
    _no_sideways_scroll(page)

    bar.locator('[data-tab="money"]').click()
    shown(page, "#moneyview")
    expect(page.locator("#hello-tab")).to_have_text("Geld")
    _no_sideways_scroll(page)

    bar.locator('[data-tab="family"]').click()
    shown(page, "#familyview")
    expect(page.locator("#hello-tab")).to_have_text("Familie")
    _no_sideways_scroll(page)

    # Dokumente: Kacheln/Ordner innerhalb des Tabs, gemerkt beim nächsten Besuch
    bar.locator('[data-tab="docs"]').click()
    expect(page.locator("#grid .card").first).to_be_visible()
    shown(page, "#docs-seg")
    page.click('#docs-seg button[data-mode="folders"]')
    shown(page, "#folderview")
    bar.locator('[data-tab="today"]').click()
    shown(page, "#todayview")
    bar.locator('[data-tab="docs"]').click()
    shown(page, "#folderview")
    expect(page.locator('#docs-seg button[data-mode="folders"]')).to_have_class("active")
    page.click('#docs-seg button[data-mode="tiles"]')
    expect(page.locator("#grid .card").first).to_be_visible()
    _no_sideways_scroll(page)

    # „Ablegen“ öffnet die Dateiauswahl
    with page.expect_file_chooser() as fc:
        page.click("#tab-add")
    assert fc.value.is_multiple()
    page.evaluate("localStorage.setItem('bl.mode', 'tiles'); localStorage.removeItem('bl.docsMode')")


def test_phone_tab_bar_back_and_overlays(page):
    """Dokument und Einstellungen ohne Tab-Leiste; Zurück aus dem Dokument führt in den Tab, aus dem es kam.
    Ein Tab-Wechsel schließt Fragen und Papierkorb; derselbe Tab noch einmal leert die Suche."""
    page.set_viewport_size(PHONE)
    page.reload()
    page.wait_for_selector(".card")
    bar = page.locator("#tabbar")
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    hidden(page, "#tabbar")
    page.go_back()
    shown(page, "#grid")
    shown(page, "#tabbar")

    bar.locator('[data-tab="today"]').click()
    shown(page, "#todayview")
    page.locator("#todayview .t-doc").first.click()
    shown(page, "#detail")
    page.go_back()
    shown(page, "#todayview")
    shown(page, "#tabbar")

    page.click("#settings-btn")
    shown(page, "#settings")
    hidden(page, "#tabbar")
    page.go_back()
    shown(page, "#main")
    shown(page, "#tabbar")

    # Fragen ist eine Ebene: der Tab-Wechsel schließt sie samt Verlaufseintrag
    page.route(re.compile(r"/ask$"), lambda route: route.fulfill(status=200, content_type="application/json",
                                                                 body='{"answer": "42", "sources": [], "keywords": []}'))
    page.evaluate("askNow('Wie hoch ist die Grundsteuer?')")
    shown(page, "#ask-panel")
    depth = page.evaluate("history.length")
    bar.locator('[data-tab="money"]').click()
    shown(page, "#moneyview")
    hidden(page, "#ask-panel")
    assert page.evaluate("history.state && history.state.layer") in (None, False)
    assert page.evaluate("history.length") == depth

    # Papierkorb schließt beim Tab-Wechsel
    bar.locator('[data-tab="docs"]').click()
    page.click("#trash-btn")
    shown(page, "#trash-bar")
    bar.locator('[data-tab="today"]').click()
    shown(page, "#todayview")
    hidden(page, "#trash-bar")
    bar.locator('[data-tab="docs"]').click()
    hidden(page, "#trash-bar")
    expect(page.locator(".card", has_text="Altes Schreiben")).to_have_count(0)

    # Derselbe Tab noch einmal: Suche weg, wieder alle Dokumente
    page.fill("#search", "Grundsteuer")
    expect(page.locator(".card")).to_have_count(1)
    bar.locator('[data-tab="docs"]').click()
    expect(page.locator("#search")).to_have_value("")
    expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
    # Die Leiste verdeckt nichts: unten ist Platz für sie
    pad = page.evaluate("parseFloat(getComputedStyle(document.querySelector('#main')).paddingBottom)")
    assert pad >= 80, pad


def test_deadline_chip_counts_the_same_as_its_list(page):
    """„Offene Fristen · n“ in den Kacheln zählt alle offenen Fristen – genau die Dokumente, die der Klick zeigt,
    auch solche in mehr als 30 Tagen."""
    soon = _upload(page, "Frist bald")
    later = _upload(page, "Frist in drei Monaten")
    _patch(page, soon, {"due_date": (date.today() + timedelta(days=5)).isoformat()})
    _patch(page, later, {"due_date": (date.today() + timedelta(days=90)).isoformat()})
    page.reload()
    page.wait_for_selector(".card")
    chip = page.locator("#tagbar .chip", has_text="Offene Fristen")
    expect(chip).to_be_visible()
    n = int(chip.inner_text().split("·")[1].split("(")[0].strip())
    chip.click()
    expect(page.locator(".card", has_text="Frist in drei Monaten")).to_be_visible()
    expect(page.locator(".card")).to_have_count(n)
    for doc in (soon, later):
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")


# ---------- Dokument als Lesekarte: direkt bearbeiten, sofort speichern ----------
def _open(page, title):
    page.fill("#search", title)
    page.locator(".card", has_text=title).first.click()
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value(title)


def _drop(page, *ids):
    for doc in ids:
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")


def test_detail_inline_edit_saves_each_field_without_button(page):
    """Kein Speichern-Knopf: jedes Feld schickt beim Verlassen nur sich selbst (PATCH), in der Reihenfolge der
    Änderungen; Verlassen ohne Änderung schickt nichts; Tags speichern sofort."""
    doc = _upload(page, "Lesekarte Test", "2026-03-04")
    page.reload()
    page.wait_for_selector(".card")
    bodies = []
    page.on("request", lambda r: bodies.append(r.post_data_json) if r.method == "PATCH" and f"/documents/{doc}" in r.url else None)
    _open(page, "Lesekarte Test")
    expect(page.locator("#save-btn")).to_have_count(0)
    hidden(page, "#m-more .d-more-body")                              # Seltenes zu
    page.click("#m-title")
    page.press("#m-title", "Enter")                                    # betreten und verlassen: nichts
    page.fill("#m-sender", "Stadtwerke Muster")
    page.press("#m-sender", "Tab")
    page.fill("#m-amount", "12,50")
    page.press("#m-amount", "Enter")
    page.select_option("#m-kind", "rechnung")
    page.fill("#m-title", "Lesekarte geändert")
    page.press("#m-title", "Enter")
    page.fill("#m-tag-input", "Lesetag")
    page.press("#m-tag-input", "Enter")
    expect(page.locator("#m-tags .chip", has_text="Lesetag")).to_have_count(1)
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    assert bodies == [{"sender": "Stadtwerke Muster"}, {"amount": 12.5}, {"doc_kind": "rechnung"},
                      {"title": "Lesekarte geändert"}, {"tags": ["Lesetag"]}], bodies
    expect(page.locator("#m-amount")).to_have_value(re.compile(r"^12,50\s€$"))
    # Tag wieder weg – ✕ speichert sofort
    page.locator("#m-tags .chip", has_text="Lesetag").locator(".x").click()
    expect(page.locator("#m-tags .chip")).to_have_count(0)
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    assert bodies[-1] == {"tags": []}
    # Ein Feld, das beim Schließen noch den Fokus hat, wird vorher gespeichert
    page.fill("#m-sender", "Stadtwerke Beispiel")
    page.go_back()
    shown(page, "#main")
    page.wait_for_function("document.querySelector('#save-state').textContent === 'Gespeichert'")
    d = _patch(page, doc, {})
    assert (d["title"], d["sender"], d["amount"], d["doc_kind"]) == ("Lesekarte geändert", "Stadtwerke Beispiel", 12.5, "rechnung")
    page.fill("#search", "")
    _drop(page, doc)


def test_detail_save_error_is_visible_and_restores(page):
    """Lehnt der Server ab: Meldung in der Karte, das Feld steht wieder auf dem gespeicherten Wert; „Erneut versuchen“
    schickt die Änderung noch einmal."""
    doc = _upload(page, "Fehlerfall Test", "2026-03-05")
    _patch(page, doc, {"sender": "Alter Absender"})
    page.reload()
    page.wait_for_selector(".card")
    _open(page, "Fehlerfall Test")
    pattern = re.compile(rf"/documents/{doc}$")

    def fail(route):
        if route.request.method == "PATCH":
            route.fulfill(status=500, content_type="application/json", body='{"detail": "Server kaputt"}')
        else:
            route.continue_()
    page.route(pattern, fail)
    page.fill("#m-sender", "Neuer Absender")
    page.press("#m-sender", "Enter")
    shown(page, "#m-error")
    expect(page.locator("#m-error")).to_contain_text("Nicht gespeichert: Server kaputt")
    expect(page.locator("#m-sender")).to_have_value("Alter Absender")
    page.unroute(pattern)
    assert _patch(page, doc, {})["sender"] == "Alter Absender"
    page.click("#m-error-retry")
    hidden(page, "#m-error")
    expect(page.locator("#m-sender")).to_have_value("Neuer Absender")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    assert _patch(page, doc, {})["sender"] == "Neuer Absender"
    page.go_back()
    page.fill("#search", "")
    _drop(page, doc)


def test_detail_read_only_document(page):
    """Nur lesbar (fremdes Dokument ohne Schreibrecht): nichts änderbar, leere Angaben ausgeblendet, kein Löschen."""
    doc = _upload(page, "Nur lesen Test", "2026-03-06")
    _patch(page, doc, {"sender": "Gemeinde"})

    def read_only(route):
        resp = route.fetch()
        data = resp.json()
        data.update(can_edit=False, is_own=False, owner_name="Sam")
        route.fulfill(response=resp, body=json.dumps(data))
    page.route(re.compile(rf"/documents/{doc}\?include_text=true$"), read_only)
    page.reload()
    page.wait_for_selector(".card")
    _open(page, "Nur lesen Test")
    expect(page.locator("#m-owner")).to_contain_text("Dokument von Sam · nur lesen")
    for sel in ("#m-title", "#m-sender", "#m-date", "#m-kind", "#m-due-done", "#m-warranty"):
        expect(page.locator(sel)).to_be_disabled()
    expect(page.locator("#m-sender")).to_have_value("Gemeinde")
    hidden(page, "#m-amount-part")                                     # kein Betrag: in der Lesekarte weg
    hidden(page, "#m-due-box")                                         # keine Frist: keine Zeile
    hidden(page, "#m-tag-input")
    hidden(page, "#m-no-date")
    expect(page.locator("#delete-btn")).to_be_disabled()
    page.go_back()
    page.fill("#search", "")
    _drop(page, doc)


def test_detail_on_phone_card_first_with_pdf_preview(page):
    """390 px: zuerst die Karte, darunter das erste Blatt als Vorschau, die das PDF öffnet – kein eingebettetes PDF."""
    page.set_viewport_size(PHONE)
    page.reload()
    page.wait_for_selector(".card")
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    hidden(page, "#pdf-view")
    prev = page.locator("#pdf-preview")
    shown(page, "#pdf-preview")
    assert "/file" in prev.get_attribute("href")
    assert page.locator("#detail .meta").bounding_box()["y"] < prev.bounding_box()["y"]
    assert page.evaluate("!document.querySelector('#pdf-view').getAttribute('src')")
    _no_sideways_scroll(page)
    page.go_back()
    shown(page, "#main")


# ---------- Familie und Papierkorb wie in der App (Etappe 3) ----------
def test_trash_from_settings_and_header(page):
    """Papierkorb an festem Ort: Einstellungen › Dokumente › Papierkorb öffnet ihn in „Dokumente“ (Zurück führt zur
    Übersicht, nicht in die Einstellungen); im Kopf von „Dokumente“ ein Knopf mit Zahl; in keiner Chipreihe."""
    page.click('#mode-seg button[data-mode="today"]')
    expect(page.locator(".chip", has_text="Papierkorb")).to_have_count(0)
    page.click("#settings-btn")
    page.locator("#s-trash-sect summary").click()
    expect(page.locator("#s-trash-text")).to_contain_text("im Papierkorb")
    page.click("#s-trash-open")
    hidden(page, "#settings")
    shown(page, "#trash-bar")
    expect(page.locator(".card", has_text="Altes Schreiben")).to_be_visible()
    expect(page.locator('#mode-seg button[data-mode="tiles"]')).to_have_class(re.compile("active"))
    expect(page.locator("#trash-btn-count")).not_to_have_text("")
    page.go_back()
    hidden(page, "#trash-bar")
    hidden(page, "#settings")
    shown(page, "#main")
    expect(page.locator(".chip", has_text="Papierkorb")).to_have_count(0)


def test_phone_scope_switch_sits_under_documents_title(page):
    """390 px: Meine/Partner unter dem Titel „Dokumente“ (wie die App), nicht oben im Kopf; auf Heute nicht zu sehen."""
    page.route(re.compile(r"/auth/me$"), lambda route: route.fulfill(response=route.fetch(), body=json.dumps(
        {**route.fetch().json(), "shared_with_me": [{"id": 999, "display_name": "Sam", "can_edit": True}]})))
    page.set_viewport_size(PHONE)
    page.reload()
    page.wait_for_selector("#main:not(.hidden)")
    page.locator('#tabbar [data-tab="docs"]').click()
    shown(page, "#scope-slot #scopebar")
    expect(page.locator(".topbar #scopebar")).to_have_count(0)
    title = page.locator("#hello-tab").bounding_box()
    seg = page.locator("#scope-seg").bounding_box()
    assert seg["y"] > title["y"], (seg, title)
    _no_sideways_scroll(page)
    page.locator('#tabbar [data-tab="today"]').click()
    hidden(page, "#scopebar")
    page.set_viewport_size({"width": 1280, "height": 860})
    expect(page.locator(".topbar #scopebar")).to_have_count(1)


# ---------- Ein Feld für Suchen und Fragen (Etappe 4) ----------
def test_one_field_for_search_and_ask(page):
    """Kein eigener Fragen-Knopf: das Suchfeld zeigt Treffer und darunter „Papigator fragen“. Die KI fragt es nur bei
    Enter auf eine Frage (Fragewort vorne oder „?“) oder beim Antippen – nie beim Tippen; lang geht mit."""
    asks = []

    def handle(route):
        asks.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"answer": "Die Grundsteuer beträgt 312 € [Dok 1].", "keywords": ["Grundsteuer"], "lang": "de",
             "sources": [{"n": 1, "id": 0, "title": "Grundsteuer", "document_date": None}]}))
    page.route(re.compile(r"/ask$"), handle)
    expect(page.locator("#ask-btn")).to_have_count(0)
    hidden(page, "#ask-offer")
    # Stichwort: Treffer, Angebot zum Fragen, aber keine Frage an die KI – auch nicht mit Enter
    page.fill("#search", "Grundsteuer")
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    shown(page, "#ask-offer")
    expect(page.locator("#ask-offer")).to_contain_text("„Grundsteuer“")
    expect(page.locator("#ask-offer")).not_to_have_class(re.compile("question"))
    page.press("#search", "Enter")
    page.wait_for_timeout(600)
    assert asks == []
    hidden(page, "#ask-panel")
    # Antippen fragt ausdrücklich
    page.click("#ask-offer")
    shown(page, "#ask-panel")
    expect(page.locator("#ask-question")).to_have_text("Grundsteuer")
    expect(page.locator("#ask-answer")).to_contain_text("312 €")
    assert asks == [{"question": "Grundsteuer", "scope": "all", "lang": "de"}], asks
    hidden(page, "#ask-offer")                                         # dieselbe Frage steht schon da
    page.keyboard.press("Escape")
    hidden(page, "#ask-panel")
    # Eine Frage: beim Tippen nichts, Enter fragt
    page.fill("#search", "Wie hoch ist die Grundsteuer")
    expect(page.locator("#ask-offer")).to_have_class(re.compile("question"))
    page.wait_for_timeout(800)
    assert len(asks) == 1
    page.press("#search", "Enter")
    expect(page.locator("#ask-question")).to_have_text("Wie hoch ist die Grundsteuer")
    assert asks[-1]["question"] == "Wie hoch ist die Grundsteuer" and asks[-1]["lang"] == "de"
    # Feld leeren schließt die Antwort samt Verlaufseintrag
    page.fill("#search", "")
    hidden(page, "#ask-panel")
    hidden(page, "#ask-offer")
    page.wait_for_function("!(history.state && history.state.layer)")


# ---------- Fotos hochladen (Funktion c) ----------
def _jpeg(color) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (200, 280), color).save(buf, "JPEG")
    return buf.getvalue()


def _ids(page) -> set:
    return set(page.evaluate("fetch('/documents?scope=all&limit=500').then(r => r.json()).then(d => d.map(x => x.id))"))


def test_upload_photos_as_one_document_and_heic_hint(page):
    """Fotos: die Auswahl nimmt PDF und Bilder; mehrere Fotos gehen in EINEM Aufruf (→ eine mehrseitige PDF), PDFs
    einzeln; HEIC wird mit Hinweis übersprungen; Fehler des Servers stehen mit Text da."""
    from e2e.conftest import make_pdf
    assert page.get_attribute("#file-input", "accept") == "application/pdf,image/jpeg,image/png,image/tiff"
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/documents") else None)
    before = _ids(page)
    page.set_input_files("#file-input", files=[
        {"name": "seite1.jpg", "mimeType": "image/jpeg", "buffer": _jpeg("red")},
        {"name": "seite2.jpg", "mimeType": "image/jpeg", "buffer": _jpeg("green")},
        {"name": "brief.pdf", "mimeType": "application/pdf", "buffer": make_pdf("Brief zu den Fotos")},
        {"name": "IMG_0003.HEIC", "mimeType": "image/heic", "buffer": b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40},
    ])
    expect(page.locator("#toast")).to_contain_text(re.compile("Fotos als ein Dokument mit 2 Seiten|HEIC"))
    page.wait_for_function("document.querySelector('#file-input').value === ''")
    assert len(posts) == 2, len(posts)                             # 1× PDF, 1× beide Fotos – das HEIC gar nicht
    new = _ids(page) - before
    assert len(new) == 2, new
    pages = sorted(len(re.findall(rb"/Type\s*/Page\b", page.request.get(f"/documents/{d}/file").body())) for d in new)
    assert pages == [1, 2], pages                                  # die Fotos: eine PDF mit zwei Seiten
    # Fehler vom Server mit Text (hier: dasselbe Foto noch einmal = Dublette)
    page.set_input_files("#file-input", files=[{"name": "seite1.jpg", "mimeType": "image/jpeg", "buffer": _jpeg("red")},
                                               {"name": "seite2.jpg", "mimeType": "image/jpeg", "buffer": _jpeg("green")}])
    expect(page.locator("#toast")).to_contain_text("2 Fotos:")
    # Nur HEIC: Hinweis, kein Aufruf
    n = len(posts)
    page.set_input_files("#file-input", files=[{"name": "IMG_0004.HEIC", "mimeType": "image/heic", "buffer": b"\x00" * 50}])
    expect(page.locator("#toast")).to_contain_text("Maximale Kompatibilität")
    assert len(posts) == n
    for doc in new:
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")


# ---------- App verbinden (Funktion f) ----------
def test_connect_app_qr_in_settings(page, server):
    """Einstellungen › App verbinden: QR mit papigator://connect?server=…&user=… (ohne Passwort oder Token), die Adresse
    zum Abtippen darunter; ohne öffentliche Adresse die dieser Seite."""
    from urllib.parse import quote
    page.click("#settings-btn")
    page.locator("#s-connect-sect summary").click()
    expect(page.locator("#s-connect-qr svg path")).to_have_count(1)
    origin = server.rstrip("/")
    expect(page.locator("#s-connect-url")).to_have_value(origin)
    link = page.get_attribute("#s-connect-qr", "data-link")
    assert link == f"papigator://connect?server={quote(origin, safe='')}&user=alex", link
    assert "token" not in link and "pass" not in link.lower()
    page.uncheck("#s-connect-user")
    link = page.get_attribute("#s-connect-qr", "data-link")
    assert link == f"papigator://connect?server={quote(origin, safe='')}", link
    page.check("#s-connect-user")
    page.go_back()


# ---------- „Los geht’s“ auf Heute (Funktion a) ----------
def test_onboarding_card_on_today(page):
    """Neues Konto: „Los geht’s“ oben auf Heute mit Häkchen; offene Schritte führen zum Ziel (Personen in „Familie“,
    App über TestFlight bzw. „App verbinden“); „Ausblenden“ meldet es dem Server und die Karte ist weg."""
    steps = [{"key": "first_document", "done": True}, {"key": "app", "done": False}, {"key": "invite", "done": False},
             {"key": "persons", "done": False}, {"key": "inbox", "done": False}]
    dismissed = []
    page.route(re.compile(r"/onboarding$"), lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"show": True, "steps": steps, "dismissed": False})))
    page.route(re.compile(r"/onboarding/dismiss$"), lambda route: (dismissed.append(1), route.fulfill(status=204))[1])
    page.click('#mode-seg button[data-mode="today"]')
    card = page.locator("#todayview .t-onb")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Los geht’s")
    expect(card).to_contain_text("1 von 5 erledigt")
    expect(card.locator('[data-step="first_document"]')).to_have_class(re.compile("done"))
    expect(card.locator('[data-step="app"] a', has_text="TestFlight")).to_have_attribute("href", "https://testflight.apple.com/join/p4FjYp3Z")
    card.locator('[data-step="persons"] [data-act="ob-persons"]').click()
    shown(page, "#familyview")
    expect(page.locator("#s-persons-sect")).to_have_attribute("open", "")
    page.click('#mode-seg button[data-mode="today"]')
    card.locator('[data-step="app"] [data-act="ob-connect"]').click()
    shown(page, "#settings")
    expect(page.locator("#s-connect-sect")).to_have_attribute("open", "")
    page.go_back()
    shown(page, "#todayview")
    card.locator('[data-act="ob-dismiss"]').click()
    expect(page.locator("#todayview .t-onb")).to_have_count(0)
    assert dismissed == [1]
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


# ---------- Bezahlen per QR (Funktion b) ----------
def _upload_text(page, title, text) -> int:
    from e2e.conftest import make_pdf
    return page.evaluate("""async ([title, text, pdf]) => {
        const fd = new FormData();
        fd.append("file", new Blob([new Uint8Array(pdf)], { type: "application/pdf" }), title + ".pdf");
        fd.append("title", title); fd.append("text", text);
        return (await (await fetch("/documents", { method: "POST", body: fd })).json()).id;
    }""", [title, text, list(make_pdf(title))])


def test_pay_by_qr_in_detail(page):
    """Bezahlen per QR: aus, bis man es in den Einstellungen einschaltet; dann im Dokument der GiroCode mit Prüfhinweis,
    Empfänger, IBAN (gruppiert), Betrag und Zweck samt Fundstelle im Brief; steht der Betrag so nicht im Brief, warnt es."""
    text = ("Stadtwerke Musterstadt GmbH\nRechnung Nr. R-2026-0815\nGesamtbetrag: 547,88 EUR\n"
            "Bitte überweisen Sie auf IBAN: DE02 1203 0000 0000 2020 51 bei der Musterbank.")
    doc = _upload_text(page, "Stromrechnung zum Bezahlen", text)
    _patch(page, doc, {"sender": "Stadtwerke Musterstadt GmbH", "amount": 547.88, "currency": "EUR", "doc_kind": "rechnung",
                       "direction": "out", "document_date": "2026-11-20", "refs": {"invoice": "R-2026-0815"}})
    page.reload()
    page.wait_for_selector(".card")
    _open(page, "Stromrechnung zum Bezahlen")
    page.wait_for_timeout(500)
    hidden(page, "#m-pay")                                             # Standard: aus
    page.go_back()
    page.click("#settings-btn")
    page.locator("#s-pay-sect summary").click()
    expect(page.locator("#s-pay-qr")).not_to_be_checked()
    page.check("#s-pay-qr")
    expect(page.locator("#toast")).to_contain_text("Bezahlen per QR ist an")
    page.go_back()
    page.locator(".card", has_text="Stromrechnung zum Bezahlen").first.click()
    pay = page.locator("#m-pay")
    expect(pay).to_be_visible()
    expect(pay).to_contain_text("Bitte Betrag und Empfänger mit dem Brief vergleichen.")
    expect(page.locator("#m-pay-qr svg path")).to_have_count(1)
    expect(page.locator("#m-pay-name")).to_have_text("Stadtwerke Musterstadt GmbH")
    expect(page.locator("#m-pay-iban")).to_have_text("DE02 1203 0000 0000 2020 51")
    expect(page.locator("#m-pay-amount")).to_have_text(re.compile(r"^547,88\s€$"))
    expect(page.locator("#m-pay-iban-src")).to_contain_text("im Brief:")
    expect(page.locator("#m-pay-amount-src")).to_contain_text("547,88")
    hidden(page, "#m-pay-warn")
    # Betrag von Hand geändert: steht so nicht im Brief → deutliche Warnung, der Code passt sich an
    page.fill("#m-amount", "600")
    page.press("#m-amount", "Enter")
    expect(page.locator("#m-pay-amount")).to_have_text(re.compile(r"^600,00\s€$"))
    shown(page, "#m-pay-warn")
    expect(page.locator("#m-pay-warn")).to_have_text("Dieser Betrag steht so nicht im Brief – bitte prüfen.")
    page.go_back()
    page.fill("#search", "")
    page.evaluate("fetch('/auth/me', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pay_qr: false }) })")
    _drop(page, doc)


# ---------- Paperless-ngx übernehmen (Funktion d) ----------
def _paperless_zip() -> bytes:
    """Kleiner Paperless-ngx-Export (wie tests/test_import_paperless.py): zwei Dokumente, eins davon im Papierkorb."""
    import io
    import zipfile
    from e2e.conftest import make_pdf

    def doc(pk, title, fname, deleted=None):
        return {"model": "documents.document", "pk": pk,
                "fields": {"correspondent": 1, "storage_path": None, "title": title, "document_type": None,
                           "content": f"{title} Beispieltext", "mime_type": "application/pdf", "checksum": "x" * 64,
                           "archive_checksum": None, "page_count": 1, "created": "2023-05-01", "modified": "2024-01-01T10:00:00Z",
                           "added": "2024-01-01T10:00:00Z", "filename": fname, "archive_filename": None,
                           "original_filename": fname, "archive_serial_number": None, "owner": 1, "tags": [],
                           "deleted_at": deleted, "restored_at": None, "transaction_id": None},
                "__exported_file_name__": fname}
    records = [{"model": "documents.correspondent", "pk": 1, "fields": {"name": "Paperless Stadtwerke", "match": "",
                                                                       "matching_algorithm": 6, "is_insensitive": True, "owner": 1}},
               doc(1, "Aus Paperless Strom", "strom-pl.pdf"), doc(2, "Aus Paperless gelöscht", "weg-pl.pdf", "2024-02-02T10:00:00Z")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("export/manifest.json", json.dumps(records))
        z.writestr("export/strom-pl.pdf", make_pdf("aus paperless strom e2e"))
        z.writestr("export/weg-pl.pdf", make_pdf("aus paperless weg e2e"))
    return buf.getvalue()


def test_paperless_import_trial_then_import(page):
    """Admin: Einstellungen › Aus Paperless-ngx übernehmen – erst Probelauf (zählt nur), dann Importieren derselben Datei;
    ein Fehler vom Server steht als Text da."""
    page.click("#settings-btn")
    page.locator("#s-paperless-sect summary").click()
    expect(page.locator("#s-pl-dry")).to_be_disabled()
    expect(page.locator("#s-pl-go")).to_be_disabled()
    expect(page.locator("#s-pl-user")).to_have_value("alex")
    # Keine ZIP-Datei: Fehlertext des Servers
    page.set_input_files("#s-pl-file", files=[{"name": "kaputt.zip", "mimeType": "application/zip", "buffer": b"kein zip"}])
    page.click("#s-pl-dry")
    expect(page.locator("#s-pl-result")).to_contain_text("Keine gültige ZIP-Datei")
    expect(page.locator("#s-pl-go")).to_be_disabled()
    page.set_input_files("#s-pl-file", files=[{"name": "export.zip", "mimeType": "application/zip", "buffer": _paperless_zip()}])
    expect(page.locator("#s-pl-result")).to_be_hidden()               # neue Datei: altes Ergebnis weg
    assert page.evaluate("pl.file && pl.file.name") == "export.zip"
    page.click("#s-pl-dry")
    expect(page.locator("#s-pl-result")).to_contain_text("Probelauf: 1 Dokument käme dazu")
    assert page.evaluate("fetch('/documents?scope=all&q=Paperless&limit=50').then(r => r.json()).then(d => d.length)") == 0
    expect(page.locator("#s-pl-go")).to_be_enabled()
    page.once("dialog", lambda d: d.accept())
    page.click("#s-pl-go")
    expect(page.locator("#s-pl-result")).to_contain_text("1 Dokument übernommen")
    expect(page.locator("#s-pl-go")).to_be_disabled()                   # nochmal erst nach neuem Probelauf
    docs = page.evaluate("fetch('/documents?scope=all&limit=500').then(r => r.json())")
    mine = [d for d in docs if d["title"].startswith("Aus Paperless")]
    assert [d["title"] for d in mine] == ["Aus Paperless Strom"], mine
    assert mine[0]["sender"] == "Paperless Stadtwerke"
    page.go_back()
    _drop(page, *[d["id"] for d in mine])
