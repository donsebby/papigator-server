"""„Dokumente fragen": Frage -> Suchbegriffe (LLM) -> FTS5 -> Ausschnitte -> Antwort mit Quellen (LLM)."""
import calendar
import datetime as dt
import json
import logging
import re

import httpx

from . import config, llm
from .db import TRIGRAM_MIN, connect, has_trigram

log = logging.getLogger("papigator.ask")

KEYWORD_PROMPT = """Du hilfst, Dokumente in einer privaten Ablage zu finden (Rechnungen, Bescheide, Verträge, Gehaltsabrechnungen, Versicherungen).
Erzeuge für die Frage 4-8 kurze Suchbegriffe oder Wortstämme, mit denen man die passenden Dokumente per Volltextsuche findet.
Denke an Synonyme und Behördendeutsch (z. B. Steuer-ID -> "Identifikationsnummer", "IdNr", "Steuerliche Identifikationsnummer"; Lohnsteuer -> "Lohnsteuerbescheinigung", "Ausdruck der elektronischen Lohnsteuerbescheinigung"; Gehalt -> "Entgeltabrechnung", "Verdienstabrechnung").
Antworte NUR mit einem JSON-Array aus Strings.

Frage: {question}"""

ANSWER_PROMPT = """Du beantwortest Fragen zu privaten Dokumenten. Unten stehen Ausschnitte aus Dokumenten, jeweils mit [Dok N] markiert.
Regeln:
- Antworte auf Deutsch, kurz und konkret. Nenne Zahlen, Nummern, Daten exakt so, wie sie im Text stehen.
- Gib hinter jeder Aussage die Quelle an, z. B. [Dok 2].
- Wenn die Ausschnitte die Frage nicht beantworten, sag das klar und rate nicht.
- Erfinde nichts.

- Heute ist der {today}.{range_note}
- Für Fragen nach Fristen, fälligen Zahlungen, Kündigungen, laufenden Verträgen, Garantie/Gewährleistung, Ablaufdaten,
  Prüfungen/Wartungen, Erstattungen, Preiserhöhungen und Ausgaben nach Kategorie (Kontoumsätze) gilt die „Übersicht aus der Ablage“
  (aus der Datenbank, verlässlich); gib dort als Quelle [Übersicht] an. Für alles andere die Dokumente.
- Widersprechen sich Dokumente, gilt das jüngste (Datum steht hinter dem Titel): Ein Gehalt im Arbeitsvertrag oder ein Preis
  im Vertrag ist der Stand bei Abschluss; spätere Gehaltsabrechnungen und Rechnungen zeigen den aktuellen. Nenne den aktuellen
  Wert mit Datum und erwähne den älteren höchstens als früheren Stand – nie beide gleichrangig.
- Vergleiche nur Gleichartiges: Brutto mit Brutto, Netto/Auszahlung mit Netto/Auszahlung.
- Bei Fragen nach mehreren Dokumenten: jedes in eine eigene Zeile, die mit „- “ beginnt (Datum, Titel, Quelle).
  Rechne keine Summen aus, wenn nicht ausdrücklich danach gefragt ist.
- Fragt jemand ausdrücklich nach einer Summe („insgesamt“, „zusammen“, „Summe“): jeder Wert in eine eigene Zeile, die mit
  „- “ beginnt (Monat oder Datum, Betrag, Quelle), danach eine Zeile „Summe: <Betrag>“. Stehen in der Übersicht
  „Summen je Absender“, sind das die vollständigen Summen aus allen Belegen: nimm sie als Ergebnis (Quelle [Übersicht])
  und rechne nicht selbst aus den wenigen Dokumenten unten nach.

Frage: {question}
{facts}
Dokumente:
{context}"""


TAG_MERGE_PROMPT = """Hier ist die Liste der Tags einer privaten Dokumentenablage, jeweils mit Anzahl der Dokumente:
{tags}

Finde Gruppen von Tags, die dasselbe meinen oder sich nur in Schreibweise, Numerus, Tippfehler, Abkürzung oder Sprache
unterscheiden (z. B. "Rechnung"/"Rechnungen", "KFZ"/"Kfz"/"Auto", "Versicherung"/"Versicherungen", "Arzt"/"Ärzte").
Wähle je Gruppe als Ziel den Tag, der am besten passt: kurz, deutsch, Singular; bei Gleichstand den mit den meisten Dokumenten.
Fasse nur zusammen, was wirklich dasselbe bezeichnet. Verwandte, aber verschiedene Begriffe ("Steuer" und "Gehalt",
"Auto" und "Versicherung", "Rechnung" und "Beleg", "Vertrag" und "Kündigung", "Rechnung" und "Abrechnung") bleiben getrennt,
ebenso Ober- und Unterbegriff ("Telekommunikation" und "Handy", "Versicherung" und "Haftpflicht", "Haus" und "Strom"):
der engere Tag ist eine Unterteilung, kein Doppel. Verwende die Tag-Namen exakt wie oben geschrieben.
Antworte NUR mit einem JSON-Array: [{{"target": "...", "sources": ["...", "..."], "reason": "kurze Begründung"}}]
Keine Gruppe gefunden: []"""


