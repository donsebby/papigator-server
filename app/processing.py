"""Verarbeitungsschritte für ein Dokument: Text holen, OCR, Thumbnail, Tagging."""
import datetime
import json
import shutil
import sys
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
from pypdf import PdfReader, PdfWriter

from . import config

log = logging.getLogger("papigator.processing")


def doc_path(doc_id: int) -> Path:
    return config.DOCS_DIR / f"{doc_id}.pdf"


def thumb_path(doc_id: int) -> Path:
    return config.THUMBS_DIR / f"{doc_id}.jpg"


def page_count(pdf: Path) -> int:
    try:
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 0


def pdf_problem(pdf: Path) -> str | None:
    """Warum sich die Datei nicht ablegen lässt – oder None. Ein PDF, das nur Rechte einschränkt (Drucken verboten,
    typisch bei Kontoauszügen), lässt sich ohne Passwort öffnen und ist in Ordnung."""
    try:
        reader = PdfReader(str(pdf))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return "Das PDF ist mit einem Passwort geschützt – bitte ohne Passwort speichern und erneut ablegen."
        if len(reader.pages) < 1:
            return "Das PDF hat keine Seiten."
    except Exception:
        return "Die Datei ist kein lesbares PDF."
    return None


def extract_text(pdf: Path) -> str:
    try:
        reader = PdfReader(str(pdf))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        log.warning("Textextraktion fehlgeschlagen: %s", e)
        return ""


def has_enough_text(text: str, pages: int) -> bool:
    # Heuristik: ein gescanntes PDF liefert quasi keinen Text
    return len(text) > 40 * max(pages, 1)


def run_ocr(pdf: Path) -> str:
    """OCR in-place (fügt Textebene hinzu) und gibt den Text zurück."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        sidecar = Path(tmp) / "ocr.txt"
        cmd = [
            sys.executable, "-m", "ocrmypdf", "-l", config.llm_config()["ocr_langs"], "--skip-text", "--rotate-pages",
            "--deskew", "--optimize", "1", "--sidecar", str(sidecar),
            "--quiet", str(pdf), str(out),
        ]
        subprocess.run(cmd, check=True, timeout=600)
        shutil.move(str(out), str(pdf))
        ocr_text = sidecar.read_text(errors="ignore").strip() if sidecar.exists() else ""
        return ocr_text or extract_text(pdf)


def make_thumbnail(pdf: Path, thumb: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "t"
        subprocess.run(
            ["pdftoppm", "-f", "1", "-l", "1", "-jpeg", "-r", "60",
             "-scale-to-x", str(config.llm_config()["thumb_width"]), "-scale-to-y", "-1",
             str(pdf), str(prefix)],
            check=True, timeout=60,
        )
        files = sorted(Path(tmp).glob("t*.jpg"))
        if files:
            shutil.move(str(files[0]), str(thumb))


def merge_pdfs(sources: list[Path], target: Path) -> None:
    writer = PdfWriter()
    for src in sources:
        for page in PdfReader(str(src)).pages:
            writer.add_page(page)
    with open(target, "wb") as f:
        writer.write(f)


# ---------- Tagging ----------

def rule_hit(pattern: str, word, lowered: str) -> bool:
    """Trifft eine Regel den (kleingeschriebenen) Text? Normale Regeln: Teilwort ab 3 Zeichen. Personen-Regeln
    (word=1): nur das ganze Wort, auch mit Genitiv-s („Mias Zeugnis“) – sonst stünde „Ben“ in jedem „neben“."""
    p = (pattern or "").strip().lower()
    if len(p) < 3:
        return False
    if word:
        return re.search(r"(?<!\w)" + re.escape(p) + r"s?(?!\w)", lowered) is not None
    return p in lowered


def rule_allowed(con, created_by, owner_id) -> bool:
    """Regeln mit Ersteller (Personen) gelten nur für Dokumente, die der Ersteller bearbeiten darf – sonst bekäme ein
    fremder Bestand den Namen einer Person, die der andere gar nicht kennen soll."""
    if created_by is None or owner_id == created_by:
        return True
    return con.execute("SELECT 1 FROM partner_shares WHERE from_user=? AND to_user=? AND can_edit=1",
                       (owner_id, created_by)).fetchone() is not None


def apply_rules(con, doc_id: int, text: str) -> list[str]:
    hits = []
    lowered = text.lower()
    owner = con.execute("SELECT owner_id FROM documents WHERE id=?", (doc_id,)).fetchone()
    owner_id = owner["owner_id"] if owner else None
    for row in con.execute("SELECT r.pattern, r.word, r.created_by, t.id, t.name FROM rules r JOIN tags t ON t.id=r.tag_id"):
        if rule_hit(row["pattern"], row["word"], lowered) and rule_allowed(con, row["created_by"], owner_id):
            con.execute(
                "INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) VALUES (?,?,'rule')",
                (doc_id, row["id"]),
            )
            hits.append(row["name"])
    return hits


LLM_PROMPT = """Du bist ein Assistent für eine private Dokumentenablage. Analysiere den folgenden Dokumenttext und antworte NUR mit einem JSON-Objekt, ohne Erklärung, ohne Markdown.
Heute ist der {today}. Scan-, Fax- und Druckvermerke (z. B. „gescannt am …“, Faxkopfzeilen) sind NICHT das Dokumentdatum.
Nennt ein Brief Tag und Monat ohne Jahr, nimm das Jahr, das zum Rest des Textes passt – im Zweifel null statt raten.

Felder:
- "title": kurzer, sprechender Titel (max. 60 Zeichen), z. B. "Rechnung Telekom Oktober 2026"
- "sender": Absender/Firma/Behörde oder null
- "document_date": das Datum, an dem das Dokument ausgestellt/geschrieben wurde, als YYYY-MM-DD – meist oben beim
  Briefkopf („Musterstadt, 14.05.2015“), als Rechnungs-, Bescheid- oder Abrechnungsdatum oder beim Unterschriftsdatum.
  NICHT: Vertragsbeginn/-ende, „gültig ab“, Leistungs- oder Abrechnungszeitraum, Fristen, Geburtsdaten, Druck- oder
  Formularstände („Stand 03/2026“), Datum in Fußzeilen. Steht dort nur Monat und Jahr („Bremen, Januar 2022“), der
  Erste des Monats; Jahresbescheinigungen nur mit Jahr („Tag der Zuwendung: 2021“): der 31.12. dieses Jahres. Steht
  kein solches Datum im Text, null – nicht raten.
- "amount": Betrag als Zahl oder null, so wie er im Text steht – bei Rechnungen der Rechnungsbetrag; bei Abrechnungen (Strom, Nebenkosten) die Nachzahlung bzw. das Guthaben; bei Verträgen, Darlehen und Beitrags-/Gebührenbescheiden die regelmäßige Rate; bei Gehaltsabrechnungen der Auszahlungsbetrag. Steht kein Betrag im Text, null – nicht rechnen oder raten.
- "currency": z. B. "EUR" oder null
- "summary": ein Satz, worum es geht
- "kind": Art des Dokuments, genau eins von: "Rechnung" (auch Quittung, Kassenbon, Beitragsrechnung), "Gutschrift",
  "Mahnung", "Abrechnung" (Strom, Nebenkosten u. ä.), "Lohnabrechnung", "Kontoauszug", "Bescheid" (Behörde setzt etwas fest),
  "Vertrag", "Versicherungsschein", "Kündigung", "Angebot", "Bestätigung" (Buchung, Termin, Auftrag, Eingang),
  "Spendenquittung", "Bescheinigung" (Nachweis, Meldebescheinigung, Krankmeldung, Impfnachweis), "Zeugnis" (Schul-,
  Arbeits-, Kurszeugnis, Halbjahresinformation, Zertifikat), "Urkunde" (Geburts-, Heirats-, Sterbeurkunde u. ä.), "Ausweis",
  "Befund" (Arztbrief, Laborbefund), "Bericht" (Protokoll, Übergabeprotokoll, Prüfbericht, Gutachten), "Antrag" (auch
  ausgefülltes Formular), "Mitteilung" (Information, Elternbrief, Schreiben ohne Forderung), "Sonstiges"
