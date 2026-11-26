"""Web-App auf Englisch (i18n.js): Sprache nach dem Browser, umschaltbar in Einstellungen → Darstellung,
jeder statische Text hat eine englische Fassung. Die übrigen Browser-Tests laufen mit de-DE (conftest.py)."""
import re

from playwright.sync_api import expect

from conftest import PASSWORD, USER


def _login(browser, server, locale):
    """Eigene Seite mit Browsersprache `locale` (die page-Fixture ist fest auf de-DE)."""
    ctx = browser.new_context(base_url=server, viewport={"width": 1280, "height": 860}, locale=locale)
    ctx.add_init_script("try { if (!localStorage.getItem('bl.mode')) localStorage.setItem('bl.mode', 'tiles') } catch (e) {}")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("/app/")
    expect(pg.locator("#login-form button[type=submit]")).to_have_text("Sign in")
    pg.fill("#login-user", USER)
    pg.fill("#login-pass", PASSWORD)
    pg.click("#login-form button[type=submit]")
    pg.wait_for_selector("#main:not(.hidden)")
    pg.wait_for_selector(".card")
    return ctx, pg, errors


def _doc_date(pg, title):
    iso = pg.evaluate(f"""fetch('/documents?q={title}').then((r) => r.json())
                          .then((r) => (r.items || r).find((d) => d.title === '{title}').document_date)""")
    return iso[:10].split("-")


# Texte aus index.html, die in beiden Sprachen gleich bleiben (Marken, Formate, Beispiele, Zeichen)
_NEUTRAL = re.compile(r"^[\W\d_]*$|^(Papigator|Admin|Server|System|Tags|Export|English|Deutsch|OCR|IMAP|SMTP|FTP|"
                      r"DD/MM/YYYY|MM/DD/YYYY|TT\.MM\.JJJJ|JJJJ-MM-TT|(?=.*[0-9.:_/@+…-])[a-z0-9.:_/@+…-]+|[A-Z0-9_]{2,}|\d+ px|"
                      r".+ · Cloud|AGPL 3\.0|.*\b(MIT|BSD|GPL|MPL|Apache|Public Domain|Gemma Terms)\b.*|"
                      r"Anthropic.*|OpenAI.*|FastAPI|Gemma|Ghostscript|OCRmyPDF|Ollama|Pillow|Poppler|Port|Pydantic|"
                      r"bcrypt|httpx|pngquant|pypdf|qpdf|unpaper|SQLite|SSL/TLS|STARTTLS|THIRD_PARTY\.md|Tesseract OCR|Uvicorn|– Server)$")


def test_every_static_text_has_an_english_version(page):
    """Jeder Text in index.html, den i18n.js ersetzen würde, steht im Wörterbuch – so gelesen wie translateDom."""
    missing = page.evaluate("""async () => {
      const html = await (await fetch('/app/index.html')).text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const norm = (s) => s.replace(/\\s+/g, ' ').trim(), keys = new Set();
      for (const el of doc.querySelectorAll('[data-i18n]')) keys.add(el.dataset.i18n);
      for (const el of doc.querySelectorAll('[data-i18n-html]')) keys.add(norm(el.innerHTML));
      const w = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, { acceptNode: (n) =>
        n.parentElement.closest('script,style,[data-i18n],[data-i18n-html],[data-i18n-skip]') ? 2 : 1 });
      for (let n = w.nextNode(); n; n = w.nextNode()) if (norm(n.nodeValue)) keys.add(norm(n.nodeValue));
      for (const a of ['placeholder', 'title', 'aria-label', 'alt'])
        for (const el of doc.body.querySelectorAll('[' + a + ']'))
          if (norm(el.getAttribute(a))) keys.add(norm(el.getAttribute(a)));
      return [...keys].filter((k) => !Object.prototype.hasOwnProperty.call(EN, k));
    }""")
    missing = sorted(k for k in missing if not _NEUTRAL.match(k))
    assert not missing, f"{len(missing)} Texte aus index.html ohne englische Fassung: {missing[:40]}"


