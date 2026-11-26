"""Checkliste für die Notfallmappe (26.11.2026): was hineingehört, was schon drin ist, was in der Ablage passen könnte.

Nur ausgerechnet, nichts gespeichert: „in der Mappe“ heißt, ein Dokument im Notfallmappen-Ordner passt zum Punkt;
Vorschläge sind eigene (oder mit Schreibrecht geteilte) Dokumente außerhalb der Mappe – hineinlegen geht über den
bestehenden Weg POST /emergency/documents. Punkte, die keine Dokumente sind (Schlüssel, Passwortmanager, Haustiere …),
stehen nur als Hinweis darunter.

Lieber etwas übersehen als falsch abhaken: verglichen werden Titel und Zusammenfassung (nie der ganze Text – „Vollmacht“
steht in vielen Bankbriefen) mit eindeutigen Wörtern, ganze Wörter oder deren Ende („Risikolebensversicherung“ passt zu
„lebensversicherung“), dazu die Art bei Ausweisen. Der Ordner ist für alle derselbe Tag – gezählt wird nur, was der
Nutzer sieht (wie GET /emergency), vorgeschlagen nur, was er bearbeiten darf (wie beim Hineinlegen).
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends

from . import auth
from .auth import User, current_user
from .db import connect
from .family import _editable, _marks, emergency_tag

router = APIRouter()

SUGGESTIONS = 3

# (Schlüssel, (de, en), [Punkte]) – Punkt: (Schlüssel, (de, en), Wörter, Arten für Treffer, Arten für Vorschläge oder None = alle)
GROUPS = [
    ("will", ("Vollmachten und letzter Wille", "Powers of attorney and will"), [
        ("poa", ("Vorsorgevollmacht", "Lasting power of attorney"),
         ["vorsorgevollmacht", "generalvollmacht", "power of attorney"], [], None),
        ("care", ("Betreuungsverfügung", "Care directive"), ["betreuungsverfügung"], [], None),
        ("living", ("Patientenverfügung", "Living will"),
         ["patientenverfügung", "living will", "advance directive"], [], None),
        ("testament", ("Testament oder Erbvertrag", "Will or inheritance contract"),
         ["testament", "erbvertrag", "hinterlegungsschein", "last will"], [], None),
        ("custody", ("Sorgerechtsverfügung für die Kinder", "Guardianship for the children"),
         ["sorgerechtsverfügung", "sorgerecht", "guardianship"], [], None),
        ("organ", ("Organspendeausweis", "Organ donor card"), ["organspendeausweis", "organspende", "organ donor"], [], None),
        ("bank_poa", ("Bankvollmacht über den Tod hinaus", "Bank power of attorney"),
         ["kontovollmacht", "bankvollmacht"], [], None),
    ]),
    ("personal", ("Persönliche Unterlagen", "Personal documents"), [
        ("id", ("Personalausweis und Reisepass", "ID card and passport"),
         ["personalausweis", "reisepass", "passport", "identity card"], ["ausweis"], None),
        ("records", ("Geburts- und Heiratsurkunde, Stammbuch", "Birth and marriage certificates"),
         ["geburtsurkunde", "heiratsurkunde", "eheurkunde", "stammbuch", "birth certificate", "marriage certificate"],
         [], None),
        ("numbers", ("Steuer-ID und Sozialversicherungsnummer", "Tax ID and social security number"),
         ["steuer-id", "steueridentifikationsnummer", "identifikationsnummer", "sozialversicherungsausweis",
          "sozialversicherungsnummer", "rentenversicherungsnummer"], [], None),
    ]),
    ("health", ("Gesundheit", "Health"), [
        ("meds", ("Medikamentenplan und Allergien", "Medication plan and allergies"),
         ["medikationsplan", "medikamentenplan", "allergiepass", "allergieausweis", "medication plan"], [], None),
        ("vaccination", ("Impfpass und Implantat-Ausweise", "Vaccination record and implant cards"),
         ["impfpass", "impfausweis", "implantatausweis", "implantatpass", "vaccination record"], [], None),
    ]),
    ("money", ("Geld", "Money"), [
        ("accounts", ("Übersicht über Konten und Depots", "Overview of accounts"),
         ["kontenübersicht", "vermögensübersicht", "depotübersicht", "vermögensaufstellung"], [], None),
        ("loans", ("Kredite und Darlehen", "Loans"), ["darlehensvertrag", "kreditvertrag", "loan agreement"], [], None),
    ]),
    ("insurance", ("Versicherungen", "Insurance"), [
        ("ins_person", ("Kranken-, Lebens-, BU- und Unfallversicherung", "Health, life, disability and accident insurance"),
         ["krankenversicherung", "lebensversicherung", "berufsunfähigkeitsversicherung", "unfallversicherung",
          "pflegeversicherung", "life insurance", "health insurance"], [], ["police", "vertrag"]),
        ("ins_property", ("Haftpflicht, Hausrat, Wohngebäude, Kfz", "Liability, contents, building, car insurance"),
         ["haftpflichtversicherung", "hausratversicherung", "wohngebäudeversicherung", "kfz-versicherung",
          "sterbegeldversicherung", "liability insurance", "home insurance", "car insurance"], [], ["police", "vertrag"]),
    ]),
    ("home", ("Wohnen und Fahrzeug", "Home and car"), [
        ("housing", ("Mietvertrag oder Grundbuchauszug", "Tenancy agreement or land register"),
         ["mietvertrag", "grundbuchauszug", "grundbuch", "tenancy agreement", "land register"], [], None),
        ("vehicle", ("Fahrzeugbrief (Zulassungsbescheinigung Teil II)", "Vehicle registration certificate"),
         ["zulassungsbescheinigung teil ii", "fahrzeugbrief", "vehicle registration"], [], None),
    ]),
    ("work", ("Arbeit und Rente", "Work and pension"), [
        ("pension", ("Renteninformation, Betriebsrente, Riester", "Pension statements"),
         ["renteninformation", "rentenauskunft", "rentenbescheid", "betriebsrente", "riester", "pension statement"],
         [], None),
        ("employment", ("Arbeitsvertrag", "Employment contract"), ["arbeitsvertrag", "employment contract"], [], None),
    ]),
]

NOTE = ("Nicht als Dokument, aber genauso wichtig: wo die Originale liegen, wer welchen Schlüssel hat, wichtige "
        "Online-Konten und der Notfallzugang zum Passwortmanager (nie die Passwörter selbst), wie man das Handy "
        "entsperrt, Notfallkontakte, wer sich um Haustiere kümmert und Wünsche zur Bestattung.",
        "Not documents, but just as important: where the originals are kept, who has which key, important online "
        "accounts and emergency access to the password manager (never the passwords themselves), how to unlock the "
        "phone, emergency contacts, who looks after pets and funeral wishes.")

_WORD = re.compile(r"[\w-]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def matches(keywords: list[str], text: str, words: Optional[list[str]] = None) -> bool:
    """Ganzes Wort oder Wortende („Privathaftpflichtversicherung“ → „haftpflichtversicherung“), „riester-rente“ →
    „riester“; Begriffe mit Leerzeichen als Wortfolge."""
    low = text.lower()
    words = words if words is not None else _words(low)
    for kw in keywords:
        if " " in kw:
            if re.search(r"(?<![\w-])" + re.escape(kw) + r"(?![\w-])", low):
                return True
            continue
        for w in words:
            if w == kw or (len(kw) >= 6 and (w.endswith(kw) or w.startswith(kw + "-"))):
                return True
    return False


def _hit(item, row, words) -> bool:
    _, _, keywords, kinds, _ = item
    return (bool(kinds) and row["doc_kind"] in kinds) or matches(keywords, row["text"], words)


def _rows(con, sql: str, args) -> list[dict]:
    out = []
    for r in con.execute(sql, args):
        d = dict(r)
        d["text"] = f"{d['title'] or ''}\n{d['summary'] or ''}"
        out.append(d)
    return out


@router.get("/emergency/checklist")
def checklist(lang: str = "de", user: User = Depends(current_user)):
    en = lang.lower().startswith("en")
    i = 1 if en else 0
    groups, present_count, total = [], 0, 0
    with connect() as con:
        tag_id = emergency_tag(con) if user.id else None
        inside, candidates = [], []
        if user.id:
            visible = auth.visible_owner_ids(con, user, "all")
            cols = "d.id, d.title, d.summary, d.doc_kind, d.document_date, d.created_at"
            if tag_id is not None:
                inside = _rows(con, f"SELECT {cols} FROM documents d JOIN document_tags dt ON dt.document_id=d.id "
                                    f"AND dt.tag_id=? WHERE d.deleted_at IS NULL AND d.owner_id IN ({_marks(visible)})",
                               (tag_id, *visible))
            editable = _editable(con, user)
            candidates = _rows(con, f"SELECT {cols} FROM documents d WHERE d.deleted_at IS NULL AND d.ext_pending=0 "
                                    f"AND d.owner_id IN ({_marks(editable)}) AND NOT EXISTS (SELECT 1 FROM document_tags "
                                    "dt WHERE dt.document_id=d.id AND dt.tag_id=?) "
                                    "ORDER BY COALESCE(d.document_date, d.created_at) DESC, d.id DESC",
                               (*editable, tag_id if tag_id is not None else -1))
        for r in inside + candidates:
            r["words"] = _words(r["text"])
        for gkey, glabel, items in GROUPS:
            out = []
            for item in items:
                key, label, _, _, suggest_kinds = item
                present = [{"id": r["id"], "title": r["title"]} for r in inside if _hit(item, r, r["words"])]
                sugg = []
                if not present:
                    for r in candidates:
                        if (suggest_kinds is None or r["doc_kind"] in suggest_kinds or not r["doc_kind"]) \
                                and _hit(item, r, r["words"]):
                            sugg.append({"id": r["id"], "title": r["title"],
                                         "date": r["document_date"] or (r["created_at"] or "")[:10]})
                            if len(sugg) >= SUGGESTIONS:
                                break
                total += 1
                present_count += bool(present)
                out.append({"key": key, "label": label[i], "present": present, "suggestions": sugg})
            groups.append({"key": gkey, "label": glabel[i], "items": out})
    return {"groups": groups, "present": present_count, "total": total, "note": NOTE[i]}
