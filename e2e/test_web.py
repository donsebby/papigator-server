"""Web-App im echten Browser: Laden, Dokument, Ordner, Papierkorb, Einstellungen, Dialoge – und vor allem
Browser-Zurück/Vor überall (Taste im Browser = page.go_back)."""
import json
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



def test_installable(page):
    """So, wie Chrome das Manifest versteht: eigenes Fenster, Start und Bereich /app/, Symbole geladen."""
    cdp = page.context.new_cdp_session(page)
    res = cdp.send("Page.getAppManifest")
    assert not res["errors"] and res["url"].endswith("/app/manifest.webmanifest")
    m = res["manifest"]
    assert m["name"] == "Papigator" and m["display"] == "kStandalone"
    assert m["startUrl"].endswith("/app/") and m["scope"].endswith("/app/") and m["id"].endswith("/app/")
    assert {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}
    assert cdp.send("Page.getInstallabilityErrors")["installabilityErrors"] == []
    for icon in m["icons"]:
        assert page.request.get(icon["url"]).ok


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
    expect(page.locator("#tagbar .chip", has_text="Papierkorb")).to_have_count(0)   # kein Chip mehr – ein Ort
    page.click("#trash-btn")
    shown(page, "#trash-bar")
    expect(page.locator("#trash-btn")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".card", has_text="Altes Schreiben")).to_be_visible()
    expect(page.locator("#trash-close-btn")).to_have_count(0)  # kein eigener Zurück-Knopf mehr
    page.go_back()
    hidden(page, "#trash-bar")
    expect(page.locator(".crumbs b")).to_have_text("Haus")
    page.go_forward()
    shown(page, "#trash-bar")
    page.click("#trash-btn")                                   # Knopf schaltet aus
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


def _fake_ask(page, answer="Die Grundsteuer beträgt 312 € [Dok 1]."):
    """Frag Papigator ohne LLM: /ask beantwortet der Test; die Bodies landen in der zurückgegebenen Liste."""
    bodies = []

    def handle(route):
        bodies.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"answer": answer, "sources": [], "keywords": ["Grundsteuer"], "lang": "de"}))
    page.route(re.compile(r"/ask$"), handle)
    return bodies


def _ask(page, q="Wie hoch ist die Grundsteuer?"):
    """Antwort-Ebene öffnen, ohne das Suchfeld anzufassen (wie Antippen von „Papigator fragen“)."""
    page.evaluate("(q) => askNow(q)", q)
    shown(page, "#ask-panel")


def test_ask_panel_back(page):
    _fake_ask(page)
    page.click('#mode-seg button[data-mode="folders"]')
    page.locator(".folder", has_text="Haus").click()          # ein Schritt im Verlauf vor dem Panel
    _ask(page)
    page.go_back()
    hidden(page, "#ask-panel")
    _ask(page)
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


def test_enter_in_dialog_runs_main_action(page):
    """Enter im Namensfeld legt den Link an – früher löste Enter „Abbrechen“ aus und verwarf die Eingabe (27.11.2026)."""
    page.click("#settings-btn")
    page.locator("#s-links-sect summary").click()
    page.click("#s-upload-new")
    page.fill("#sh-name", "Per Enter")
    page.press("#sh-name", "Enter")
    expect(page.locator("#sh-result")).to_be_visible()
    assert "/s/" in page.input_value("#sh-url")
    page.click("#sh-close")
    expect(page.locator("#s-links")).to_contain_text("Per Enter")


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
    expect(bar.locator(".chip", has_text="Papierkorb")).to_have_count(0)     # ein Ort, kein Filter – Knopf in der Leiste
    shown(page, "#trash-btn")
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
    hidden(page, "#m-due-done")                                              # ohne Frist nichts abzuhaken
    page.fill("#m-due", day.strftime("%d.%m.%Y"))
    page.press("#m-due", "Enter")                                            # speichert sofort, ohne Knopf
    shown(page, "#m-due-done")
    page.locator("#m-more summary").click()                                  # Wiederholen steht unter „Mehr Angaben“
    page.select_option("#m-due-repeat", "yearly")
    shown(page, "#m-due-rep")
    page.click("#m-due-done")
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
    page.fill("#m-date", "01.02.2018")                          # Eintippen blendet den Hinweis aus
    hidden(page, "#m-date-hint")
    page.press("#m-date", "Escape")                             # Esc im Feld nimmt es zurück (nichts gespeichert)
    expect(page.locator("#m-date")).to_have_value("")
    shown(page, "#detail")
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
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    page.focus("#m-date")                                      # leeres Datum verlassen lässt es dabei
    page.press("#m-date", "Enter")
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
    expect(page.locator("#m-chip-warranty")).to_contain_text("Gewährleistung bis")   # als Chip in der Karte
    page.click("#m-chip-warranty")                             # öffnet „Mehr Angaben“
    expect(page.locator("#m-warranty")).to_be_visible()
    expect(page.locator("#m-warranty")).to_have_value("24:legal")
    expect(page.locator("#m-warranty-hint")).to_contain_text("Gewährleistung bis")
    page.select_option("#m-warranty", "36:garantie")
    expect(page.locator("#m-warranty-hint")).to_contain_text("Garantie bis")
    expect(page.locator("#m-chip-warranty")).to_contain_text("Garantie bis")
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
    hidden(page, "#m-chip-warranty")
    page.click("#m-title")                                     # Feld betreten und verlassen ändert nichts
    page.press("#m-title", "Enter")
    page.go_back()
    shown(page, "#main")
    assert _patch(page, other, {})["warranty_months"] is None


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


def test_update_banner_for_admins(page):
    """Neue Server-Version: Hinweis oben mit Link; „Ausblenden“ gilt für genau diese Version."""
    import json as _json
    body = _json.dumps({"current": "2026.11.25.1", "enabled": True, "latest": "2099.1.1", "newer": True,
                        "url": "https://github.com/donsebby/papigator-server/releases/tag/v2099.1.1", "notes": ""})
    page.route("**/updates", lambda r: r.fulfill(status=200, content_type="application/json", body=body))
    page.evaluate("localStorage.removeItem('bl.update.hidden')")
    page.reload()
    shown(page, "#update-banner")
    expect(page.locator("#update-text")).to_contain_text("Papigator 2099.1.1 ist da")
    expect(page.locator("#update-link")).to_have_attribute("href", re.compile("v2099.1.1"))
    page.click("#update-dismiss")
    hidden(page, "#update-banner")
    page.reload()
    page.wait_for_selector("#main:not(.hidden)")
    hidden(page, "#update-banner")
    page.unroute_all(behavior="ignoreErrors")


