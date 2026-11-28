"""Nachprüfung der Web-App vom 27.11.2026: Tastatur für Karten/Chips/Hochladen, Hinweise bei Ladefehlern, Geld
(Monatsdiagramm mit vielen Monaten, Buchungszeilen am Handy, Fehlertext der Suche, „Umsätze vorhanden“) und das
Fristdatum mit Jahr in „Heute“. Englische Teile stehen in test_web_en.py. Läuft nach test_web.py (Dateiname) und
räumt alles wieder ab, was es anlegt."""
import datetime as dt
import re

from playwright.sync_api import expect

from e2e.test_web import _patch, _upload, hidden, shown

PHONE = {"width": 375, "height": 812}


def _drop(page, *ids):
    for i in ids:
        page.evaluate(f"fetch('/documents/{i}?permanent=true', {{ method: 'DELETE' }})")


def _months_back(n, day=15):
    """Ein Datum je Monat, vom laufenden Monat n-1 Monate zurück (immer verschiedene Monate)."""
    t = dt.date.today()
    out = []
    for i in range(n):
        y, m = divmod(t.month - 1 - i, 12)
        out.append(dt.date(t.year + y, m + 1, min(day, t.day if i == 0 else day)))
    return out


def _import(page, csv_text, name):
    """CSV über den Dialog in „Geld“ importieren (Ansicht muss offen sein)."""
    page.locator('#moneyview [data-mact="import"]').first.click()
    shown(page, "#bank-dialog")
    page.set_input_files("#bk-file", files=[{"name": "e2e.csv", "mimeType": "text/csv", "buffer": csv_text.encode()}])
    page.select_option("#bk-account", "")          # neues Konto – andere Tests lassen Konten stehen (vorausgewählt)
    expect(page.locator("#bk-name")).to_be_visible()
    expect(page.locator("#bk-preview")).to_contain_text("Umsätze")
    page.fill("#bk-name", name)
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    expect(page.locator('#moneyview [data-mact="undo"]')).to_be_visible()


def _undo_import(page):
    undo = page.locator('#moneyview [data-mact="undo"]')
    if undo.count():
        undo.first.click()
        expect(undo).to_have_count(0)


LONG_NAME = "ZALANDO SE Zahlungsabwicklung Onlinehandel Berlin"


def _monthly_csv(n=12):
    rows = ["Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag"]
    for i, d in enumerate(_months_back(n)):
        rows.append(f"{d.strftime('%d.%m.%Y')};{LONG_NAME};Bestellung Nummer 4711-{i} Kartenzahlung;-{20 + i},95")
    return "\n".join(rows) + "\n"


# ---------- Tastatur ----------

def test_keyboard_opens_card_with_enter_and_space(page):
    card = page.locator(".card", has_text="Grundsteuer")
    expect(card).to_have_attribute("role", "button")
    expect(card).to_have_attribute("tabindex", "0")
    expect(card).to_have_attribute("aria-label", "Grundsteuer")
    card.focus()
    page.keyboard.press("Enter")
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Grundsteuer")
    page.go_back()
    shown(page, "#main")
    hidden(page, "#detail")
    page.locator(".card", has_text="Arztbrief").focus()
    page.keyboard.press(" ")
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Arztbrief")
    page.go_back()
    shown(page, "#main")


