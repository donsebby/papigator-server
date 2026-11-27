"""Doppelte Dokumente finden: dasselbe Papier zweimal gescannt, einmal per Mail und einmal per Scanner …

Exakt gleiche Dateien fängt schon der Upload per SHA-256 ab. Hier geht es um inhaltlich gleiche:
Text als Wort-Dreiergruppen (Shingles), Ähnlichkeit = Jaccard. Verglichen werden nur Paare, deren
Bottom-k-Skizzen (die 64 kleinsten Shingle-Hashes) sich mehrfach überschneiden – gefunden über einen
Index statt jedes mit jedem; 3000 Dokumente brauchen so ein paar Sekunden statt einer halben Minute.

Monatliche Abrechnungen (Gehalt, Konto) sehen sich sehr ähnlich, unterscheiden sich aber in Zahlen,
Datum und Betrag – Ziffern zählen deshalb als Wörter, und verschiedene Daten oder Beträge schließen ein
Paar aus, solange der Text nicht fast identisch ist.

Nur Dokumente, die der Nutzer bearbeiten darf (eigene und Partner mit Schreibrecht), denn auflösen heißt
eins in den Papierkorb legen.
"""
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth
from .auth import User, current_user
from .db import connect

router = APIRouter()

MAX_CHARS = 20000      # Text pro Dokument für den Vergleich
MAX_DOCS = 5000
MAX_PAIRS = 200
SKETCH = 64           # Bottom-k-Skizze
SKETCH_MIN = 0.4      # geschätzte Ähnlichkeit, ab der genau verglichen wird
SKETCH_SHARED = 24    # gemeinsame Skizzen-Hashes für einen Kandidaten (bei 60 % Gleichheit erwartet: ~48 von 64)
SKETCH_COMMON = 300   # Hashes in mehr Skizzen sind Floskeln („mit freundlichen Grüßen“) und sagen nichts
STRONG = 0.75          # reicht, wenn sich Datum und Betrag nicht widersprechen (eins darf fehlen)
NEAR_SAME = 0.95       # so ähnlich: auch bei verschiedenem Datum/Betrag (z. B. falsch erkanntes Datum)
WITH_META = 0.55       # reicht, wenn Datum gleich und Betrag gleich/fehlend (schlechter Scan, ~10 % OCR-Fehler)

_WORD = re.compile(r"[0-9a-zß]+")
_HYPHEN = re.compile(r"(\w)-[ \t]*\n\s*(\w)")


def _normalize(text: str) -> str:
    """OCR-Unterschiede zweier Scans glätten: Trennung am Zeilenende zusammenziehen („Solida-\nritätszuschlag“),
    Akzente/Umlaute weg (ü/u, í/i), „rn“ wie „m“ (typische Verwechslung)."""
    t = _HYPHEN.sub(r"\1\2", (text or "")[:MAX_CHARS])
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch)).replace("rn", "m")


def _shingles(text: str) -> set[int]:
    words = [w for w in _WORD.findall(_normalize(text)) if len(w) > 1 or w.isdigit()]
    if len(words) < 3:
        return {hash(" ".join(words))} if words else set()
    return {hash((words[i], words[i + 1], words[i + 2])) for i in range(len(words) - 2)}


def _estimate(ka: frozenset, kb: frozenset) -> float:
    """Jaccard-Schätzung aus zwei Bottom-k-Skizzen."""
    union = sorted(ka | kb)[:SKETCH]
    return sum(1 for x in union if x in ka and x in kb) / len(union)


def _same_amount(a, b) -> Optional[bool]:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b)) < 0.01


def _pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _brief(r) -> dict:
    return {"id": r["id"], "title": r["title"], "document_date": r["document_date"], "sender": r["sender"],
            "amount": r["amount"], "currency": r["currency"], "page_count": r["page_count"],
            "created_at": r["created_at"], "owner_id": r["owner_id"],
            "thumbnail_url": f"/documents/{r['id']}/thumbnail", "file_url": f"/documents/{r['id']}/file"}