def suggest_tag_merges(tags: list[dict], prompt: str | None = None) -> list[dict]:
    """tags: [{id, name, count}]. Liefert Gruppen [{target, sources, reason}] mit Tag-Dicts; unbekannte Namen werden verworfen.
    Raises RuntimeError, wenn das LLM aus ist, und httpx-Fehler, wenn es nicht erreichbar ist."""
    if len(tags) < 2:
        return []
    by_name = {t["name"].lower(): t for t in tags}
    listing = "\n".join(f'- {t["name"]} ({t["count"]})' for t in tags)
    # Denkende Modelle (gemma4) brauchen für die Liste gern eine Minute
    content = _chat((prompt or TAG_MERGE_PROMPT).format(tags=listing), timeout=300)
    if content is None:
        raise RuntimeError("KI-Erkennung ist aus oder kein LLM-Server eingetragen")
    m = re.search(r"\[.*\]", content, re.S)
    try:
        data = json.loads(m.group(0)) if m else []
    except Exception:
        log.warning("Tag-Vorschläge: unlesbare LLM-Antwort: %s", content[:200])
        data = []
    groups, used = [], set()
    for g in data if isinstance(data, list) else []:
        if not isinstance(g, dict):
            continue
        target = by_name.get(str(g.get("target", "")).strip().lower())
        sources = [by_name[s.strip().lower()] for s in g.get("sources", [])
                   if isinstance(s, str) and s.strip().lower() in by_name]
        sources = [s for s in sources if target and s["id"] != target["id"] and s["id"] not in used]
        if not target or not sources or target["id"] in used:
            continue
        reason = str(g.get("reason", ""))[:200]
        if _NARROWER.search(reason):
            continue   # „Handy ist eine Unterkategorie von Telekommunikation“ – kein Doppel, trotz Prompt
        used.update(s["id"] for s in sources); used.add(target["id"])
        groups.append({"target": target, "sources": sources, "reason": reason})
    return groups


# Begründungen, mit denen das LLM selbst sagt, dass es Ober- und Unterbegriff zusammenwirft
_NARROWER = re.compile(r"unterkategorie|unterbegriff|oberbegriff|oberkategorie|teilbereich|teilmenge|gehört zu|gehören zu|"
                       r"ist eine (art|form|sorte)|sind eine (art|form)|teil von|untergeordnet|spezialfall", re.I)


def _chat(prompt: str, timeout: int = 180, purpose: str = "tag") -> str | None:
    """Eine Frage ans LLM (siehe llm.chat): None, wenn die KI aus oder für diese Aufgabe nicht freigegeben ist."""
    return llm.chat(prompt, timeout, purpose)


def chat(prompt: str, timeout: int = 180) -> str | None:
    """Öffentliche Variante für andere Module (Tagging)."""
    return _chat(prompt, timeout, "tag")


_NUMBERS = {"ein": 1, "eine": 1, "einen": 1, "einem": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
            "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "achtzehn": 18,
            "vierundzwanzig": 24,
            # Englisch: „last six months“
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twenty-four": 24}
_MONTHS = {m: i for i, m in enumerate(["januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
                                         "september", "oktober", "november", "dezember"], 1)}
_MONTHS_EN = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                            "september", "october", "november", "december"], 1)}
_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")"
# „Letztes Jahr“/„last year“ meint das Kalenderjahr davor; „past“ nur mit Zahl („past 6 months“) oder rollierend
_LAST_CAL = r"(?:letzte[nmrs]?|vergangene[nmrs]?|zurückliegende[nmrs]?|last|previous)"
_LAST = r"(?:letzte[nmrs]?|vergangene[nmrs]?|zurückliegende[nmrs]?|last|past|previous)"
# Zeiteinheiten Deutsch/Englisch → deutsche Kurzform
_UNIT = r"(tag|woche|monat|jahr|day|week|month|year)"
_UNITS = {"day": "tag", "week": "woche", "month": "monat", "year": "jahr"}
# Wörter, die nur die Zeit oder die Frageform beschreiben – als Suchbegriff finden sie in jedem Dokument etwas
_NOT_KEYWORDS = {"meine", "mein", "meinen", "meiner", "welche", "welcher", "welches", "brauche", "wann", "wieviel",
                 "wie viel", "steht", "habe", "hatte", "letzte", "letzten", "letztes", "letzter", "vergangenen",
                 "vergangene", "vergangenes", "monat", "monate", "monaten", "monats", "woche", "wochen", "jahr",
                 "jahre", "jahren", "jahres", "tage", "tagen", "dieses", "diesem", "diesen", "dieser", "seit", "bisher",
                 "insgesamt", "summe", "alle", "allen", "zeig", "zeige", "liste", "bitte", "zusammen", "jeweils",
                 "quartal", "sich", "mich", "mir", "dich", "dir", "uns", "viel", "viele", "kostet", "entwickelt",
                 "entwicklung", "gezahlt", "bezahlt", "verdiene", "verdient", "hoch", "aktuell", "aktuelle", "aktuellen",
                 "gerade", "eigentlich", "schon", "noch", "immer", "gibt",
                 # Englische Zeit- und Fragewörter („last 6 months“, „this year“, „since January“)
                 "last", "past", "previous", "this", "since", "month", "months", "week", "weeks", "year",
                 "years", "days", "ago", "which", "when", "show", "please", "much", "many",
                 *_NUMBERS, *_MONTHS, *_MONTHS_EN}