def _png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), (40, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_own_avatar_in_settings_and_family(page):
    """Profilbild: Einstellungen → Konto & Geräte → „Profilbild wählen“; danach im Kreis, und wieder entfernen."""
    page.click("#settings-btn")
    page.locator("summary", has_text="Konto & Geräte").click()
    expect(page.locator("#s-avatar")).not_to_have_class(re.compile("has-img"))
    with page.expect_file_chooser() as fc:
        page.click("#s-avatar-pick")
    fc.value.set_files(files=[{"name": "ich.png", "mimeType": "image/png", "buffer": _png()}])
    expect(page.locator("#s-avatar img")).to_be_visible()
    assert page.evaluate("(async () => (await fetch(document.querySelector('#s-avatar img').src)).headers.get('content-type'))()") == "image/jpeg"
    page.click("#s-avatar-del")
    expect(page.locator("#s-avatar img")).to_have_count(0)
    page.go_back()


def test_weekly_digest_settings(page):
    """Wochenübersicht (25.11.2026): Abschnitt lädt ohne JS-Fehler, ohne SMTP der Hinweis; ohne Adresse springt der
    Schalter zurück, mit Adresse bleiben Schalter, Wochentag und Adresse nach dem Neuladen gespeichert."""
    page.click("#settings-btn")
    shown(page, "#s-smtp")                                     # Admin: Mail-Versand im Server-Bereich
    page.locator("#s-digest-sect summary").click()
    shown(page, "#s-digest-nosmtp")
    expect(page.locator("#s-digest-day option")).to_have_count(7)
    expect(page.locator("#s-digest-test")).to_be_disabled()
    page.click("#s-digest-on")                                 # noch keine Adresse: der Server lehnt ab
    expect(page.locator("#s-digest-status")).to_contain_text("Empfängeradresse")
    expect(page.locator("#s-digest-on")).not_to_be_checked()
    page.fill("#s-digest-email", "alex@example.org")
    page.press("#s-digest-email", "Tab")
    expect(page.locator("#toast")).to_contain_text("Wochenübersicht gespeichert")
    page.select_option("#s-digest-day", "4")
    page.click("#s-digest-on")
    expect(page.locator("#s-digest-status")).to_contain_text("Freitag")
    page.reload()
    page.wait_for_selector("#main:not(.hidden)")
    page.click("#settings-btn")
    page.locator("#s-digest-sect summary").click()
    expect(page.locator("#s-digest-on")).to_be_checked()
    expect(page.locator("#s-digest-day")).to_have_value("4")
    expect(page.locator("#s-digest-email")).to_have_value("alex@example.org")
    page.click("#s-digest-on")                                 # wieder aus – für die übrigen Tests
    expect(page.locator("#s-digest-status")).to_contain_text("Aus")
    page.go_back()


def test_links_from_weekly_mail_open_document_and_review(page):
    """Links aus der Mail: /app/#doc-<id> öffnet das Dokument (Zurück → Übersicht), /app/#review „Zu prüfen“, /app/#weekly die Einstellung."""
    doc = next(d for d in page.request.get("/documents").json() if d["title"] == "Grundsteuer")
    page.goto("about:blank")
    page.goto(f"/app/#doc-{doc['id']}")
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Grundsteuer")
    page.go_back()
    shown(page, "#main")
    hidden(page, "#detail")
    page.goto("about:blank")
    page.goto("/app/#review")
    shown(page, "#main")
    expect(page).not_to_have_url(re.compile("#review"))
    assert page.evaluate("state.review") is True                 # Liste „Zu prüfen“
    page.goto("about:blank")
    page.goto("/app/#weekly")
    shown(page, "#settings")
    expect(page.locator("#s-digest-sect")).to_have_attribute("open", "")


def test_family_settings(page):
    # Familie (am Ende, weil es Ordner anlegt): Person hinzufügen, Notfallmappe füllen und teilen, Aufräumen – seit
    # 26.11.2026 in der Ansicht „Familie“ wie in der App; die Fristen pro Ordner (Admin) bleiben in den Einstellungen
    _upload(page, "Patientenverfügung Alex")                    # für die Checkliste der Notfallmappe
    page.click('#mode-seg button[data-mode="family"]')
    shown(page, "#family-manage")
    page.locator("#s-persons-sect summary").click()
    expect(page.locator("#s-persons")).to_contain_text("Noch keine Personen")
    page.fill("#s-person-name", "Lena")
    page.press("#s-person-name", "Enter")
    expect(page.locator("#s-persons .person-pill")).to_have_text("Lena")
    # Bild für Lena: auf den Kreis klicken, Datei wählen – danach steht das Bild da und lässt sich wieder entfernen
    with page.expect_file_chooser() as fc:
        page.locator("#s-persons .avatar-btn").click()
    fc.value.set_files(files=[{"name": "lena.png", "mimeType": "image/png", "buffer": _png()}])
    expect(page.locator("#s-persons .avatar-btn img")).to_be_visible()
    page.locator("#s-persons [data-noimg]").click()
    expect(page.locator("#s-persons .avatar-btn img")).to_have_count(0)
    page.locator("#s-emergency-sect summary").click()
    expect(page.locator("#s-emergency")).to_contain_text("Noch leer")
    expect(page.locator("#s-emergency-share")).to_be_disabled()
    page.fill("#s-emergency-q", "Grundsteuer")
    page.click("#s-emergency-search")
    page.locator("#s-emergency-results button", has_text="Hinzufügen").first.click()
    expect(page.locator("#s-emergency")).to_contain_text("Grundsteuer")
    expect(page.locator("#s-emergency-share")).to_be_enabled()
    # Checkliste: zählt, was drin ist, schlägt Passendes aus der Ablage vor – ein Klick legt es hinein
    check = page.locator("#s-emergency-check")
    expect(check).to_contain_text("Checkliste")
    expect(check).to_contain_text("0 von")
    check.locator("[data-add]", has_text="Patientenverfügung Alex").click()
    expect(page.locator("#s-emergency")).to_contain_text("Patientenverfügung Alex")
    expect(check).to_contain_text("1 von")
    expect(check.locator(".em-item.done")).to_contain_text("Patientenverfügung")
    page.click("#s-emergency-share")
    expect(page.locator("#sh-expiry")).to_have_value("365")
    page.click("#sh-create")
    expect(page.locator("#sh-result")).to_be_visible()
    page.go_back()                                             # Browser-Zurück schließt den Dialog
    expect(page.locator("#share-dialog")).not_to_be_visible()
    shown(page, "#familyview")
    page.locator("#s-cleanup-sect summary").click()
    page.click("#s-cleanup-run")
    expect(page.locator("#s-cleanup-state")).to_contain_text("Nichts zum Aufräumen")
    page.click("#settings-btn")
    page.locator("#s-sect-retention summary").click()
    shown(page, "#s-cleanup-admin")
    expect(page.locator("#s-cleanup-rules")).to_contain_text("Vorgabe:")
    page.go_back()
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


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
    page.fill("#m-amount", "547,88")                           # deutsch mit Komma
    page.press("#m-amount", "Enter")
    page.locator("#m-more summary").click()
    page.fill("#m-currency", "eur")
    page.press("#m-currency", "Enter")
    page.select_option("#m-direction", "out")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    page.go_back()
    shown(page, "#main")
    page.reload()
    page.locator(".card", has_text="Rechnung mit Betrag").click()
    expect(page.locator("#m-title")).to_have_value("Rechnung mit Betrag")
    expect(page.locator("#m-amount")).to_have_value(re.compile(r"^547,88\s€$"))
    expect(page.locator("#m-currency")).to_have_value("EUR")
    expect(page.locator("#m-direction")).to_have_value("out")
    expect(page.locator("#m-chip-direction")).to_have_text("Ausgabe")
    # Einnahme und leerer Betrag
    page.locator("#m-more summary").click()
    page.select_option("#m-direction", "in")
    page.focus("#m-amount")                                    # antippen zeigt die nackte Zahl, dann leeren
    expect(page.locator("#m-amount")).to_have_value("547,88")
    page.fill("#m-amount", "")
    page.press("#m-amount", "Enter")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    hidden(page, "#m-chip-direction")                          # ohne Betrag keine Richtung als Chip
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
    page.wait_for_function("!(history.state && history.state.layer)")   # Einstellungen samt Verlaufseintrag zu
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
    expect(page.locator("#m-chip-expires")).to_contain_text("Gültig bis")
    hidden(page, "#m-chip-refund")
    page.locator("#m-more summary").click()
    shown(page, "#m-expires-row")
    hidden(page, "#m-refund-row")
    page.go_back()
    shown(page, "#todayview")
    page.locator(".t-blick .t-row", has_text="Erstattung offen").locator(".t-open").click()
    expect(page.locator("#m-title")).to_have_value("Arztrechnung Dr. Muster")
    expect(page.locator("#m-refund")).to_have_value("open")
    expect(page.locator("#m-chip-refund")).to_have_text("Erstattung offen")
    page.click("#m-chip-refund")                               # öffnet „Mehr Angaben“
    page.select_option("#m-refund", "done")
    expect(page.locator("#m-chip-refund")).to_have_text("Erstattet")
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
    row = page.locator(".t-due .t-row", has_text="Hundesteuer")   # nicht „Zuletzt angesehen“ (28.11.2026)
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
    expect(page.locator(".t-due .t-row", has_text="Hundesteuer")).to_have_count(0)
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


def test_today_assign_several_deadlines_at_once(page):
    """„Wer kümmert sich?“ für mehrere: in „Fristen“ auf „Zuweisen“, Fristen antippen, Person wählen – alle auf einmal."""
    from datetime import date, timedelta
    ids = [_upload(page, t) for t in ("Kita-Beitrag", "Rundfunkbeitrag")]
    for i in ids:
        _patch(page, i, {"due_date": (date.today() + timedelta(days=5)).isoformat()})
    _today(page)
    card = page.locator(".t-due")
    card.locator('[data-act="assign"]').click()
    expect(card.locator(".t-assign")).to_contain_text("Fristen antippen")
    expect(card.locator(".t-check")).to_have_count(0)                    # Abhaken ruht während der Auswahl
    for t in ("Kita-Beitrag", "Rundfunkbeitrag"):
        card.locator(".t-row", has_text=t).locator(".t-open").click()
    expect(card.locator(".t-assign-n")).to_have_text("2 ausgewählt")
    expect(card.locator("#t-assign-to")).to_be_enabled()
    card.locator("#t-assign-to").select_option(label="Alex")
    expect(page.locator("#toast")).to_contain_text("2 Fristen zugewiesen")
    expect(card.locator(".t-assign")).to_have_count(0)
    me = page.evaluate("fetch('/auth/me').then(r => r.json())")["id"]
    for i in ids:
        assert page.evaluate(f"fetch('/documents/{i}').then(r => r.json())")["due_assignee"] == me
        _patch(page, i, {"due_date": ""})                                # aufräumen für die anderen Tests
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_today_money_card_and_balanced_columns(page):
    """Heute: Karte „Geld“ (jüngster Monat mit Umsätzen, größte Posten) und die Karten auf beide Spalten verteilt –
    keine große Lücke unter wenigen Fristen."""
    from datetime import date, timedelta
    d = lambda n: (date.today() - timedelta(days=n)).strftime("%d.%m.%Y")
    csv_ = ("Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n"
            f"{d(3)};REWE Markt;Einkauf;-84,20\n{d(4)};Stadtwerke;Strom Abschlag;-96,00\n{d(35)};REWE Markt;Einkauf;-61,10\n")
    acc = page.evaluate("""async (csv) => { const fd = new FormData();
        fd.append("file", new Blob([csv], { type: "text/csv" }), "giro.csv"); fd.append("account_name", "Giro");
        return (await (await fetch("/bank/import", { method: "POST", body: fd })).json()).account_id; }""", csv_)
    page.reload()
    _today(page)
    card = page.locator(".t-money")
    expect(card).to_contain_text("ausgegeben")
    expect(card.locator(".t-mcat")).not_to_have_count(0)
    # Verteilung: die Spalten unterscheiden sich höchstens um die höchste Karte
    diff, tallest = page.evaluate("""() => { const m = document.querySelector('#todayview .t-main'), s = document.querySelector('#todayview .t-side');
        const cards = [...document.querySelectorAll('#todayview .t-cols .tcard, #todayview .t-cols .t-review')];
        return [Math.abs(m.offsetHeight - s.offsetHeight), Math.max(...cards.map(c => c.offsetHeight))]; }""")
    assert diff <= tallest + 14, (diff, tallest)
    card.locator('[data-act="money"]').first.click()
    shown(page, "#moneyview")
    page.evaluate(f"fetch('/bank/accounts/{acc}', {{ method: 'DELETE' }})")
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
    expect(mv).to_contain_text("Am meisten bei ZALANDO SE")   # Schreibweise der Bank, nicht der Händler-Schlüssel (27.11.2026)
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
    # Fragen offen, darüber Jährliche Kosten: ein Esc schließt nur die Ebene, das Panel bleibt (dann das nächste Esc)
    _fake_ask(page)
    _ask(page)
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-layer")).to_contain_text("Jährliche Kosten")
    page.locator("#moneyview h2").first.click()                # Fokus aus dem Suchfeld
    page.keyboard.press("Escape")
    expect(mv).to_contain_text("Wofür")
    shown(page, "#ask-panel")
    page.keyboard.press("Escape")
    hidden(page, "#ask-panel")
    page.wait_for_function("!(history.state && history.state.layer)")
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
    page.click("#trash-btn")
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
    expect(pg.locator("#whatsnew-dialog")).to_be_visible()          # neues Konto: Tour
    pg.keyboard.press("Escape")
    expect(pg.locator("#whatsnew-dialog")).to_be_hidden()
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


def test_logout_asks_first(page):
    """Abmelden fragt nach: „Abbrechen“ lässt einen angemeldet, „OK“ meldet ab."""
    page.click("#settings-btn")
    page.once("dialog", lambda d: d.dismiss())
    page.click("#logout-btn")
    shown(page, "#settings")
    page.once("dialog", lambda d: (d.message.startswith("Wirklich abmelden") or None) and d.accept())
    page.click("#logout-btn")
    expect(page.locator("#login-form")).to_be_visible()


def test_money_layer_focus(page):
    """Ebene in Geld: Fokus auf „‹ Geld“, nach Esc zurück auf den Knopf, der sie geöffnet hat."""
    import datetime as _dt
    t = _dt.date.today()
    ago = lambda n: (t - _dt.timedelta(days=n)).strftime("%d.%m.%Y")
    csv_ = (f"Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n{ago(405)};Hanse Kraft;Kfz;-655,80\n{ago(40)};Hanse Kraft;Kfz;-701,20\n")
    acc = page.evaluate("""async (csv) => { const fd = new FormData();
        fd.append("file", new Blob([csv], { type: "text/csv" }), "giro.csv"); fd.append("account_name", "Giro");
        return (await (await fetch("/bank/import", { method: "POST", body: fd })).json()).account_id; }""", csv_)
    page.click('#mode-seg button[data-mode="money"]')
    expect(page.locator('#mode-seg button[data-mode="money"]')).to_have_attribute("aria-pressed", "true")
    mv = page.locator("#moneyview")
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-back")).to_be_focused()
    page.keyboard.press("Escape")
    expect(mv.locator('[data-mact="yearly"]')).to_be_focused()
    page.evaluate(f"fetch('/bank/accounts/{acc}', {{ method: 'DELETE' }})")


def test_version_status_in_about(page):
    """Einstellungen → Über Papigator: laufende Version, „Aktuellste Version“ mit Prüfzeit, „Jetzt prüfen“ fragt neu."""
    import json as _json
    seen = []
    def handle(route):
        seen.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"current": "2026.11.25.1", "enabled": True, "latest": "2026.11.25.1", "newer": False, "url": None,
             "notes": None, "checked": "2026-11-25T12:00:00"}))
    page.route("**/updates*", handle)
    page.click("#settings-btn")
    page.locator("summary", has_text="Über Papigator").click()
    expect(page.locator("#s-update-status")).to_contain_text("Server 2026.11.25.1")
    expect(page.locator("#s-update-status")).to_contain_text("Aktuellste Version")
    expect(page.locator("#s-version")).to_contain_text("aktuell")
    expect(page.locator("#s-update-check")).to_be_checked()
    page.click("#s-update-now")
    page.wait_for_timeout(300)
    assert any("force=true" in u for u in seen)
    page.unroute_all(behavior="ignoreErrors")