def find_pairs(con, owners: list[int], lang: str = "de") -> list[dict]:
    if not owners:
        return []
    marks = ",".join("?" * len(owners))
    rows = con.execute(
        f"SELECT id, title, owner_id, document_date, sender, amount, currency, page_count, created_at, "
        f"substr(text, 1, {MAX_CHARS}) AS text FROM documents "
        f"WHERE deleted_at IS NULL AND status='done' AND ext_pending=0 AND owner_id IN ({marks}) "
        f"AND text IS NOT NULL AND length(text) > 80 ORDER BY id LIMIT {MAX_DOCS}", owners).fetchall()
    ignored = {(r[0], r[1]) for r in con.execute("SELECT doc_a, doc_b FROM duplicate_ignores")}
    docs = []
    for r in rows:
        sh = _shingles(r["text"])
        if len(sh) >= 5:
            docs.append((r, sh, frozenset(sorted(sh)[:SKETCH])))
    # Kandidaten: Paare, die sich mehrere Skizzen-Hashes teilen
    index = defaultdict(list)
    for n, (_, _, k) in enumerate(docs):
        for x in k:
            index[x].append(n)
    shared = Counter()
    for members in index.values():
        if 1 < len(members) <= SKETCH_COMMON:
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    shared[(members[i], members[j])] += 1
    pairs = []
    for (i, j), cnt in shared.items():
        if cnt < SKETCH_SHARED:
            continue
        ra, sa, ka = docs[i]
        rb, sb, kb = docs[j]
        if min(len(sa), len(sb)) < WITH_META * max(len(sa), len(sb)):
            continue  # Jaccard ≤ klein/groß
        if _estimate(ka, kb) < SKETCH_MIN:
            continue
        if _pair_key(ra["id"], rb["id"]) in ignored:
            continue
        score = len(sa & sb) / len(sa | sb)
        same_date = ra["document_date"] and ra["document_date"] == rb["document_date"]
        diff_date = ra["document_date"] and rb["document_date"] and not same_date
        amount = _same_amount(ra["amount"], rb["amount"])
        if score >= NEAR_SAME:
            pass
        elif diff_date or amount is False:
            continue  # z. B. zwei Monatsabrechnungen
        elif score >= STRONG:
            pass
        elif not (score >= WITH_META and same_date and amount is not False):
            continue
        en = lang.lower().startswith("en")   # Gründe auch englisch (27.11.2026)
        reasons = [f"Text {round(score * 100)} % " + ("alike" if en else "gleich")]
        if same_date:
            reasons.append("same date" if en else "gleiches Datum")
        if amount:
            reasons.append("same amount" if en else "gleicher Betrag")
        if ra["sender"] and (ra["sender"] or "").lower() == (rb["sender"] or "").lower():
            reasons.append("same sender" if en else "gleicher Absender")
        first, second = (ra, rb) if ra["id"] < rb["id"] else (rb, ra)  # links das ältere
        pairs.append({"score": round(score, 3), "reasons": reasons, "a": _brief(first), "b": _brief(second)})
    pairs.sort(key=lambda p: -p["score"])
    return pairs[:MAX_PAIRS]


def _editable(con, user: User) -> list[int]:
    return [user.id, *[p for p, edit in auth.partner_ids(con, user.id).items() if edit]]


@router.get("/duplicates")
def list_duplicates(lang: str = "de", user: User = Depends(current_user)):
    if user.id == 0:
        return []
    with connect() as con:
        pairs = find_pairs(con, _editable(con, user), lang)
        names = {r["id"]: r["display_name"] for r in con.execute("SELECT id, display_name FROM users")}
    for p in pairs:
        for side in ("a", "b"):
            p[side]["owner_name"] = names.get(p[side]["owner_id"])
            p[side]["is_own"] = p[side]["owner_id"] == user.id
    return pairs


class ResolveIn(BaseModel):
    keep: int
    drop: int


def _doc_for_edit(con, user: User, doc_id: int):
    r = con.execute("SELECT * FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,)).fetchone()
    if not r or not auth.can_edit(con, user, r["owner_id"]):
        raise HTTPException(404, "Dokument nicht gefunden")
    return r


@router.post("/duplicates/resolve")
def resolve(body: ResolveIn, user: User = Depends(current_user)):
    """Eins behalten, das andere in den Papierkorb. Tags und leere Felder übernimmt das behaltene."""
    if body.keep == body.drop:
        raise HTTPException(400, "Zwei verschiedene Dokumente angeben")
    with connect() as con:
        keep = _doc_for_edit(con, user, body.keep)
        drop = _doc_for_edit(con, user, body.drop)
        # Tags und Felder nur innerhalb eines Bestands übernehmen (27.11.2026) – sonst wanderten z. B. Personen-Tags
        # aus Bs Bestand an As Dokument, wo As Partner sie sehen
        same = keep["owner_id"] == drop["owner_id"]
        if same:
            con.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) "
                        "SELECT ?, tag_id, source FROM document_tags WHERE document_id=?", (keep["id"], drop["id"]))
        fill = {k: drop[k] for k in ("document_date", "sender", "amount", "currency", "summary", "due_date", "due_note")
                if same and keep[k] in (None, "") and drop[k] not in (None, "")}
        if fill:
            con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in fill)}, updated_at=datetime('now') WHERE id=?",
                        (*fill.values(), keep["id"]))
        con.execute("UPDATE documents SET deleted_at=datetime('now') WHERE id=?", (drop["id"],))
    return {"kept": body.keep, "trashed": body.drop, "filled": sorted(fill)}


class IgnoreIn(BaseModel):
    a: int
    b: int


@router.post("/duplicates/ignore", status_code=204)
def ignore(body: IgnoreIn, user: User = Depends(current_user)):
    """„Beide behalten“: das Paar taucht nicht mehr auf."""
    if body.a == body.b:
        raise HTTPException(400, "Zwei verschiedene Dokumente angeben")
    with connect() as con:
        _doc_for_edit(con, user, body.a)
        _doc_for_edit(con, user, body.b)
        con.execute("INSERT OR IGNORE INTO duplicate_ignores(doc_a, doc_b) VALUES (?,?)", _pair_key(body.a, body.b))
