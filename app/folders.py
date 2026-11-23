"""Ordner: Tags können in anderen Tags liegen (tags.parent_id).

Die Dokumente bleiben flach getaggt – „Strom“ liegt im Ordner „Haus“, weil der Tag „Strom“ dort
einsortiert ist, nicht weil das Dokument „Haus/Strom“ heißt. Umsortieren ist damit eine einzige
Änderung am Tag, und die KI muss weiterhin nur einfache Tags vorschlagen.
"""
import re


def hidden_tag_ids(con, visible_owners: list[int]) -> set[int]:
    """Tags, deren Namen ein Nutzer nicht erfahren soll („Psychotherapie“ eines anderen): sie hängen nur an Dokumenten
    anderer und wurden nicht von ihm (oder einem Partner) angelegt. Oberordner eines sichtbaren Tags bleiben sichtbar.
    Personen (Familie) sind auch ohne Dokument privat: ein Personen-Tag anderer bleibt verborgen, solange keiner der
    sichtbaren Nutzer die Person angelegt hat und kein sichtbares Dokument sie trägt.
    `visible_owners` = alle Bestände, die der Nutzer sehen darf (auth.visible_owner_ids(..., "all"))."""
    marks = ",".join("?" * len(visible_owners)) or "NULL"
    foreign = {r[0] for r in con.execute(
        "SELECT t.id FROM tags t WHERE (EXISTS (SELECT 1 FROM document_tags dt WHERE dt.tag_id=t.id) "
        "OR EXISTS (SELECT 1 FROM persons p WHERE p.tag_id=t.id)) "
        f"AND (t.created_by IS NULL OR t.created_by NOT IN ({marks})) "
        f"AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.tag_id=t.id AND p.user_id IN ({marks})) "
        "AND NOT EXISTS (SELECT 1 FROM document_tags dt JOIN documents d ON d.id=dt.document_id "
        f"WHERE dt.tag_id=t.id AND d.owner_id IN ({marks}))", visible_owners * 3)}
    if not foreign:
        return set()
    tree = tag_tree(con)
    keep: set[int] = set()
    for tid in tree:
        if tid in foreign:
            continue
        p, seen = tree[tid][1], 0
        while p and p in tree and seen < 50:
            keep.add(p); p = tree[p][1]; seen += 1
    return foreign - keep


def tag_tree(con) -> dict[int, tuple[str, int | None]]:
    """id -> (name, parent_id)"""
    return {r["id"]: (r["name"], r["parent_id"]) for r in con.execute("SELECT id, name, parent_id FROM tags")}


def path_of(tree: dict[int, tuple[str, int | None]], tag_id: int) -> list[str]:
    """Namen von der Wurzel bis zum Tag; bricht Kreise sicherheitshalber ab."""
    out, seen = [], set()
    while tag_id is not None and tag_id in tree and tag_id not in seen:
        seen.add(tag_id)
        name, parent = tree[tag_id]
        out.append(name)
        tag_id = parent
    return out[::-1]


def would_cycle(con, tag_id: int, new_parent: int | None) -> bool:
    """True, wenn new_parent der Tag selbst oder einer seiner Unterordner ist."""
    tree = tag_tree(con)
    p, seen = new_parent, set()
    while p is not None and p not in seen:
        if p == tag_id:
            return True
        seen.add(p)
        p = tree.get(p, (None, None))[1]
    return False


def ensure_path(con, parts: list[str]) -> str | None:
    """Für einen Scanner-Unterordner wie Haus/Strom: Tags anlegen, wo sie fehlen, und neue Tags im
    darüberliegenden einsortieren. Vorhandene Tags behalten ihren Ordner. Gibt den letzten Tag zurück."""
    parent_id, name = None, None
    for raw in parts:
        name = re.sub(r"\s+", " ", raw).strip()
        if not name:
            continue
        row = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            parent_id = row["id"]
        else:
            parent_id = con.execute("INSERT INTO tags(name, parent_id) VALUES (?, ?)", (name, parent_id)).lastrowid
    return name


def _safe(s: str) -> str:
    out = "".join(c if c.isalnum() or c in " -_.,()&" else "_" for c in (s or "")).strip()[:80]
    return "_" if not out.strip(". ") else out  # „..“ oder „.“ wären im ZIP ein Weg nach oben


def export_path(tree, doc_tag_ids: list[int], date: str | None, title: str | None, doc_id: int) -> str:
    """Pfad im Ordner-Export: tiefster Tag (bei Gleichstand alphabetisch) / Jahr / Datum Titel.pdf."""
    best = None
    for tid in doc_tag_ids:
        p = path_of(tree, tid)
        if p and (best is None or (len(p), [x.lower() for x in best]) > (len(best), [x.lower() for x in p])):
            best = p
    folders = [_safe(x) for x in (best or ["Ohne Ordner"])]
    year = (date or "")[:4] if (date or "")[:4].isdigit() else "ohne Datum"
    name = " ".join(filter(None, [(date or "")[:10], _safe(title or f"Dokument {doc_id}")]))
    return "/".join([*folders, year, f"{name}.pdf"])