def _minus_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def time_range(question: str, today: dt.date | None = None) -> tuple[dt.date, dt.date] | None:
    """Zeitraum aus gängigen Formulierungen („letzten sechs Monate“, „letztes Jahr“, „seit März“, „dieses Jahr“).
    Ohne erkennbaren Zeitraum None. Jahreszahlen allein filtern bewusst nicht: „Lohnsteuer 2025“ steckt in einem
    Dokument vom Januar 2026."""
    today = today or dt.date.today()
    q = question.lower()
    m = re.search(_LAST + r"\s+" + _NUM + r"\s+" + _UNIT, q)
    if not m:
        m = re.search(r"past\s+()(day|week|month|year)\b", q)   # „past year“ = die letzten zwölf Monate
    if m:
        n = 1 if not m.group(1) else int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1)]
        unit = _UNITS.get(m.group(2), m.group(2))
        if unit == "tag":
            return today - dt.timedelta(days=n), today
        if unit == "woche":
            return today - dt.timedelta(weeks=n), today
        # ab Monatsanfang: Abrechnungen tragen oft den Ersten des Monats als Datum
        return _minus_months(today, n * (12 if unit == "jahr" else 1)).replace(day=1), today
    if re.search(_LAST_CAL + r"\s+(?:jahr|year\b)|vorjahr", q):
        y = today.year - 1
        return dt.date(y, 1, 1), dt.date(y, 12, 31)
    if re.search(_LAST_CAL + r"\s+(?:monat|month\b)|vormonat", q):
        first = today.replace(day=1)
        last = first - dt.timedelta(days=1)
        return last.replace(day=1), last
    if re.search(_LAST_CAL + r"\s+(?:woche|week\b)", q):
        return today - dt.timedelta(weeks=1), today
    if re.search(r"(?:dieses|diesem|diesen|laufenden?|aktuellen?)\s+jahr|seit jahresbeginn|seit anfang des jahres"
                 r"|(?:this|current)\s+year\b|year\s+to\s+date", q):
        return dt.date(today.year, 1, 1), today
    if re.search(r"(?:dieses|diesem|diesen|laufenden?|aktuellen?)\s+monat|(?:this|current)\s+month\b", q):
        return today.replace(day=1), today
    months = {**_MONTHS, **_MONTHS_EN}
    m = re.search(r"(?:seit|since)\s+(?:anfang\s+|the\s+(?:start|beginning)\s+of\s+)?("
                  + "|".join(months) + r")\b(?:\s+(\d{4}))?", q)
    if m:
        mon = months[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if mon <= today.month else today.year - 1)
        return dt.date(y, mon, 1), today
    m = re.search(r"(?:seit|since)\s+(\d{4})\b", q)
    if m and 1990 <= int(m.group(1)) <= today.year:
        return dt.date(int(m.group(1)), 1, 1), today
    return None


def keywords_for(question: str) -> list[str]:
    words = []
    try:
        content = _chat(KEYWORD_PROMPT.format(question=question), timeout=60, purpose="ask") or ""
        m = re.search(r"\[.*\]", content, re.S)
        if m:
            words = [str(w).strip() for w in json.loads(m.group(0)) if str(w).strip()]
    except Exception as e:
        log.warning("Keyword-Extraktion fehlgeschlagen: %s", e)
    # Fallback/Ergänzung: Wörter aus der Frage selbst (ab 4 Zeichen)
    words += re.findall(r"[A-Za-zÄÖÜäöüß0-9-]{4,}", question)
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k not in seen and k not in _NOT_KEYWORDS and not re.fullmatch(r"\d{1,3}", k):
            seen.add(k); out.append(w)
    return out[:12]


def _fts_or_query(words: list[str]) -> str:
    parts = []
    for w in words:
        w = w.replace('"', "")
        toks = [t for t in w.split() if t]
        if not toks:
            continue
        # Mehrwort-Begriffe als Phrase mit Prefix auf dem letzten Token, Einzelwörter als Prefix
        parts.append('"' + " ".join(toks) + '"*' if len(toks) == 1 else '"' + " ".join(toks) + '"')
    return " OR ".join(parts)


def _snippets(text: str, words: list[str], max_chars: int = 1400) -> str:
    """Textfenster um die Trefferstellen, zusammengesetzt bis max_chars."""
    if not text:
        return ""
    low = text.lower()
    spots = []
    for w in words:
        for tok in w.lower().split():
            i = low.find(tok)
            n = 0
            while i != -1 and n < 3:
                spots.append(i); i = low.find(tok, i + 1); n += 1
    if not spots:
        return text[:max_chars]
    spots.sort()
    out, last_end = [], -1
    for sp in spots:
        a, b = max(0, sp - 250), min(len(text), sp + 350)
        if a < last_end:
            continue
        out.append(("…" if a > 0 else "") + text[a:b].strip() + ("…" if b < len(text) else ""))
        last_end = b
        if sum(len(o) for o in out) > max_chars:
            break
    return "\n".join(out)[:max_chars]


CONTEXT_CHARS = 24000  # ≈ 7000 Tokens: passt in das Kontextfenster lokaler Modelle (gemma4: 32k+)


FACT_DAYS_AHEAD = 90