def test_english_interface_loads_and_main_views(browser, server):
    ctx, pg, errors = _login(browser, server, "en-US")
    expect(pg.locator("html")).to_have_attribute("lang", "en")
    for mode, label in [("today", "Today"), ("tiles", "Tiles"), ("folders", "Folders"), ("money", "Money"),
                        ("family", "Family")]:
        expect(pg.locator(f'#mode-seg button[data-mode="{mode}"]')).to_have_text(label)
    expect(pg.locator("#tagbar .chip", has_text="To review")).to_be_visible()
    expect(pg.locator("#search")).to_have_attribute("placeholder", "Search or ask …")
    y, m, d = _doc_date(pg, "Grundsteuer")
    expect(pg.locator(".card", has_text="Grundsteuer")).to_contain_text(f"{m}/{d}/{y}")   # USA: MM/DD/YYYY
    # Heute
    pg.click('#mode-seg button[data-mode="today"]')
    expect(pg.locator("#hello-title")).to_have_text(re.compile(r"^Good (morning|afternoon|evening|day), Alex$"))
    expect(pg.locator("#hello-date")).not_to_have_text(re.compile(r"tag|woch", re.I))
    # Ordner, Geld, Familie
    pg.click('#mode-seg button[data-mode="folders"]')
    expect(pg.locator(".crumbs b")).to_have_text("All folders")
    for mode in ("money", "family"):
        pg.click(f'#mode-seg button[data-mode="{mode}"]')
        pg.wait_for_timeout(300)
    # Dokument: Felder, Arten vom Server auf Englisch (/documents/kinds?lang=en), Datumsfeld im US-Format
    pg.click('#mode-seg button[data-mode="tiles"]')
    pg.locator(".card", has_text="Grundsteuer").click()
    expect(pg.locator("#detail")).to_be_visible()
    expect(pg.locator("#m-date")).to_have_attribute("placeholder", "MM/DD/YYYY")
    expect(pg.locator("#m-date")).to_have_value(f"{m}/{d}/{y}")
    expect(pg.locator("#m-kind option", has_text="Invoice")).to_have_count(1)
    expect(pg.locator("#m-more summary")).to_have_text("More details")
    expect(pg.locator("#save-btn")).to_have_count(0)                     # kein Speichern-Knopf mehr
    pg.click("#share-btn")
    expect(pg.locator("#share-dialog")).to_be_visible()
    expect(pg.locator("#share-dialog")).not_to_contain_text("Freigabe")
    pg.go_back()
    expect(pg.locator("#share-dialog")).to_be_hidden()
    pg.go_back()
    expect(pg.locator("#main")).to_be_visible()
    # Fragen: im Suchfeld, „Ask Papigator“ darunter; die Frage geht mit lang=en an den Server
    bodies = []
    pg.route(re.compile(r"/ask$"), lambda route: (bodies.append(route.request.post_data_json), route.fulfill(
        status=200, content_type="application/json",
        body='{"answer": "Your property tax is 312 EUR.", "sources": [], "keywords": [], "lang": "en"}'))[1])
    pg.fill("#search", "What is my property tax?")
    expect(pg.locator("#ask-offer")).to_contain_text("Ask Papigator")
    pg.press("#search", "Enter")
    expect(pg.locator("#ask-panel")).to_be_visible()
    expect(pg.locator("#ask-answer")).to_have_text("Your property tax is 312 EUR.")
    expect(pg.locator("#ask-panel")).not_to_contain_text("Frag ")
    assert bodies and bodies[0]["lang"] == "en", bodies
    pg.go_back()
    expect(pg.locator("#ask-panel")).to_be_hidden()
    pg.fill("#search", "")
    # Einstellungen: Sprachwahl „Automatisch“, Sprache der Erkennung vom Server
    pg.click("#settings-btn")
    expect(pg.locator("#settings")).to_be_visible()
    expect(pg.locator('#lang-seg button[data-lang="auto"]')).to_have_text("Automatic")
    expect(pg.locator('#lang-seg button[data-lang="auto"]')).to_have_class(re.compile(r"\bactive\b"))
    pg.locator("#settings summary", has_text="AI recognition").first.click()
    expect(pg.locator("#settings")).to_contain_text("Recognition language")
    expect(pg.locator("#s-llm-lang")).to_have_value("de")
    pg.go_back()
    expect(pg.locator("#main")).to_be_visible()
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)