- "recipient": an wen das Dokument gerichtet ist oder für wen es ausgestellt wurde, wie im Anschriftfeld bzw. bei
  „Kunde:“, „Mitglied:“, „Käufer:“, „Patient:“, „Name:“ – nur der Name, ohne Anschrift; mehrere Namen mit „und“; bei einem
  Antrag die Stelle, an die er geht; null, wenn nicht erkennbar. Nie der Absender
- "references": Objekt mit Nummern, die so im Text stehen, sonst null: "contract" (Vertrags-, Versicherungsschein-,
  Mitglieds-, Darlehensnummer), "customer" (Kunden-, Beitrags-, Personal-, Versichertennummer), "case" (Aktenzeichen,
  Steuernummer, Vorgangs-, Register-, Buchungsnummer), "invoice" (Rechnungs-, Bestell-, Angebotsnummer), "other" (sonstige
  Kennnummer wie Ausweis-, Zertifikats-, Prüfbericht-Nr., IBAN)
- "direction": "Ausgabe" (ich muss zahlen oder habe bezahlt), "Einnahme" (ich bekomme Geld: Gehalt, Erstattung, Guthaben, Gutschrift) oder "keine" (dieses Dokument bewegt selbst kein Geld, z. B. Vertrag, Mahnung zu einer Rechnung, Information)
- "autopay": true, wenn der Betrag per Lastschrift/Abbuchung eingezogen wird oder schon bezahlt ist (Karte, PayPal, bar), sonst false
- "recurring": "monatlich", "vierteljährlich", "halbjährlich" oder "jährlich", wenn das Dokument eine regelmäßige Zahlung festlegt, die ICH leiste (Vertrag, Abo, Darlehen, Beitrags-/Gebührenbescheid), sonst null. Regelmäßiges Geld, das ich bekomme (Gehalt im Arbeitsvertrag, Miete als Vermieter, Rente), ist null – dann "direction": "Einnahme". Betrag und Rhythmus müssen zusammenpassen: „vierteljährlich 55,08 €“ ist "vierteljährlich" mit 55.08, nicht "monatlich"
- "tax_35a": ausgewiesene Arbeits-/Lohnkosten für Handwerker oder haushaltsnahe Dienstleistungen nach § 35a EStG als Zahl, sonst null
- "due_date": eine Frist als YYYY-MM-DD nur, wenn der Empfänger bis dahin selbst etwas tun muss (zahlen, widersprechen,
  kündigen, zurückgeben, anmelden, einreichen) und der Text das Datum nennt oder eindeutig vorgibt („zahlbar bis
  30.04.“, „innerhalb von 14 Tagen nach Rechnungsdatum“). Sonst null. Keine Frist bei Lastschrift, bei
  Gehaltsabrechnungen (Auszahlung), Gutschriften, Kündigungsbestätigungen oder bloßen Vertragsdaten (Beginn, Ende, Laufzeit).
- "due_note": wofür die Frist gilt, 2-4 Wörter, z. B. "Zahlungsziel", "Kündigungsfrist Vertrag", oder null
- "goods": true, wenn das Dokument eine Rechnung, Quittung, ein Kassenbon oder eine Bestellbestätigung über den KAUF eines
  Gegenstands ist (Gerät, Möbel, Kleidung, Werkzeug, Fahrrad …); false bei Dienstleistungen, Reparaturen, Handwerkerarbeiten,
  Strom/Gas/Wasser, Telefon, Abos, Miete, Versicherungen, Gebühren, Lebensmitteln und Verbrauchsmaterial
- "warranty_years": Dauer einer Hersteller- oder Händlergarantie in Jahren, NUR wenn sie so im Text steht („3 Jahre
  Garantie“, „36 Monate Herstellergarantie“ → 3), sonst null. Nicht die gesetzliche Gewährleistung, keine angebotene oder
  nur gegen Aufpreis/Registrierung mögliche Verlängerung
- "online": true, wenn es ein Online-Kauf mit Lieferung ist (Online-Shop, Bestellung im Internet), sonst false –
  Kassenbons und Käufe im Laden (bar, EC-Karte an der Kasse) sind false
- "expires_what": NUR bei Ausweisen und Karten, genau eins von "Personalausweis", "Reisepass", "Führerschein",
  "Aufenthaltstitel", "Bankkarte", "Kreditkarte", "Gesundheitskarte", "Schwerbehindertenausweis", "Parkausweis",
  "BahnCard" – sonst null (nicht bei Angeboten, Gutscheinen, Tickets, Rezepten, Verträgen)
- "expires_on": bei so einem Ausweis/einer Karte das „gültig bis“ als YYYY-MM-DD (nur Monat/Jahr wie „03/28“: letzter
  Tag des Monats), nicht Geburts- oder Ausstellungsdatum; sonst null
- "service_what": wenn das Dokument eine durchgeführte Prüfung/Wartung belegt, genau eins von "Hauptuntersuchung"
  (TÜV/DEKRA-Prüfbericht), "Heizungswartung", "Schornsteinfeger", "Rauchmelder" – sonst null
- "service_next": nächster Termin dafür als YYYY-MM-DD, nur wenn er im Text steht („nächste HU 03/2027“ → 2027-03-31), sonst null
- "tags": Liste aus 1-3 Tags. {tag_rule} Spendenquittungen, Steuerbescheide und Belege mit § 35a-Anteil bekommen zusätzlich den Tag "Steuer".{lang_note}

Dokumenttext:
\"\"\"
{text}
\"\"\""""


def _excerpt(text: str, limit: int) -> str:
    """Lange Dokumente: Anfang UND Ende mitgeben. Bei Verträgen steht das Datum oft erst bei den Unterschriften –
    abgeschnitten nach 6000 Zeichen nahm das Modell dann einen Scan-Stempel (Vertrag von 2015 als „2026“)."""
    if len(text) <= limit:
        return text
    tail = limit // 3
    return text[: limit - tail] + "\n[…]\n" + text[-tail:]


# Englische KI-Texte (Einstellung „Sprache der Erkennung“, PAPIGATOR_LANGUAGE=en): Titel, Zusammenfassung, neue Tags und
# (seit 26.11.2026) die Fristnotiz auf Englisch – deren Wortlisten (_PAYMENT_NOTE, _NO_DUE_NOTE, _DUE_ACTION …) kennen
# beide Sprachen. Alle anderen Werte, die der Code auswertet (kind, direction, recurring, expires_what, service_what – über
# KINDS, DIRECTIONS, INTERVALS, EXPIRES, SERVICES), bleiben deutsch.
# Der deutsche Prompt bleibt byte-gleich (tests/test_llm_language.py), der englische hängt nur diesen Absatz an.
LLM_LANG_EN = """
Sprache: Wer diese Ablage nutzt, liest Englisch. Schreibe "title" und "summary" auf Englisch, auch wenn das Dokument
deutsch ist, z. B. "title": "Telekom invoice October 2026", "summary": "Phone bill for October, paid by direct debit."
Alle anderen Felder genau wie oben beschrieben: "kind", "direction", "recurring", "expires_what" und "service_what" nur
mit den deutschen Werten aus den Listen oben, "due_note" auf Englisch (z. B. "Payment due", "Notice period contract"),
"sender", "recipient" und Nummern so, wie sie im Text stehen."""
LLM_LANG_EN_TAGS = """ Neue Tags auf Englisch; statt des Tags "Steuer" heißt er "Tax"."""


