"""Web-App installierbar (PWA): Manifest ohne Anmeldung lesbar, mit allen Symbolen, die Chrome dafür verlangt."""
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_manifest_and_icons(client):
    r = client.get("/app/manifest.webmanifest")          # Browser holen das Manifest ohne Anmeldedaten
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "Papigator" and m["display"] == "standalone" and m["start_url"] == "./" == m["scope"]
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"] + [i for s in m.get("shortcuts", []) for i in s["icons"]]:
        img = client.get(f"/app/{icon['src']}")
        assert img.status_code == 200 and img.headers["content-type"] == "image/png", icon["src"]
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert int.from_bytes(img.content[16:20], "big") == w and int.from_bytes(img.content[20:24], "big") == h


def test_page_links_manifest():
    html = (STATIC / "index.html").read_text()
    assert '<link rel="manifest" href="manifest.webmanifest">' in html
    assert 'href="apple-touch-icon.png"' in html and (STATIC / "apple-touch-icon.png").exists()
    assert "serviceWorker" not in (STATIC / "app.js").read_text()   # kein Cache, der ein altes app.js ausliefert
