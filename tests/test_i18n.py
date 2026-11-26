"""Web-App zweisprachig: jeder Text, den app.js über tr()/trn() zeigt, hat eine englische Fassung in i18n.js,
Platzhalter und HTML-Tags passen zusammen, und die deutschen Werte vom Server, die die Web-App nur anzeigt
(Rhythmus, Ausweise, Prüfungen, Geld-Kategorien), sind übersetzt. Die statischen Texte aus index.html prüft
e2e/test_web.py im Browser (dort sieht man die Textknoten so, wie i18n.js sie sieht)."""
import json
import re
from collections import Counter
from pathlib import Path

from app import bank, cleanup, processing

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS_STR = r'"((?:[^"\\\n]|\\.)*)"'
TAG = r"</?(a|b|i|br|code|em|kbd|small|span|strong)\b"   # „inbox/<Benutzername>“ ist Text, kein Tag


def en_dict() -> dict:
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    m = re.search(r"// EN-BEGIN\nconst EN = (\{.*?\n\});\n// EN-END", src, re.S)
    assert m, "EN-Block in i18n.js nicht gefunden (Markierungen EN-BEGIN/EN-END)"
    return json.loads(m.group(1))   # strenges JSON – Komma am Ende o. ä. fiele hier auf


def js_keys() -> set[str]:
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r"\btr\(\s*" + JS_STR, src))
    for one, other in re.findall(r"\btrn\([^\"]*?,\s*" + JS_STR + r"\s*,\s*" + JS_STR, src):
        keys |= {one, other}
    for a, b in re.findall(r"\btr\([^\"()]*\?\s*" + JS_STR + r"\s*:\s*" + JS_STR, src):   # tr(x ? "…" : "…")
        keys |= {a, b}
    return {json.loads(f'"{k}"') for k in keys}


def test_dictionary_is_strict_json_with_text_values():
    en = en_dict()
    assert len(en) > 800
    assert all(isinstance(v, str) and v.strip() for v in en.values())


def test_every_js_key_is_translated():
    en, keys = en_dict(), js_keys()
    assert len(keys) > 600
    missing = sorted(k for k in keys if k not in en)
    assert not missing, f"{len(missing)} Texte ohne englische Fassung in i18n.js: {missing[:20]}"


def test_placeholders_and_tags_match():
    bad = []
    for k, v in en_dict().items():
        if set(re.findall(r"\{(\w+)\}", k)) != set(re.findall(r"\{(\w+)\}", v)):
            bad.append((k, v, "Platzhalter"))
        if Counter(re.findall(TAG, k)) != Counter(re.findall(TAG, v)):
            bad.append((k, v, "HTML-Tags"))
    assert not bad, bad[:10]


def test_translation_function_is_not_shadowed():
    # tr/trn/LANG/LOC/EN sind globale Namen aus i18n.js – eine zweite Deklaration in app.js wäre ein
    # Syntaxfehler, und die Seite bliebe leer
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    for name in ("tr", "trn", "LANG", "LOC", "EN", "translateDom"):
        assert not re.search(rf"^(?:const|let|var|function)\s+{name}\b", src, re.M), name
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index('src="i18n.js"') < html.index('src="app.js"')


def test_server_values_shown_by_the_web_app_are_translated():
    en = en_dict()
    values = [*processing.INTERVALS, *processing.EXPIRES, *processing.SERVICES, *bank.CATEGORIES]
    # Aufräumen: Bezeichnungen und Begründungen der Vorgaben (eigene Ordner-Einstellungen zeigen den Ordnernamen)
    values += [x for _, label, _, _, basis in cleanup.DEFAULTS for x in (label, basis)]
    values += [cleanup.NEVER_LABEL, cleanup.NEVER_BASIS, "Eigene Einstellung für diesen Ordner"]
    assert "Eigene Einstellung für diesen Ordner" in Path(cleanup.__file__).read_text(encoding="utf-8")
    missing = [v for v in values if v not in en]
    assert not missing, missing
