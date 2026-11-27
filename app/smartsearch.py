"""Suche in ganzen Sätzen: „Gehalt letzte 6 Monate“, „Versicherungen von Sam“, „Was ist bald fällig?“.

Ohne KI und ohne Wartezeit: Ein Satz wird in Filter zerlegt – Zeitraum, Person (Besitzer), offene Fristen und
Suchwörter. Ein Wort, das zu einem Ordner/Tag passt („Gehaltsunterlagen“ → „Gehalt“), wird durch dessen Namen
ersetzt; die Volltextsuche findet damit sowohl Dokumente mit diesem Tag als auch solche, in deren Text es steht.
Füllwörter („die“, „letzten“, „Unterlagen“) fallen weg, sonst fände die UND-Suche gar nichts.

Sichtbarkeit: Es werden nur Tags und Personen erkannt, die der Fragende sehen darf (wie bei /tags) – ein Chip darf
nicht verraten, dass es einen fremden Ordner oder Nutzer gibt."""
import calendar
import datetime as dt
import re

from . import ask, auth, folders, processing

# Wörter, die nur Satzbau sind – als Suchwort würden sie die UND-Suche leerlaufen lassen
_FILLER = {
    "die", "der", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "und", "oder", "von", "vom",
    "aus", "für", "fuer", "mit", "zum", "zur", "bis", "bei", "beim", "nach", "über", "ueber", "was", "wie", "wo",
    "wer", "ich", "wir", "muss", "müssen", "muessen", "soll", "sollen", "kann", "ist", "sind", "war", "waren", "hat",
    "haben", "unterlagen", "dokumente", "dokument", "papiere", "papier", "sachen", "alles", "zeige", "zeig", "finde",
    "such", "suche", "brauche", "brauchen", "gib", "mal", "bald", "demnächst", "nächste", "nächsten", "naechste",
    "jetzt", "heute", "unsere", "unser", "unseren", "meine", "mein", "meinen", "meiner", "deine", "the", "im", "in",
    "am", "an", "auf", "zu", "es", "noch", "schon", "alle", "allen", "letzte", "letzten", "letztes", "letzter",
    # Englisch: „Show me all bills from last year“, „What is due soon?“
    # (keine Wörter, die auch Namen oder Marken sind: Till, Will, Total, About You …)
    "of", "and", "or", "for", "from", "with", "by", "my", "our", "your", "all", "any", "every", "everything",
    "anything", "something", "documents", "document", "docs", "papers", "paper", "files", "stuff", "things", "me",
    "find", "search", "get", "give", "what", "who", "where", "how", "is", "are", "was", "were", "be", "been", "do",
    "does", "did", "have", "has", "had", "need", "needs", "must", "should", "could", "would", "soon", "next",
    "upcoming", "coming", "there", "that", "these", "those", "them", "they", "some", "we", "us", "until", "the",
} | set(ask._NOT_KEYWORDS)

# Fragen nach Fälligem: offene Fristen statt Volltext („to pay“ nur als Wendung – „pay slips“ sind Gehaltszettel)
_DUE = re.compile(r"\b(fällig\w*|faellig\w*|frist(?:en)?|deadlines?|zahlen|bezahlen|erledigen|offene?n?"
                  r"|due|overdue|payable|outstanding|(?:to|must|should)\s+(?:i\s+|we\s+)?pay)\b", re.I)
_YEAR = re.compile(r"\b(?:aus|von|vom|in|im|im jahr|jahr|jahrgang|from|of|during|year)\s+(?:the\s+year\s+)?"
                   r"((?:19|20)\d{2})\b", re.I)
_MINUS = re.compile(r"(?:^|\s)-([A-Za-zÄÖÜäöüß0-9][A-Za-zÄÖÜäöüß0-9&.\-]*)")
_MONTH_WORDS = {"januar", "jänner", "februar", "märz", "maerz", "april", "mai", "juni", "juli", "august", "september",
                "oktober", "november", "dezember", "january", "february", "march", "may", "june", "july", "october",
                "december"}
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß0-9][A-Za-zÄÖÜäöüß0-9&.\-]*")