def test_british_english_and_other_languages(browser, server):
    ctx, pg, errors = _login(browser, server, "en-GB")
    pg.locator(".card", has_text="Grundsteuer").click()
    expect(pg.locator("#m-date")).to_have_attribute("placeholder", "DD/MM/YYYY")
    ctx.close()
    ctx2, pg2, errors2 = _login(browser, server, "fr-FR")          # weder Deutsch noch Englisch → Englisch
    expect(pg2.locator("html")).to_have_attribute("lang", "en")
    expect(pg2.locator('#mode-seg button[data-mode="tiles"]')).to_have_text("Tiles")
    ctx2.close()
    assert not errors and not errors2, errors + errors2


def test_language_setting_switches_and_persists(page):
    expect(page.locator("html")).to_have_attribute("lang", "de")
    page.click("#settings-btn")
    expect(page.locator('#lang-seg button[data-lang="auto"]')).to_have_class(re.compile(r"\bactive\b"))
    with page.expect_navigation():
        page.click('#lang-seg button[data-lang="en"]')                    # lädt neu
    page.wait_for_selector("#main:not(.hidden)")
    # Vor dem Neuladen ist der Eintrag der Einstellungen zurückgenommen – kein toter Schritt „Zurück“
    assert not page.evaluate("history.state && history.state.layer")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator('#mode-seg button[data-mode="tiles"]')).to_have_text("Tiles")
    page.reload()
    page.wait_for_selector("#main:not(.hidden)")
    expect(page.locator("html")).to_have_attribute("lang", "en")          # gemerkt (pro Browser)
    page.click("#settings-btn")
    expect(page.locator('#lang-seg button[data-lang="en"]')).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator('#lang-seg button[data-lang="de"]')).to_have_text("Deutsch")   # Sprachnamen bleiben
    with page.expect_navigation():
        page.click('#lang-seg button[data-lang="auto"]')                  # Automatisch: de-DE → wieder Deutsch
    page.wait_for_selector("#main:not(.hidden)")
    expect(page.locator("html")).to_have_attribute("lang", "de")
    expect(page.locator('#mode-seg button[data-mode="tiles"]')).to_have_text("Kacheln")


def test_recognition_language_setting_is_saved(page):
    page.click("#settings-btn")
    page.locator("#settings summary", has_text="KI-Erkennung").first.click()
    expect(page.locator("#s-llm-lang")).to_have_value("de")
    page.select_option("#s-llm-lang", "en")
    page.click("#s-ai-save")
    expect(page.locator("#toast")).to_be_visible()
    for _ in range(20):
        if page.evaluate("fetch('/settings').then((r) => r.json()).then((s) => s.llm_language)") == "en":
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError("llm_language nicht gespeichert")
    page.evaluate("""fetch('/settings', { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify({ llm_language: 'de' }) })""")


