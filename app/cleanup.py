"""Aufbewahren & aufräumen: Vorschläge, welche Dokumente weg dürfen. Gelöscht wird nie automatisch – die Oberfläche
zeigt die Vorschläge nach Grund gruppiert, weg kommt nur, was jemand selbst in den Papierkorb legt.

Vorgaben für Privathaushalte in Deutschland, bewusst vorsichtig. Die Frist läuft immer bis zum Ende des Jahres, in dem
das Dokument datiert ist, plus N Jahre (Verjährung beginnt zum Jahresende, § 199 BGB):

- Rechnungen, Quittungen, Kassenbons: 3 Jahre – Gewährleistung 2 Jahre, regelmäßige Verjährung 3 Jahre (§§ 195, 199 BGB)
- Handwerkerrechnungen: 5 Jahre – Gewährleistung für Arbeiten am Bauwerk (§ 634a BGB); die Aufbewahrungspflicht für
  Privatleute (§ 14b Abs. 1 S. 5 UStG, 2 Jahre) ist kürzer, zählt hier aber nicht – ohne Rechnung kein Mangelanspruch.
- Kontoauszüge: 3 Jahre – privat keine Pflicht, genug für Rückfragen und Verjährung
- Steuer (Bescheide, Erklärungen, Finanzamt): 10 Jahre – Festsetzungsfrist bis zu 10 Jahre (§ 169 AO)
- Nie vorschlagen: Gehalt/Lohn (Nachweis bis zur Rente), Abrechnungen, Verträge, Versicherungen, Zeugnisse, Urkunden,
  Gesundheit, Vollmachten, Ausweise, Immobilien, Kredite, Notfallmappe …

Zugeordnet wird über die Namen der Ordner/Tags eines Dokuments samt Oberordnern (ohne Groß-/Kleinschreibung).
Wörter für „nie“ greifen großzügig (irgendwo im Namen), Wörter mit Frist nur eng (Name oder Wortende,
„Stromrechnung“, aber nicht „Gehaltsabrechnung“). Pro Name gilt die genaueste Vorgabe, pro Dokument: „nie“ schlägt
alles, sonst die längste Frist. Ohne passende Vorgabe wird nichts vorgeschlagen. Ein Admin kann pro Ordner eine eigene
Frist oder „nie“ einstellen (Tabelle `retention`); die gilt vor den Vorgaben.
"""
import datetime as dt
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth, folders
from .auth import User, current_user, require_admin
from .db import connect

router = APIRouter()

# Nie vorschlagen – Teilwort irgendwo im (normalisierten) Namen genügt
NEVER_WORDS = [
    "gehalt", "lohn", "rente", "pension", "abrechnung", "vertrag", "vertraege", "versicherung", "police", "zeugnis",
    "urkunde", "gesundheit", "arzt", "aerzt", "befund", "kranken", "medizin", "impf", "notfall", "vollmacht",
    "verfuegung", "testament", "erbschaft", "nachlass", "ausweis", "reisepass", "grundbuch", "immobilie", "darlehen",
    "kredit", "schule", "studium", "ausbildung", "geburt", "heirat", "scheidung", "behinderung", "pflege",
    # endet auf „rechnung“, ist aber keine Rechnung: „Steuerberechnung“ darf nicht nach 3 Jahren weg
    "berechnung", "verrechnung", "unterhalt",
    # Englische Ordner (Erkennung auf Englisch, 25.11.2026)
    "salary", "payslip", "payroll", "contract", "insurance", "certificate", "health", "medical", "doctor", "passport",
    "mortgage", "loan", "school",
]