def test_keyboard_filter_chips_apply_with_enter(page):
    """„Offene Fristen“ per Enter an und wieder aus; „Tag“ öffnet per Enter die Auswahl, „Haus“ darin filtert."""
    doc = _upload(page, "Frist per Tastatur")
    try:
        _patch(page, doc, {"due_date": (dt.date.today() + dt.timedelta(days=4)).isoformat()})
        page.reload()
        page.wait_for_selector(".card")
        due = page.locator("#tagbar .chip", has_text="Offene Fristen")
        expect(due).to_have_attribute("role", "button")
        expect(due).to_have_attribute("tabindex", "0")
        due.focus()
        page.keyboard.press("Enter")
        expect(page.locator("#tagbar .chip", has_text="Offene Fristen")).to_have_class(re.compile(r"\bactive\b"))
        expect(page.locator(".card", has_text="Frist per Tastatur")).to_be_visible()
        expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
        page.locator("#tagbar .chip", has_text="Offene Fristen").focus()
        page.keyboard.press("Enter")                                   # wieder aus
        expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
        # Tag-Auswahl: Chip im Popover ist ebenfalls per Tastatur erreichbar
        page.locator("#tagbar .chip", has_text=re.compile(r"^Tag")).focus()
        page.keyboard.press("Enter")
        haus = page.locator("#popover .chip", has_text=re.compile(r"^Haus"))
        expect(haus).to_be_visible()
        haus.focus()
        page.keyboard.press("Enter")
        active = page.locator("#tagbar .chip.active", has_text="Haus")
        expect(active).to_be_visible()
        expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
        expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
        active.focus()
        page.keyboard.press(" ")                                       # Leertaste nimmt den Filter weg
        expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
    finally:
        _drop(page, doc)


def test_keyboard_upload_label_opens_file_chooser(page):
    label = page.locator('.topbar label[for="file-input"]')
    expect(label).to_have_attribute("role", "button")
    expect(label).to_have_attribute("tabindex", "0")
    # Selten (CI, etwa 1 von 10) kam die Dateiauswahl nicht – nicht nachstellbar, vermutlich holt der Start der Seite
    # kurz den Fokus. Deshalb bis zu dreimal: fokussieren, Enter, kurz warten.
    for attempt in range(3):
        label.focus()
        expect(label).to_be_focused()
        try:
            with page.expect_file_chooser(timeout=5000) as fc:
                page.keyboard.press("Enter")
            break
        except Exception:
            if attempt == 2:
                raise
    assert fc.value.is_multiple()


# ---------- Ladefehler ----------

LIST_URL = re.compile(r"/documents\?.*with_total=true")   # nur die Kachel-Liste (loadDocs), nicht Heute/Familie