def _eur_text(v: float, cur: str | None = None) -> str:
    return f"{_fmt_money(round(v * 100))} {'€' if (cur or 'EUR') == 'EUR' else cur}"


# Fragen nach Summen oder Verlauf brauchen auch alte Belege
_HISTORY_Q = re.compile(r"insgesamt|zusammen|summe|alle\b|jemals|bisher|seit\b|verlauf|entwick|gestiegen|gesunken|"
                        r"früher|vorher|damals|erhöh|vergleich|wie oft|wie viele|veränder", re.I)
# Ausdrücklich nach Geld-Summen gefragt: nur dann gehen „Summen je Absender“ in die Übersicht – sonst rechnet das LLM
# gern ungefragt zusammen („Zeig mir die Rechnungen von X“ ist keine Frage nach einer Summe)
_SUM_Q = re.compile(r"insgesamt|zusammen|summe|wie ?viel|ausgegeben|verdient|eingenommen|"
                    r"\bwas (?:habe|hab|haben) (?:ich|wir)\b.*\b(?:gezahlt|bezahlt|bekommen)", re.I)
SERIES_KEEP = 3   # gleichartige Belege eines Absenders (Lohnzettel, Monatsrechnungen): nur die jüngsten


def _newest_of_series(docs: list[dict], limit: int) -> list[dict]:
    """Ohne Zeitraum in der Frage meint man den aktuellen Stand: Bei vielen ähnlich passenden Belegen desselben
    Absenders (z. B. 24 Gehaltsabrechnungen) nur die jüngsten SERIES_KEEP mitnehmen – sonst liest das LLM zufällig
    alte und hält überholte Beträge für aktuell. Der Platz bleibt für andere Treffer. Reihenfolge nach Relevanz."""
    by_sender: dict[str, list[dict]] = {}
    for d in docs:
        by_sender.setdefault((d["sender"] or f"#{d['id']}").strip().lower(), []).append(d)
    keep = set()
    for group in by_sender.values():
        newest = sorted(group, key=lambda d: d["document_date"] or "", reverse=True)
        keep.update(d["id"] for d in newest[:SERIES_KEEP])
    return [d for d in docs if d["id"] in keep][:limit]


def _named_senders(owner_ids: list[int], words: list[str]) -> dict[str, list[str]]:
    """Suchwörter, die in einem Absender vorkommen („Telekom“ → „Telekom Deutschland GmbH“): {Wort: [Absender, …]}.
    Allgemeine Wörter („Versicherung“ steckt in vielen Firmennamen) zählen nur, wenn sie höchstens drei Absender treffen."""
    marks = ",".join("?" * len(owner_ids))
    out = {}
    with connect() as con:
        for w in words:
            w = w.strip()
            if len(w) < 3 or " " in w or w.lower() in _NOT_KEYWORDS:
                continue
            like = "%" + w.replace("\\", "").replace("%", "").replace("_", "") + "%"
            rows = con.execute(
                f"SELECT DISTINCT sender FROM documents WHERE deleted_at IS NULL AND ext_pending=0 AND sender LIKE ? "
                f"AND owner_id IN ({marks}) LIMIT 4", (like, *owner_ids)).fetchall()
            if 0 < len(rows) <= 3:
                out[w] = [r["sender"] for r in rows]
    return out


def _sender_facts(owner_ids: list[int], named: dict[str, list[str]], span) -> str:
    """Für genannte Absender: Summen je Jahr aus der Datenbank – „Was habe ich insgesamt an X gezahlt?“ braucht alle
    Belege, das LLM sieht aber nur ein paar. Doppelscans (gleicher Betrag am gleichen Tag) zählen einmal."""
    from .extras import _SQL_IN, _SQL_OUT
    senders = sorted({s for v in named.values() for s in v})
    if not senders:
        return ""
    marks = ",".join("?" * len(owner_ids))
    lines = []
    with connect() as con:
        for name in senders:
            where, args = "", []
            if span:
                where, args = " AND document_date BETWEEN ? AND ?", [span[0].isoformat(), span[1].isoformat()]
            rows = con.execute(
                f"SELECT substr(document_date,1,4) AS y, COUNT(*) AS n, SUM(CASE WHEN {_SQL_OUT} THEN amount END) AS spent, "
                f"SUM(CASE WHEN {_SQL_IN} THEN amount END) AS received FROM ("
                f"  SELECT * FROM documents WHERE deleted_at IS NULL AND ext_pending=0 AND document_date IS NOT NULL "
                f"  AND lower(trim(sender)) = lower(trim(?)) AND owner_id IN ({marks}){where} "
                f"  GROUP BY document_date, amount, direction, doc_kind) GROUP BY y ORDER BY y",
                (name, *owner_ids, *args)).fetchall()
            if not rows:
                continue
            parts = []
            for r in rows:
                money = []
                if r["spent"]:
                    money.append(f"Ausgaben {_eur_text(r['spent'])}")
                if r["received"]:
                    money.append(f"Einnahmen {_eur_text(r['received'])}")
                n = "1 Dokument" if r["n"] == 1 else f"{r['n']} Dokumente"
                parts.append(f"{r['y']}: {n}" + (f", {', '.join(money)}" if money else ""))
            spent = sum(r["spent"] or 0 for r in rows)
            received = sum(r["received"] or 0 for r in rows)
            total = []
            if spent:
                total.append(f"Ausgaben {_eur_text(spent)}")
            if received:
                total.append(f"Einnahmen {_eur_text(received)}")
            lines.append(f"- {name}: " + "; ".join(parts) + (f" – zusammen {', '.join(total)}" if total else ""))
    if not lines:
        return ""
    head = "Summen je Absender aus der Datenbank (alle Belege, nur Euro" + (", im gefragten Zeitraum" if span else "") + \
           "; für „insgesamt“/Summen diese Zahlen nehmen, Quelle [Übersicht]):"
    return head + "\n" + "\n".join(lines) + "\n"


