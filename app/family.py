"""Familie: Personen ohne eigenes Konto (Kinder) und die Notfallmappe.

Personen: Eine Person ist ein Ordner (Tag) plus eine Regel auf ihren Vornamen – „Mia“ bekommt jedes Dokument, in dem
„Mia“ als ganzes Wort steht. Anlegen darf jeder Nutzer (nicht nur Admins), denn die Regel wirkt nur auf Dokumente, die
der Anlegende bearbeiten darf (eigener Bestand, Partner mit Schreibrecht) – beim Anlegen und bei jedem neuen Dokument
(processing.rule_allowed). Wer eine Person angelegt hat, steht in `persons`; den Namen sehen nur Nutzer, die den
Bestand des Anlegenden sehen dürfen (folders.hidden_tag_ids). Tag-Namen sind global eindeutig: legen zwei Nutzer ohne
Freigabe dieselbe Person an, teilen sie sich den Tag, ohne voneinander zu erfahren. Entfernen nimmt nur die eigene
Markierung samt Regel weg; Ordner und Dokumente bleiben.

Notfallmappe: ein besonderer Ordner (tags.kind = 'emergency') für das, was Angehörige im Ernstfall brauchen
(Vollmachten, Patientenverfügung, Versicherungen, wichtige Kontakte). Wird beim ersten Dokument angelegt; teilen geht
über einen normalen Freigabe-Link auf den Ordner – der enthält wie immer nur eigene Dokumente des Erstellers.
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import auth, folders, processing, share
from .auth import User, current_user
from .db import connect

router = APIRouter()

EMERGENCY_NAME = "Notfallmappe"


def _editable(con, user: User) -> list[int]:
    """Eigener Bestand plus Partner mit Schreibrecht."""
    return [user.id, *[p for p, edit in auth.partner_ids(con, user.id).items() if edit]]


def _marks(ids: list) -> str:
    return ",".join("?" * len(ids)) or "NULL"


# ---------- Personen ----------

class PersonIn(BaseModel):
    name: str


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw or "").strip()
    if not (2 <= len(name) <= 40) or not re.search(r"[^\W\d_]", name) or re.search(r"[,/\\#]", name):
        raise HTTPException(400, "Bitte einen Vornamen angeben (2 bis 40 Zeichen, ohne Komma oder Schrägstrich)")
    return name


def _person_out(con, user: User, tag_id: int) -> dict:
    visible = auth.visible_owner_ids(con, user, "all")
    editable = _editable(con, user)
    t = con.execute("SELECT id, name, color FROM tags WHERE id=?", (tag_id,)).fetchone()
    count = con.execute(
        "SELECT COUNT(*) FROM document_tags dt JOIN documents d ON d.id=dt.document_id "
        f"WHERE dt.tag_id=? AND d.deleted_at IS NULL AND d.owner_id IN ({_marks(visible)})", (tag_id, *visible)).fetchone()[0]
    auto = con.execute(f"SELECT 1 FROM rules WHERE tag_id=? AND word=1 AND created_by IN ({_marks(visible)})",
                       (tag_id, *visible)).fetchone() is not None
    mine = con.execute(f"SELECT 1 FROM persons WHERE tag_id=? AND user_id IN ({_marks(editable)})",
                       (tag_id, *editable)).fetchone() is not None
    from . import avatars
    return {"id": t["id"], "name": t["name"], "color": t["color"], "count": count, "auto": auto, "can_remove": mine,
            "avatar": avatars.version("p", tag_id)}


@router.get("/persons")
def list_persons(user: User = Depends(current_user)):
    """Personen, die ich oder jemand, dessen Bestand ich sehen darf, angelegt hat."""
    if user.id == 0:
        return []
    with connect() as con:
        visible = auth.visible_owner_ids(con, user, "all")
        ids = [r[0] for r in con.execute(
            f"SELECT DISTINCT p.tag_id FROM persons p JOIN tags t ON t.id=p.tag_id WHERE p.user_id IN ({_marks(visible)}) "
            "ORDER BY t.name COLLATE NOCASE", visible)]
        return [_person_out(con, user, i) for i in ids]


def _own_or_unused(con, tag, visible: list[int], me: int) -> bool:
    """Ordner angelegt von mir/einem sichtbaren Bestand, an einem sichtbaren Dokument oder noch ganz unbenutzt."""
    if tag["created_by"] in visible:
        return True
    marks = _marks(visible)
    if con.execute(f"SELECT 1 FROM document_tags dt JOIN documents d ON d.id=dt.document_id "
                   f"WHERE dt.tag_id=? AND d.owner_id IN ({marks}) LIMIT 1", (tag["id"], *visible)).fetchone():
        return True
    used = con.execute("SELECT 1 FROM document_tags WHERE tag_id=? LIMIT 1", (tag["id"],)).fetchone() or \
        con.execute("SELECT 1 FROM persons WHERE tag_id=? AND user_id<>? LIMIT 1", (tag["id"], me)).fetchone()
    return not used


@router.post("/persons", status_code=201)
def create_person(body: PersonIn, user: User = Depends(current_user)):
    """Person in einem Schritt: Ordner (falls neu), Markierung, Regel auf den Vornamen (ganzes Wort, ab 3 Zeichen) und
    gleich auf vorhandene Dokumente anwenden – nur auf solche, die ich bearbeiten darf."""
    if user.id == 0:
        raise HTTPException(400, "Erst einen Nutzer anlegen")
    name = _clean_name(body.name)
    with connect() as con:
        t = con.execute("SELECT * FROM tags WHERE name=?", (name,)).fetchone()
        if t and t["kind"] == "emergency":
            raise HTTPException(400, "Dieser Name ist für die Notfallmappe reserviert")
        if t:
            tag_id = t["id"]
            # Nur einen Ordner, der mir (oder einem Bestand, den ich sehe) gehört, zur Person machen – fremde,
            # verborgene Ordner bleiben, wie sie sind. Bewusst ohne meine eigene Personen-Markierung zu zählen,
            # sonst reichte ein zweiter Aufruf (hidden_tag_ids sieht die Markierung als „sichtbar“ an).
            if _own_or_unused(con, t, auth.visible_owner_ids(con, user, "all"), user.id):
                con.execute("UPDATE tags SET kind='person' WHERE id=? AND kind IS NULL", (tag_id,))
        else:
            tag_id = con.execute("INSERT INTO tags(name, created_by, kind) VALUES (?,?, 'person')", (name, user.id)).lastrowid
        con.execute("INSERT OR IGNORE INTO persons(tag_id, user_id) VALUES (?,?)", (tag_id, user.id))
        pattern = con.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()["name"]
        tagged = 0
        if len(pattern) >= 3:
            if not con.execute("SELECT 1 FROM rules WHERE tag_id=? AND created_by=? AND word=1", (tag_id, user.id)).fetchone():
                con.execute("INSERT INTO rules(pattern, tag_id, created_by, word) VALUES (?,?,?,1)", (pattern, tag_id, user.id))
            editable = _editable(con, user)
            for d in con.execute(
                    f"SELECT id, text FROM documents WHERE deleted_at IS NULL AND text IS NOT NULL AND owner_id IN ({_marks(editable)})",
                    editable).fetchall():
                if processing.rule_hit(pattern, 1, d["text"].lower()):
                    tagged += con.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) VALUES (?,?,'rule')",
                                          (d["id"], tag_id)).rowcount
        out = _person_out(con, user, tag_id)
        out["tagged"] = tagged
        return out


@router.delete("/persons/{tag_id}", status_code=204)
def remove_person(tag_id: int, user: User = Depends(current_user)):
    """Markierung und Namensregel entfernen (eigene bzw. von Partnern mit Schreibrecht). Ordner und Dokumente bleiben."""
    with connect() as con:
        visible = auth.visible_owner_ids(con, user, "all") if user.id else []
        editable = _editable(con, user) if user.id else []
        owners = [r[0] for r in con.execute("SELECT user_id FROM persons WHERE tag_id=?", (tag_id,))]
        mine = [u for u in owners if u in editable]
        if not mine:
            if any(u in visible for u in owners):
                raise HTTPException(403, "Diese Person hat jemand angelegt, dessen Dokumente du nur lesen darfst")
            raise HTTPException(404, "Person nicht gefunden")
        con.execute(f"DELETE FROM persons WHERE tag_id=? AND user_id IN ({_marks(mine)})", (tag_id, *mine))
        con.execute(f"DELETE FROM rules WHERE tag_id=? AND word=1 AND created_by IN ({_marks(mine)})", (tag_id, *mine))
        if not con.execute("SELECT 1 FROM persons WHERE tag_id=?", (tag_id,)).fetchone():
            con.execute("UPDATE tags SET kind=NULL WHERE id=? AND kind='person'", (tag_id,))
            from . import avatars
            avatars.remove("p", tag_id)        # niemand führt die Person mehr – das Bild geht mit


# ---------- Notfallmappe ----------

def emergency_tag(con, create: bool = False, user: Optional[User] = None) -> Optional[int]:
    """Der Notfallmappen-Ordner: markiert, sonst ein vorhandener Ordner „Notfallmappe“ (wird markiert), sonst neu."""
    r = con.execute("SELECT id FROM tags WHERE kind='emergency' ORDER BY id LIMIT 1").fetchone()
    if r:
        return r["id"]
    r = con.execute("SELECT id, kind FROM tags WHERE name=?", (EMERGENCY_NAME,)).fetchone()
    if r and r["kind"] is None:
        con.execute("UPDATE tags SET kind='emergency' WHERE id=?", (r["id"],))
        return r["id"]
    if r or not create:
        return r["id"] if r else None
    return con.execute("INSERT INTO tags(name, created_by, kind) VALUES (?,?, 'emergency')",
                       (EMERGENCY_NAME, (user.id or None) if user else None)).lastrowid


@router.get("/emergency")
def get_emergency(request: Request, user: User = Depends(current_user)):
    """Inhalt der Notfallmappe (sichtbare Dokumente), wie viele davon eigene sind (nur die kommen in einen Link)
    und meine bestehenden Links darauf."""
    from .main import serialize  # späte Bindung gegen Zirkel
    with connect() as con:
        tag_id = emergency_tag(con)
        out = {"tag_id": tag_id, "name": EMERGENCY_NAME, "documents": [], "own_count": 0, "links": []}
        if tag_id is None or user.id == 0:
            return out
        visible = auth.visible_owner_ids(con, user, "all")
        if tag_id not in folders.hidden_tag_ids(con, visible):   # umbenannter Name nur für die, die den Ordner sehen
            out["name"] = con.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()["name"]
        rows = con.execute(
            "SELECT d.* FROM documents d JOIN document_tags dt ON dt.document_id=d.id AND dt.tag_id=? "
            f"WHERE d.deleted_at IS NULL AND d.owner_id IN ({_marks(visible)}) "
            "ORDER BY COALESCE(d.document_date, d.created_at) DESC, d.id DESC", (tag_id, *visible)).fetchall()
        out["documents"] = [serialize(con, r, user) for r in rows]
        out["own_count"] = sum(1 for r in rows if r["owner_id"] == user.id and not r["ext_pending"])
        out["links"] = [share._out(con, request, r) for r in con.execute(
            "SELECT * FROM share_links WHERE user_id=? AND kind='tag' AND target=? ORDER BY id DESC", (user.id, str(tag_id)))]
        return out


class EmergencyDocIn(BaseModel):
    document_id: int


@router.post("/emergency/documents")
def add_to_emergency(body: EmergencyDocIn, user: User = Depends(current_user)):
    """Dokument in die Notfallmappe legen (legt sie beim ersten Mal an). Nur Dokumente, die ich bearbeiten darf."""
    from .main import get_doc, serialize
    with connect() as con:
        get_doc(con, body.document_id, user, edit=True)
        tag_id = emergency_tag(con, create=True, user=user)
        con.execute("INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) VALUES (?,?,'user')",
                    (body.document_id, tag_id))
        return {"tag_id": tag_id, "document": serialize(con, get_doc(con, body.document_id, user), user)}


@router.delete("/emergency/documents/{doc_id}", status_code=204)
def remove_from_emergency(doc_id: int, user: User = Depends(current_user)):
    """Aus der Notfallmappe nehmen – das Dokument selbst bleibt."""
    from .main import get_doc
    with connect() as con:
        get_doc(con, doc_id, user, edit=True)
        tag_id = emergency_tag(con)
        if tag_id is not None:
            con.execute("DELETE FROM document_tags WHERE document_id=? AND tag_id=?", (doc_id, tag_id))


def person_names(con, user: User) -> set[str]:
    """Namen der Personen, die der Nutzer kennen darf (für die Satz-Suche)."""
    if user.id == 0:
        return set()
    visible = auth.visible_owner_ids(con, user, "all")
    return {r[0] for r in con.execute(
        f"SELECT DISTINCT t.name FROM persons p JOIN tags t ON t.id=p.tag_id WHERE p.user_id IN ({_marks(visible)})", visible)}

