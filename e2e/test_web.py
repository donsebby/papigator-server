"""Web-App im echten Browser: Laden, Dokument, Ordner, Papierkorb, Einstellungen, Dialoge – und vor allem
Browser-Zurück/Vor überall (Taste im Browser = page.go_back)."""
import re

from playwright.sync_api import expect


def shown(page, sel):
    return expect(page.locator(sel)).to_be_visible()


def hidden(page, sel):
    return expect(page.locator(sel)).to_be_hidden()


def test_app_loads_without_errors(page):
    # Ein fehlendes Element im HTML ließ app.js früher beim Start abbrechen und die Seite blieb leer
    expect(page.locator(".card")).to_have_count(3)
    shown(page, "#settings-btn")


def test_document_back_and_forward(page):
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Grundsteuer")
    page.go_back()
    shown(page, "#main"); hidden(page, "#detail")
    page.locator(".card", has_text="Arztbrief").click()
    shown(page, "#detail")
    page.click("#back-btn")                                   # Knopf „Übersicht“
    shown(page, "#main")


def test_share_dialog_over_document(page):
    page.locator(".card", has_text="Grundsteuer").click()
    page.click("#share-btn")
    expect(page.locator("#share-dialog")).to_be_visible()
    page.go_back()                                            # schließt nur den Dialog
    expect(page.locator("#share-dialog")).to_be_hidden()
    shown(page, "#detail")
    page.click("#share-btn")
    page.keyboard.press("Escape")                             # Esc nimmt den Verlaufseintrag zurück …
    expect(page.locator("#share-dialog")).to_be_hidden()
    page.wait_for_function("history.state && 'id' in history.state")   # (asynchron, ein Mensch ist nie so schnell)
    page.go_back()                                            # … also führt Zurück jetzt aus dem Dokument
    shown(page, "#main"); hidden(page, "#detail")


def test_folders_back(page):
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()
    expect(page.locator(".crumbs b")).to_have_text("Haus")
    page.locator(".folder", has_text="Strom").click()
    expect(page.locator(".crumbs b")).to_have_text("Strom")
    expect(page.locator(".card", has_text="Stromrechnung 2025")).to_be_visible()
    page.go_back()
    expect(page.locator(".crumbs b")).to_have_text("Haus")
    page.go_back()
    expect(page.locator(".crumbs b")).to_have_text("Alle Ordner")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_new_folder(page):
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()
    page.once("dialog", lambda d: d.accept("Garten"))
    page.click("#folder-new")
    expect(page.locator(".folder", has_text="Garten")).to_be_visible()
    expect(page.locator(".folder", has_text="Garten")).to_contain_text("leer")
    page.once("dialog", lambda d: d.accept("strom"))          # gibt es schon (andere Schreibweise)
    page.click("#folder-new")
    expect(page.locator("#toast")).to_contain_text("gibt es schon")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_trash_back_returns_to_folder(page):
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()
    page.locator(".chip", has_text="Papierkorb").click()
    shown(page, "#trash-bar")
    expect(page.locator(".card", has_text="Altes Schreiben")).to_be_visible()
    expect(page.locator("#trash-close-btn")).to_have_count(0)  # kein eigener Zurück-Knopf mehr
    page.go_back()
    hidden(page, "#trash-bar")
    expect(page.locator(".crumbs b")).to_have_text("Haus")
    page.go_forward()
    shown(page, "#trash-bar")
    page.locator(".chip", has_text="Papierkorb").click()      # Chip schaltet aus
    hidden(page, "#trash-bar")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_settings_and_dialog_back(page):
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()          # ein Schritt im Verlauf vor den Einstellungen
    page.click("#settings-btn")
    shown(page, "#settings")
    page.go_back()
    shown(page, "#main"); hidden(page, "#settings")
    page.click("#settings-btn")
    page.locator("#s-links-sect summary").click()
    page.click("#s-upload-new")
    expect(page.locator("#share-dialog")).to_be_visible()
    expect(page.locator("#sh-title")).to_have_text("Briefkasten-Link")
    page.go_back()                                            # erst der Dialog …
    expect(page.locator("#share-dialog")).to_be_hidden()
    shown(page, "#settings")
    page.go_back()                                            # … dann die Einstellungen
    shown(page, "#main")
    page.click("#settings-btn")
    page.click("#settings-back")                              # Knopf „Übersicht“ …
    shown(page, "#main")
    page.wait_for_function("!(history.state && history.state.layer)")
    page.go_back()                                            # … und Zurück öffnet sie nicht wieder, sondern
    hidden(page, "#settings")                                 #     geht im Ordner eine Ebene höher
    shown(page, "#main")
    expect(page.locator(".crumbs b")).to_have_text("Alle Ordner")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_ask_panel_back(page):
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()          # ein Schritt im Verlauf vor dem Panel
    page.click("#ask-btn")
    shown(page, "#ask-panel")
    page.go_back()
    hidden(page, "#ask-panel")
    page.click("#ask-btn")
    page.click("#ask-close")
    hidden(page, "#ask-panel")
    page.wait_for_function("!(history.state && history.state.layer)")
    page.go_back()                                            # führt aus dem Ordner, öffnet das Panel nicht wieder
    hidden(page, "#ask-panel")
    expect(page.locator(".crumbs b")).to_have_text("Alle Ordner")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_create_briefkasten_link(page):
    page.click("#settings-btn")
    page.locator("#s-links-sect summary").click()
    page.click("#s-upload-new")
    page.fill("#sh-name", "Steuerberater")
    page.click("#sh-create")
    expect(page.locator("#sh-result")).to_be_visible()
    assert "/s/" in page.input_value("#sh-url")
    page.click("#sh-close")
    expect(page.locator("#s-links")).to_contain_text("Briefkasten · 0 eingegangen")


def test_folder_download(page, tmp_path):
    import zipfile
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()
    with page.expect_download() as dl:
        page.click("#folder-dl")
    path = tmp_path / "haus.zip"
    dl.value.save_as(path)
    names = zipfile.ZipFile(path).namelist()
    assert "Haus/Inhaltsverzeichnis.pdf" in names
    assert any(n.startswith("Haus/Strom/") and n.endswith("Stromrechnung 2025.pdf") for n in names), names
    page.click('#mode-seg button[data-mode="tiles"]')


# ---------- Rückmeldungen vom 11.11.2026 ----------

def _folders(page):
    page.click('#mode-seg button[data-mode="folders"]')
    expect(page.locator(".crumbs")).to_be_visible()


def test_new_folder_shows_in_tag_filter(page):
    _folders(page)
    page.once("dialog", lambda d: d.accept("Steuer 2026"))
    page.click("#folder-new")
    expect(page.locator(".folder", has_text="Steuer 2026")).to_be_visible()
    page.click('#mode-seg button[data-mode="tiles"]')
    page.locator("#tagbar .chip", has_text="Tag").click()
    expect(page.locator("#popover")).to_contain_text("Steuer 2026")


def test_start_view_setting(page):
    page.click("#settings-btn")
    page.evaluate("document.querySelector('#start-seg').closest('details').open = true")
    page.click('#start-seg button[data-start="folders"]')
    page.reload()
    expect(page.locator(".crumbs")).to_be_visible()                 # startet in der Ordner-Ansicht
    page.evaluate("localStorage.setItem('bl.start', 'last')")


def test_upload_into_open_folder(page, tmp_path):
    from e2e.conftest import make_pdf
    _folders(page)
    page.locator(".folder", has_text="Haus").click()
    f = tmp_path / "Wasserrechnung.pdf"
    f.write_bytes(make_pdf("Wasser im Ordner"))
    page.set_input_files("#file-input", str(f))
    expect(page.locator("#toast")).to_contain_text("in „Haus“")
    expect(page.locator(".card", has_text="Wasserrechnung")).to_be_visible()