# key, Bezeichnung, Jahre nach Jahresende, Wörter (Name oder Wortende), Begründung
DEFAULTS = [
    ("receipts", "Rechnungen & Quittungen", 3,
     ["rechnung", "quittung", "kassenbon", "kassenzettel", "beleg", "kaufbeleg", "receipt", "invoice"],
     "Gewährleistung 2 Jahre, Verjährung 3 Jahre ab Jahresende (§§ 195, 199 BGB)"),
    ("craftsmen", "Handwerkerrechnungen", 5,
     ["handwerker", "handwerkerrechnung", "handwerkerleistung", "tradesman", "tradesmen", "handyman"],
     "Gewährleistung für Arbeiten am Haus 5 Jahre (§ 634a BGB) – Aufbewahrungspflicht 2 Jahre (§ 14b UStG)"),
    ("statements", "Kontoauszüge", 3,
     ["kontoauszug", "kontoauszuege", "bankstatement", "accountstatement"],
     "Privat keine Pflicht – 3 Jahre reichen für Rückfragen und die Verjährung"),
    ("tax", "Steuer", 10,
     ["steuer", "steuerbescheid", "steuererklaerung", "finanzamt", "tax", "taxreturn", "taxassessment"],
     "Das Finanzamt kann bis zu 10 Jahre nachfordern (§ 169 AO)"),
]
NEVER_LABEL = "Nie vorschlagen"
NEVER_BASIS = "Gehalt bis zur Rente, Verträge, Versicherungen, Zeugnisse, Urkunden, Gesundheit, Vollmachten, Notfallmappe …"
_PLURAL = ("", "e", "en", "n", "s", "es", "er")
OWN_BASIS = "Eigene Einstellung für diesen Ordner"

# Englische Oberfläche (27.11.2026): Gruppen und Begründungen kamen bisher immer deutsch
EN = {
    "Rechnungen & Quittungen": "Invoices & receipts",
    "Gewährleistung 2 Jahre, Verjährung 3 Jahre ab Jahresende (§§ 195, 199 BGB)":
        "2-year statutory warranty, claims expire 3 years after year end (§§ 195, 199 BGB)",
    "Handwerkerrechnungen": "Tradesman invoices",
    "Gewährleistung für Arbeiten am Haus 5 Jahre (§ 634a BGB) – Aufbewahrungspflicht 2 Jahre (§ 14b UStG)":
        "5-year warranty for work on the house (§ 634a BGB) – must be kept 2 years (§ 14b UStG)",
    "Kontoauszüge": "Bank statements",
    "Privat keine Pflicht – 3 Jahre reichen für Rückfragen und die Verjährung":
        "No legal duty for private persons – 3 years cover queries and the limitation period",
    "Steuer": "Tax",
    "Das Finanzamt kann bis zu 10 Jahre nachfordern (§ 169 AO)": "The tax office can claim back up to 10 years (§ 169 AO)",
    "Nie vorschlagen": "Never suggest",
    "Gehalt bis zur Rente, Verträge, Versicherungen, Zeugnisse, Urkunden, Gesundheit, Vollmachten, Notfallmappe …":
        "Pay slips until retirement, contracts, insurance, references, certificates, health, powers of attorney, emergency folder …",
    OWN_BASIS: "Your own setting for this folder",
}


def _t(s, en: bool):
    return EN.get(s, s) if en and isinstance(s, str) else s