def build_prompt(text: str, existing_tags: list[str], restricted: bool = False, lang: str = "de",
                 today: datetime.date | None = None) -> str:
    """Prompt fürs Erkennen. lang="de" ergibt genau den bisherigen Prompt."""
    listed = ", ".join(existing_tags) or "(noch keine)"
    only_listed = restricted and existing_tags
    if only_listed:
        tag_rule = (f"Verwende AUSSCHLIESSLICH Tags aus dieser Liste, exakt so geschrieben: {listed}. "
                    "Passt keiner, gib eine leere Liste zurück.")
    elif lang == "en":
        tag_rule = (f"Verwende bevorzugt diese vorhandenen Tags: {listed}. Neue Tags nur, wenn wirklich keiner passt. "
                    'Tags sind kurze englische Substantive im Singular wie "Invoice", "Insurance", "Tax", "Contract", '
                    '"Authority", "Doctor", "Salary", "Children". Kein Absendername als Tag.')
    else:
        tag_rule = (f"Verwende bevorzugt diese vorhandenen Tags: {listed}. Neue Tags nur, wenn wirklich keiner passt. "
                    'Tags sind kurze deutsche Substantive im Singular wie "Rechnung", "Versicherung", "Steuer", "Vertrag", '
                    '"Behörde", "Arzt", "Gehalt", "Kinder". Kein Absendername als Tag.')
    # Nur vorhandene Tags erlaubt: dann auch kein „Tax“ statt „Steuer“ – die Liste gilt, wie sie ist
    lang_note = (LLM_LANG_EN + ("" if only_listed else LLM_LANG_EN_TAGS)) if lang == "en" else ""
    return LLM_PROMPT.format(tag_rule=tag_rule, text=_excerpt(text, config.LLM_MAX_CHARS), lang_note=lang_note,
                             today=(today or datetime.date.today()).strftime("%d.%m.%Y"))


def llm_extract(text: str, existing_tags: list[str], restricted: bool = False) -> tuple[dict | None, bool]:
    """Liefert (Ergebnis, erreichbar). erreichbar=False nur bei Verbindungsproblemen.
    restricted=True: nur vorhandene Tags erlaubt (keine neuen erfinden)."""
    cfg = config.llm_config()
    if not cfg["enabled"] or not cfg["url"] or not text.strip():
        return None, True
    prompt = build_prompt(text, existing_tags, restricted, cfg["language"])
    try:
        from . import ask  # hier importiert, damit kein Zirkel entsteht
        content = ask.chat(prompt, timeout=300) or ""
        m = re.search(r"\{.*\}", content, re.S)
        return (json.loads(m.group(0)) if m else None), True
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
        log.warning("LLM nicht erreichbar: %s", e)
        return None, False
    except Exception as e:
        log.warning("LLM-Tagging fehlgeschlagen: %s", e)
        return None, True


def _iso_date(v) -> str | None:
    """YYYY-MM-DD aus LLM-Ausgaben; akzeptiert auch TT.MM.JJJJ. Unplausibles fällt weg."""
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2}).*", s) or None
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        from datetime import date
        return date(y, mo, d).isoformat() if 1900 <= y <= 2100 else None
    except ValueError:
        return None