def test_document_notes(page):
    """Notiz am Dokument: hinzufügen, steht mit „Alex“ und Zeit da, wieder löschen."""
    page.locator(".card", has_text="Grundsteuer").first.click()
    shown(page, "#detail")
    expect(page.locator("#m-notes")).to_contain_text("Noch keine Notizen")
    page.fill("#m-note-text", "Bescheid geprüft, Einspruch nicht nötig")
    page.click("#m-note-add")
    expect(page.locator("#m-notes .note")).to_contain_text("Bescheid geprüft")
    expect(page.locator("#m-notes .note")).to_contain_text("Alex")
    page.locator("#m-notes [data-note-del]").click()
    expect(page.locator("#m-notes")).to_contain_text("Noch keine Notizen")


def test_calendar_subscription(page):
    """Einstellungen → Kalender-Abo: Link erstellen, der Link liefert einen Kalender, abschalten."""
    page.click("#settings-btn")
    page.locator("#s-cal-sect summary").click()
    page.click("#s-cal-create")
    url = page.locator("#s-cal-url")
    expect(url).to_have_value(re.compile(r"/calendar/.+\.ics$"))
    expect(page.locator("#s-cal-open")).to_have_attribute("href", re.compile(r"^webcal://"))
    body = page.evaluate("(async (u) => (await fetch(u, { credentials: 'omit' })).text())", url.input_value())
    assert "BEGIN:VCALENDAR" in body
    # darunter die Adresse für Home Assistant (27.11.2026): derselbe Link als summary.json
    ha = page.locator("#s-cal-ha")
    expect(ha).to_have_value(url.input_value()[:-4] + "/summary.json")
    summary = page.evaluate("(async (u) => (await fetch(u, { credentials: 'omit' })).json())", ha.input_value())
    assert {"open", "overdue", "due_7_days", "next", "today"} <= set(summary)
    page.once("dialog", lambda d: d.accept())
    page.click("#s-cal-del")
    expect(page.locator("#s-cal-create")).to_be_visible()