SERIES_SPREAD = 5  # Verlauf ohne Zeitraum: so viele Belege einer Reihe, gleichmäßig vom ältesten bis zum jüngsten


def _spread_series(docs: list[dict], limit: int) -> list[dict]:
    """„Wie hat sich X entwickelt?“: aus einer langen Reihe ähnlicher Belege nicht die zufällig bestbewerteten
    (oft alle aus einem Jahr), sondern gleichmäßig über die Zeit verteilte – ältester und jüngster immer dabei."""
    by_sender: dict[str, list[dict]] = {}
    for d in docs:
        by_sender.setdefault((d["sender"] or f"#{d['id']}").strip().lower(), []).append(d)
    series, rest = [], set()
    for group in by_sender.values():
        dated = sorted(group, key=lambda d: d["document_date"] or "")
        if len(dated) <= SERIES_SPREAD:
            rest.update(d["id"] for d in dated)
            continue
        step = (len(dated) - 1) / (SERIES_SPREAD - 1)
        series += [dated[round(i * step)] for i in range(SERIES_SPREAD)]
    # Die Reihe ist, wonach gefragt wird – sie zuerst, auch wenn seltene Suchwörter andere Dokumente höher bewerten
    return (series + [d for d in docs if d["id"] in rest])[:limit]


