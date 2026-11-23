"""Einladungen im Browser: Admin erstellt Link und QR-Code, eingeladene Person legt ihr Konto an."""
from playwright.sync_api import expect


def test_invite_create_and_welcome_page(page, browser, server):
    # Admin: Einstellungen → Nutzer → Einladen zeigt Link und QR-Code; Zurück schließt nur den Dialog
    page.click("#settings-btn")
    page.locator("#s-admin summary").click()
    page.fill("#s-inv-name", "Kim")
    page.select_option("#s-inv-share", "read")
    page.click("#s-inv-add")
    expect(page.locator("#invite-dialog")).to_be_visible()
    expect(page.locator("#iv-title")).to_have_text("Einladung für Kim")
    expect(page.locator("#iv-qr svg path")).to_have_count(1)
    url = page.input_value("#iv-url")
    assert "/app/#invite=" in url
    expect(page.locator("#s-invites")).to_contain_text("Kim")
    page.go_back()
    expect(page.locator("#invite-dialog")).to_be_hidden()
    expect(page.locator("#settings")).to_be_visible()

    # Eingeladene Person: eigener Browser, nicht angemeldet
    ctx = browser.new_context(base_url=server, viewport={"width": 1280, "height": 860}, locale="de-DE")
    ctx.add_init_script("try { if (!localStorage.getItem('bl.mode')) localStorage.setItem('bl.mode', 'tiles') } catch (e) {}")
    guest = ctx.new_page()
    errors: list[str] = []
    guest.on("pageerror", lambda e: errors.append(str(e)))
    guest.goto(url)
    expect(guest.locator("#invite")).to_be_visible()
    expect(guest.locator("#inv-title")).to_have_text("Kim, du wurdest zu Papigator eingeladen")
    guest.fill("#inv-user", "alex")                           # vergebener Name: Fehler vom Server
    guest.fill("#inv-pass", "ein-langes-passwort")
    guest.click("#invite-form button[type=submit]")
    expect(guest.locator("#inv-error")).to_have_text("Benutzername existiert bereits")
    guest.fill("#inv-user", "kim")
    guest.click("#invite-form button[type=submit]")
    expect(guest.locator("#main")).to_be_visible()
    expect(guest.locator("#main")).to_contain_text("Keine Dokumente")            # eigener Pot ist leer …
    guest.get_by_role("button", name="Partner").click()
    expect(guest.locator(".card", has_text="Grundsteuer")).to_be_visible()   # … Alex hat seinen freigegeben
    assert "#invite=" not in guest.url
    guest.goto(url)                                           # zweites Mal: verbraucht
    expect(guest.locator("#inv-sub")).to_contain_text("schon benutzt")
    expect(guest.locator("#inv-fields")).to_be_hidden()
    ctx.close()
    assert not errors, "JavaScript-Fehler: " + " | ".join(errors)
