"""Fehlertexte des Servers in der Web-App: jeder Text aus tools/error-texts/server-errors.json (feste Texte und Muster
mit Werten) hat eine englische Fassung in i18n.js – fest im Wörterbuch EN, mit Werten in der Musterliste EN_ERR.
Die Liste liegt nur im privaten Repo (außerhalb von server/); im öffentlichen fehlt sie, dann wird übersprungen."""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
LIST = Path(__file__).resolve().parents[2] / "tools" / "error-texts" / "server-errors.json"


def _i18n():
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    en = json.loads(re.search(r"// EN-BEGIN\nconst EN = (\{.*?\n\});\n// EN-END", src, re.S).group(1))
    err = json.loads(re.search(r"// ERR-BEGIN\nconst EN_ERR = (\[.*?\n\]);\n// ERR-END", src, re.S).group(1))
    return en, err


def _pattern(de: str) -> re.Pattern:
    """Wie trErr in i18n.js: {name} steht für einen beliebigen Wert."""
    parts = re.split(r"\{(\w+)\}", de)
    return re.compile("^" + "".join("(.+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts)) + "$", re.S)


def _sample(text: str) -> str:
    """Server-Text mit Python-Ausdrücken ({e}, {n}, {'gehört' if …}) wie zur Laufzeit: ein Wert je Platzhalter."""
    return re.sub(r"\{[^{}]*\}", "Wert1", text)


def test_error_patterns_are_json_with_matching_names():
    _, err = _i18n()
    assert err, "EN_ERR leer"
    for de, en in err:
        assert set(re.findall(r"\{(\w+)\}", en)) <= set(re.findall(r"\{(\w+)\}", de)), (de, en)


@pytest.mark.skipif(not LIST.exists(), reason="Fehlertext-Liste nur im privaten Repo")
def test_every_server_error_text_has_an_english_version():
    en, err = _i18n()
    data = json.loads(LIST.read_text(encoding="utf-8"))
    patterns = [_pattern(de) for de, _ in err]
    missing = []
    for kind in ("literal", "templates", "indirect"):
        for item in data[kind]:
            text = item["text"]
            if "{" not in text:
                if text not in en:
                    missing.append(text)
            elif not any(p.match(_sample(text)) for p in patterns):
                missing.append(text)
    assert not missing, f"{len(missing)} Fehlertexte ohne englische Fassung: {missing[:20]}"