def facts(owner_ids: list[int], today: dt.date, warranty: bool = False) -> str:
    """Übersicht aus der Datenbank: offene Fristen (bald fällig und kürzlich verpasst) und laufende Verträge mit
    Betrag, Rhythmus und nächster Kündigungsfrist. Die Volltextsuche findet so etwas schlecht („was muss ich bald
    zahlen?“ enthält keinen Absender), das LLM soll es aber nicht erraten."""
    from . import extras
    if not owner_ids:
        return ""
    marks = ",".join("?" * len(owner_ids))
    lines = []
    with connect() as con:
        rows = con.execute(
            f"SELECT title, sender, amount, currency, due_date, due_note FROM documents WHERE deleted_at IS NULL AND ext_pending=0 "
            f"AND due_date IS NOT NULL AND due_done=0 AND due_date BETWEEN ? AND ? AND owner_id IN ({marks}) ORDER BY due_date LIMIT 25",
            ((today - dt.timedelta(days=30)).isoformat(), (today + dt.timedelta(days=FACT_DAYS_AHEAD)).isoformat(), *owner_ids)).fetchall()
        contracts = extras.contracts(con, owner_ids, today)
        # Worum es beim Vertrag geht, sagt der Absender oft nicht („Lindwurm AG“ → Kfz-Versicherung): Titel des letzten Belegs
        titles = {r["id"]: r["title"] for r in con.execute(
            f"SELECT id, title FROM documents WHERE id IN ({','.join('?' * len(contracts)) or 'NULL'})",
            [c["documents"][0] for c in contracts if c["documents"]])}
        # Jüngste Gehaltsabrechnung je Arbeitgeber – Gehaltsangaben in älteren Dokumenten (Arbeitsvertrag) sind überholt
        pay = con.execute(
            f"SELECT sender, title, document_date, amount, currency FROM documents d WHERE deleted_at IS NULL AND ext_pending=0 "
            f"AND doc_kind='gehalt' AND document_date IS NOT NULL AND owner_id IN ({marks}) AND document_date = ("
            f"SELECT MAX(document_date) FROM documents e WHERE e.deleted_at IS NULL AND e.ext_pending=0 AND e.doc_kind='gehalt' "
            f"AND lower(trim(e.sender)) = lower(trim(d.sender)) AND e.owner_id = d.owner_id) "
            f"GROUP BY lower(trim(sender)), owner_id ORDER BY document_date DESC LIMIT 5", owner_ids).fetchall()
        soon = extras.upcoming(con, owner_ids, today)
        # Laufende Gewährleistung/Garantie aus Kaufbelegen (endet frühestens heute), dazu kürzlich abgelaufene
        # nur bei Fragen nach Garantie/Gewährleistung – sonst füllt die Liste den Kontext jeder Frage
        warranty = [] if not warranty else con.execute(
            f"SELECT title, sender, document_date, warranty_kind, warranty_until FROM documents WHERE deleted_at IS NULL "
            f"AND ext_pending=0 AND warranty_until >= ? AND owner_id IN ({marks}) ORDER BY warranty_until LIMIT 40",
            ((today - dt.timedelta(days=365)).isoformat(), *owner_ids)).fetchall()
        tax = con.execute(
            # gleicher Betrag am gleichen Tag: dieselbe Rechnung (zweiter Scan) – nur einmal
            f"SELECT substr(document_date,1,4) AS y, MIN(title) AS title, tax_35a FROM documents WHERE deleted_at IS NULL "
            f"AND ext_pending=0 AND tax_35a > 0 AND document_date >= ? AND owner_id IN ({marks}) "
            f"GROUP BY document_date, tax_35a ORDER BY document_date",
            (f"{today.year - 3}-01-01", *owner_ids)).fetchall()
    if rows:
        lines.append(f"Offene Fristen (verpasst seit {_de(today - dt.timedelta(days=30))} oder fällig bis {_de(today + dt.timedelta(days=FACT_DAYS_AHEAD))}):")
        for r in rows:
            amt = f", {_eur_text(r['amount'], r['currency'])}" if r["amount"] else ""
            when = _de(dt.date.fromisoformat(r["due_date"]))
            past = " (verpasst)" if r["due_date"] < today.isoformat() else ""
            lines.append(f"- {when}{past}: {r['due_note'] or 'Frist'} – {r['title']}" + (f" ({r['sender']})" if r["sender"] else "") + amt)
    else:
        lines.append(f"Offene Fristen bis {_de(today + dt.timedelta(days=FACT_DAYS_AHEAD))}: keine.")
    if contracts:
        lines.append("Laufende Verträge und regelmäßige Zahlungen:")
        for c in contracts[:25]:
            cancel = f", kündbar bis {_de(dt.date.fromisoformat(c['cancel_by']['date']))}" if c.get("cancel_by") else ""
            about = titles.get(c["documents"][0]) if c["documents"] else None
            since = (f", laut Rechnung vom {_de(dt.date.fromisoformat(c['amount_from']))} – im Vertrag standen noch "
                     f"{_eur_text(c['contract_amount'])}") if c.get("amount_from") else ""
            lines.append(f"- {c['sender']}" + (f" („{about}“)" if about else "") +
                         f": {_eur_text(c['amount'])} {c['interval']} (≈ {_eur_text(c['per_month'])} im Monat{since}){cancel}")
    if pay:
        lines.append("Gehalt – jüngste Abrechnung je Arbeitgeber (ältere Angaben, z. B. das Gehalt im Arbeitsvertrag, sind überholt):")
        for r in pay:
            amt = f": {_eur_text(r['amount'], r['currency'])} (erkannter Betrag, meist die Auszahlung)" if r["amount"] else ""
            lines.append(f"- {r['sender'] or 'Arbeitgeber'}, {_de(dt.date.fromisoformat(r['document_date'][:10]))} „{r['title']}“{amt}")
    if warranty:
        lines.append("Gewährleistung/Garantie aus Kaufbelegen (gesetzliche Gewährleistung 2 Jahre ab Kauf, gerechnet ab "
                     "Belegdatum – Näherung; Garantie = so im Beleg genannt):")
        for r in warranty:
            what = "Garantie" if r["warranty_kind"] == "garantie" else "Gewährleistung"
            end = dt.date.fromisoformat(r["warranty_until"])
            state = "läuft bis" if end >= today else "abgelaufen am"
            bought = f", gekauft {_de(dt.date.fromisoformat(r['document_date'][:10]))}" if r["document_date"] else ""
            lines.append(f"- {r['title']}" + (f" ({r['sender']})" if r["sender"] else "") + f"{bought}: {what} {state} {_de(end)}")
    if soon:
        lines.append("Im Blick (läuft bald ab, Prüfung/Wartung fällig, Erstattung offen, teurer geworden):")
        for i in soon:
            when = _de(dt.date.fromisoformat(i["date"][:10])) if i.get("date") else "ohne Datum"
            if i["type"] == "expiry":
                lines.append(f"- {i['what']} gültig bis {when} („{i['title']}“)")
            elif i["type"] == "service":
                lines.append(f"- {i['what']} fällig {when} (letzter Beleg „{i['title']}“)")
            elif i["type"] == "refund":
                lines.append(f"- Erstattung offen: „{i['title']}“ vom {when}" + (f", {_eur_text(i['amount'])}" if i.get("amount") else ""))
            else:
                lines.append(f"- {i['what']}: seit {when} {_eur_text(i['amount'])} statt {_eur_text(i['from'])} (+{i['percent']} %)"
                             + (f", kündbar bis {_de(dt.date.fromisoformat(i['cancel_by']))}" if i.get("cancel_by") else ""))
    if tax:
        lines.append("Arbeitskosten nach § 35a EStG (Handwerker, haushaltsnahe Dienstleistungen), nach Jahr des Belegs:")
        for y in sorted({r["y"] for r in tax}):
            items = [r for r in tax if r["y"] == y]
            lines.append(f"- {y}: " + "; ".join(f"{r['title']} {_eur_text(r['tax_35a'])}" for r in items) +
                         f" – zusammen {_eur_text(sum(r['tax_35a'] for r in items))}")
    return "\nÜbersicht aus der Ablage:\n" + "\n".join(lines) + "\n"