def test_cancel_letter_dialog(page):
    """Kündigungshelfer: Formular ausfüllen, PDF kommt als Download; eigene Angaben bleiben im Browser."""
    page.click("#settings-btn")
    page.evaluate("openCancelDialog({ sender: 'Stromwerk Süd GmbH', documents: [] })")
    expect(page.locator("#cx-to")).to_have_value("Stromwerk Süd GmbH")
    page.fill("#cx-from", "Alex Muster")
    page.fill("#cx-from-addr", "Gartenweg 3\n12345 Musterstadt")
    page.fill("#cx-about", "Stromliefervertrag")
    with page.expect_download() as dl:
        page.click("#cx-pdf")
    assert dl.value.suggested_filename.startswith("Kuendigung_Stromwerk")
    assert "Gartenweg" in page.evaluate("localStorage.getItem('bl.cancel.me')")


def test_tiles_pages(page):
    """Kacheln seitenweise: 25 pro Seite → Weiter/Zurück, Seite 1 nach Filterwechsel; Größe gemerkt."""
    for i in range(27):
        _upload(page, f"Seitentest {i:02d}")
    page.evaluate("localStorage.setItem('bl.pageSize', '25')")
    page.reload()
    page.wait_for_selector(".card")
    page.fill("#search", "Seitentest")
    expect(page.locator("#pg-info")).to_contain_text("Seite 1 von 2")
    expect(page.locator(".card")).to_have_count(25)
    page.click("#pg-next")
    expect(page.locator("#pg-info")).to_contain_text("Seite 2 von 2")
    expect(page.locator(".card")).to_have_count(2)
    page.select_option("#pg-size", "50")
    expect(page.locator(".card")).to_have_count(27)
    expect(page.locator("#pg-prev")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('bl.pageSize')") == "50"
    page.fill("#search", "")


def test_family_view(page):
    """Familie: eigene Ansicht mit „Wer kümmert sich?“, Mitgliedern, Notfallmappe und Aufräumen; Links in die Einstellungen."""
    from datetime import date, timedelta
    doc = _upload(page, "Familienfrist")
    _patch(page, doc, {"due_date": (date.today() + timedelta(days=4)).isoformat()})
    page.click('#mode-seg button[data-mode="family"]')
    fv = page.locator("#familyview")
    shown(page, "#familyview")
    expect(fv).to_contain_text("Wer kümmert sich?")
    expect(fv.locator(".f-group", has_text="Noch niemand")).to_contain_text("Familienfrist")
    expect(fv).to_contain_text("Notfallmappe")
    fv.locator('[data-sect="s-persons-sect"]').click()          # Personen stehen gleich darunter, nicht in den Einstellungen
    hidden(page, "#settings")
    expect(page.locator("#s-persons-sect")).to_have_attribute("open", "")
    expect(page.locator("#s-persons")).to_be_in_viewport()
    for sect in ("s-persons-sect", "s-emergency-sect", "s-cleanup-sect", "s-invite-sect"):
        shown(page, f"#family-manage #{sect}")                  # Einladen: Admin (der Testnutzer ist Admin)
    # Aus den Einstellungen führt der Verweis hierher
    page.click("#settings-btn")
    page.locator("#s-family-sect summary").click()
    page.locator('#s-family-sect [data-family-sect]').click()
    hidden(page, "#settings")
    shown(page, "#familyview")
    assert not page.evaluate("history.state && history.state.layer")   # Einstellungen samt Verlaufseintrag zu
    _patch(page, doc, {"due_date": ""})
    page.evaluate("localStorage.setItem('bl.mode', 'tiles')")


def test_whatsnew_tour_and_news_from_settings(page):
    """Einstellungen → Über Papigator: Einführung und „Was ist neu“ – blättern (Knöpfe, Pfeiltasten), Zurück schließt."""
    page.click("#settings-btn")
    page.locator("summary", has_text="Über Papigator").click()
    page.click("#s-tour")
    dlg = page.locator("#whatsnew-dialog")
    expect(dlg).to_be_visible()
    expect(page.locator("#wn-title")).to_have_text("Füttere ihn")
    expect(page.locator("#wn-back")).to_be_hidden()
    expect(page.locator("#wn-ill img.g1")).to_have_count(1)          # Alligator-Posen fürs Füttern
    n = page.locator("#wn-dots i").count()
    assert n >= 4
    page.click("#wn-next")
    expect(page.locator("#wn-title")).to_have_text("Alles wiederfinden")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#wn-title")).to_have_text("Füttere ihn")
    for _ in range(n - 1):
        page.keyboard.press("ArrowRight")
    expect(page.locator("#wn-next")).to_have_text("Los geht’s")
    expect(page.locator("#wn-skip")).to_be_hidden()
    page.click("#wn-next")
    expect(dlg).to_be_hidden()
    page.click("#s-whatsnew")
    expect(dlg).to_be_visible()
    expect(page.locator("#wn-tag")).to_contain_text("Neu")
    expect(page.locator("#wn-ill")).not_to_contain_text("App-Icon")
    page.go_back()                                                    # Browser-Zurück schließt den Dialog
    expect(dlg).to_be_hidden()
    expect(page.locator("#settings")).to_be_visible()


def test_kind_recipient_refs_edit_and_filter(page):
    """Detail: Dokumentart, Empfänger und Nummern ändern und speichern; Filter „Art“ zeigt nur diese Art."""
    doc_id = _upload(page, "Zeugnis Mia", "2026-01-30")
    page.reload()
    page.locator(".card", has_text="Zeugnis Mia").click()
    shown(page, "#detail")
    expect(page.locator("#m-kind option[value=zeugnis]")).to_have_count(1)
    page.select_option("#m-kind", "zeugnis")
    page.fill("#m-recipient", "Mia Muster")
    page.press("#m-recipient", "Enter")
    page.locator("#m-more summary").click()
    page.fill("#m-refs input[data-ref=other]", "S-2231")
    page.press("#m-refs input[data-ref=other]", "Enter")
    expect(page.locator("#m-refs-sum")).to_have_text("Nummern · 1")
    expect(page.locator("#save-state")).to_have_text("Gespeichert")
    doc = page.evaluate("async (id) => (await fetch(`/documents/${id}`)).json()", doc_id)
    assert doc["doc_kind"] == "zeugnis" and doc["recipient"] == "Mia Muster" and doc["refs"] == {"other": "S-2231"}
    page.go_back()
    shown(page, "#main")
    page.reload()
    page.locator("#tagbar .chip", has_text="Art").click()
    page.locator("#popover .chip", has_text="Zeugnis").click()
    expect(page.locator(".card")).to_have_count(1)
    expect(page.locator(".card", has_text="Zeugnis Mia")).to_be_visible()
    page.locator("#tagbar .chip", has_text="Zeugnis").click()       # Filter wieder weg
    expect(page.locator(".card", has_text="Zeugnis Mia")).to_be_visible()
    expect(page.locator(".card").nth(1)).to_be_visible()                 # wieder alle


def test_progress_shows_backfill(page):
    """Ältere Dokumente werden still ergänzt: der Fortschritt oben sagt es, auch wenn nichts Neues verarbeitet wird."""
    real = page.evaluate("async () => (await fetch('/health')).json()")
    fake = {**real, "documents": {"done": 12}, "backfill": 40, "backfill_total": 100}
    page.route("**/health", lambda r: r.fulfill(json=fake))
    page.evaluate("updateProgress()")
    expect(page.locator("#progress")).to_be_visible()
    expect(page.locator("#progress-label")).to_contain_text("60 von 100 fertig")
    # neue Scans und Nachtragen gleichzeitig: beides steht da
    fake = {**fake, "documents": {"done": 12, "pending": 2}}
    page.evaluate("updateProgress()")
    expect(page.locator("#progress-label")).to_contain_text("2 warten")
    expect(page.locator("#progress-label")).to_contain_text("40 ältere werden ergänzt")
    fake = {**fake, "documents": {"done": 14}, "backfill": 0}
    page.evaluate("updateProgress()")
    expect(page.locator("#progress")).to_be_hidden()
    page.unroute("**/health")


def test_kind_label_person_hint_and_kind_chip(page):
    """Dokumentart auf der Kachel (nicht bei Rechnungen), Empfänger → Person als Vorschlag im Detail, Chip „Art“ in der
    Satz-Suche. „Jo“ hat als kurzer Name keine Namensregel – genau dort hilft der Empfänger."""
    person = page.evaluate("""async () => (await fetch('/persons', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'Jo' }) })).json()""")
    doc_id = _upload(page, "Anmeldung Schwimmkurs", "2026-02-10")
    _patch(page, doc_id, {"doc_kind": "antrag", "recipient": "Jo Muster"})
    bill = _upload(page, "Rechnung Sportverein", "2026-02-11")
    _patch(page, bill, {"doc_kind": "rechnung"})
    page.reload()
    card = page.locator(".card", has_text="Anmeldung Schwimmkurs")
    expect(card.locator(".kind-tag")).to_have_text("Antrag")
    expect(page.locator(".card", has_text="Rechnung Sportverein").locator(".kind-tag")).to_have_count(0)
    # Detail: Vorschlag annehmen – Tag gesetzt, Personen-Zeile zeigt Jo, Vorschlag weg; Speichern behält den Tag
    card.click()
    shown(page, "#detail")
    hint = page.locator("#m-persons [data-person-add]")
    expect(hint).to_have_text("Person Jo zuordnen?")
    expect(page.locator("#m-persons")).to_contain_text("An Jo –")
    hint.click()
    expect(page.locator("#m-persons .person-pill")).to_have_text("Jo")
    expect(page.locator("#m-persons [data-person-add]")).to_have_count(0)
    expect(page.locator("#m-tags .chip", has_text="Jo")).to_have_count(1)
    doc = page.evaluate("async (id) => (await fetch(`/documents/${id}`)).json()", doc_id)
    assert "Jo" in {t["name"] for t in doc["tags"]} and doc["person_hints"] == []
    page.go_back()
    shown(page, "#main")
    # „Nein“ blendet den Vorschlag in diesem Browser aus
    other = _upload(page, "Kursbestätigung", "2026-02-12")
    _patch(page, other, {"doc_kind": "bestaetigung", "recipient": "Jo Muster"})
    page.reload()
    page.locator(".card", has_text="Kursbestätigung").click()
    page.locator("#m-persons [data-person-no]").click()
    expect(page.locator("#m-persons [data-person-add]")).to_have_count(0)
    page.go_back()
    page.reload()
    page.locator(".card", has_text="Kursbestätigung").click()
    shown(page, "#detail")
    expect(page.locator("#m-recipient")).to_have_value("Jo Muster")
    expect(page.locator("#m-persons [data-person-add]")).to_have_count(0)
    page.go_back()
    shown(page, "#main")
    # Satz-Suche: „Alle Anträge“ wird zum Chip „Art: Antrag“, entfernen führt zurück zur Übersicht
    page.fill("#search", "Alle Anträge")
    page.press("#search", "Enter")
    chip = page.locator('#smartbar .chip[data-kind="doc_kind"]')
    expect(chip).to_have_text("Art: Antrag")
    expect(page.locator(".card", has_text="Anmeldung Schwimmkurs")).to_be_visible()
    expect(page.locator(".card", has_text="Grundsteuer")).to_have_count(0)
    chip.click()
    hidden(page, "#smartbar")
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    # Auch ein einzelnes Wort wird zum Chip, wenn es eine Art mit Dokumenten ist – sonst bleibt es die Live-Suche
    page.fill("#search", "Anträge")
    page.press("#search", "Enter")
    expect(page.locator('#smartbar .chip[data-kind="doc_kind"]')).to_have_text("Art: Antrag")
    expect(page.locator(".card", has_text="Grundsteuer")).to_have_count(0)
    page.fill("#search", "Urkunden")                                      # keine Urkunde im Bestand
    page.press("#search", "Enter")
    hidden(page, "#smartbar")
    page.fill("#search", "")
    expect(page.locator(".card", has_text="Grundsteuer")).to_be_visible()
    page.evaluate(f"fetch('/persons/{person['id']}', {{ method: 'DELETE' }})")


# ---------- Prüf-Funde 26.11.2026: Esc, Start-Links, Zeitzone, Dialoge ----------
def test_escape_closes_document_and_ask_panel(page):
    """Esc im Dokument wirkt wie Browser-Zurück, ohne Nachfrage (gespeichert wird beim Verlassen jedes Felds); Esc in
    einem Feld nimmt nur dessen Änderung zurück. Esc schließt „Fragen“."""
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Grundsteuer")
    page.keyboard.press("Escape")
    shown(page, "#main"); hidden(page, "#detail")
    page.locator(".card", has_text="Grundsteuer").click()
    shown(page, "#detail")
    asked = []
    page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.fill("#m-sender", "Gemeinde Musterstadt")
    page.keyboard.press("Escape")                                    # im Feld: Änderung zurück, Dokument bleibt offen
    shown(page, "#detail")
    expect(page.locator("#m-sender")).to_have_value("")
    page.keyboard.press("Escape")                                    # jetzt schließt es
    shown(page, "#main"); hidden(page, "#detail")
    assert asked == []
    doc = next(d for d in page.request.get("/documents").json() if d["title"] == "Grundsteuer")
    assert doc["sender"] != "Gemeinde Musterstadt"
    # Fragen-Panel: Esc schließt es samt Verlaufseintrag
    _fake_ask(page)
    _ask(page)
    page.keyboard.press("Escape")
    hidden(page, "#ask-panel")
    page.wait_for_function("!(history.state && history.state.layer)")


def _fake_whatsnew(page):
    """„Was ist neu“ für den Testnutzer erzwingen (Alex hat es schon gesehen) – nur die Antwort, nichts gespeichert."""
    body = json.dumps({"mode": "news", "release": "2099-01-01", "label": "Test",
                       "cards": [{"title": "Neu im Test", "text": "Nur ein Test.", "anim": None}]})
    page.route(re.compile(r"/whatsnew\?"), lambda route: route.fulfill(status=200, content_type="application/json", body=body))
    page.route(re.compile(r"/whatsnew/seen"), lambda route: route.fulfill(status=200, content_type="application/json", body="{}"))


def test_start_link_is_not_covered_by_whatsnew(page):
    """Start-Link aus der Mail + neue Version: kein „Was ist neu“ über dem Link (Esc/Überspringen schlossen sonst das
    Dokument mit); es kommt erst, wenn man in die Übersicht zurückkehrt – und schließt nur sich selbst."""
    _fake_whatsnew(page)
    dlg = page.locator("#whatsnew-dialog")
    doc = next(d for d in page.request.get("/documents").json() if d["title"] == "Grundsteuer")
    page.goto("about:blank")
    page.goto(f"/app/#doc-{doc['id']}")
    shown(page, "#detail")
    page.wait_for_timeout(400)
    expect(dlg).to_be_hidden()
    page.go_back()
    shown(page, "#main"); hidden(page, "#detail")
    # Wochenübersicht: Einstellungen offen, kein Dialog; zurück in die Übersicht → jetzt „Was ist neu“, Esc schließt nur ihn
    page.goto("about:blank")
    page.goto("/app/#weekly")
    shown(page, "#settings")
    page.wait_for_timeout(400)
    expect(dlg).to_be_hidden()
    page.click("#settings-back")
    shown(page, "#main")
    expect(dlg).to_be_visible()
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    shown(page, "#main")
    page.wait_for_function("!(history.state && history.state.layer)")
    # Ein Dialog unter einer später geöffneten Ebene (hier: Einstellungen darüber) nimmt beim Schließen nicht die
    # Einstellungen mit: nur er wird ausgetragen, die Einstellungen bleiben samt ihrem Verlaufseintrag
    page.evaluate("checkWhatsNew('news')")
    expect(dlg).to_be_visible()
    page.evaluate("openSettings()")
    page.wait_for_function("history.state && history.state.layer === 'settings'")
    page.click("#wn-next")                                           # „Alles klar“ (eine Karte)
    expect(dlg).to_be_hidden()
    page.wait_for_timeout(300)
    shown(page, "#settings")
    assert page.evaluate("history.state && history.state.layer") == "settings"
    assert page.evaluate("layers.map((l) => l.name)") == ["settings"]
    page.go_back()                                                    # Zurück schließt die Einstellungen
    shown(page, "#main"); hidden(page, "#settings")
    page.unroute(re.compile(r"/whatsnew\?"))


def test_start_links_in_an_open_tab(page):
    """Link aus der Mail in einen offenen Tab eingefügt (nur der Hash ändert sich): Dokument bzw. „Zu prüfen“ öffnen."""
    doc = next(d for d in page.request.get("/documents").json() if d["title"] == "Arztbrief")
    page.evaluate(f"location.hash = '#doc-{doc['id']}'")
    shown(page, "#detail")
    expect(page.locator("#m-title")).to_have_value("Arztbrief")
    n = page.evaluate("history.length")
    page.go_back()
    shown(page, "#main"); hidden(page, "#detail")
    page.go_forward()                                                 # „Vor“ öffnet nicht ein zweites Mal über den Hash
    page.wait_for_timeout(300)
    assert page.evaluate("history.length") == n
    page.goto("/app/")
    page.wait_for_selector("#main:not(.hidden)")
    page.evaluate("location.hash = '#review'")
    page.wait_for_function("state.review === true")
    expect(page).not_to_have_url(re.compile("#review"))
    page.locator("#tagbar .chip", has_text="Alle").click()


def test_dates_west_of_utc_are_not_a_day_early(browser, server):
    """Reine Daten (JJJJ-MM-TT) erscheinen am selben Tag – auch in einer Zeitzone westlich von UTC."""
    from conftest import PASSWORD, USER
    ctx = browser.new_context(base_url=server, viewport={"width": 1280, "height": 860}, locale="de-DE",
                              timezone_id="America/Los_Angeles")
    ctx.add_init_script("try { localStorage.setItem('bl.mode', 'tiles') } catch (e) {}")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("/app/")
    pg.fill("#login-user", USER)
    pg.fill("#login-pass", PASSWORD)
    pg.click("#login-form button[type=submit]")
    pg.wait_for_selector(".card")
    assert pg.evaluate("fmtDate('2026-11-14')") == "14.11.2026"
    assert pg.evaluate("fmtDate('2026-01-01')") == "01.01.2026"
    iso = next(d for d in pg.request.get("/documents").json() if d["title"] == "Grundsteuer")["document_date"][:10]
    y, m, d = iso.split("-")
    expect(pg.locator(".card", has_text="Grundsteuer")).to_contain_text(f"{d}.{m}.{y}")
    ctx.close()
    assert not errors, errors


def test_cancel_dialog_closes_with_browser_back(page):
    """Kündigungshelfer ist ein Dialog wie die anderen: Browser-Zurück schließt ihn, die Einstellungen bleiben."""
    page.click("#settings-btn")
    shown(page, "#settings")
    page.evaluate("openCancelDialog({ sender: 'Stromwerk Süd GmbH', documents: [] })")
    expect(page.locator("#cancel-dialog")).to_be_visible()
    page.go_back()
    expect(page.locator("#cancel-dialog")).to_be_hidden()
    shown(page, "#settings")
    page.go_back()
    shown(page, "#main")


def test_amount_is_shown_formatted_in_detail(page):
    """Der Betrag steht lesbar in der Karte (Tausenderpunkt, Komma, Währung); antippen zeigt die nackte Zahl."""
    doc_id = _upload(page, "Kaufvertrag Auto", "2025-03-01")
    _patch(page, doc_id, {"amount": 18658.56, "currency": "EUR"})
    page.reload()
    page.fill("#search", "Kaufvertrag Auto")                          # (viele Dokumente aus anderen Tests – Seite 1 reicht nicht)
    page.locator(".card", has_text="Kaufvertrag Auto").click()
    shown(page, "#detail")
    expect(page.locator("#m-amount")).to_have_value(re.compile(r"^18\.658,56\s€$"))
    page.focus("#m-amount")
    expect(page.locator("#m-amount")).to_have_value("18658,56")
    page.press("#m-amount", "Enter")                                   # unverändert: nichts gespeichert, wieder lesbar
    expect(page.locator("#m-amount")).to_have_value(re.compile(r"^18\.658,56\s€$"))
    assert page.evaluate("fmtMoney(18658.56, 'XX')") == "18.658,56 XX"   # ungültiger Code bricht nichts
    page.keyboard.press("Escape")
    shown(page, "#main")
    page.fill("#search", "")
    page.evaluate(f"fetch('/documents/{doc_id}', {{ method: 'DELETE' }})")


def test_phone_width_settings_switch_money_bars_and_long_words(page):
    """390 px: Umschalter „Startansicht“ bricht um (Gleiter in der richtigen Zeile), Geld-Balken behalten Breite,
    lange Wörter sprengen keine Kachel."""
    doc_id = _upload(page, "Versicherungsunterlagen", "2025-04-01")
    _patch(page, doc_id, {"sender": "Donaudampfschifffahrtsgesellschaftskapitänswitwenrentenversicherung"})
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.fill("#search", "Versicherungsunterlagen")
    expect(page.locator(".card", has_text="Versicherungsunterlagen")).to_be_visible()
    assert page.evaluate("[...document.querySelectorAll('.card')].every((c) => c.scrollWidth <= c.clientWidth + 1)")
    page.click("#settings-btn")
    page.evaluate("document.querySelector('#start-seg').closest('details').open = true")
    seg = page.locator("#start-seg")
    expect(seg).to_be_visible()
    box = seg.bounding_box()
    assert box["x"] + box["width"] <= 390, box
    page.locator('#start-seg button[data-start="family"]').click()    # letzte Taste – nach dem Umbruch unten
    page.wait_for_timeout(600)
    knob = page.locator("#start-seg .seg-knob").bounding_box()
    btn = page.locator('#start-seg button[data-start="family"]').bounding_box()
    assert abs(knob["y"] - btn["y"]) < 2 and abs(knob["x"] - btn["x"]) < 2, (knob, btn)
    page.locator('#start-seg button[data-start="last"]').click()
    page.evaluate("localStorage.removeItem('bl.start')")
    page.go_back()
    shown(page, "#main")
    page.fill("#search", "")
    # Geld-Balken (Kategorie mit langem Namen): der Balken schrumpft nicht auf 0
    w = page.evaluate("""() => {
      const b = document.createElement('button'); b.className = 'm-bar';
      b.innerHTML = '<span class="n">Haushalt, Lebensmittel & Drogerie</span><span class="track"><i class="fill" style="width:50%"></i></span><span class="v">1.234,56 €</span>';
      const box = document.createElement('div'); box.style.width = '358px'; box.appendChild(b); document.body.appendChild(box);
      const r = b.querySelector('.track').getBoundingClientRect().width; box.remove(); return r; }""")
    assert w >= 40, w
    page.evaluate(f"fetch('/documents/{doc_id}', {{ method: 'DELETE' }})")


def test_kind_word_with_folder_of_same_name_keeps_chip_results(page):
    """Einzelnes Art-Wort im Plural („Zeugnisse“) bei einem Ordner gleichen Namens: kein Chip „Art“ (der Ordner geht
    vor), aber die Treffer der Satz-Suche werden genommen statt auf die Live-Suche zurückzufallen."""
    doc_id = _upload(page, "Zeugnisse Mia Grundschule", "2026-07-01")
    _patch(page, doc_id, {"doc_kind": "zeugnis", "tags": ["Zeugnisse"]})
    page.fill("#search", "Zeugnisse")
    page.press("#search", "Enter")
    shown(page, "#smartbar")
    expect(page.locator('#smartbar .chip[data-kind="word"]')).to_have_text("Zeugnisse")
    expect(page.locator(".card", has_text="Zeugnisse Mia Grundschule")).to_be_visible()
    page.fill("#search", "")
    hidden(page, "#smartbar")
    page.evaluate(f"fetch('/documents/{doc_id}', {{ method: 'DELETE' }})")


def test_search_placeholder_fits_on_phones(page):
    """390 px: ein Feld für Suchen und Fragen – sein Platzhalter passt ganz hinein (kein eigenes Fragen-Feld mehr)."""
    page.set_viewport_size({"width": 390, "height": 844})
    inp = page.locator("#search")
    expect(inp).to_have_attribute("placeholder", "Suchen oder fragen …")
    expect(page.locator("#ask-btn")).to_have_count(0)
    assert page.evaluate("(() => { const i = document.querySelector('#search'); const c = document.createElement('canvas').getContext('2d');"
                         " c.font = getComputedStyle(i).font; return c.measureText(i.placeholder).width <= i.clientWidth - 50; })()")
    page.set_viewport_size({"width": 1280, "height": 860})