def test_money_in_english(browser, server):
    """Geld auf Englisch: Import-Vorschau, Kategorien übersetzt (Werte bleiben deutsch), Kategorie und jährliche
    Kosten mit Browser-Zurück, Beträge im US-Format."""
    import datetime as _dt
    t = _dt.date.today()
    ago = lambda n: (t - _dt.timedelta(days=n)).strftime("%d.%m.%Y")   # noqa: E731
    csv_ = ("Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n"
            f"{ago(400)};Nordlicht Versicherung AG;Hausrat;-1.180,40\n"
            f"{ago(35)};Nordlicht Versicherung AG;Hausrat;-1.204,60\n"
            f"{ago(12)};zalando SE;Bestellung 4711;-49,95\n")
    ctx, pg, errors = _login(browser, server, "en-US")
    pg.click('#mode-seg button[data-mode="money"]')
    mv = pg.locator("#moneyview")
    expect(mv).to_be_visible()
    pg.locator('#moneyview [data-mact="import"]').first.click()
    pg.set_input_files("#bk-file", files=[{"name": "giro-en.csv", "mimeType": "text/csv", "buffer": csv_.encode()}])
    expect(pg.locator("#bk-preview")).to_contain_text("3 transactions")
    pg.fill("#bk-name", "Checking EN")
    pg.click("#bk-go")
    expect(pg.locator("#bank-dialog")).not_to_be_visible()
    pg.click('#moneyview [data-mperiod="all"]')
    expect(mv).to_contain_text("What for")
    bar = mv.locator(".m-bar", has_text="Clothing")
    expect(bar).to_be_visible()
    bar.click()
    expect(mv.locator("#m-det-h")).to_have_text("Clothing")
    expect(mv.locator(".m-tx", has_text="zalando").first).to_contain_text("49.95")
    pg.go_back()
    expect(mv.locator("#m-det-h")).to_have_count(0)
    expect(mv).to_contain_text("Yearly costs")
    mv.locator('[data-mact="yearly"]').click()
    expect(mv.locator(".m-yrow", has_text="Nordlicht")).to_contain_text("1,204.60")
    pg.go_back()
    expect(mv).to_contain_text("What for")
    assert "Wofür" not in mv.inner_text()
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)


def test_amount_in_english_detail(browser, server):
    """Englisch: „€18,658.56“ in der Karte; antippen und verlassen speichert nichts, „1,234.56“ ist 1234.56 (Komma
    trennt Tausender) – früher wurde daraus 1,23."""
    ctx, pg, errors = _login(browser, server, "en-US")
    from e2e.test_web import _patch, _upload
    doc = _upload(pg, "Amount EN")
    _patch(pg, doc, {"amount": 18658.56, "currency": "EUR"})
    bodies = []
    pg.on("request", lambda r: bodies.append(r.post_data_json) if r.method == "PATCH" and f"/documents/{doc}" in r.url else None)
    pg.evaluate(f"openDetail({doc})")
    amount = pg.locator("#m-amount")
    expect(amount).to_have_value("€18,658.56")
    pg.focus("#m-amount")
    expect(amount).to_have_value("18658.56")
    pg.press("#m-amount", "Enter")
    expect(amount).to_have_value("€18,658.56")
    pg.focus("#m-amount")
    pg.fill("#m-amount", "1,234.56")
    pg.press("#m-amount", "Enter")
    expect(amount).to_have_value("€1,234.56")
    assert bodies == [{"amount": 1234.56}], bodies
    pg.evaluate(f"fetch('/documents/{doc}?permanent=true', {{ method: 'DELETE' }})")
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)


def test_server_error_texts_in_english(browser, server):
    """Fehlermeldungen des Servers (deutsch) erscheinen in der englischen Oberfläche englisch – feste Texte und Muster."""
    ctx, pg, errors = _login(browser, server, "en-US")
    assert pg.evaluate("api('/documents/99999999').then(() => '', (e) => e.message)") == "Document not found"
    assert pg.evaluate("api('/ask', json('POST', { question: 'x' })).then(() => '', (e) => e.message)") == "Question too short"
    assert pg.evaluate("trErr('Tag „Haus“ gibt es schon – zum Zusammenführen beide auswählen')") == \
        "Tag “Haus” already exists – select both to merge them"
    assert pg.evaluate("trErr('Etwas ganz Unbekanntes')") == "Etwas ganz Unbekanntes"
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)