def _fail_list(page):
    """Kachel-Liste antwortet mit 500, ein Tag-Filter löst loadDocs aus: Hinweis statt stiller alter Kacheln."""
    page.route(LIST_URL, lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail": "kaputt"}'))
    page.locator("#tagbar .chip", has_text=re.compile(r"^Tag")).click()
    page.locator("#popover .chip", has_text=re.compile(r"^Haus")).click()
    expect(page.locator("#toast")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Liste ließ sich nicht laden")
    page.unroute(LIST_URL)
    page.wait_for_timeout(200)


def _open_deleted(page):
    """Karte steht noch da, das Dokument ist inzwischen endgültig gelöscht: Hinweis, kein leeres Dokument."""
    doc = _upload(page, "Gleich weg")
    page.reload()
    card = page.locator(".card", has_text="Gleich weg")
    expect(card).to_be_visible()
    _drop(page, doc)
    page.wait_for_function(f"fetch('/documents/{doc}').then((r) => r.status === 404)")
    card.click()
    expect(page.locator("#toast")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Dokument ließ sich nicht öffnen")
    hidden(page, "#detail")
    page.wait_for_timeout(200)


def test_document_list_failure_shows_toast(page):
    _fail_list(page)           # die Fixture prüft beim Abbau: kein unbehandelter Fehler im Browser


def test_opening_deleted_document_shows_toast(page):
    _open_deleted(page)


# ---------- Geld ----------

def test_money_caption_many_months_and_search_failure(page):
    """„Umsätze vorhanden: … – …“ im Kopf; Kategorie mit 12 Monaten: Diagramm `.many` (Beträge aus, Beschriftung
    ausgedünnt); fehlgeschlagene Buchungssuche zeigt einen Fehlertext statt endlos „Suche läuft …“."""
    page.click('#mode-seg button[data-mode="money"]')
    shown(page, "#moneyview")
    mv = page.locator("#moneyview")
    _import(page, _monthly_csv(), "Viele Monate")
    try:
        sub = mv.locator(".m-sub")
        expect(sub).to_contain_text(re.compile(r"Umsätze vorhanden: \d\d\.\d\d\.\d{4} – \d\d\.\d\d\.\d{4}"))
        expect(sub).not_to_contain_text("Umsätze vom")
        cov = page.evaluate("fetch('/bank/summary').then((r) => r.json()).then((s) => s.covered)")
        fmt = lambda iso: ".".join(reversed(iso[:10].split("-")))   # noqa: E731
        expect(sub).to_contain_text(f"Umsätze vorhanden: {fmt(cov['first'])} – {fmt(cov['last'])}")
        # Kategorie mit vielen Monaten
        page.click('#moneyview [data-mperiod="all"]')
        bar = mv.locator(".m-bar", has_text="Kleidung")
        expect(bar).to_be_visible()
        bar.click()
        expect(mv.locator("#m-det-h")).to_have_text("Kleidung")
        months = mv.locator(".m-detail .m-months")
        expect(months).to_have_class(re.compile(r"\bmany\b"))
        info = months.evaluate("""m => { const mos = [...m.querySelectorAll('.mo')];
            return { n: mos.length,
                     labelled: mos.filter((o) => [...o.childNodes].filter((c) => c.nodeType === 3)
                                                   .map((c) => c.nodeValue).join('').trim()).length,
                     amounts: mos.filter((o) => getComputedStyle(o.querySelector('b')).display !== 'none').length }; }""")
        assert info["n"] >= 12, info
        assert 0 < info["labelled"] < info["n"], info          # ausgedünnt, aber nicht leer
        assert info["amounts"] == 0, info                      # Beträge über den Balken ausgeblendet
        page.go_back()
        expect(mv.locator("#m-det-h")).to_have_count(0)
        # Suche schlägt fehl
        page.route(re.compile(r"/bank/search\?"), lambda route: route.fulfill(status=500, body="nope"))
        page.fill("#search", "Zalando")
        expect(mv).to_contain_text("Suche fehlgeschlagen – bitte noch einmal versuchen.")
        expect(mv).not_to_contain_text("Suche läuft")
        page.unroute(re.compile(r"/bank/search\?"))
        page.fill("#search", "")
        expect(mv).to_contain_text("Wofür")
    finally:
        _undo_import(page)


def test_money_rows_wrap_on_phone(page):
    """375 px: Buchungszeilen in der Kategorie (langer Name, Auswahl, Knöpfe) brechen um statt die Seite zu verbreitern."""
    page.set_viewport_size(PHONE)
    page.reload()
    page.wait_for_selector("#main:not(.hidden)")
    page.locator('#tabbar [data-tab="money"]').click()
    shown(page, "#moneyview")
    mv = page.locator("#moneyview")
    _import(page, _monthly_csv(4), "Handy")
    try:
        page.click('#moneyview [data-mperiod="all"]')
        mv.locator(".m-bar", has_text="Kleidung").click()
        expect(mv.locator("#m-det-h")).to_have_text("Kleidung")
        rows = mv.locator(".m-detail .m-tx", has_text="ZALANDO")
        expect(rows.first).to_be_visible()
        expect(rows.first.locator(".m-recat")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), \
            page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        edges = mv.locator(".m-detail .m-tx").evaluate_all(
            "rs => rs.map((r) => Math.max(r.getBoundingClientRect().right, ...[...r.children].map((c) => c.getBoundingClientRect().right)))")
        assert edges and max(edges) <= PHONE["width"] + 0.5, edges
        page.go_back()
        expect(mv.locator("#m-det-h")).to_have_count(0)
    finally:
        _undo_import(page)
        page.evaluate("localStorage.setItem('bl.mode', 'tiles'); localStorage.removeItem('bl.docsMode')")


# ---------- Heute: Fristdatum mit Jahr ----------

def test_today_due_pill_shows_year_outside_current_year(page):
    t = dt.date.today()
    later = dt.date(t.year + 1, 6, 12)
    soon = t + dt.timedelta(days=10)
    ids = [_upload(page, "Frist nächstes Jahr")]
    _patch(page, ids[0], {"due_date": later.isoformat()})
    if soon.year == t.year:
        ids.append(_upload(page, "Frist dieses Jahr"))
        _patch(page, ids[1], {"due_date": soon.isoformat()})
    try:
        page.reload()
        page.click('#mode-seg button[data-mode="today"]')
        shown(page, "#todayview")
        more = page.locator("#todayview .t-more")
        if more.count():
            more.click()
        pill = page.locator(".t-due .t-row", has_text="Frist nächstes Jahr").locator(".t-pill")
        expect(pill).to_have_text(re.compile(rf"^12\. Juni? {later.year}$"))
        if len(ids) > 1:
            pill2 = page.locator(".t-due .t-row", has_text="Frist dieses Jahr").locator(".t-pill")
            expect(pill2).to_have_text(re.compile(rf"^{soon.day}\. \S+$"))
            expect(pill2).not_to_contain_text(str(soon.year))
    finally:
        _drop(page, *ids)
        page.evaluate("localStorage.setItem('bl.mode', 'tiles')")



def test_back_closes_document_after_double_open(page):
    """Doppelklick auf eine Frist in „Heute“ öffnete das Dokument zweimal (zwei Verlaufseinträge): „Übersicht“
    schloss es dann erst beim zweiten Klick (28.11.2026)."""
    from e2e.test_web import _today
    doc = _upload(page, "Doppelklick-Test")
    _patch(page, doc, {"due_date": (dt.date.today() + dt.timedelta(days=4)).isoformat(), "due_note": "Zahlen"})
    _today(page)
    page.locator(".t-row", has_text="Doppelklick-Test").locator(".t-open").dblclick()
    shown(page, "#detail")
    page.click("#back-btn")
    hidden(page, "#detail")
    shown(page, "#todayview")
    page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_tag_from_suggestions_needs_no_enter(page):
    """Tag aus der Vorschlagsliste gewählt: gleich übernommen (28.11.2026); getippt wartet er weiter auf Enter."""
    doc = _upload(page, "Tagwahl-Test")
    try:
        page.evaluate(f"openDetail({doc})")
        shown(page, "#detail")
        inp = page.locator("#m-tag-input")
        inp.click()
        page.keyboard.type("Hau")                                    # getippt: nichts passiert
        expect(page.locator("#m-tags .chip", has_text="Haus")).to_have_count(0)
        # so meldet Chrome die Wahl aus der <datalist> (die native Liste lässt sich nicht anklicken)
        inp.evaluate("(el) => { el.value = 'Haus'; el.dispatchEvent(new InputEvent('input', "
                     "{ inputType: 'insertReplacementText', bubbles: true })); }")
        expect(page.locator("#m-tags .chip", has_text="Haus")).to_have_count(1)
        expect(inp).to_have_value("")
        expect(page.locator("#save-state")).to_have_text("Gespeichert")
    finally:
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")


def test_drag_document_into_subfolder(page):
    """Ordner-Ansicht: Dokument auf einen Unterordner ziehen verschiebt es dorthin – statt die Ablage für neue Dateien
    zu öffnen (28.11.2026)."""
    from e2e.test_web import _folders
    doc = _upload(page, "Umzugskarton-Beleg")
    _patch(page, doc, {"tags": ["Haus"]})
    try:
        _folders(page)
        page.locator(".folder", has_text="Haus").click()
        card = page.locator(".card", has_text="Umzugskarton-Beleg")
        expect(card).to_be_visible()
        for img in card.locator(".thumb img").all():                    # Vorschaubild zieht nicht selbst (sonst „Datei“)
            expect(img).to_have_attribute("draggable", "false")
        page.drag_and_drop('.card:has-text("Umzugskarton-Beleg")', '.folder:has-text("Strom")')
        expect(page.locator("#toast")).to_contain_text("Nach „Strom“ verschoben")
        hidden(page, "#dropzone")
        tags = page.evaluate(f"fetch('/documents/{doc}').then((r) => r.json()).then((d) => d.tags.map((t) => t.name))")
        assert sorted(tags) == ["Haus", "Strom"], tags                   # Haus bleibt als Oberordner
        page.locator(".folder", has_text="Strom").click()
        expect(page.locator(".card", has_text="Umzugskarton-Beleg")).to_be_visible()
    finally:
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")
        page.click('#mode-seg button[data-mode="tiles"]')


def test_customize_today_hides_sections(page):
    """Einstellungen → Heute anpassen: Bereiche aus- und wieder einblenden (je Konto, Server /auth/me)."""
    from e2e.test_web import _today
    doc = _upload(page, "Anpassen-Test")
    _patch(page, doc, {"due_date": (dt.date.today() + dt.timedelta(days=5)).isoformat(), "due_note": "Zahlen"})
    try:
        _today(page)
        expect(page.locator(".t-due")).to_be_visible()
        page.locator("#todayview [data-act='customize']").click()
        shown(page, "#settings")
        expect(page.locator("#s-today-sect")).to_have_attribute("open", "")
        box = page.locator("#s-today-list input[data-today='due']")
        expect(box).to_be_checked()
        box.uncheck()
        page.wait_for_function("fetch('/auth/me').then((r) => r.json()).then((m) => m.today_hidden.includes('due'))")
        page.click("#settings-back")
        shown(page, "#todayview")
        expect(page.locator(".t-due")).to_have_count(0)
        expect(page.locator(".t-cols.no-due")).to_have_count(1)
    finally:
        page.evaluate("fetch('/auth/me', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, "
                      "body: JSON.stringify({ today_hidden: [] }) })")
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")
        page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_light_theme_native_controls_stay_light(page):
    """Windows dunkel + Papigator hell: Auswahllisten waren schwarz mit schwarzer Schrift – color-scheme folgt jetzt
    der Wahl in Darstellung (28.11.2026)."""
    page.emulate_media(color_scheme="dark")
    page.evaluate("localStorage.setItem('bl.theme', 'light'); applyTheme()")
    try:
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "light"
        page.evaluate("localStorage.setItem('bl.theme', 'dark'); applyTheme()")
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
    finally:
        page.evaluate("localStorage.removeItem('bl.theme'); applyTheme()")
        page.emulate_media(color_scheme="light")


def test_today_extra_cards_last_seen_and_week(page):
    """Zusatzkarten auf „Heute“ (28.11.2026): „Zuletzt angesehen“ (nur dieser Browser) und die Wochenzeile;
    beide lassen sich unter „Heute anpassen“ abschalten."""
    from e2e.test_web import _today
    doc = _upload(page, "Zuletzt-Test")
    try:
        page.evaluate(f"openDetail({doc})")
        shown(page, "#detail")
        page.click("#back-btn")
        hidden(page, "#detail")
        _today(page)
        expect(page.locator(".t-lastseen")).to_contain_text("Zuletzt-Test")
        expect(page.locator(".t-week")).to_contain_text("Diese Woche")
        page.evaluate("fetch('/auth/me', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, "
                      "body: JSON.stringify({ today_hidden: ['lastseen', 'week'] }) }).then(() => refreshMe())")
        page.wait_for_function("state.me && state.me.today_hidden.includes('week')")
        page.reload()
        shown(page, "#todayview")
        expect(page.locator(".t-due, .t-new")).not_to_have_count(0)
        expect(page.locator(".t-lastseen")).to_have_count(0)
        expect(page.locator(".t-week")).to_have_count(0)
    finally:
        page.evaluate("fetch('/auth/me', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, "
                      "body: JSON.stringify({ today_hidden: [] }) })")
        page.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")
        page.evaluate("localStorage.removeItem('bl.lastseen'); localStorage.setItem('bl.mode', 'tiles')")