def _norm(name: str) -> str:
    s = (name or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", s)


def default_rule(name: str) -> Optional[tuple]:
    """Vorgabe für einen Ordnernamen: ("never",) | (key, label, years, basis) | None."""
    n = _norm(name)
    if not n:
        return None
    if any(w in n for w in NEVER_WORDS):
        return ("never",)
    best, best_len = None, 0
    for key, label, years, words, basis in DEFAULTS:
        for w in words:
            if len(w) > best_len and any(n.endswith(w + p) for p in _PLURAL):
                best, best_len = (key, label, years, basis), len(w)
    return best


def _ancestors(tree: dict, tag_id: int) -> list[int]:
    out, seen = [], set()
    while tag_id is not None and tag_id in tree and tag_id not in seen:
        seen.add(tag_id); out.append(tag_id); tag_id = tree[tag_id][1]
    return out


def _rule_for_tag(tree: dict, custom: dict, tag_id: int, never_ids=frozenset()):
    """Regel für einen Tag: besondere Ordner (Notfallmappe, auch umbenannt) nie, sonst eigene Einstellung vor der
    Vorgabe nach Namen."""
    if tag_id in never_ids:
        return ("never",)
    if tag_id in custom:
        years = custom[tag_id]
        if years is None:
            return ("never",)
        return (f"tag:{tag_id}", tree[tag_id][0], years, OWN_BASIS)
    r = default_rule(tree[tag_id][0])
    return r if r is None or r[0] == "never" else (f"default:{r[0]}", *r[1:])


def decide(tree: dict, custom: dict, tag_ids: list[int], never_ids=frozenset()):
    """Für ein Dokument: None (nichts vorschlagen, auch bei „nie“) oder (key, label, years, basis) mit der längsten Frist."""
    best = None
    for tid in {a for t in tag_ids for a in _ancestors(tree, t)}:
        r = _rule_for_tag(tree, custom, tid, never_ids)
        if r is None:
            continue
        if r[0] == "never":
            return None
        if best is None or r[2] > best[2] or (r[2] == best[2] and r[0] < best[0]):
            best = r
    return best


TAX_RULE = next((f"default:{k}", label, years, basis) for k, label, years, _, basis in DEFAULTS if k == "tax")


def money_rule(d, rule):
    """Geld-Felder gehen vor den Ordnern: Ein Vertrag mit Rhythmus, ein Arbeitsvertrag oder eine Gehaltsabrechnung wird
    nie vorgeschlagen, auch wenn er nur unter „Rechnungen“ liegt (laufende Zahlung, Nachweis); ein Beleg mit § 35a-Anteil
    stand in einer Steuererklärung und bleibt mindestens so lange wie Steuerunterlagen."""
    if rule is None:
        return None
    if d["recurring"] or (d["doc_kind"] or "") in ("vertrag", "police", "gehalt", "urkunde", "zeugnis", "ausweis"):
        return None
    if (d["tax_35a"] or 0) > 0 and rule[2] < TAX_RULE[2]:
        return TAX_RULE
    return rule


def _editable(con, user: User) -> list[int]:
    return [user.id, *[p for p, edit in auth.partner_ids(con, user.id).items() if edit]]


@router.get("/cleanup")
def suggestions(lang: str = "de", user: User = Depends(current_user)):
    """Dokumente, deren Aufbewahrungsfrist abgelaufen ist – nach Grund gruppiert. Nur Dokumente, die ich bearbeiten
    darf; nicht im Papierkorb, nicht ungeprüft aus dem Briefkasten, ohne offene Frist."""
    from .main import serialize  # späte Bindung gegen Zirkel
    if user.id == 0:
        return {"groups": [], "count": 0}
    today = dt.date.today()
    with connect() as con:
        owners = _editable(con, user)
        tree = folders.tag_tree(con)
        custom = {r["tag_id"]: r["years"] for r in con.execute("SELECT tag_id, years FROM retention")}
        never_ids = {r[0] for r in con.execute("SELECT id FROM tags WHERE kind='emergency'")}
        tags_of: dict[int, list[int]] = {}
        marks = ",".join("?" * len(owners))
        rows = con.execute(
            f"SELECT d.* FROM documents d WHERE d.owner_id IN ({marks}) AND d.deleted_at IS NULL AND d.ext_pending=0 "
            "AND NOT (d.due_date IS NOT NULL AND COALESCE(d.due_done, 0)=0) "
            "AND COALESCE(d.date_check, 0)=0 "   # zweifelhaftes Datum: erst prüfen, dann über Löschen reden
            "AND NOT COALESCE(d.warranty_until >= date('now', 'localtime'), 0) "   # Kaufbeleg mit laufender Garantie
            "ORDER BY COALESCE(d.document_date, d.created_at), d.id", owners).fetchall()
        if not rows:
            return {"groups": [], "count": 0}
        for r in con.execute(
                f"SELECT dt.document_id, dt.tag_id FROM document_tags dt JOIN documents d ON d.id=dt.document_id "
                f"WHERE d.owner_id IN ({marks}) AND d.deleted_at IS NULL", owners):
            tags_of.setdefault(r[0], []).append(r[1])
        groups: dict[str, dict] = {}
        for d in rows:
            rule = money_rule(d, decide(tree, custom, tags_of.get(d["id"], []), never_ids))
            date = (d["document_date"] or d["created_at"] or "")[:10]
            if not rule or not date[:4].isdigit():
                continue
            until = dt.date(int(date[:4]) + rule[2], 12, 31)
            if today <= until:
                continue
            en = lang.lower().startswith("en")
            g = groups.setdefault(rule[0], {"key": rule[0], "label": rule[1] if rule[0].startswith("tag:") else _t(rule[1], en),
                                            "years": rule[2], "basis": _t(rule[3], en),
                                            "custom": rule[0].startswith("tag:"), "documents": []})
            doc = serialize(con, d, user)
            doc["keep_until"] = until.isoformat()
            g["documents"].append(doc)
    out = sorted(groups.values(), key=lambda g: (-len(g["documents"]), g["label"].lower()))
    return {"groups": out, "count": sum(len(g["documents"]) for g in out)}


@router.get("/cleanup/rules")
def list_rules(lang: str = "de", user: User = Depends(current_user)):
    """Vorgaben (zum Anzeigen) und eigene Einstellungen pro Ordner (nur sichtbare Ordner)."""
    with connect() as con:
        hidden = folders.hidden_tag_ids(con, auth.visible_owner_ids(con, user, "all")) if user.id else set()
        custom = [{"tag_id": r["tag_id"], "tag": r["name"], "years": r["years"]} for r in con.execute(
            "SELECT r.tag_id, r.years, t.name FROM retention r JOIN tags t ON t.id=r.tag_id ORDER BY t.name COLLATE NOCASE")
            if r["tag_id"] not in hidden]
    en = lang.lower().startswith("en")
    defaults = [{"key": k, "label": _t(label, en), "years": y, "basis": _t(basis, en), "words": words}
                for k, label, y, words, basis in DEFAULTS]
    defaults.append({"key": "never", "label": _t(NEVER_LABEL, en), "years": None, "basis": _t(NEVER_BASIS, en), "words": NEVER_WORDS})
    return {"defaults": defaults, "custom": custom}


class RetentionIn(BaseModel):
    tag: str
    years: Optional[int] = None   # None = nie vorschlagen


@router.put("/cleanup/rules")
def set_rule(body: RetentionIn, admin: User = Depends(require_admin)):
    if body.years is not None and not (1 <= body.years <= 100):
        raise HTTPException(400, "Frist zwischen 1 und 100 Jahren – oder leer für „nie vorschlagen“")
    with connect() as con:
        t = con.execute("SELECT id, name FROM tags WHERE name=?", (body.tag.strip(),)).fetchone()
        hidden = folders.hidden_tag_ids(con, auth.visible_owner_ids(con, admin, "all")) if admin.id else set()
        if not t or t["id"] in hidden:
            raise HTTPException(404, "Ordner nicht gefunden")
        con.execute("INSERT INTO retention(tag_id, years) VALUES (?,?) ON CONFLICT(tag_id) DO UPDATE SET years=excluded.years",
                    (t["id"], body.years))
        return {"tag_id": t["id"], "tag": t["name"], "years": body.years}


@router.delete("/cleanup/rules/{tag_id}", status_code=204)
def reset_rule(tag_id: int, admin: User = Depends(require_admin)):
    """Eigene Einstellung entfernen – dann gilt wieder die Vorgabe."""
    with connect() as con:
        con.execute("DELETE FROM retention WHERE tag_id=?", (tag_id,))