def ask(question: str, owner_ids: list[int], limit: int = 8, today: dt.date | None = None,
        extra_facts=None) -> dict:
    """extra_facts(span) → Text: weitere verlässliche Übersicht (Kontoumsätze aus app/bank.py, mit den Rechten des
    Fragenden gerechnet)."""
    if not llm.usable("ask"):
        # KI aus – oder nur ein Cloud-Server eingetragen, der fürs Fragen nicht freigegeben ist
        return {"answer": "Fragen ist ausgeschaltet: Es ist keine KI eingetragen, die Dokumente dafür lesen darf.",
                "sources": [], "keywords": [], "error": True}
    today = today or dt.date.today()
    span = time_range(question, today)
    words = keywords_for(question)
    q = _fts_or_query(words)
    docs = []
    named = _named_senders(owner_ids, words) if owner_ids else {}
    if q and owner_ids:
        marks = ",".join("?" * len(owner_ids))
        where, args = "", []
        if span:
            # Zeitraum: nur datierte Dokumente darin – das Eingangsdatum sagt nichts (alte Briefe werden spät gescannt)
            where = " AND d.document_date BETWEEN ? AND ?"
            args = [span[0].isoformat(), span[1].isoformat()]
            limit = max(limit, 12)
        with connect() as con:
            def search(query):
                return [dict(r) for r in con.execute(
                    f"SELECT d.id, d.title, d.document_date, d.sender, d.amount, d.currency, d.text, "
                    f"bm25(documents_fts) AS rank "
                    f"FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid "
                    f"WHERE documents_fts MATCH ? AND d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks}){where} "
                    f"ORDER BY rank LIMIT ?", (query, *owner_ids, *args, limit * 4 if not span else limit)).fetchall()]
            # Eine genannte Firma muss passen – sonst verdrängen „Rechnung“, „Beleg“ (selten, hoher Wert) die Telekom-Belege.
            # Findet das nichts (Firma steht nur im Absender-Feld anders), ohne diese Pflicht.
            docs = search(f"({_fts_or_query(list(named))}) AND ({q})") if named else []
            restricted = bool(docs)
            if not docs:
                docs = search(q)
            # Noch Platz: Begriffe mitten in zusammengesetzten Wörtern („Steuerbescheid“ → „Einkommensteuerbescheid“) –
            # nicht, wenn schon die Belege der genannten Firma gefunden sind
            want = limit * 4 if not span else limit
            long = [w.replace('"', "") for w in words if len(w) >= TRIGRAM_MIN and not w.isdigit()]
            if len(docs) < want and long and not restricted and has_trigram(con):
                seen = [d["id"] for d in docs] or [0]
                rows = con.execute(
                    f"SELECT d.id, d.title, d.document_date, d.sender, d.amount, d.currency, d.text "
                    f"FROM documents_tri JOIN documents d ON d.id = documents_tri.rowid "
                    f"WHERE documents_tri MATCH ? AND d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks}){where} "
                    f"AND d.id NOT IN ({','.join('?' * len(seen))}) ORDER BY bm25(documents_tri) LIMIT ?",
                    (" OR ".join(f'"{w}"' for w in long), *owner_ids, *args, *seen, want - len(docs))).fetchall()
                docs += [dict(r) for r in rows]
            if not span and not _HISTORY_Q.search(question):
                docs = _newest_of_series(docs, limit)
            elif not span:
                docs = _spread_series(docs, limit)
            else:
                docs = docs[:limit]
    try:
        overview = facts(owner_ids, today, warranty=bool(_WARRANTY_Q.search(question))) + (_sender_facts(owner_ids, named, span) if _SUM_Q.search(question) else "")
        if extra_facts:
            overview += extra_facts(span)
    except Exception:
        log.exception("Übersicht für die Frage fehlgeschlagen")
        overview = ""
    if not docs and not _OVERVIEW_Q.search(question):
        none = "Dazu habe ich keine passenden Dokumente gefunden."
        if span:
            none = f"Zwischen {_de(span[0])} und {_de(span[1])} habe ich dazu keine passenden Dokumente gefunden."
        return {"answer": none, "sources": [], "keywords": words, "range": _range_out(span)}
    if span:
        docs.sort(key=lambda d: d["document_date"] or "")  # zeitliche Fragen: chronologisch lesen
    # Kurze Dokumente (Abrechnungen, Bescheide) ganz mitgeben, lange als Ausschnitte um die Treffer
    per_doc = CONTEXT_CHARS // max(len(docs), 1)
    ctx_parts, sources = [], []
    for i, d in enumerate(docs, 1):
        head = f"[Dok {i}] {d['title']}" + (f" ({d['document_date']})" if d["document_date"] else "") + (f" – {d['sender']}" if d["sender"] else "")
        if "warranty_until" in d.keys() and d["warranty_until"]:   # Kaufbeleg: Ende der Gewährleistung/Garantie
            what = "Garantie" if d["warranty_kind"] == "garantie" else "Gewährleistung"
            head += f" [{what} bis {_de(dt.date.fromisoformat(d['warranty_until']))}]"
        text = d["text"] or ""
        body = text if len(text) <= per_doc else _snippets(text, words, per_doc)
        ctx_parts.append(head + "\n" + body)
        sources.append({"n": i, "id": d["id"], "title": d["title"], "document_date": d["document_date"], "sender": d["sender"]})
    range_note = ""
    if span:
        range_note = (f" Gefragt ist der Zeitraum {_de(span[0])} bis {_de(span[1])}; die Dokumente unten stammen "
                      f"aus diesem Zeitraum.")
    try:
        answer = _chat(ANSWER_PROMPT.format(question=question, context="\n\n".join(ctx_parts) or "(keine passenden gefunden)",
                                            today=_de(today), range_note=range_note, facts=overview),
                       timeout=240, purpose="ask") or ""
    except Exception as e:
        log.warning("Antwort fehlgeschlagen: %s", e)
        return {"answer": "Das LLM ist gerade nicht erreichbar.", "sources": sources, "keywords": words, "error": True}
    return {"answer": check_sum(answer.strip()), "sources": sources, "keywords": words, "range": _range_out(span)}