def _amount(v) -> float | None:
    """Betrag als Zahl; versteht „1.234,56“, „1,234.56“, „190,32 €“, „12,-“, „-12,50“ und echte Zahlen."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2) if abs(v) < 1e12 else None
    s = str(v or "").strip().replace("\u2212", "-")
    s = re.sub(r"(?<=\d)[.,]\s*[-\u2013\u2014]+(?!\d)", "", s)   # „12,-“ / „12,–“: glatter Betrag
    s = re.sub(r"[^\d,.\-]", "", s)
    if not re.search(r"\d", s) or s.count("-") > 1 or "-" in s[1:]:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}([.,])\d{3}(\1\d{3})*", s):
        s = re.sub(r"[.,]", "", s)   # „1.234“ / „1,234“: nur Tausenderpunkte, keine Nachkommastellen
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


_CURRENCY_SIGNS = {"€": "EUR", "EURO": "EUR", "EUROS": "EUR", "$": "USD", "US$": "USD", "£": "GBP", "FR.": "CHF", "SFR": "CHF"}


def _currency(v) -> str | None:
    """Währung als ISO-Code: „€“/„Euro“ → EUR, „$“ → USD, „usd“ → USD; Unbekanntes fällt weg."""
    s = str(v or "").strip().upper()
    s = _CURRENCY_SIGNS.get(s, s)
    return s if re.fullmatch(r"[A-Z]{3}", s) else None


# Währung neben einem glatten Betrag: Euro wie bisher, dazu Dollar, Pfund, Franken (englische Rechnungen: „$50“, „USD 50“)
_CUR_WORDS = r"(?:€|eur\b|euro|\$|us\$|usd\b|£|gbp\b|chf\b)"


def amount_in_text(amount: float, text: str) -> bool:
    """Steht der Betrag wirklich im Text? Wie beim Datum: sonst hat das Sprachmodell ihn erfunden oder
    zusammengerechnet. Erkannt werden „324,87“, „324.87“, „1.234,56“, „1,234.56“, „1 234,56“, „EUR 324,87“ –
    glatte Beträge nur mit Nachkommastellen („50,00“, „50,-“) oder Währung („50 €“, „EUR 50“, „$50“, „50 USD“), damit eine
    50 in einer Postleitzahl oder einem Datum nicht zählt. Vorzeichen spielen keine Rolle."""
    if amount is None or not text:
        return False
    cents = round(abs(amount) * 100)
    whole, frac = divmod(cents, 100)
    t = re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("\u202f", " ")).lower()
    plain = str(whole)
    groups = []
    rest = plain
    while len(rest) > 3:
        groups.insert(0, rest[-3:])
        rest = rest[:-3]
    groups.insert(0, rest)
    ints = {(plain, None)} | ({(sep.join(groups), sep) for sep in (".", ",", " ", "'")} if len(groups) > 1 else set())
    pats = []
    for iv, tsep in ints:
        iv_re = re.escape(iv)
        for dsep in (",", "."):
            if dsep == tsep:
                continue
            pats.append(rf"{iv_re}{re.escape(dsep)}{frac:02d}(?!\d)")
            if frac == 0:
                pats.append(rf"{iv_re}{re.escape(dsep)}\s?[-\u2013\u2014]")
        if frac == 0:
            pats.append(rf"{iv_re}(?![.,]?\d)\s?{_CUR_WORDS}")
            pats.append(rf"{_CUR_WORDS}\s?{iv_re}(?![.,]?\d)")
    body = "|".join(pats)
    return bool(re.search(rf"(?<!\d)(?<!\d[.,])(?:{body})(?![.,]\d)", t))


# Dokumentart (seit 26.11.2026 allgemein, nicht nur Geld): interner Code → Bezeichnung. Die alten Codes bleiben
# (gehalt = Lohnabrechnung, spende = Spendenquittung, info = Mitteilung), weil Geld-Funktionen daran hängen.
KIND_LABELS = {
    "rechnung": ("Rechnung", "Invoice"), "gutschrift": ("Gutschrift", "Credit note"), "mahnung": ("Mahnung", "Reminder"),
    "abrechnung": ("Abrechnung", "Statement of charges"), "gehalt": ("Lohnabrechnung", "Payslip"),
    "kontoauszug": ("Kontoauszug", "Bank statement"), "bescheid": ("Bescheid", "Official notice"),
    "vertrag": ("Vertrag", "Contract"), "police": ("Versicherungsschein", "Insurance policy"),
    "kuendigung": ("Kündigung", "Cancellation"), "angebot": ("Angebot", "Quote"),
    "bestaetigung": ("Bestätigung", "Confirmation"), "spende": ("Spendenquittung", "Donation receipt"),
    "bescheinigung": ("Bescheinigung", "Certificate"), "zeugnis": ("Zeugnis", "Report / diploma"),
    "urkunde": ("Urkunde", "Official record"), "ausweis": ("Ausweis", "ID document"), "befund": ("Befund", "Medical report"),
    "bericht": ("Bericht", "Report"), "antrag": ("Antrag", "Application"), "info": ("Mitteilung", "Notification"),
    "sonstiges": ("Sonstiges", "Other"),
}
# Was die KI sagt (Bezeichnung, alte Namen, Kurzformen) → Code
KINDS = {**{c: c for c in KIND_LABELS}, **{de.lower(): c for c, (de, _) in KIND_LABELS.items()},
         "kündigung": "kuendigung", "gehalt": "gehalt", "spende": "spende", "info": "info", "versicherung": "police",
         "bestätigung": "bestaetigung", "quittung": "rechnung", "zertifikat": "zeugnis", "arztbrief": "befund",
         "protokoll": "bericht", "formular": "antrag", "brief": "info"}


def kind_code(label) -> str | None:
    """Was die KI als Art sagt → Code. Zusammengesetzte Wörter, die es in der Liste nicht gibt („Beitragsrechnung“,
    „Kündigungsbestätigung“), zählen nach ihrem Grundwort am Ende – das längste passende gewinnt (26.11.2026)."""
    word = str(label or "").strip().lower()
    if not word:
        return None
    if word in KINDS:
        return KINDS[word]
    for key in sorted(KINDS, key=len, reverse=True):
        if len(key) >= 5 and word.endswith(key):
            return KINDS[key]
    return None


# Arten, die selbst kein Geld bewegen – ein Betrag darin (Kontostand, Angebotssumme) ist keine Ausgabe
NON_MONEY_KINDS = ("kontoauszug", "angebot", "bescheinigung", "zeugnis", "urkunde", "ausweis", "befund", "bericht",
                   "antrag")
REF_KEYS = ("contract", "customer", "case", "invoice", "other")
DIRECTIONS = {"ausgabe": "out", "einnahme": "in", "keine": "none", "out": "out", "in": "in", "none": "none"}
INTERVALS = ("monatlich", "vierteljährlich", "halbjährlich", "jährlich")
# Fristen, die nur „bis dann wird bezahlt“ heißen – bei Lastschrift ist da nichts zu tun
# Englische Notizen (Erkennung auf Englisch) gleich behandelt: „Payment due“, „Direct debit“, „Instalment“
_PAYMENT_NOTE = re.compile(r"zahl|abbuch|lastschrift|einzug|fällig|überweis|rate|\bpay(?!out| ?slip)|amount due|balance due|"
                           r"debit|transfer|instal+ment", re.I)
# Kündigung ist keine Zahlung, auch wenn „fällig“ oder „due“ darin steht
_CANCEL_NOTE = re.compile(r"kündig|kündbar|cancel|notice period|period of notice|give notice|terminat", re.I)
OLD_DUE_DAYS = 14   # beim Erkennen schon so lange vorbei: Altbestand, gleich als erledigt ablegen


_MONTH_NAMES = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober",
                "november", "dezember"]
_MONTH_ALT = {3: ("mrz", "mär"), 9: ("sept",), 2: ("febr",)}
_MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december"]
_NO_DUE_NOTE = re.compile(r"auszahl|gutschrift|erstatt|gehalt|lohn|vertragsbeginn|vertragsende|laufzeit|gültig|beendigung|"
                          r"austritt|ende des arbeits|vertragsablauf|ablauf|ausweis|mitglied|"
                          r"payout|pay-out|refund|reimburs|credit note|salary|\bwages?\b|contract start|start of (?:the )?contract|"
                          r"contract end|end of (?:the )?contract|\bterm\b|\bvalid|expir|id card|passport|membership|"
                          r"end of employment", re.I)
# … außer die Notiz nennt etwas zu tun („Ausweis abholen“, „Erstattung beantragen“) – „\bzahl“ trifft nicht „Auszahlung“
_DUE_ACTION = re.compile(r"abhol|einreich|vorleg|vorzeig|kündig|widersp|beantrag|verlänger|erneuer|rückgabe|zurückgeb|"
                         r"anmeld|ummeld|abmeld|"
                         r"pick up|collect|submit|hand in|\bpresent|\bshow\b|cancel|notice period|give notice|terminat|object|appeal|"
                         r"\bapply|application|renew|extend|\breturn|register", re.I)
# Zahlen rettet nur Ausweis/Mitgliedschaft/Gültigkeit („Mitgliedsbeitrag zahlen“), nie Auszahlung/Gehalt/Erstattung
_DUE_PAY = re.compile(r"\bzahl|bezahl|überweis|\bpay(?!out| ?slip)|\btransfer", re.I)
_NO_DUE_MONEY_IN = re.compile(r"auszahl|gutschrift|erstatt|gehalt|lohn|payout|pay-out|refund|reimburs|credit note|salary|\bwages?\b",
                              re.I)
_RELATIVE = re.compile(r"(?:innerhalb|binnen)\s+(?:von\s+)?(\d{1,3}|eine[sm]?|zwei|drei|vier|vierzehn)\s+"
                       r"(tag|tage|tagen|woche|wochen|monat|monate|monaten)", re.I)
_WORDS = {"ein": 1, "eine": 1, "einem": 1, "eines": 1, "zwei": 2, "drei": 3, "vier": 4, "vierzehn": 14}


def _norm(text: str) -> str:
    """OCR-Fehler in Zahlen glätten („2O18“, „l2.03.“) und Leerraum vereinheitlichen – nur für den Datumsabgleich."""
    t = re.sub(r"(?<=\d)[Oo](?=\d)|(?<=\d)[Oo]\b|\b[Oo](?=\d)", "0", text)
    t = re.sub(r"(?<=\d)[lI](?=\d)|\b[lI](?=\d)", "1", t)
    t = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", t)
    return re.sub(r"\s+", " ", t).lower()


def _english() -> bool:
    """Erkennung auf Englisch? (nur für mehrdeutige Datumsangaben gefragt, daher ohne Zwischenspeicher)"""
    try:
        return config.language() == "en"
    except Exception:
        return False


def date_in_text(iso: str, text: str, allow_month: bool = True) -> bool:
    """Steht das Datum wirklich im Text? (14.05.2015, 14.5.15, 2015-05-14, 14. Mai 2015, Mai 2015 bei
    Monatsabrechnungen.) Sonst hat das Sprachmodell es erfunden oder aus Fristen/Laufzeiten zusammengesetzt."""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(iso)
    except ValueError:
        return False
    t = _norm(text or "")
    yy, y = f"{d.year % 100:02d}", str(d.year)
    days, months = {str(d.day), f"{d.day:02d}"}, {str(d.month), f"{d.month:02d}"}
    for dd in days:
        for mm in months:
            if re.search(rf"(?<!\d){dd}([./-]){mm}\1(?:{y}|{yy})(?!\d)", t):
                return True
    if iso in t:
        return True
    # US-Reihenfolge MM/DD/YYYY (nur mit Schrägstrich): eindeutig, wenn der Tag über 12 liegt (als TT/MM gäbe es den
    # Monat nicht); sonst ist 01/08/2026 auf Deutsch der 1. August – dann nur, wenn die Erkennung auf Englisch steht
    if d.day != d.month:
        for dd in days:
            for mm in months:
                if re.search(rf"(?<!\d){mm}/{dd}/(?:{y}|{yy})(?!\d)", t) and (d.day > 12 or _english()):
                    return True
    mon = _MONTH_NAMES[d.month - 1]
    # Schreibweisen: „März“, „Maerz“, „Marz“ (Umlaut verloren), „Mrz.“, „Sept.“ …
    alts = {mon, mon[:3], mon.replace("ä", "ae"), mon.replace("ä", "a")} | set(_MONTH_ALT.get(d.month, ()))
    short = "|".join(sorted((re.escape(a) for a in alts), key=len, reverse=True))
    if re.search(rf"(?<!\d)0?{d.day}\.?\s*(?:{short})\.?\s*{y}", t):
        return True
    # Englische Rechnungen (Adobe, Apple …): „Jan 8, 2026“, „January 8th, 2026“, „8 Jan 2026“
    en = _MONTHS_EN[d.month - 1]
    en_any = rf"(?:{en}|{en[:3]}\.?)"
    if re.search(rf"{en_any}\s*0?{d.day}(?:st|nd|rd|th)?,?\s*{y}|(?<!\d)0?{d.day}(?:st|nd|rd|th)?\s*{en_any},?\s*{y}", t):
        return True
    if not allow_month:
        return False
    # Monatsdokumente (Gehalt, Abrechnung): „Abrechnung März 2018“ oder „Abrechnungsmonat 03/2018“ reicht für ein
    # Datum in diesem Monat – aber nur in diesem Zusammenhang, nicht bei „gültig ab März 2026“
    month = rf"(?:{short})\.?\s*{y}|(?<!\d)(?:{d.month}|{d.month:02d})\s*/\s*{y}"
    if re.search(rf"(?:abrechnung|monat|bezüge|bezuege|gehalt|lohn|entgelt|verdienst|zeitraum)[^\n]{{0,40}}?(?:{month})", t):
        return True
    # Briefkopf/Unterschrift nur mit Monat: „Bremen, Januar 2022“, „Kassel, im April 2026“ (Ort, Komma, Monat Jahr)
    # – nicht „Stand: März 2026“
    if re.search(rf"[a-zäöüß]{{3}},\s*(?:im\s+)?(?:{short})\.?\s*{y}(?!\d)", t):
        return True
    # Jahresbescheinigungen ohne Tagesdatum: „Tag der Zuwendung: 2021“, „Bescheinigung für das Jahr 2021“
    return bool(re.search(rf"(?:tag der zuwendung|zuwendungsjahr|kalenderjahr|beitragsjahr|steuerjahr|für das jahr|"
                          rf"jahresbescheinigung)\s*:?\s*{y}(?!\d)", t))


# ---------- Gewährleistung / Garantie (17.11.2026) ----------
# Kaufbelege über Waren: gesetzliche Gewährleistung 2 Jahre (§ 438 BGB) ab Übergabe – wir rechnen ab Belegdatum, das ist
# eine Näherung. Eine längere Garantie zählt nur, wenn sie so im Text steht (wie Datum und Betrag).
# warranty_months: NULL = nie geprüft, 0 = keine (kein Kauf einer Ware), sonst Monate; warranty_kind: legal | garantie;
# warranty_until wird nur hier berechnet (warranty_end), damit Liste, Filter und Anzeige dasselbe Ende kennen.
WARRANTY_LEGAL = 24
_NUM = {"ein": 1, "eine": 1, "einem": 1, "einer": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5, "sechs": 6,
        "sieben": 7, "acht": 8, "zehn": 10, "zwölf": 12, "zwoelf": 12}
_NUMS = r"\d{1,3}|" + "|".join(sorted(_NUM, key=len, reverse=True))
_UNIT = r"jahre?n?|jährige[nrs]?|jaehrige[nrs]?|monate?n?|monatige[nrs]?"
_GARANTIE = re.compile(rf"(?P<n>{_NUMS})[\s-]*(?P<u>{_UNIT})\b(?P<mid>[^.!?;]{{0,40}}?)garantie"
                       rf"|garantie(?P<mid2>[^.!?;]{{0,30}}?)(?<![\w])(?P<n2>{_NUMS})[\s-]*(?P<u2>{_UNIT})\b")
_GARANTIE_NOT = re.compile(r"keine|ohne|bis zu|registrier|verlänger|verlaenger|zubuch|optional|aufpreis|gebühr|"
                           r"erweiter|abschließ|abschliess|angebot|möglich|moeglich")


def warranty_end(doc_date: str | None, months: int | None) -> str | None:
    """Ende der Gewährleistung/Garantie (Monatsende abgeschnitten: 31.01. + 1 Monat = 28./29.02.)."""
    import calendar
    from datetime import date as _date
    if not doc_date or not months:
        return None
    try:
        d = _date.fromisoformat(str(doc_date)[:10])
    except ValueError:
        return None
    m = d.month - 1 + int(months)
    y, m = d.year + m // 12, m % 12 + 1
    return _date(y, m, min(d.day, calendar.monthrange(y, m)[1])).isoformat()


def garantie_in_text(months: int, text: str) -> bool:
    """Steht eine Garantie von genau dieser Dauer im Text („3 Jahre Garantie“, „Herstellergarantie: 36 Monate“) –
    und nicht nur als Angebot („bis zu 5 Jahre nach Registrierung“, „Garantieverlängerung zubuchen“)?"""
    t = _norm(text or "")
    for m in _GARANTIE.finditer(t):
        n, u = (m.group("n"), m.group("u")) if m.group("n") else (m.group("n2"), m.group("u2"))
        num = int(n) if n.isdigit() else _NUM.get(n, 0)
        got = num if u.startswith("monat") else num * 12
        # Umfeld nur im selben Satz: „Keine Rücknahme. 3 Jahre Garantie“ ist eine Garantie
        ctx = re.split(r"[.!?;]", t[max(0, m.start() - 25): m.start()])[-1] + m.group(0) + \
            re.split(r"[.!?;]", t[m.end(): m.end() + 15])[0]
        if got == months and not _GARANTIE_NOT.search(ctx):
            return True
    return False


# ---------- Ablaufdaten, Wartungen, Erstattungen, Rücksendefrist (17.11.2026) ----------
# Eigene Felder statt der Frist: ein Dokument hat nur eine Frist (oft das Zahlungsziel), und ein Pass bis 2033 soll nicht
# jahrelang unter „Fristen“ stehen – „Heute“ zeigt sie erst, wenn sie näher rücken (/upcoming in main).
EXPIRES = ("Personalausweis", "Reisepass", "Führerschein", "Aufenthaltstitel", "Bankkarte", "Kreditkarte",
           "Gesundheitskarte", "Schwerbehindertenausweis", "Parkausweis", "BahnCard")
SERVICES = {"Hauptuntersuchung": 24, "Heizungswartung": 12, "Schornsteinfeger": 12, "Rauchmelder": 12}
_GOAE = re.compile(r"\bgo[äa]e?\b|\bgoz\b|gebührenordnung für (?:ärzte|zahnärzte)|gebuehrenordnung fuer", re.I)
_REFUND_DOC = re.compile(r"erstattung|leistungsabrechnung|beihilfe|abrechnung ihrer|kostenerstattung|wir erstatten", re.I)


def _closed(value, choices) -> str | None:
    v = str(value or "").strip().lower()
    return next((c for c in choices if c.lower() == v), None)


def month_in_text(iso: str, text: str) -> bool:
    """Steht das Datum – oder nur sein Monat – im Text? „gültig bis 03/28“, „03/2028“, „03.2028“, „März 2028“."""
    from datetime import date as _date
    if date_in_text(iso, text, allow_month=False):
        return True
    try:
        d = _date.fromisoformat(iso)
    except ValueError:
        return False
    t = _norm(text or "")
    mm = rf"(?:{d.month}|{d.month:02d})"
    if re.search(rf"(?<![\d./]){mm}\s*[/.-]\s*(?:{d.year}|{d.year % 100:02d})(?![\d./])", t):
        return True
    mon = _MONTH_NAMES[d.month - 1]
    return bool(re.search(rf"(?:{re.escape(mon)}|{re.escape(mon[:3])})\.?\s*{d.year}", t))


def _exact_in_month(iso: str, text: str) -> str | None:
    """Das Modell hat den Tag verrutscht („01.02.2031“ → 2031-02-28): ein vollständiges Datum im selben Monat aus dem
    Text nehmen (das späteste, falls mehrere – „gültig bis“ steht hinter Ausstellungsdaten)."""
    from datetime import date as _date
    d = _date.fromisoformat(iso)
    hits = []
    for m in re.finditer(rf"(?<!\d)(\d{{1,2}})\.\s*0?{d.month}\.\s*(?:{d.year}|{d.year % 100:02d})(?!\d)", _norm(text or "")):
        try:
            hits.append(_date(d.year, d.month, int(m.group(1))).isoformat())
        except ValueError:
            pass
    return max(hits) if hits else None


def _month_end(iso: str) -> str:
    import calendar
    from datetime import date as _date
    d = _date.fromisoformat(iso)
    return _date(d.year, d.month, calendar.monthrange(d.year, d.month)[1]).isoformat()


def apply_expiry(con, doc_id: int, data: dict) -> None:
    """Ausweise und Karten: „gültig bis“ (eigenes Feld). Nur aus einer festen Liste, nur wenn es im Text steht und
    nach dem Ausstellungsdatum liegt – sonst wäre ein Geburtsdatum schnell ein Ablaufdatum."""
    from datetime import date as _date
    row = con.execute("SELECT document_date, text, expires_on FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row or row["expires_on"]:
        return
    what = _closed(data.get("expires_what"), EXPIRES)
    exp = _iso_date(data.get("expires_on"))
    if not what or not exp:
        return
    text = row["text"] or ""
    if not date_in_text(exp, text, allow_month=False):
        exact = _exact_in_month(exp, text)
        if exact:
            exp = exact
        elif month_in_text(exp, text):
            exp = _month_end(exp)                 # nur Monat/Jahr („03/28“): gilt bis Monatsende
        else:
            return
    if (row["document_date"] and exp <= row["document_date"][:10]) or not "2000-01-01" < exp < f"{_date.today().year + 16}-01-01":
        return
    con.execute("UPDATE documents SET expires_on=?, expires_what=? WHERE id=?", (exp, what, doc_id))


def apply_service(con, doc_id: int, data: dict) -> None:
    """Prüfung/Wartung belegt (HU-Bericht, Heizungswartung …): nächster Termin aus dem Text, sonst Belegdatum + übliche
    Frist. Welche davon offen ist, entscheidet /upcoming: nur der jüngste Beleg je Art und Besitzer."""
    row = con.execute("SELECT document_date, text, service_what FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row or row["service_what"]:
        return
    what = _closed(data.get("service_what"), SERVICES)
    if not what:
        return
    nxt = _iso_date(data.get("service_next"))
    text = row["text"] or ""
    if nxt and not date_in_text(nxt, text, allow_month=False) and _exact_in_month(nxt, text):
        nxt = _exact_in_month(nxt, text)
    elif nxt and month_in_text(nxt, text):
        if not date_in_text(nxt, text, allow_month=False):
            nxt = _month_end(nxt)
    else:
        nxt = warranty_end(row["document_date"], SERVICES[what])
    if nxt and row["document_date"] and nxt <= row["document_date"][:10]:
        nxt = None
    con.execute("UPDATE documents SET service_what=?, service_next=? WHERE id=?", (what, nxt, doc_id))


def apply_refund(con, doc_id: int) -> None:
    """Arztrechnungen nach GOÄ/GOZ (Privatpatient): „Erstattung offen“. Kommt später eine Leistungsabrechnung oder ein
    Beihilfebescheid, in dem der Rechnungsbetrag steht, gilt sie als erstattet. Ohne KI – nur Text und Betrag."""
    row = con.execute("SELECT owner_id, doc_kind, amount, text, refund_state, document_date FROM documents WHERE id=?",
                      (doc_id,)).fetchone()
    if not row:
        return
    text = row["text"] or ""
    if row["refund_state"] is None and row["doc_kind"] == "rechnung" and row["amount"] and _GOAE.search(text):
        con.execute("UPDATE documents SET refund_state='open' WHERE id=?", (doc_id,))
        return
    if row["doc_kind"] != "rechnung" and _REFUND_DOC.search(text) and not _GOAE.search(text):
        # nur Rechnungen, die nicht jünger sind als die Abrechnung (die erstattet, was vorher kam)
        for r in con.execute("SELECT id, amount FROM documents WHERE owner_id=? AND refund_state='open' AND id<>? "
                             "AND deleted_at IS NULL AND amount > 0 AND (? IS NULL OR document_date IS NULL "
                             "OR document_date <= ?)", (row["owner_id"], doc_id, row["document_date"],
                                                         row["document_date"])).fetchall():
            if amount_in_text(r["amount"], text):
                con.execute("UPDATE documents SET refund_state='done', refund_doc=? WHERE id=?", (doc_id, r["id"]))


def apply_return_window(con, doc_id: int, data: dict) -> None:
    """Einstellung „Rücksendefrist merken“: Online-Kauf einer Ware → Frist 14 Tage ab Belegdatum (Widerruf), wenn das
    Dokument noch keine Frist hat. Aus = nichts (Standard)."""
    from datetime import date as _date, timedelta
    from .db import get_setting
    if get_setting(con, "return_window", "0") != "1" or data.get("online") is not True or data.get("goods") is not True:
        return
    row = con.execute("SELECT document_date, due_date FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row or row["due_date"] or not row["document_date"]:
        return
    due = (_date.fromisoformat(row["document_date"][:10]) + timedelta(days=14)).isoformat()
    if due >= _date.today().isoformat():
        con.execute("UPDATE documents SET due_date=?, due_note='Rücksendefrist', due_done=0 WHERE id=?", (due, doc_id))


def apply_extras(con, doc_id: int, data: dict) -> None:
    """Zusatzfelder aus einer KI-Antwort – jedes nur, wenn es noch nie gesetzt wurde (Nachtragen im Bestand: refill_w)."""
    apply_warranty(con, doc_id, data)
    apply_expiry(con, doc_id, data)
    apply_service(con, doc_id, data)
    apply_refund(con, doc_id)


def apply_warranty(con, doc_id: int, data: dict) -> None:
    """Gewährleistung/Garantie setzen – nur, wenn sie noch nie gesetzt wurde (Korrekturen von Hand bleiben)."""
    row = con.execute("SELECT doc_kind, direction, document_date, text, warranty_months FROM documents WHERE id=?",
                      (doc_id,)).fetchone()
    if not row:
        return
    if row["warranty_months"] is not None:
        # schon gesetzt (vielleicht von Hand): nur das Ende nachziehen, falls sich das Datum geändert hat
        end = warranty_end(row["document_date"], row["warranty_months"])
        con.execute("UPDATE documents SET warranty_until=? WHERE id=? AND warranty_until IS NOT ?", (end, doc_id, end))
        return
    if not isinstance(data.get("goods"), bool):
        return   # Antwort ohne Angabe (älteres Modell, abgeschnitten): offen lassen statt „keine“
    kind = row["doc_kind"] or kind_code(data.get("kind"))
    direction = row["direction"] or DIRECTIONS.get(str(data.get("direction") or "").strip().lower())
    # Kassenbons nennt das Modell auch mal „Info“ – mit goods=true zählen sie wie eine Rechnung
    if kind not in ("rechnung", "info", "bestaetigung", "sonstiges", None) or direction == "in" or (
            kind != "rechnung" and data.get("goods") is not True):
        return
    months, wkind = 0, None
    if data["goods"]:
        months, wkind = WARRANTY_LEGAL, "legal"
        try:
            years = float(data.get("warranty_years") or 0)
        except (TypeError, ValueError):
            years = 0
        if 0 < years <= 30:
            m = round(years * 12)
            if m > WARRANTY_LEGAL and garantie_in_text(m, row["text"] or ""):
                months, wkind = m, "garantie"
    con.execute("UPDATE documents SET warranty_months=?, warranty_kind=?, warranty_until=? WHERE id=?",
                (months, wkind, warranty_end(row["document_date"], months), doc_id))


def _relative_due_ok(due: str, doc_date: str | None, text: str) -> bool:
    """„Innerhalb von 14 Tagen“: Frist = Dokumentdatum + Zeitraum (± 3 Tage, Zustellung/Wochenende)."""
    from datetime import date as _date, timedelta
    if not doc_date:
        return False
    try:
        d0, d1 = _date.fromisoformat(doc_date), _date.fromisoformat(due)
    except ValueError:
        return False
    for m in _RELATIVE.finditer(_norm(text or "")):
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDS.get(m.group(1).lower(), 0)
        unit = m.group(2).lower()
        days = n * (7 if unit.startswith("woche") else 30 if unit.startswith("monat") else 1)
        # Spielraum: Zustellung/Wochenende; bei Monatsfristen auch die 3-Tage-Bekanntgabe bei Bescheiden
        if abs((d1 - d0).days - days) <= (6 if unit.startswith("monat") else 3):
            return True
    return False


def flag_doubtful_dates(con) -> int:
    """Einmalig für den Bestand: Dokumente markieren, deren Datum so nicht im Text steht (date_check=1). Das Datum
    bleibt stehen – vielleicht hat es jemand von Hand richtig eingetragen –, es erscheint aber unter „Zu prüfen“.
    Speichern eines Datums (Web/App) nimmt die Markierung wieder weg."""
    rows = con.execute("SELECT id, document_date, text FROM documents WHERE document_date IS NOT NULL "
                       "AND deleted_at IS NULL AND COALESCE(text, '') != ''").fetchall()
    # Positionsweise: beim Start (init_db) liefert die Verbindung Tupel, sonst sqlite3.Row
    ids = [r[0] for r in rows if not date_in_text(str(r[1])[:10], r[2])]
    for i in ids:
        con.execute("UPDATE documents SET date_check=1 WHERE id=?", (i,))
    log.info("Datum prüfen: %s von %s Dokumenten markiert", len(ids), len(rows))
    return len(ids)


def recheck_flagged_dates(con) -> int:
    """Bei jedem Start: markierte Dokumente erneut prüfen – kennt der Abgleich inzwischen mehr Schreibweisen,
    verschwindet eine falsche Markierung von selbst."""
    rows = con.execute("SELECT id, document_date, text FROM documents WHERE date_check=1").fetchall()
    ok = [r[0] for r in rows if r[1] and r[2] and date_in_text(str(r[1])[:10], r[2])]
    for i in ok:
        con.execute("UPDATE documents SET date_check=0 WHERE id=?", (i,))
    if ok:
        log.info("Datum prüfen: %s Markierungen aufgehoben", len(ok))
    return len(ok)


# Verträge, bei denen ich regelmäßig Geld bekomme – ihr Betrag ist keine Ausgabe und wird durch spätere
# Gehaltsabrechnungen überholt
_INCOME_CONTRACT = re.compile(r"arbeitsvertrag|anstellungsvertrag|dienstvertrag|ausbildungsvertrag|änderungsvertrag zum arbeits"
                              r"|gehaltserhöhung|gehaltsanpassung|vergütungsvereinbarung|rentenbescheid|rentenanpassung", re.I)


def money_fields(data: dict) -> dict:
    """Art, Richtung, Lastschrift, Rhythmus und § 35a-Anteil aus der LLM-Antwort, geprüft."""
    out = {}
    kind = kind_code(data.get("kind"))
    if kind:
        out["doc_kind"] = kind
    direction = DIRECTIONS.get(str(data.get("direction") or "").strip().lower())
    rec = str(data.get("recurring") or "").strip().lower()
    if kind in ("vertrag", "police") and (direction == "in" or _INCOME_CONTRACT.search(str(data.get("title") or ""))):
        rec = ""               # Arbeitsvertrag & Co.: regelmäßiges Geld kommt herein – kein Vertrag, der etwas kostet
    if kind in ("mahnung", "kuendigung") or (kind in ("vertrag", "police") and (rec in INTERVALS or direction == "in")):
        direction = "none"     # Mahnung: die Rechnung zählt schon; Vertrag mit Rate: gezahlt wird über Abbuchungen
    if kind in NON_MONEY_KINDS:
        direction, rec = "none", ""   # Kontoauszug, Angebot, Zeugnis …: ein Betrag darin ist keine Zahlung
    if kind in ("gehalt", "gutschrift"):
        direction = "in"
    if direction:
        out["direction"] = direction
    if isinstance(data.get("autopay"), bool):
        out["autopay"] = 1 if data["autopay"] else 0
    if rec in INTERVALS:
        out["recurring"] = rec
    tax = _amount(data.get("tax_35a"))
    if tax is not None and 0 < tax < 100000:
        out["tax_35a"] = tax
    return out


def _due_is_payment(note) -> bool:
    return bool(note) and bool(_PAYMENT_NOTE.search(str(note))) and not _CANCEL_NOTE.search(str(note))


def apply_money(con, doc_id: int, data: dict) -> None:
    """Nur die Geld-Felder nachtragen (Altbestand) – Titel, Datum, Tags und ein gespeicherter Betrag bleiben, wie sie
    sind. Fehlt der Betrag (abgelegt, als es keine Geld-Felder gab), kommt er dazu – wenn er so im Text steht.
    Eine vom LLM gesetzte Zahlungsfrist bei Lastschrift wird als erledigt abgelegt."""
    fields = money_fields(data)
    new_amount = _amount(data.get("amount"))
    row = con.execute("SELECT amount, currency, text FROM documents WHERE id=?", (doc_id,)).fetchone()
    if row and row["amount"] is None and new_amount is not None:
        if amount_in_text(new_amount, row["text"] or ""):
            fields["amount"] = new_amount
            cur = _currency(data.get("currency"))
            if cur and not row["currency"]:
                fields["currency"] = cur
        else:
            fields.pop("direction", None)   # Betrag erfunden: Richtung und Rhythmus hängen daran
            fields.pop("recurring", None)
    # Meint die KI heute einen anderen Betrag als den gespeicherten (z. B. Guthaben statt Gesamtkosten, Rate statt
    # Gesamtbetrag), passen Richtung und Rhythmus nicht zum gespeicherten Betrag – dann lieber offen lassen
    elif row and row["amount"] is not None and new_amount is not None and abs(new_amount - row["amount"]) > 0.01 * max(row["amount"], 1):
        fields.pop("direction", None)
        fields.pop("recurring", None)
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id))
    if fields.get("autopay") == 1:
        row = con.execute("SELECT due_note, due_done FROM documents WHERE id=? AND due_date IS NOT NULL", (doc_id,)).fetchone()
        if row and not row["due_done"] and _due_is_payment(row["due_note"]):
            con.execute("UPDATE documents SET due_done=1 WHERE id=?", (doc_id,))
    # Frist war schon vorbei, als das Dokument abgelegt wurde (alter Brief): kein „überfällig“
    con.execute("UPDATE documents SET due_done=1 WHERE id=? AND due_done=0 AND due_date < date(created_at, ?)",
                (doc_id, f"-{OLD_DUE_DAYS} days"))


def _alnum(s) -> str:
    return re.sub(r"[^0-9a-zäöüß]", "", str(s or "").lower())


def recipient_from(data: dict, text: str) -> str | None:
    """Empfänger – nur, wenn jedes längere Wort davon so im Text steht (keine erfundenen Namen)."""
    name = re.sub(r"\s+", " ", str(data.get("recipient") or "")).strip(" ,;:")[:120]
    words = [w for w in re.findall(r"[\wäöüßÄÖÜ-]{3,}", name) if w.lower() not in ("und", "herrn", "frau", "herr")]
    if not name or not words:
        return None
    low = (text or "").lower()
    # ganze Wörter: „Ben“ darf nicht in „Benachrichtigung“ stecken
    return name if all(re.search(r"(?<![\wäöüß])" + re.escape(w.lower()) + r"(?![\wäöüß])", low) for w in words) else None


def refs_from(data: dict, text: str) -> dict | None:
    """Nummern (Vertrag, Kunde, Aktenzeichen, Rechnung, sonstige) – nur, was so im Text steht."""
    raw = data.get("references")
    if not isinstance(raw, dict):
        return None
    low = (text or "").lower()
    out = {}
    for k in REF_KEYS:
        v = re.sub(r"\s+", " ", str(raw.get(k) or "")).strip()[:60]
        a = _alnum(v)
        if len(a) < 3 or not any(ch.isdigit() for ch in a):
            continue
        # Zeichen in Folge, dazwischen nur Trenner (Leerzeichen . / -), an Wortgrenzen – „2026 1“ darf nicht aus
        # „… 2026, 1 …“ zusammengesetzt werden
        pat = r"(?<![0-9a-zäöüß])" + r"[\s./-]?".join(re.escape(ch) for ch in a) + r"(?![0-9a-zäöüß])"
        if re.search(pat, low):
            out[k] = v
    return out or None


def kind_fields(con, doc_id: int, data: dict, refill: bool = False) -> dict:
    """Dokumentart, Empfänger und Nummern. refill (Bestand nachtragen): Art nur, wo bisher keine oder „Mitteilung“
    stand (die Geld-Arten sind erprobt), Empfänger und Nummern nur, wo noch nichts steht."""
    row = con.execute("SELECT text, doc_kind, recipient, refs FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return {}
    text = row["text"] or ""
    out = {}
    kind = kind_code(data.get("kind"))
    if kind and (not refill or row["doc_kind"] in (None, "info")):
        out["doc_kind"] = kind
    rec = recipient_from(data, text)
    if rec and (not refill or not row["recipient"]):
        out["recipient"] = rec
    refs = refs_from(data, text)
    if refs and (not refill or not row["refs"]):
        out["refs"] = json.dumps(refs, ensure_ascii=False)
    return out


def apply_kind(con, doc_id: int, data: dict) -> None:
    """Bestand nachtragen (llm_status='refill_k', 26.11.2026): nur Art, Empfänger, Nummern."""
    fields = kind_fields(con, doc_id, data, refill=True)
    if fields.get("doc_kind") in NON_MONEY_KINDS:
        fields.update(direction="none", recurring=None)   # z. B. „Mitteilung“ → „Angebot“: Summe zählt nicht als Ausgabe
    if fields:
        con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), doc_id))


def apply_llm(con, doc_id: int, data: dict, allowed: set[str] | None = None) -> None:
    """allowed: wenn gesetzt, werden nur Tags daraus (case-insensitive) übernommen."""
    fields = money_fields(data)
    fields.update({k: v for k, v in kind_fields(con, doc_id, data).items() if k != "doc_kind"})
    if data.get("sender") not in (None, ""):
        from .extras import canonical_sender   # späte Bindung: extras importiert processing
        fields["sender"] = canonical_sender(con, str(data["sender"]).strip()[:120])
    if data.get("summary") not in (None, ""):
        fields["summary"] = str(data["summary"]).strip()[:500]
    # Datum und Betrag prüfen: ein „14.03.2025“ oder „190,32 €“ würde Jahresfilter und die App-Liste sprengen. Und es muss im Text stehen –
    # sonst lieber kein Datum (landet unter „Zu prüfen“) als ein falsches (Vertrag von 2015 als „2026“).
    trow = con.execute("SELECT text FROM documents WHERE id=?", (doc_id,)).fetchone()
    text = (trow["text"] if trow else "") or ""
    date = _iso_date(data.get("document_date"))
    if date and text and not date_in_text(date, text):
        log.info("Dokument %s: Datum %s steht nicht im Text – verworfen", doc_id, date)
        date = None
    if date:
        fields["document_date"] = date
        fields["date_check"] = 0   # steht im Text – geprüft
        fields["no_date"] = 0
    # Betrag: wie das Datum nur, wenn er so im Text steht – ein zusammengerechneter oder geratener Betrag
    # würde Summen, Steuer-Paket und Verträge verfälschen
    amount = _amount(data.get("amount"))
    if amount is not None and text and not amount_in_text(amount, text):
        log.info("Dokument %s: Betrag %s steht nicht im Text – verworfen", doc_id, amount)
        amount = None
    elif amount is not None:
        fields["amount"] = amount
    cur = _currency(data.get("currency"))
    if cur and (amount is not None or data.get("amount") in (None, "")):
        fields["currency"] = cur
    title = data.get("title")
    if title:
        fields["title"] = str(title)[:120]
    # Frist nur übernehmen, wenn sie wie ein Datum aussieht und der Nutzer noch keine gesetzt hat
    due = _iso_date(data.get("due_date"))
    note = str(data.get("due_note") or "")[:60] or None
    if due and fields.get("autopay") == 1 and _due_is_payment(note):
        due = None   # Lastschrift: am Abbuchungstag ist nichts zu tun
    if due and note and _NO_DUE_NOTE.search(note) and not (
            _DUE_ACTION.search(note) or (_DUE_PAY.search(note) and not _NO_DUE_MONEY_IN.search(note))):
        due = None   # Auszahlung, Gutschrift, Vertragslaufzeit: nichts zu tun
    if due and date and due < date:
        due = None   # Frist vor dem Dokument: verwechselt (z. B. Leistungszeitraum)
    if due and text and not (date_in_text(due, text, allow_month=False) or _relative_due_ok(due, date, text)):
        log.info("Dokument %s: Frist %s steht nicht im Text – verworfen", doc_id, due)
        due = None
    if due:
        row = con.execute("SELECT due_date, date(created_at, ?) AS cutoff FROM documents WHERE id=?",
                          (f"-{OLD_DUE_DAYS} days", doc_id)).fetchone()
        if row and not row["due_date"]:
            fields["due_date"] = due
            fields["due_note"] = note
            # Schon beim Ablegen lange vorbei (alter Brief, spät gescannt): nicht als „überfällig“ melden
            fields["due_done"] = 1 if row["cutoff"] and due < row["cutoff"] else 0
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id))
    apply_extras(con, doc_id, data)
    apply_return_window(con, doc_id, data)
    for name in data.get("tags") or []:
        name = str(name).strip()[:40]
        if not name:
            continue
        if allowed is not None:
            match = next((a for a in allowed if a.lower() == name.lower()), None)
            if not match:
                continue
            name = match
        con.execute("INSERT OR IGNORE INTO tags(name, created_by) VALUES (?, (SELECT owner_id FROM documents WHERE id=?))",
                    (name, doc_id))
        tag_id = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]
        con.execute(
            "INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) VALUES (?,?,'llm')",
            (doc_id, tag_id),
        )


def apply_missing(con, doc_id: int, data: dict) -> None:
    """Übernommen aus Paperless-ngx (llm_status='refill_p', 26.11.2026): die KI ergänzt nur, was fehlt (Betrag, Frist,
    Zusammenfassung, Nummern …). Titel, Datum, Absender, Art und Tags aus dem Import bleiben – die Art geht als
    Vorgabe in die Geld-Regeln (Richtung, Rhythmus) ein."""
    row = con.execute("SELECT title, sender, document_date, doc_kind, amount, currency, summary FROM documents WHERE id=?",
                      (doc_id,)).fetchone()
    if not row:
        return
    d = dict(data)
    # was inzwischen jemand von Hand eingetragen hat (Betrag, Zusammenfassung), bleibt ebenfalls; die Frist sowieso
    for col, key in (("title", "title"), ("sender", "sender"), ("document_date", "document_date"), ("amount", "amount"),
                     ("currency", "currency"), ("summary", "summary")):
        if row[col]:
            d.pop(key, None)
    if row["doc_kind"]:
        d["kind"] = row["doc_kind"]
    if con.execute("SELECT 1 FROM document_tags WHERE document_id=? AND source<>'parent' LIMIT 1", (doc_id,)).fetchone():
        d["tags"] = []
    apply_llm(con, doc_id, d)


def llm_test(url: str, model: str, key: str | None = None) -> dict:
    """Verbindungstest (siehe llm.test)."""
    from . import llm
    return llm.test(url, model, key)
