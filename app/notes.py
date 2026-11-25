"""Notizen am Dokument (25.11.2026): kurze Vermerke wie „Mit Versicherung telefoniert, Rückruf abwarten“.

Sehen und schreiben darf, wer das Dokument sieht (auch mit nur-lesen-Freigabe – eine Notiz ändert das Dokument nicht).
Löschen: die eigene Notiz, oder wer das Dokument bearbeiten darf. Den Namen des Verfassers sieht nur, wer ihn ohnehin
kennt (wie bei „Wer kümmert sich?“). Nie in Freigabe-Links, Exporten oder der Volltextsuche; in der öffentlichen Demo
gesperrt (sonst sähen alle Besucher, was einer schreibt).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import auth
from . import due as duemod
from .auth import User, current_user
from .db import connect

router = APIRouter()


class NoteIn(BaseModel):
    text: str = Field(..., max_length=2000)


def _doc(con, user: User, doc_id: int):
    row = con.execute("SELECT id, owner_id FROM documents WHERE id=? AND deleted_at IS NULL", (doc_id,)).fetchone()
    if not row or user.id == 0 or not auth.can_view(con, user, row["owner_id"]):
        raise HTTPException(404, "Dokument nicht gefunden")
    return row


def _out(con, user: User, r, known) -> dict:
    know = r["user_id"] == user.id or known is None or r["user_id"] in known
    return {"id": r["id"], "text": r["text"], "created_at": r["created_at"], "mine": r["user_id"] == user.id,
            "author": r["display_name"] if know else None}


@router.get("/documents/{doc_id}/notes")
def list_notes(doc_id: int, user: User = Depends(current_user)):
    with connect() as con:
        _doc(con, user, doc_id)
        known = duemod.known_user_ids(con, user)
        rows = con.execute("SELECT n.id, n.text, n.created_at, n.user_id, u.display_name FROM document_notes n "
                           "LEFT JOIN users u ON u.id=n.user_id WHERE n.document_id=? ORDER BY n.created_at, n.id",
                           (doc_id,)).fetchall()
        return [_out(con, user, r, known) for r in rows]


@router.post("/documents/{doc_id}/notes", status_code=201)
def add_note(doc_id: int, body: NoteIn, user: User = Depends(current_user)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Die Notiz ist leer")
    with connect() as con:
        _doc(con, user, doc_id)
        cur = con.execute("INSERT INTO document_notes(document_id, user_id, text) VALUES (?, ?, ?)", (doc_id, user.id, text))
        r = con.execute("SELECT n.id, n.text, n.created_at, n.user_id, u.display_name FROM document_notes n "
                        "LEFT JOIN users u ON u.id=n.user_id WHERE n.id=?", (cur.lastrowid,)).fetchone()
        return _out(con, user, r, None)


@router.delete("/documents/{doc_id}/notes/{note_id}", status_code=204)
def delete_note(doc_id: int, note_id: int, user: User = Depends(current_user)):
    with connect() as con:
        doc = _doc(con, user, doc_id)
        n = con.execute("SELECT user_id FROM document_notes WHERE id=? AND document_id=?", (note_id, doc_id)).fetchone()
        if not n:
            raise HTTPException(404, "Notiz nicht gefunden")
        if n["user_id"] != user.id and not auth.can_edit(con, user, doc["owner_id"]):
            raise HTTPException(403, "Nur eigene Notizen – oder mit Bearbeitungsrecht am Dokument")
        con.execute("DELETE FROM document_notes WHERE id=?", (note_id,))