# Fragen, die die Übersicht (Fristen, Verträge) auch ohne Dokumenttreffer beantworten kann
_WARRANTY_Q = re.compile(r"garantie|gewährleist|gewaehrleist|warranty", re.I)
_OVERVIEW_Q = re.compile(r"frist|fällig|zahlen|bezahl|kündig|vertr[aä]g|abo|monatlich|laufend|überweis|rechnung|garantie|"
                         r"gewährleist|gewaehrleist|ausweis|reisepass|pass\b|führerschein|karte|gültig|abl[aä]uf|tüv|"
                         r"hauptuntersuchung|\bhu\b|wartung|schornstein|erstatt|teurer|preiserhöh|ausgegeben|ausgaben|gekostet|"
                         r"einnahmen|kleidung|lebensmittel|wofür", re.I)

_MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})(?!\d|,\d)")


def _money(m: re.Match) -> int:
    return int(m.group(1).replace(".", "")) * 100 + int(m.group(2))


def _fmt_money(cents: int) -> str:
    return f"{cents // 100:,}".replace(",", ".") + f",{cents % 100:02}"


def check_sum(answer: str) -> str:
    """Sprachmodelle verrechnen sich bei Summen. Steht eine Liste mit je einem Betrag und danach eine Summenzeile
    in der Antwort, wird die Summe nachgerechnet und bei Abweichung ersetzt."""
    lines = answer.split("\n")
    for i, line in enumerate(lines):
        if not re.match(r"\W*(gesamt\w*|summe|insgesamt)\b", line.strip(), re.I):
            continue
        if "[Übersicht]" in line:
            return answer   # Summe aus der Datenbank: zählt alle Belege, die Liste darüber zeigt oft nur einige
        found = list(_MONEY.finditer(line))
        items = []
        for prev in reversed(lines[:i]):
            if not prev.strip():
                if items:
                    break
                continue
            if not re.match(r"\s*([-*•]|\d+\.)\s", prev):
                break
            amounts = list(_MONEY.finditer(prev))
            if len(amounts) != 1 or "[Übersicht]" in prev:
                return answer   # unklar, welcher Betrag zählt: nichts anfassen
            items.append(_money(amounts[0]))
        if len(found) != 1 or len(items) < 2:
            return answer
        total = sum(items)
        if _money(found[0]) != total:
            a, b = found[0].span()
            lines[i] = line[:a] + _fmt_money(total) + line[b:] + " (vom Server nachgerechnet)"
            log.info("Summe korrigiert: %s -> %s", found[0].group(0), _fmt_money(total))
        return "\n".join(lines)
    return answer


def _de(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def _range_out(span) -> dict | None:
    return {"from": span[0].isoformat(), "to": span[1].isoformat()} if span else None


SENDER_MERGE_PROMPT = """Hier ist die Liste der Absender einer privaten Dokumentenablage, jeweils mit Anzahl der Dokumente:
{tags}

Finde Gruppen von Absendern, die dieselbe Firma, Behörde oder Person meinen und sich nur in Schreibweise, Rechtsform,
Zusatz oder Abkürzung unterscheiden (z. B. "Telekom"/"Telekom Deutschland GmbH"/"Deutsche Telekom", "Stadtwerke"/"Stadtwerke Musterstadt",
"AOK"/"AOK Bayern"). Wähle je Gruppe als Ziel den gebräuchlichsten, gut lesbaren Namen; bei Gleichstand den mit den meisten Dokumenten.
Verschiedene Unternehmen bleiben getrennt, auch wenn sie ähnlich heißen ("Allianz" und "Alliander"). Verwende die Namen exakt wie oben geschrieben.
Antworte NUR mit einem JSON-Array: [{{"target": "...", "sources": ["...", "..."], "reason": "kurze Begründung"}}]
Keine Gruppe gefunden: []"""


def suggest_sender_merges(senders: list[dict]) -> list[dict]:
    """senders: [{id, name, count}] (id = Name). Gleiche Logik wie bei Tags, anderer Prompt.
    Schlägt das LLM eine Schreibweise in Großbuchstaben als Ziel vor, wird die häufigste normal geschriebene genommen."""
    groups = suggest_tag_merges(senders, SENDER_MERGE_PROMPT)
    caps = lambda n: n == n.upper() and n != n.lower()
    for g in groups:
        if caps(g["target"]["name"]):
            better = sorted((s for s in g["sources"] if not caps(s["name"])), key=lambda s: -s["count"])
            if better:
                b = better[0]
                g["sources"] = [s for s in g["sources"] if s is not b] + [g["target"]]
                g["target"] = b
    return groups