def test_move_folder_by_drag_and_undo(page):
    _folders(page)
    page.once("dialog", lambda d: d.accept("Wohnung"))
    page.click("#folder-new")
    page.drag_and_drop('.folder:has-text("Wohnung")', '.folder:has-text("Haus")')
    expect(page.locator("#toast")).to_contain_text("liegt jetzt in „Haus“")
    expect(page.locator(".folder", has_text="Wohnung")).to_have_count(0)      # nicht mehr oben
    page.locator("#toast .toast-act").click()                                 # Rückgängig
    expect(page.locator(".folder", has_text="Wohnung")).to_be_visible()


def test_rename_and_delete_folder(page):
    _folders(page)
    page.once("dialog", lambda d: d.accept("Kram"))
    page.click("#folder-new")
    page.locator(".folder", has_text="Kram").click()
    page.once("dialog", lambda d: d.accept("Krempel"))
    page.click("#folder-rename")
    expect(page.locator(".crumbs b")).to_have_text("Krempel")
    page.once("dialog", lambda d: d.accept())
    page.click("#folder-delete")
    expect(page.locator(".crumbs b")).to_have_text("Alle Ordner")
    expect(page.locator(".folder", has_text="Krempel")).to_have_count(0)


def test_settings_tag_click_selects(page):
    page.click("#settings-btn")
    page.locator("#s-sect-tags summary").click()
    page.locator("#s-tags .chip", has_text="Gesundheit").click()
    expect(page.locator("#s-tag-merge")).to_be_visible()
    expect(page.locator("#s-tag-rename-btn")).to_be_visible()
    page.once("dialog", lambda d: d.accept("Arzt"))
    page.click("#s-tag-rename-btn")
    expect(page.locator("#s-tags")).to_contain_text("Arzt")


def test_folder_view_has_no_filter_chips_and_searches_in_folder(page):
    _folders(page)
    bar = page.locator("#tagbar")
    for label in ("Tag", "Jahr", "Absender"):
        expect(bar.locator(".chip", has_text=label)).to_have_count(0)
    expect(bar.locator(".chip", has_text="Papierkorb")).to_be_visible()      # ein Ort, kein Filter
    page.locator(".folder", has_text="Haus").click()
    expect(page.locator("#search")).to_have_attribute("placeholder", "In „Haus“ suchen …")
    page.fill("#search", "Arztbrief")                                         # liegt nicht in „Haus“
    expect(bar.locator(".chip", has_text="Überall")).to_be_visible()
    expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
    bar.locator(".chip", has_text="Überall").click()
    expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
    page.fill("#search", "Stromrechnung")                                     # im Unterordner „Strom“
    bar.locator(".chip", has_text="In „Haus“").click()
    expect(page.locator(".card", has_text="Stromrechnung 2025")).to_be_visible()
    page.fill("#search", "")
    expect(page.locator(".crumbs b")).to_have_text("Haus")                   # wieder beim Blättern im Ordner
    page.click('#mode-seg button[data-mode="tiles"]')
    expect(bar.locator(".chip", has_text="Absender")).to_be_visible()         # in den Kacheln sind die Filter da


def test_pause_and_resume_recognition(page):
    page.click("#settings-btn")
    page.evaluate("document.querySelector('#s-ai').open = true")
    expect(page.locator("#s-llm-pause-status")).to_have_text("Erkennung läuft.")
    page.select_option("#s-llm-pause-for", "morning")
    page.click("#s-llm-pause-btn")
    expect(page.locator("#s-llm-pause-status")).to_contain_text("pausiert bis morgen 07:00 Uhr")
    shown(page, "#s-llm-resume-btn")
    page.click("#s-llm-resume-btn")
    expect(page.locator("#s-llm-pause-status")).to_have_text("Erkennung läuft.")
    hidden(page, "#s-llm-resume-btn")