def _minus_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _range(q: str, today: dt.date) -> tuple[dt.date, dt.date] | None:
    """Zeitraum aus dem Satz. „Letzte 6 Monate“ heißt hier genau sechs Monate zurück (bis zum gleichen Tag),
    nicht ab Monatsanfang – sonst kämen sieben Gehaltsabrechnungen. Sonst wie bei „Frag Papigator“."""
    m = re.search(ask._LAST + r"\s+" + ask._NUM + r"\s+(?:monat|month)", q.lower())
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else ask._NUMBERS[m.group(1)]
        return _minus_months(today, n), today
    m = _YEAR.search(q)
    if m and 1900 <= int(m.group(1)) <= today.year + 1:
        y = int(m.group(1))
        return dt.date(y, 1, 1), dt.date(y, 12, 31)
    return ask.time_range(q, today)


def _de(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")   # nicht vom Locale abhängig


def _day(d: dt.date, en: bool) -> str:
    """Datum für die Chips: deutsch TT.MM.JJJJ, englisch „14 Nov 2026“ (in jeder englischen Region eindeutig)."""
    return f"{d.day} {_EN_MONTHS[d.month - 1]} {d.year}" if en else _de(d)


# Was hinter einem Tag-Namen stehen darf, damit das Wort nur diesen Tag meint: Endungen und Sammelwörter
# („Gehaltsunterlagen“ → Gehalt). „Steuerbescheid“ bleibt dagegen ein eigenes Suchwort – sonst würde es zu „Steuer“.
_TAG_TAIL = {"", "e", "en", "n", "s", "es", "er", "ern", "unterlagen", "sachen", "papiere", "dokumente", "zettel",
             "belege", "briefe", "post", "kram"}


def _tag_for(word: str, tags: list[str]) -> str | None:
    """Passender Tag für ein Wort: gleich, Mehrzahl („Rechnungen“), Sammelwort („Gehaltsunterlagen“ → „Gehalt“)
    oder knapp abgekürzt („Versicherun“). Der längste Treffer gewinnt."""
    w = word.lower()
    best = None
    for t in tags:
        tl = t.lower()
        if " " in tl or len(tl) < 3:
            continue
        tail = w[len(tl):] if w.startswith(tl) else None
        if tail is not None and tail.startswith("s") and tail[1:] in _TAG_TAIL - {""}:
            tail = tail[1:]   # Fugen-s: „Gehalts|unterlagen“
        if tail in _TAG_TAIL or (len(w) >= 5 and tl.startswith(w) and len(tl) - len(w) <= 2):
            if best is None or len(t) > len(best):
                best = t
    return best


# Dokumentart aus einem Wort („Zeugnisse“ → zeugnis). Feste Liste statt processing.KINDS: dort stehen auch Wörter wie
# „Versicherung“, „Brief“, „Quittung“, die hier Suchwörter bleiben sollen (eine Versicherung ist mehr als ihr Schein).
# „Gehalt“ bleibt Suchwort/Tag, nur „Lohnabrechnungen“ ist die Art. Englisch nur, wo das Wort eindeutig ist.
_KIND_WORDS = {
    "rechnung": ("rechnung", "rechnungen", "invoice", "invoices"),
    "gutschrift": ("gutschrift", "gutschriften"),
    "mahnung": ("mahnung", "mahnungen"),
    "abrechnung": ("abrechnung", "abrechnungen"),
    "gehalt": ("lohnabrechnung", "lohnabrechnungen", "gehaltsabrechnung", "gehaltsabrechnungen", "payslip", "payslips"),
    "kontoauszug": ("kontoauszug", "kontoauszüge", "kontoauszuege"),
    "bescheid": ("bescheid", "bescheide"),
    "vertrag": ("vertrag", "verträge", "vertraege", "contract", "contracts"),
    "police": ("versicherungsschein", "versicherungsscheine", "police", "policen"),
    "kuendigung": ("kündigung", "kündigungen", "kuendigung", "kuendigungen"),
    "angebot": ("angebot", "angebote"),
    "bestaetigung": ("bestätigung", "bestätigungen", "bestaetigung", "bestaetigungen"),
    "spende": ("spendenquittung", "spendenquittungen", "spendenbescheinigung", "spendenbescheinigungen"),
    "bescheinigung": ("bescheinigung", "bescheinigungen", "certificate", "certificates"),
    "zeugnis": ("zeugnis", "zeugnisse", "zeugnissen", "diploma", "diplomas"),
    "urkunde": ("urkunde", "urkunden"),
    "ausweis": ("ausweis", "ausweise"),
    "befund": ("befund", "befunde", "befunden", "arztbrief", "arztbriefe"),
    "bericht": ("bericht", "berichte"),
    "antrag": ("antrag", "anträge", "antraege"),
}
_KIND_OF = {w: code for code, words in _KIND_WORDS.items() for w in words}


def _stem(word: str) -> str:
    """Für die Präfix-Suche reicht der Wortstamm: „Stadtwerken“ → „Stadtwerk“ findet auch „Stadtwerke“."""
    w = word
    m = re.search(r"nisse?n?$", w, re.I)   # „Zeugnisse“, „Ergebnissen“ → „Zeugnis“, „Ergebnis“
    if m and len(w) - len(m.group(0)) >= 3:
        return w[: m.start()] + "nis"
    lw = w.lower()
    # Englische Mehrzahl: „utilities“ → „utilit“ (findet utility/utilities), „taxes“ → „tax“, „bills“ → „bill“
    if lw.endswith("ies") and len(w) >= 7:
        return w[:-3]
    if re.search(r"(?:[xz]|(?<=..)(?:ss|sh|ch))es$", lw) and len(w) >= 5:
        return w[:-2]
    for end in ("ern", "en", "n"):
        if len(w) - len(end) >= 5 and lw.endswith(end):
            return w[: -len(end)]
    # Plural-e: „Briefe“ → „Brief“ (als Präfix findet der Stamm auch die Einzahl); kurze Wörter („Rente“) bleiben
    if len(w) >= 6 and lw.endswith(("fe", "ge", "de", "te", "ke")):   # Briefe, Belege, Bescheide, Atteste, Werke
        return w[:-1]
    # -s: ab vier Buchstaben Rest; „Preis“, „Status“, „Pass“ bleiben
    if len(w) >= 5 and lw.endswith("s") and not lw.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def interpret(con, user: auth.User, question: str, today: dt.date | None = None, lang: str = "de") -> dict:
    """Satz → {"chips": [...], "params": {...}} – params passen direkt zu GET /documents.
    Chip-Arten: word (Suchwort, evtl. Tag), person (Person ohne Konto – wie ein Tag-Suchwort in q), doc_kind
    (Dokumentart, `code` → Parameter kind, mehrere mit Komma), range, owner, due.
    lang: Sprache der Beschriftungen (Zeitraum, Fristen, Art) – „en“ für die englische Oberfläche, sonst Deutsch
    (ältere Apps schicken keine)."""
    today = today or dt.date.today()
    en = (lang or "").lower().startswith("en")
    q = question.strip()
    visible = auth.visible_owner_ids(con, user, "all")
    chips, params = [], {"scope": "all"}
    used: set[str] = set()   # Wörter, die ein Filter verbraucht hat

    span = _range(q, today)
    if span:
        chips.append({"kind": "range", "label": f"{_day(span[0], en)} – {_day(span[1], en)}",
                      "from": span[0].isoformat(), "to": span[1].isoformat()})
        params["date_from"], params["date_to"] = span[0].isoformat(), span[1].isoformat()
        m = _YEAR.search(q)
        if m:
            used.add(m.group(1))
        # „seit 2023“, „since March 2025“: Jahre und Monatsnamen gehören zum Zeitraum, nicht in die Wortsuche (27.11.2026)
        used.update(w.lower() for w in _WORD.findall(q) if re.fullmatch(r"(?:19|20)\d{2}", w) or w.lower() in _MONTH_WORDS)

    if _DUE.search(q):
        chips.append({"kind": "due", "label": "Open deadlines" if en else "Offene Fristen"})
        params["due"] = True
        for m in _DUE.finditer(q):   # „to pay“: auch „pay“ ist kein Suchwort
            used.update(m.group(0).lower().split())

    # „-mobilfunk“: ausschließen statt suchen (27.11.2026) – bisher wurde das Minus verschluckt und es zum Suchwort
    minus = [m.group(1) for m in _MINUS.finditer(q)]
    used.update(w.lower() for w in minus)
    # Personen: nur sichtbare Nutzer, Name als ganzes Wort („Sam“, „Sams“)
    words = _WORD.findall(q)
    if visible:
        marks = ",".join("?" * len(visible))
        people = con.execute(f"SELECT id, display_name FROM users WHERE id IN ({marks})", visible).fetchall()
        named = []
        for w in words:
            wl = w.lower().rstrip("s") if len(w) > 3 else w.lower()
            for p in people:
                if p["display_name"] and wl in (p["display_name"].lower(), p["display_name"].lower().rstrip("s")):
                    if p["id"] not in (x["id"] for x in named):
                        named.append(p)
                    used.add(w.lower())
        if len(named) == 1:
            chips.append({"kind": "owner", "label": named[0]["display_name"], "owner_id": named[0]["id"]})
            params["owner"] = named[0]["id"]
        elif named:   # „von Alex und Sam“: ein Chip für alle Genannten (ältere Apps ohne owner_ids filtern dann nicht)
            names = [x["display_name"] for x in named]
            label = " & ".join(names)   # sprachneutral, die Suche versteht Deutsch und Englisch
            chips.append({"kind": "owner", "label": label, "owner_id": None, "owner_ids": [x["id"] for x in named]})
            params["owner"] = ",".join(str(x["id"]) for x in named)

    hidden = folders.hidden_tag_ids(con, visible) if visible else set()
    tags = [r["name"] for r in con.execute("SELECT id, name FROM tags") if r["id"] not in hidden]
    # Personen ohne Konto (Familie, z. B. Kinder) sind Tags: Chip „person“, gesucht wird wie bei jedem Tag über q
    from .family import person_names   # späte Bindung gegen Zirkel
    persons = {n.lower() for n in person_names(con, user)}
    # Dokumentarten nur anbieten, wenn es sichtbare Dokumente dieser Art gibt – solange der Bestand noch nachgetragen
    # wird (refill_k), bliebe „Zeugnisse“ sonst leer, obwohl die Wortsuche sie fände
    have_kinds = set()
    if visible:
        have_kinds = {r[0] for r in con.execute(
            f"SELECT DISTINCT doc_kind FROM documents WHERE deleted_at IS NULL AND doc_kind IS NOT NULL "
            f"AND owner_id IN ({','.join('?' * len(visible))})", visible)}
    keywords, kinds = [], []
    for w in words:
        wl = w.lower()
        if wl in used or wl in _FILLER or len(w) < 3 or _DUE.fullmatch(w) or re.fullmatch(r"\d{1,2}", w):
            continue
        tag = _tag_for(w, tags)
        code = _KIND_OF.get(wl)
        if code and not tag and code in have_kinds:   # eigener Ordner gleichen Namens geht vor (wie bisher)
            if code not in (k["code"] for k in kinds):
                # value: das Wort, falls die Art gelockert werden muss (siehe main.smart_search)
                kinds.append({"kind": "doc_kind", "label": processing.KIND_LABELS[code][1 if en else 0], "code": code, "value": _stem(w),
                              "word": w})
            continue
        value = tag or _stem(w)
        if value.lower() in (k["value"].lower() for k in keywords):
            continue
        kind = "person" if tag and tag.lower() in persons else "word"
        keywords.append({"kind": kind, "label": tag or w, "value": value, "tag": bool(tag)})
    excluded = [{"kind": "word", "label": ("without " if en else "ohne ") + w, "value": "-" + w, "tag": False} for w in minus]
    chips = keywords + excluded + kinds + chips
    if keywords or excluded:
        params["q"] = " ".join(k["value"] for k in keywords + excluded)
    if kinds:
        params["kind"] = ",".join(k["code"] for k in kinds)
    return {"query": q, "chips": chips, "params": params}