def test_settings_use_two_columns_on_wide_screens(page):
    # Breit (Admin): „Ordnung“ und „Server“ stehen rechts neben „Konto & Teilen“ und „Dokumente“
    page.set_viewport_size({"width": 1440, "height": 900})
    page.click("#settings-btn")
    shown(page, "#settings")
    pos = lambda: page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#settings .sgroup')]
        .filter(g => g.offsetParent).map(g => [(g.querySelector('.sgroup-h')?.firstChild.textContent || 'Über').trim(),
        [Math.round(g.getBoundingClientRect().x), Math.round(g.getBoundingClientRect().y)]]))""")
    p = pos()
    assert p["Ordnung"][0] > p["Konto & Teilen"][0] + 300 and p["Ordnung"][1] == p["Konto & Teilen"][1]
    assert p["Server"][0] == p["Ordnung"][0] and p["Dokumente"][0] == p["Konto & Teilen"][0]
    assert p["Darstellung"][1] > p["Familie"][1] > p["Dokumente"][1]        # die Optik stellt man einmal ein – weiter unten
    # Schmal: alles untereinander, „Über Papigator“ wieder ganz unten
    page.set_viewport_size({"width": 800, "height": 900})
    p = pos()
    assert len({x for x, _ in p.values()}) == 1
    assert p["Über"][1] == max(y for _, y in p.values())


def test_sentence_search_shows_removable_chips(page):
    asked = []
    # nur die Satz-Suche der Dokumente (/search) – nicht die Buchungssuche für den Hinweis „Im Konto“
    page.on("request", lambda r: asked.append(r.url) if "/search?" in r.url and "/bank/" not in r.url else None)
    smart, cards = page.locator("#smartbar"), page.locator(".card")
    # Ein einzelnes Stichwort bleibt die schnelle Live-Suche – ohne /search und ohne Chips
    page.fill("#search", "Grundsteuer")
    expect(cards).to_have_count(1)                                           # (andere Tests legen Dokumente an –
    hidden(page, "#smartbar")                                                #  darum unten Titel statt Anzahlen)
    assert not asked
    # Ein Satz: Tag „Haus“ (samt Unterordner „Strom“), Person, Zeitraum – als Chips über den Treffern
    page.fill("#search", "Haus von Alex letzte 6 Monate")
    page.press("#search", "Enter")
    shown(page, "#smartbar")
    expect(smart.locator('.chip[data-kind="word"]')).to_have_text("Haus")
    expect(smart.locator('.chip[data-kind="owner"]')).to_have_text("von Alex")
    expect(smart.locator('.chip[data-kind="range"]')).to_have_count(1)
    assert "--tag" in smart.locator('.chip[data-kind="word"]').get_attribute("style")   # Tag-Farbe wie sonst
    expect(page.locator(".card", has_text="Stromrechnung 2025")).to_be_visible()
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
    hidden(page, "#tagbar")                                                   # Filter kommen nach dem Leeren zurück
    # Zurück aus einem Treffer: Chips und Treffer sind noch da
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    page.go_back()
    shown(page, "#main")
    expect(smart.locator('.chip[data-kind="word"]')).to_have_count(1)
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
    # Chip „Haus“ weg: nur noch Person und Zeitraum – jetzt auch der Arztbrief
    smart.locator('.chip[data-kind="word"]').click()
    expect(smart.locator('.chip[data-kind="word"]')).to_have_count(0)
    expect(smart.locator('.chip[data-kind="owner"]')).to_be_visible()
    expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
    # Feld leeren: normale Übersicht
    page.fill("#search", "")
    hidden(page, "#smartbar")
    shown(page, "#tagbar")
    expect(page.locator("#tagbar .chip", has_text="Absender")).to_be_visible()
    expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    assert not page.evaluate("document.querySelector('#search').value")


def test_sentence_search_falls_back_without_server_support(page):
    # Älterer Server ohne /search: ein Satz wird wie bisher als Volltext gesucht
    page.route(re.compile(r"/search\?"), lambda route: route.fulfill(status=404, content_type="application/json",
                                                                       body='{"detail": "Not Found"}'))
    page.fill("#search", "Grundsteuer Beispieltext")
    page.press("#search", "Enter")
    expect(page.locator(".card")).to_have_count(1)
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    hidden(page, "#smartbar")
    page.fill("#search", "")
    expect(page.locator(".card", has_text="Arztbrief")).to_be_visible()


def test_sentence_search_debounce_dropped_hint_and_folder(page):
    smart = page.locator("#smartbar")
    # Ohne Enter: nach kurzer Pause; der Zeitraum 2020 passt nicht und wird gelockert – mit Hinweis
    page.fill("#search", "Grundsteuer aus 2020")
    expect(smart.locator('.chip[data-kind="word"]')).to_have_text("Grundsteuer")
    expect(smart.locator(".smart-hint")).to_have_text("Ohne „01.01.2020 – 31.12.2020“ gesucht")
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    page.fill("#search", "")
    hidden(page, "#smartbar")
    # Im geöffneten Ordner bleibt auch ein Satz die Suche in diesem Ordner
    _folders(page)
    page.locator(".folder", has_text="Haus").click()
    page.fill("#search", "Arztbrief Beispieltext")
    page.press("#search", "Enter")
    expect(page.locator("#tagbar .chip", has_text="Überall")).to_be_visible()
    hidden(page, "#smartbar")
    expect(page.locator(".card", has_text="Arztbrief")).to_have_count(0)
    page.fill("#search", "")
    page.click('#mode-seg button[data-mode="tiles"]')


def test_repeating_deadline_and_calendar_link(page, tmp_path):
    """Frist mit „Wiederholen: Jährlich“ erledigen → gleich der nächste Termin, wieder offen; dazu der .ics-Link."""
    import datetime as dt
    from e2e.conftest import make_pdf
    f = tmp_path / "Kfz-Steuer.pdf"
    f.write_bytes(make_pdf("Kfz-Steuer jährlich"))
    page.set_input_files("#file-input", str(f))
    page.locator(".card", has_text="Kfz-Steuer").click()
    shown(page, "#detail")
    hidden(page, "#m-due-ics")                                               # ohne Frist kein Kalender-Link
    if len(page.request.get("/users").json()) == 1:                          # test_invite.py legt vorher „Kim“ an
        hidden(page, "#m-due-assignee-row")                                  # allein: niemand zum Zuteilen
    day = dt.date.today() + dt.timedelta(days=5)
    page.fill("#m-due", day.strftime("%d.%m.%Y"))
    page.select_option("#m-due-repeat", "yearly")
    page.check("#m-due-done")
    page.click("#save-btn")
    expect(page.locator("#toast")).to_contain_text("nächste Frist")
    nxt = day.replace(year=day.year + 1, day=min(day.day, 28 if day.month == 2 else 31))
    expect(page.locator("#m-due")).to_have_value(nxt.strftime("%d.%m.%Y"))
    expect(page.locator("#m-due-done")).not_to_be_checked()
    expect(page.locator("#m-due-repeat")).to_have_value("yearly")
    shown(page, "#m-due-ics")
    r = page.request.get(page.locator("#m-due-ics").get_attribute("href"))
    assert r.ok and "BEGIN:VEVENT" in r.text() and "RRULE:FREQ=YEARLY" in r.text()
    page.go_back()
    shown(page, "#main")


# ---------- Ohne Datum statt Hochladedatum, Prüfgründe (16.11.2026) ----------

def _upload(page, title: str, date: str | None = None) -> int:
    """Dokument über die API des angemeldeten Browsers anlegen (Text ohne Datumsangabe), optional mit Datum."""
    from e2e.conftest import make_pdf
    return page.evaluate("""async ([title, pdf, date]) => {
        const fd = new FormData();
        fd.append("file", new Blob([new Uint8Array(pdf)], { type: "application/pdf" }), title + ".pdf");
        fd.append("title", title); fd.append("text", title + " Beispieltext");
        const d = await (await fetch("/documents", { method: "POST", body: fd })).json();
        if (date) await fetch(`/documents/${d.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify({ document_date: date }) });
        return d.id;
    }""", [title, list(make_pdf(title)), date])


def test_undated_document_shows_ohne_datum(page):
    # Ein Vertrag von 2018 ohne erkanntes Datum stand früher als „heute“ da (Hochladedatum als Dokumentdatum)
    from datetime import date
    today = date.today().strftime("%d.%m.%Y")
    _upload(page, "Mietvertrag ohne Datum")
    _upload(page, "Kaufvertrag datiert", "2018-03-05")
    page.reload()
    undated = page.locator(".card", has_text="Mietvertrag ohne Datum").locator(".sub")
    expect(undated).to_contain_text("Ohne Datum")
    expect(undated).not_to_contain_text(today)
    dated = page.locator(".card", has_text="Kaufvertrag datiert").locator(".sub")
    expect(dated).to_contain_text("05.03.2018")
    expect(dated).not_to_contain_text("Ohne Datum")
    # Detail: Datumsfeld bleibt leer, mit Hinweis; das Hochladedatum steht nur ausdrücklich als „hinzugefügt“
    page.locator(".card", has_text="Mietvertrag ohne Datum").click()
    shown(page, "#detail")
    expect(page.locator("#m-date")).to_have_value("")
    shown(page, "#m-date-hint")
    expect(page.locator("#m-info")).to_contain_text(f"hinzugefügt {today}")
    page.fill("#m-date", "01.02.2018")                          # Eintippen blendet den Hinweis aus (nicht gespeichert)
    hidden(page, "#m-date-hint")
    page.go_back()
    shown(page, "#main")
    page.locator(".card", has_text="Kaufvertrag datiert").click()
    expect(page.locator("#m-title")).to_have_value("Kaufvertrag datiert")
    expect(page.locator("#m-date")).to_have_value("05.03.2018")
    hidden(page, "#m-date-hint")
    page.go_back()
    shown(page, "#main")
    # Sortierung nach „Hinzugefügt“ nennt das Hochladedatum ausdrücklich
    page.select_option("#sort-select", "added")
    expect(page.locator(".card", has_text="Mietvertrag ohne Datum").locator(".sub")).to_contain_text(f"hinzugefügt {today}")
    page.select_option("#sort-select", "date")


def test_confirm_no_date_leaves_review(page):
    """„Hat kein Datum“ im Dokument: Datum bleibt leer, der Aufruf zum Eintragen und der Prüf-Grund verschwinden."""
    doc_id = _upload(page, "Bedienungsanleitung Herd")
    _patch(page, doc_id, {"tags": ["Haus"]})
    page.reload()
    page.locator(".card", has_text="Bedienungsanleitung Herd").click()
    expect(page.locator("#m-reasons")).to_contain_text("Kein Datum erkannt")
    expect(page.locator("#m-date-hint-text")).to_have_text("Kein Datum erkannt – bitte eintragen")
    page.click("#m-no-date")
    expect(page.locator("#m-date-hint-text")).to_have_text("Ohne Datum abgelegt")
    hidden(page, "#m-no-date")
    expect(page.locator("#m-date")).to_have_value("")
    expect(page.locator("#m-reasons")).not_to_contain_text("Kein Datum")
    page.click("#save-btn")                                    # Speichern mit leerem Datum lässt es dabei
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    expect(page.locator("#m-date-hint-text")).to_have_text("Ohne Datum abgelegt")
    page.go_back()
    shown(page, "#main")
    assert _patch(page, doc_id, {})["no_date"] == 1


def test_bulk_no_date_in_review(page):
    """Mehrere unter „Zu prüfen“ auswählen → „Hat kein Datum“: nur die ohne Datum, ein fragliches Datum bleibt."""
    a, b = _upload(page, "Garantieschein Toaster"), _upload(page, "Garantieschein Mixer")
    c = _upload(page, "Garantieschein Föhn", "2025-05-05")
    for d in (a, b, c):
        _patch(page, d, {"tags": ["Haus"]})
    page.reload()
    page.locator(".card", has_text="Garantieschein Toaster").locator(".check").click()   # außerhalb von „Zu prüfen“: kein Knopf
    shown(page, "#selection-bar")
    hidden(page, "#nodate-selected-btn")
    page.click("#clear-selection-btn")
    page.locator("#tagbar .chip", has_text="Zu prüfen").click()
    page.locator(".card", has_text="Garantieschein Toaster").locator(".check").click()
    shown(page, "#nodate-selected-btn")
    page.locator(".card", has_text="Garantieschein Mixer").click()
    page.click("#nodate-selected-btn")
    expect(page.locator("#toast")).to_contain_text("2 Dokument(e) ohne Datum abgelegt")
    hidden(page, "#selection-bar")
    # Testuploads bleiben wegen „Verarbeitung fehlgeschlagen“ stehen – der Grund „Kein Datum“ ist aber weg
    for t in ("Garantieschein Toaster", "Garantieschein Mixer"):
        expect(page.locator(".card", has_text=t).locator(".reasons")).not_to_contain_text("Kein Datum")
    assert _patch(page, c, {})["no_date"] == 0
    page.locator("#tagbar .chip", has_text="Zu prüfen").click()


def test_warranty_chip_card_and_detail(page):
    """Garantie: Chip mit Zahl, Karten mit Ende, Feld im Dokument; Speichern ohne Änderung lässt sie, wie sie ist."""
    from datetime import date
    bought = date.today().replace(year=date.today().year - 1).isoformat()
    doc = _upload(page, "Waschmaschine Kauf", bought)
    _patch(page, doc, {"warranty_months": 24, "warranty_kind": "legal", "tags": ["Haus"]})
    page.reload()
    chip = page.locator("#tagbar .chip", has_text="Garantie")
    expect(chip).to_contain_text("Garantie · 1")
    chip.click()
    card = page.locator(".card", has_text="Waschmaschine Kauf")
    expect(card.locator(".sub.warranty")).to_contain_text("Gewährleistung bis")
    expect(page.locator(".card")).to_have_count(1)
    card.click()
    expect(page.locator("#m-warranty")).to_have_value("24:legal")
    expect(page.locator("#m-warranty-hint")).to_contain_text("Gewährleistung bis")
    page.click("#save-btn")                                    # Speichern ohne Änderung
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    page.select_option("#m-warranty", "36:garantie")
    page.click("#save-btn")
    expect(page.locator("#m-warranty-hint")).to_contain_text("Garantie bis")
    page.go_back()
    shown(page, "#main")
    expect(page.locator("#tagbar .chip", has_text="Garantie")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator(".card", has_text="Waschmaschine Kauf").locator(".sub.warranty")).to_contain_text("Garantie bis")
    page.locator("#tagbar .chip", has_text="Garantie").click()  # aus
    expect(page.locator(".card .sub.warranty")).to_have_count(0)
    d = _patch(page, doc, {})
    assert (d["warranty_months"], d["warranty_kind"]) == (36, "garantie")
    # Ein Brief ohne Gewährleistung: Speichern ohne Änderung legt keine fest
    other = _upload(page, "Brief Gemeinde", "2025-02-02")
    page.reload()
    page.locator(".card", has_text="Brief Gemeinde").click()
    expect(page.locator("#m-title")).to_have_value("Brief Gemeinde")
    hidden(page, "#m-warranty-hint")
    page.click("#save-btn")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    assert _patch(page, other, {})["warranty_months"] is None
    page.go_back()
    shown(page, "#main")


def test_review_reasons_on_cards_and_detail(page):
    # Gründe kommen vom Server (review_reasons) – hier eingespeist, damit der Test nicht vom Serverstand abhängt;
    # unbekannte Codes fallen weg, ohne Feld (älterer Server) bleibt die Zeile leer
    import json
    doc_id = _upload(page, "Brief zum Prüfen")

    def inject(route):
        resp = route.fetch()
        data = resp.json()
        for d in data if isinstance(data, list) else [data]:
            if d.get("id") == doc_id:
                d["review_reasons"] = ["no_date", "no_tags", "kaputt"]
            else:
                d.pop("review_reasons", None)
        route.fulfill(response=resp, body=json.dumps(data))
    page.route(re.compile(r"/documents(\?|/\d+(\?|$))"), inject)
    page.reload()
    page.wait_for_selector(".card")
    # Außerhalb von „Prüfen“ keine Gründe auf den Karten
    expect(page.locator(".card .reasons")).to_have_count(0)
    page.locator("#tagbar .chip", has_text="Zu prüfen").click()
    card = page.locator(".card", has_text="Brief zum Prüfen")
    expect(card.locator(".reasons")).to_have_text("Kein Datum erkannt · Keine Tags")
    expect(page.locator(".card", has_text="Grundsteuer").locator(".reasons")).to_have_count(0)
    card.click()
    expect(page.locator("#m-title")).to_have_value("Brief zum Prüfen")
    expect(page.locator("#m-reasons")).to_have_text("Zu prüfen: Kein Datum erkannt · Keine Tags")
    page.go_back()
    shown(page, "#main")
    page.locator(".card", has_text="Grundsteuer").click()
    expect(page.locator("#m-title")).to_have_value("Grundsteuer")
    hidden(page, "#m-reasons")
    page.go_back()
    shown(page, "#main")
    page.locator("#tagbar .chip", has_text="Zu prüfen").click()
    page.unroute_all(behavior="ignoreErrors")   # sonst stört eine noch laufende Umleitung den nächsten Test


def test_family_settings(page):
    # Familie (am Ende, weil es Ordner anlegt): Person hinzufügen, Notfallmappe füllen und teilen, Aufräumen
    page.click("#settings-btn")
    page.locator("#s-persons-sect summary").click()
    expect(page.locator("#s-persons")).to_contain_text("Noch keine Personen")
    page.fill("#s-person-name", "Lena")
    page.press("#s-person-name", "Enter")
    expect(page.locator("#s-persons .person-pill")).to_have_text("Lena")
    page.locator("#s-emergency-sect summary").click()
    expect(page.locator("#s-emergency")).to_contain_text("Noch leer")
    expect(page.locator("#s-emergency-share")).to_be_disabled()
    page.fill("#s-emergency-q", "Grundsteuer")
    page.click("#s-emergency-search")
    page.locator("#s-emergency-results button", has_text="Hinzufügen").first.click()
    expect(page.locator("#s-emergency")).to_contain_text("Grundsteuer")
    expect(page.locator("#s-emergency-share")).to_be_enabled()
    page.click("#s-emergency-share")
    expect(page.locator("#sh-expiry")).to_have_value("365")
    page.click("#sh-create")
    expect(page.locator("#sh-result")).to_be_visible()
    page.go_back()                                             # Browser-Zurück schließt den Dialog
    expect(page.locator("#share-dialog")).not_to_be_visible()
    shown(page, "#settings")
    page.locator("#s-cleanup-sect summary").click()
    shown(page, "#s-cleanup-admin")
    page.click("#s-cleanup-run")
    expect(page.locator("#s-cleanup-state")).to_contain_text("Nichts zum Aufräumen")


# ---------- Geld ist zurück (16.11.2026): Betrag im Dokument, Sortieren, Steuer-Paket, Verträge & Abos ----------

def _patch(page, doc_id: int, body: dict) -> dict:
    return page.evaluate("""async ([id, body]) => (await fetch(`/documents/${id}`, { method: "PATCH",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json()""", [doc_id, body])


def test_amount_fields_save_and_reload(page):
    doc_id = _upload(page, "Rechnung mit Betrag", "2025-09-15")
    page.reload()
    page.locator(".card", has_text="Rechnung mit Betrag").click()
    shown(page, "#detail")
    expect(page.locator("#m-amount")).to_have_value("")
    page.fill("#m-amount", "547.88")
    page.fill("#m-currency", "eur")
    page.select_option("#m-direction", "out")
    page.click("#save-btn")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    page.go_back()
    shown(page, "#main")
    page.reload()
    page.locator(".card", has_text="Rechnung mit Betrag").click()
    expect(page.locator("#m-title")).to_have_value("Rechnung mit Betrag")
    expect(page.locator("#m-amount")).to_have_value("547.88")
    expect(page.locator("#m-currency")).to_have_value("EUR")
    expect(page.locator("#m-direction")).to_have_value("out")
    # Einnahme und leerer Betrag
    page.select_option("#m-direction", "in")
    page.fill("#m-amount", "")
    page.click("#save-btn")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    doc = page.evaluate("async (id) => (await fetch(`/documents/${id}`)).json()", doc_id)
    assert doc["amount"] is None and doc["direction"] == "in"
    page.go_back()
    shown(page, "#main")


def test_sort_by_amount(page):
    big = _upload(page, "Teure Reparatur", "2024-01-10")
    small = _upload(page, "Kleiner Beleg", "2024-01-11")
    _patch(page, big, {"amount": 9999.99})
    _patch(page, small, {"amount": 0.5})
    page.reload()
    page.select_option("#sort-select", "amount")
    expect(page.locator(".card").first).to_contain_text("Teure Reparatur")
    expect(page.locator("#dir-btn")).to_have_attribute("title", "höchster zuerst")
    page.click("#dir-btn")
    expect(page.locator(".card").first).to_contain_text("Kleiner Beleg")
    page.click("#dir-btn")
    page.select_option("#sort-select", "date")


def test_tax_package_download(page, tmp_path):
    import json
    import zipfile
    doc_id = _upload(page, "Handwerkerrechnung", "2025-04-11")
    _patch(page, doc_id, {"amount": 240, "currency": "EUR", "direction": "out", "sender": "Malerbetrieb"})
    page.click("#settings-btn")
    shown(page, "#settings")
    page.locator("summary", has_text="Export").click()
    shown(page, "#s-pkg-btn")
    page.select_option("#s-pkg-year", "2025")
    with page.expect_download() as dl:
        page.click("#s-pkg-btn")
    path = tmp_path / "paket.zip"
    dl.value.save_as(path)
    z = zipfile.ZipFile(path)
    sums = json.loads(z.read("summen.json"))
    assert sums["spent"] >= 240 and any("Malerbetrieb Handwerkerrechnung" in n for n in z.namelist())


def test_contracts_section_renders(page):
    from datetime import date
    today = date.today()
    for i in (3, 2, 1):                                          # drei Monatsbelege → erkanntes Abo
        y, m = divmod(today.month - 1 - i, 12)
        day = date(today.year + y, m + 1, 5).isoformat()
        doc_id = _upload(page, f"Streaming Rechnung {i}", day)
        _patch(page, doc_id, {"amount": 13.99, "currency": "EUR", "direction": "out", "sender": "Streamingdienst"})
    page.click("#settings-btn")
    shown(page, "#settings")
    page.locator("summary", has_text="Verträge").click()
    contract = page.locator("#s-contracts .contract", has_text="Streamingdienst")
    expect(contract).to_be_visible()
    expect(contract).to_contain_text("13,99 €")
    expect(contract).to_contain_text("monatlich")
    expect(page.locator("#s-contracts")).to_contain_text("pro Monat")
    contract.click()                                             # führt zu den Belegen dieses Absenders
    shown(page, "#main")
    expect(page.locator(".card", has_text="Streaming Rechnung")).to_have_count(3)


# ---------- Farbthemen (wie in der iOS-App) ----------
COLOR_THEMES = ["wiese", "sonne", "ozean", "lavendel", "bonbon", "schlicht"]


def _mesh(page):
    return page.evaluate("getComputedStyle(document.body, '::before').backgroundImage")


def _accent(page):
    return page.evaluate("getComputedStyle(document.querySelector('label.primary[for=file-input]')).backgroundImage")


def test_color_theme_switch_persists_after_reload(page):
    html = page.locator("html")
    expect(html).to_have_attribute("data-color", "wiese")                 # Standard: Wiese
    mesh, accent = _mesh(page), _accent(page)
    page.click("#settings-btn")
    expect(page.locator('#color-picker .sw[data-color="wiese"]')).to_have_attribute("aria-checked", "true")
    page.click('#color-picker .sw[data-color="ozean"]')
    expect(html).to_have_attribute("data-color", "ozean")
    expect(page.locator('#color-picker .sw[data-color="ozean"]')).to_have_class(re.compile(r"\bactive\b"))
    assert _mesh(page) != mesh and _accent(page) != accent               # Hintergrund und Akzent wechseln sofort
    page.reload()
    page.wait_for_selector("#main:not(.hidden), #settings:not(.hidden)")
    expect(html).to_have_attribute("data-color", "ozean")                 # gemerkt (pro Browser)
    assert _mesh(page) != mesh and _accent(page) != accent
    page.evaluate("localStorage.setItem('bl.color', 'quatsch')")         # Unbekanntes → Wiese
    page.reload()
    page.wait_for_selector("#main:not(.hidden), #settings:not(.hidden)")
    expect(html).to_have_attribute("data-color", "wiese")


def test_every_color_theme_light_and_dark_without_errors(page):
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        seen = set()
        for t in COLOR_THEMES:
            page.evaluate(f"localStorage.setItem('bl.color', '{t}')")
            page.reload()
            page.wait_for_selector(".card")
            expect(page.locator("html")).to_have_attribute("data-color", t)
            seen.add((_mesh(page), _accent(page)))
            page.click('#mode-seg button[data-mode="folders"]')          # Ordner-Kacheln und Einstellungen auch zeichnen
            expect(page.locator(".folder", has_text="Haus")).to_be_visible()
            page.click("#settings-btn")
            shown(page, "#color-picker")
            page.click("#settings-back")
            shown(page, "#main")
            page.wait_for_function("!(history.state && history.state.layer)")
            page.click('#mode-seg button[data-mode="tiles"]')
        assert len(seen) == len(COLOR_THEMES), f"Themen sehen gleich aus ({scheme})"
    assert not page.errors


def test_greeting_folder_icons_and_empty_states(page):
    expect(page.locator("#hello-title")).to_contain_text("Alex")
    page.click('#mode-seg button[data-mode="folders"]')
    # Passendes Symbol nach dem Namen: Haus → Haus; Spezielles zuerst, Englisches nur als ganzes Wort
    expect(page.locator('.folder[data-folder="Haus"] .ic path')).to_have_attribute("d", re.compile(r"^M4 21V9"))
    icon = lambda name: page.evaluate("(n) => folderIcon(n)", name)
    assert "M12 20s" in icon("Hausarzt") and "M12 3l7 4" in icon("Autoversicherung")
    assert "M5 16h14" in icon("My car") and icon("Karten") == icon("Sonstiges")
    page.click('#mode-seg button[data-mode="tiles"]')
    page.fill("#search", "xyzgibtesnicht")
    shown(page, "#empty")
    expect(page.locator("#empty-title")).to_have_text("Nichts gefunden")
    page.wait_for_function("document.querySelector('#empty img').naturalWidth > 0")   # Alligator geladen
    hidden(page, "#empty-upload")
    # Prüfen und Papierkorb leer (nur die Anzeige, ohne die Testdaten zu ändern)
    page.evaluate("state.docs = []; state.review = true; renderGrid()")
    expect(page.locator("#empty-title")).to_have_text("Alles geprüft!")
    page.evaluate("state.review = false; state.trash = true; renderGrid()")
    expect(page.locator("#empty-title")).to_have_text("Papierkorb ist leer")
    page.evaluate("state.trash = false; state.q = ''; state.smart = null; renderGrid()")
    expect(page.locator("#empty-title")).to_have_text("Noch ganz leer hier")
    shown(page, "#empty-upload")


def test_phone_width_has_no_horizontal_scroll(page):
    """Bei 390 px passt alles: Suche in eigener Zeile, Hochladen als runder Knopf, Einstellungen erreichbar."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.wait_for_selector(".card")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    box = page.locator("#settings-btn").bounding_box()
    assert box and box["x"] + box["width"] <= 390
    page.click("#settings-btn")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_demo_banner_and_locked_account_controls(page):
    """Öffentliche Demo (/auth/me → demo): Hinweis über der Übersicht, auch bei 390 px ohne seitliches Scrollen;
    in den Einstellungen keine Knöpfe, die der Server in der Demo ablehnt. Ohne Demo kein Hinweis."""
    import json
    hidden(page, "#demo-banner")

    def as_demo(route):
        resp = route.fetch()
        data = resp.json()
        data["demo"] = True
        route.fulfill(response=resp, body=json.dumps(data))
    page.route(re.compile(r"/auth/me$"), as_demo)
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.wait_for_selector(".card")
    expect(page.locator("#demo-banner")).to_have_text("Demo mit erfundenen Daten – wird täglich zurückgesetzt")
    shown(page, "#demo-banner")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.click("#settings-btn")
    page.wait_for_selector("#settings:not(.hidden)")
    page.locator("summary", has_text="Konto & Geräte").click()
    shown(page, "#s-demo-account")
    hidden(page, "#s-account-edit")
    hidden(page, "#s-sessions-revoke")
    hidden(page, "#s-share-add-row")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert not page.errors, page.errors


def _today(page):
    page.click('#mode-seg button[data-mode="today"]')
    shown(page, "#todayview")


def test_today_im_blick_and_detail_fields(page):
    """„Im Blick“ in Heute: bald ablaufend und Erstattung offen; Klick öffnet das Dokument mit den Feldern, Zurück führt heim."""
    from datetime import date, timedelta
    exp = (date.today() + timedelta(days=20)).isoformat()
    card = _upload(page, "Bankkarte Sparkasse")
    _patch(page, card, {"expires_on": exp, "tags": ["Bank"]})
    bill = _upload(page, "Arztrechnung Dr. Muster", "2026-10-02")
    _patch(page, bill, {"refund_state": "open", "tags": ["Arzt"]})
    page.reload()
    _today(page)
    blick = page.locator(".t-blick")
    expect(blick).to_contain_text("Läuft ab")
    expect(blick).to_contain_text("Bankkarte Sparkasse")
    expect(blick).to_contain_text("Erstattung offen")
    blick.locator(".t-row", has_text="Bankkarte Sparkasse").locator(".t-open").click()
    expect(page.locator("#m-title")).to_have_value("Bankkarte Sparkasse")
    shown(page, "#m-expires-row")
    hidden(page, "#m-refund-row")
    page.go_back()
    shown(page, "#todayview")
    page.locator(".t-blick .t-row", has_text="Erstattung offen").locator(".t-open").click()
    expect(page.locator("#m-title")).to_have_value("Arztrechnung Dr. Muster")
    expect(page.locator("#m-refund")).to_have_value("open")
    page.select_option("#m-refund", "done")
    page.click("#save-btn")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    page.go_back()
    shown(page, "#todayview")
    expect(page.locator(".t-blick")).not_to_contain_text("Erstattung offen")
    assert _patch(page, bill, {})["refund_state"] == "done"
    _patch(page, card, {"expires_on": ""})


def test_today_view_deadlines_family_new_and_back(page):
    """„Heute“: Fristen zum Abhaken, das Neueste, Familie; Klick ins Dokument und Browser-Zurück landen wieder hier."""
    from datetime import date, timedelta
    doc = _upload(page, "Hundesteuer")
    _patch(page, doc, {"due_date": (date.today() + timedelta(days=3)).isoformat(), "due_note": "Abbuchung prüfen"})
    page.evaluate("fetch('/persons', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Mia' }) })")
    _today(page)
    row = page.locator(".t-row", has_text="Hundesteuer")
    expect(row).to_be_visible()
    expect(row.locator(".t-pill")).to_have_text("in 3 Tagen")
    hidden(page, "#sort-select")
    expect(page.locator("#tagbar .chip", has_text="Tag")).to_have_count(0)   # nur Orte wie in den Ordnern
    expect(page.locator(".t-doc")).not_to_have_count(0)
    page.locator(".t-doc", has_text="Hundesteuer").click()             # das Neueste steht vorn
    shown(page, "#detail")
    page.go_back()
    shown(page, "#todayview")
    # Familie (Mia ist neu, die Familie wird höchstens einmal pro Minute nachgezählt): neu laden
    page.reload()
    shown(page, "#todayview")
    page.locator(".t-person", has_text="Mia").click()
    expect(page.locator("#smartbar .smart-chip")).to_contain_text("von Mia")
    hidden(page, "#todayview")
    page.locator("#smartbar .smart-chip").click()                      # ✕: zurück zu „Heute“
    shown(page, "#todayview")
    # Abhaken: Frist verschwindet nach dem Auffrischen
    row.locator(".t-check").click()
    expect(page.locator(".t-row", has_text="Hundesteuer")).to_have_count(0)
    # „Zum Prüfen“ (Hundesteuer hat kein Datum): die Liste hat so viele Dokumente, wie die Karte verspricht
    card = page.locator(".t-review")
    n = int(re.search(r"(\d+) Dokument", card.inner_text()).group(1))
    card.click()
    hidden(page, "#todayview")
    expect(page.locator(".card")).to_have_count(n)
    page.locator("#tagbar .chip", has_text="Alle").click()
    shown(page, "#todayview")
    # Notfallmappe öffnet ihre Dokumente
    page.evaluate(f"fetch('/emergency/documents', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ document_id: {doc} }}) }})")
    page.reload()
    page.locator(".t-emerg").click()
    expect(page.locator(".card", has_text="Hundesteuer")).to_be_visible()
    page.locator("#smartbar .smart-chip").click()
    shown(page, "#todayview")
    # Neues Dokument, während „Heute“ offen ist: erscheint unter „Neu“ und zieht nach, sobald es verarbeitet ist
    _upload(page, "Wasserrechnung")
    page.reload()
    # Hier ohne OCR-Werkzeuge endet die Verarbeitung als „Fehler“ – wichtig ist, dass „Neu“ ohne Neuladen nachzieht
    expect(page.locator(".t-doc", has_text="Wasserrechnung")).to_be_visible()
    expect(page.locator(".t-doc", has_text="Wasserrechnung")).not_to_contain_text("OCR läuft", timeout=20000)
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_today_is_default_start_and_fits_phone_width(page):
    # Ohne gespeicherte Ansicht startet ein neuer Browser in „Heute“
    assert page.evaluate("localStorage.removeItem('bl.mode'); localStorage.removeItem('bl.start'); startMode()") == "today"
    page.evaluate("localStorage.setItem('bl.mode', 'today')")
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    shown(page, "#todayview")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_switching_views_does_not_move_the_switcher(page):
    """Kacheln, Ordner, Heute und Geld: Leisten gleich hoch, der Umschalter bleibt beim Klicken an seinem Platz."""
    tops = []
    for mode in ("tiles", "folders", "today", "money", "tiles"):
        page.click(f'#mode-seg button[data-mode="{mode}"]')
        page.wait_for_timeout(300)
        tops.append(page.locator("#mode-seg").bounding_box()["y"])
    assert max(tops) - min(tops) < 1, tops


BANK_CSV = ("Umsatzanzeige C24;;;\nBuchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n"
            "14.11.2026;ZALANDO SE;Winterjacke;-89,95\n28.10.2026;H&M Musterstadt;Kartenzahlung;-29,95\n"
            "21.09.2026;ZALANDO SE;Rücksendung;49,95\n13.11.2026;REWE Markt;Karte;-12,00\n"
            "10.11.2026;Flohmarkt Verein;Stand 12;-18,00\n31.10.2026;Arbeitgeber GmbH;Gehalt;2.950,00\n")


def test_money_import_overview_category_and_sorting(page):
    """Geld: Import mit Vorschau, Übersicht, Kategorie mit Browser-Zurück, Einsortieren mit Regel."""
    page.click('#mode-seg button[data-mode="money"]')
    shown(page, "#moneyview")
    page.locator('#moneyview [data-mact="import"]').first.click()
    shown(page, "#bank-dialog")
    page.set_input_files("#bk-file", files=[{"name": "c24_umsaetze.csv", "mimeType": "text/csv",
                                             "buffer": BANK_CSV.encode("cp1252")}])
    expect(page.locator("#bk-preview")).to_contain_text("6 Umsätze")
    expect(page.locator("#bk-preview")).to_contain_text("„Buchungsdatum“")
    expect(page.locator("#bk-preview")).not_to_contain_text("kind:")
    page.fill("#bk-name", "C24 Girokonto")
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    mv = page.locator("#moneyview")
    expect(mv).to_contain_text("C24 Girokonto")
    page.click('#moneyview [data-mperiod="all"]')
    expect(mv.locator(".m-bar", has_text="Kleidung")).to_contain_text("69,95")
    mv.locator(".m-bar", has_text="Kleidung").click()
    expect(mv.locator("#m-det-h")).to_have_text("Kleidung")
    expect(mv).to_contain_text("Am meisten bei zalando")
    mv.locator(".m-tx", has_text="H&M").locator(".m-recat").select_option("Haushalt")   # falsch einsortiert → umstellen
    expect(mv.locator(".m-bar", has_text="Haushalt")).to_contain_text("29,95")
    page.go_back()                                             # Browser-Zurück schließt die Kategorie
    expect(mv.locator("#m-det-h")).to_have_count(0)
    shown(page, "#moneyview")
    # Nicht einsortiert: Auswahl zeigt „Kategorie wählen …“, nicht die erste Kategorie
    mv.locator(".m-bar", has_text="Nicht einsortiert").click()
    expect(mv.locator(".m-tx", has_text="Flohmarkt Verein").locator(".m-recat option:checked")).to_have_text("Kategorie wählen …")
    page.go_back()
    expect(mv.locator("#m-det-h")).to_have_count(0)
    # Der Einsortieren-Knopf bricht um und bleibt in seiner Spalte (lief vorher rechts hinaus)
    for w in (1440, 900):
        page.set_viewport_size({"width": w, "height": 900})
        assert mv.locator('[data-mact="sort"]').evaluate("b => b.scrollWidth <= b.clientWidth + 1")
    mv.locator('[data-mact="sort"]').click()
    card = mv.locator("[data-tx]", has_text="Flohmarkt Verein")
    expect(card).to_be_visible()
    card.locator('[data-mpick="Kinder"]').click()
    expect(card).to_have_count(0)
    page.go_back()                                             # zurück zur Übersicht
    expect(mv.locator(".m-bar", has_text="Kinder")).to_contain_text("18,00")
    mv.locator('[data-mact="undo"]').click()
    expect(mv).to_contain_text("Wofür")
    page.click('#mode-seg button[data-mode="tiles"]')
    hidden(page, "#moneyview")


def test_money_import_with_accounts_in_file(page):
    """C24-Export mit allen Konten: Kontenliste in der Vorschau, privates Konto abwählbar."""
    csv_ = ("Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Feld 6;Feld 7;Feld 8\n"
            "Lastschrift;12.11.26;-40,00 €;REWE Markt;Karte;;1000000001;Gemeinschaftskonto\n"
            "Pocket-Umbuchung;13.11.26;-200,00 €;Gemeinschaftskonto;Sparen;;2000000002;Urlaub\n"
            "Pocket-Umbuchung;13.11.26;200,00 €;Urlaub;Sparen;Alex;1000000001;Gemeinschaftskonto\n")
    page.click('#mode-seg button[data-mode="money"]')
    page.locator('#moneyview [data-mact="import"]').first.click()
    page.set_input_files("#bk-file", files=[{"name": "alle.csv", "mimeType": "text/csv", "buffer": csv_.encode()}])
    expect(page.locator("#bk-preview")).to_contain_text("2 Konten in der Datei")
    hidden(page, "#bk-name-row")
    page.locator(".bk-accounts .m-tx", has_text="Urlaub").locator("[data-bkshare]").uncheck()
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    expect(page.locator("#moneyview .m-sub")).to_contain_text("2 Konten")            # Kopf zählt, statt aufzuzählen
    expect(page.locator("#moneyview .m-sub")).not_to_contain_text("Urlaub")
    expect(page.locator("#m-account option").first).to_have_text("Alle 2 Konten")
    expect(page.locator("#moneyview .m-tx", has_text="Urlaub")).to_contain_text("nur du")
    expect(page.locator("#moneyview")).to_contain_text("Gemeinschaftskonto")
    page.locator('#moneyview [data-mact="undo"]').click()
    expect(page.locator("#moneyview")).not_to_contain_text("Urlaub")


def test_money_account_visibility_can_change_later(page):
    """„Wer sieht's?“ am Konto: nachträglich teilen und wieder privat stellen."""
    users = page.request.get("/users").json()
    sam = next((u for u in users if u["username"] == "sam-e2e"), None)
    if not sam:
        r = page.request.post("/users", data={"username": "sam-e2e", "display_name": "Sam", "password": "sam-e2e-pass-123"})
        assert r.ok, r.text()
        sam = r.json()
    assert page.request.put("/shares", data={"user_id": sam["id"], "can_edit": False}).ok
    try:
        csv_ = "Buchungstag;Empfänger;Verwendungszweck;Betrag\n03.11.2026;Bäckerei Korn;Brötchen;-4,20\n"
        page.click('#mode-seg button[data-mode="money"]')
        page.locator('#moneyview [data-mact="import"]').first.click()
        page.set_input_files("#bk-file", files=[{"name": "spar.csv", "mimeType": "text/csv", "buffer": csv_.encode()}])
        page.select_option("#bk-account", "")
        page.fill("#bk-name", "Sparkonto Test")
        for box in page.locator("#bk-people input").all():                 # test_invite.py legt vorher „Kim“ an
            box.uncheck()
        expect(page.locator("#bk-go")).to_be_enabled()
        page.click("#bk-go")
        row = page.locator("#moneyview .m-tx", has_text="Sparkonto Test")
        expect(row).to_contain_text("nur du")
        row.locator("[data-mvisbtn]").click()
        page.locator("#moneyview .m-share:not(.hidden) label", has_text="Sam").locator("input").check()
        expect(page.locator("#toast")).to_contain_text("mitgeteilt")
        expect(row).to_contain_text("sichtbar für Sam")
        acc = next(a for a in page.request.get("/bank/accounts").json()["accounts"] if a["name"] == "Sparkonto Test")
        assert acc["visible_to"] == [sam["id"]]
        page.locator("#moneyview .m-share:not(.hidden) label", has_text="Sam").locator("input").uncheck()
        expect(row).to_contain_text("nur du")
        page.on("dialog", lambda d: d.accept())
        row.locator("[data-mdel]").click()
        expect(page.locator("#moneyview")).not_to_contain_text("Sparkonto Test")
    finally:
        page.request.delete(f"/shares/{sam['id']}")


def test_money_category_open_close_with_long_list(page):
    """Viele Buchungen: Kategorie öffnet sich oben – die Seite springt hin, „‹ Alle Kategorien“ und Browser-Zurück
    führen zurück; Nachladen im Hintergrund zeichnet ohne Änderung nicht neu (offene Auswahl bleibt)."""
    import datetime as dt
    rows = ["Transaktionstyp;Buchungsdatum;Betrag;Zahlungsempfänger;Verwendungszweck;Kategorie;Unterkategorie"]
    names = [("REWE Markt", "Lebensmittel"), ("ZALANDO SE", "Kleidung"), ("APPLE", "Freizeit"), ("Stadtwerke", "Wohnen"),
             ("Aral", "Auto"), ("Unbekannt GmbH", "Sonstiges")]
    d0 = dt.date(2026, 11, 15)
    for i in range(240):
        n, c = names[i % len(names)]
        rows.append(f"Lastschrift;{(d0 - dt.timedelta(days=i)).strftime('%d.%m.%Y')};-{10 + i % 50},{i % 100:02d} €;{n};Zweck {i};{c};X")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.click('#mode-seg button[data-mode="money"]')
    page.locator('#moneyview [data-mact="import"]').first.click()
    page.set_input_files("#bk-file", files=[{"name": "c24.csv", "mimeType": "text/csv", "buffer": ("\n".join(rows) + "\n").encode()}])
    expect(page.locator("#bk-preview")).to_contain_text("240 Umsätze")
    page.fill("#bk-name", "Lang")
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    mv = page.locator("#moneyview")
    page.click('#moneyview [data-mperiod="all"]')
    expect(mv.locator(".m-bar", has_text="Abos")).to_be_visible()
    in_view = "e => { const r = e.getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight; }"
    mv.locator(".m-bar", has_text="Abos").click()
    expect(mv.locator("#m-det-h")).to_have_text("Abos")
    page.wait_for_function(f"({in_view})(document.querySelector('.m-detail .m-backcat'))")
    # Nachladen ohne Änderung: dasselbe Element bleibt (nichts klappt zu)
    mv.locator(".m-detail").evaluate("e => e.dataset.mark = '1'")
    page.evaluate("renderMoney()")
    page.wait_for_timeout(500)
    expect(mv.locator(".m-detail[data-mark='1']")).to_have_count(1)
    mv.locator(".m-detail .m-backcat").first.click()
    expect(mv.locator("#m-det-h")).to_have_count(0)
    page.wait_for_function(f"({in_view})(document.querySelector('#m-cat-h'))")
    mv.locator(".m-bar", has_text="Kleidung").click()
    expect(mv.locator("#m-det-h")).to_have_text("Kleidung")
    page.go_back()
    expect(mv.locator("#m-det-h")).to_have_count(0)
    mv.locator('[data-mact="undo"]').click()


def test_money_search_and_hint_from_document_search(page):
    """Suche: in „Geld“ durchsucht das Feld die Buchungen (mit Summe), in den Kacheln zeigt ein Hinweis die Treffer
    im Konto und führt per Klick dorthin."""
    page.click('#mode-seg button[data-mode="money"]')
    page.locator('#moneyview [data-mact="import"]').first.click()
    page.set_input_files("#bk-file", files=[{"name": "c24.csv", "mimeType": "text/csv", "buffer": BANK_CSV.encode("cp1252")}])
    expect(page.locator("#bk-preview")).to_contain_text("6 Umsätze")
    page.fill("#bk-name", "Giro")
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    mv = page.locator("#moneyview")
    page.fill("#search", "Zalando November 2026")
    expect(mv.locator(".t-count")).to_contain_text("1 Buchung · 89,95")
    expect(mv).to_contain_text("01.11.2026")
    mv.locator('[data-mact="clearsearch"]').click()
    expect(mv).to_contain_text("Wofür")
    # Kacheln: Dokumentsuche plus Hinweis „Im Konto“
    page.click('#mode-seg button[data-mode="tiles"]')
    page.fill("#search", "zalando")
    expect(page.locator("#bank-hint")).to_contain_text("Im Konto: 2 Buchungen")
    page.click("#bank-hint")
    shown(page, "#moneyview")
    expect(mv.locator(".t-count")).to_contain_text("2 Buchungen")
    mv.locator('[data-mact="clearsearch"]').click()
    mv.locator('[data-mact="undo"]').click()


def test_money_yearly_costs_layer_mark_and_pot(page):
    """Jährliche Kosten: Karte in Geld, eigene Ebene (Browser-Zurück), Buchung über die Suche als jährlich markieren,
    Topf wählen."""
    import datetime as _dt
    t = _dt.date.today()
    ago = lambda n: (t - _dt.timedelta(days=n)).strftime("%d.%m.%Y")
    csv_ = ("Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n"
            f"{ago(405)};Hanse Kraft Versicherung AG;Kfz-Versicherung;-655,80\n"
            f"{ago(40)};Hanse Kraft Versicherung AG;Kfz-Versicherung;-701,20\n"
            f"{ago(20)};ADAC e.V.;Mitgliedsbeitrag;-94,00\n")
    page.click('#mode-seg button[data-mode="money"]')
    page.locator('#moneyview [data-mact="import"]').first.click()
    page.set_input_files("#bk-file", files=[{"name": "giro.csv", "mimeType": "text/csv", "buffer": csv_.encode()}])
    expect(page.locator("#bk-preview")).to_contain_text("3 Umsätze")
    page.fill("#bk-name", "Giro")
    page.click("#bk-go")
    expect(page.locator("#bank-dialog")).not_to_be_visible()
    mv = page.locator("#moneyview")
    expect(mv).to_contain_text("Jährliche Kosten")
    expect(mv).to_contain_text("701,20")
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-yrow", has_text="Hanse Kraft")).to_contain_text("+7 %")
    mv.locator("#m-pot").select_option(label="Giro")
    expect(mv.locator("#m-pot option:checked")).to_have_text("Giro")
    mv.locator("#m-pot").select_option(label="kein Topf")        # das Giro ist kein Topf
    expect(mv.locator("#m-pot option:checked")).to_have_text("kein Topf")
    page.go_back()                                             # Browser-Zurück schließt die Ebene
    expect(mv).to_contain_text("Wofür")
    # nur einmal abgebucht: über die Suche als jährlich markieren
    page.fill("#search", "ADAC")
    mv.locator(".m-tx", has_text="ADAC").locator("[data-myear]").click()
    mv.locator('[data-mact="clearsearch"]').click()
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-yrow", has_text="ADAC")).to_contain_text("von Hand")
    page.keyboard.press("Escape")                              # Escape schließt die Ebene
    expect(mv).to_contain_text("Wofür")
    mv.locator('[data-mact="yearly"]').click()
    mv.locator('[data-mact="closeyearly"]').click()            # ‹ Geld
    expect(mv).to_contain_text("Wofür")
    # Esc unter den Einstellungen lässt die Ebenen in Ruhe; Zurück schließt erst die Einstellungen, dann die Ebene
    mv.locator('[data-mact="yearly"]').click()
    page.click("#settings-btn")
    shown(page, "#settings")
    page.keyboard.press("Escape")
    shown(page, "#settings")
    page.go_back()
    hidden(page, "#settings")
    expect(mv.locator(".m-layer")).to_contain_text("Jährliche Kosten")
    # Suche bei offener Ebene: die Ebene geht zu, die Treffer kommen
    page.fill("#search", "ADAC")
    expect(mv.locator(".m-tx", has_text="ADAC")).to_be_visible()
    expect(mv.locator(".m-layer")).to_have_count(0)
    page.fill("#search", "")
    # Logo oben links: aus der Ebene zurück zu „Heute“
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-layer")).to_contain_text("Jährliche Kosten")
    page.click("#brand-home")
    hidden(page, "#moneyview")
    expect(page.locator('#mode-seg button[data-mode="today"]')).to_have_class(re.compile("active"))
    # … und aus dem Papierkorb
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()
    page.locator(".chip", has_text="Papierkorb").click()
    shown(page, "#trash-bar")
    page.click("#brand-home")
    hidden(page, "#trash-bar")
    expect(page.locator('#mode-seg button[data-mode="today"]')).to_have_class(re.compile("active"))
    # Import zurücknehmen (die anderen Tests sehen kein Konto)
    page.click('#mode-seg button[data-mode="money"]')
    mv.locator('[data-mact="undo"]').click()
    expect(mv).to_contain_text("Noch kein Konto")


def test_delete_own_account(page, browser, server):
    """Einstellungen → Konto & Geräte → Konto löschen: mit Passwort, danach zurück zur Anmeldung, Anmelden geht nicht mehr."""
    import httpx
    c = httpx.Client(base_url=server)
    assert page.request.post("/users", data={"username": "weg-e2e", "display_name": "Weg",
                                             "password": "weg-e2e-pass-123"}).ok          # Alex (Admin) legt an
    ctx = browser.new_context(base_url=server, viewport={"width": 1280, "height": 860}, locale="de-DE")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("/app/")
    pg.fill("#login-user", "weg-e2e")
    pg.fill("#login-pass", "weg-e2e-pass-123")
    pg.click("#login-form button[type=submit]")
    pg.wait_for_selector("#main:not(.hidden)")
    pg.click("#settings-btn")
    pg.locator("summary", has_text="Konto & Geräte").click()
    pg.locator("#s-delete summary").click()
    pg.fill("#s-delete-pass", "weg-e2e-pass-123")
    pg.on("dialog", lambda d: d.accept())
    pg.click("#s-delete-me")
    expect(pg.locator("#login-form")).to_be_visible()
    assert c.post("/auth/login", json={"username": "weg-e2e", "password": "weg-e2e-pass-123"}).status_code == 401
    ctx.close()
    assert not errors, errors
