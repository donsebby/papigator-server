"""Prüft eine mit load.py gefüllte Instanz: Erkennung gegen die Wahrheit aus make_docs.py, dann Absender, Verträge,
Fristen, Dubletten, Steuer-Paket und Fragen an „Frag Papigator“.

    PAPIGATOR_URL=http://127.0.0.1:8765 python check.py /tmp/papigator-demo extract|money|ask ["Frage" …]
"""
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx

H = Path(sys.argv[1] if len(sys.argv) > 1 else "papigator-demo")
T = json.load(open(H / "tokens.json"))
c = httpx.Client(base_url=os.environ.get("PAPIGATOR_URL", "http://127.0.0.1:8765"), timeout=600)
A = T["alex"]
docs = json.load(open(H / "docs.json"))
part = sys.argv[2] if len(sys.argv) > 2 else "extract"


def get(url, **p):
    r = c.get(url, params=p, headers=A)
    r.raise_for_status()
    return r.json()


if part == "extract":
    by_id = {d["id"]: d for d in get("/documents", scope="all", limit=500)}
    miss = {"amount": 0, "date": 0, "due": 0}
    for x in docs:
        d = by_id.get(x["id"])
        if not d:
            print("FEHLT", x["file"]); continue
        t = x["truth"]
        notes = []
        if t.get("amount") is not None and (d["amount"] is None or abs(d["amount"] - t["amount"]) > 0.01):
            notes.append(f"Betrag {d['amount']} statt {t['amount']}"); miss["amount"] += 1
        if t.get("amount") is None and d["amount"] is not None:
            notes.append(f"Betrag {d['amount']} (Dokument hat keinen)")
        if d["document_date"] != x["date"]:
            notes.append(f"Datum {d['document_date']} statt {x['date']}"); miss["date"] += 1
        if t.get("due") and d.get("due_date") != t["due"]:
            notes.append(f"Frist {d.get('due_date')} statt {t['due']}"); miss["due"] += 1
        if not t.get("due") and d.get("due_date"):
            notes.append(f"Frist {d.get('due_date')} ({d.get('due_note')}) erfunden?")
        if t.get("currency") and d.get("currency") != t["currency"]:
            notes.append(f"Währung {d.get('currency')}")
        want = {"Ausgabe": "out", "Einnahme": "in"}.get(str(t.get("direction", "")).split(" ")[0])
        if want and d.get("direction") != want:
            notes.append(f"Richtung {d.get('direction')} statt {want}")
        tags = ",".join(tg["name"] for tg in d["tags"])
        extra = f"{d.get('doc_kind') or '?'}/{d.get('direction') or '?'}{'/LS' if d.get('autopay') else ''}{'/' + d['recurring'] if d.get('recurring') else ''}"
        print(f"{x['id']:>3} {x['file'][:30]:<30} | {d['sender'] or '–':<26.26} | {extra:<26.26} | {tags:<26.26} | {'; '.join(notes)}")
    print("Abweichungen:", miss)

elif part == "money":
    print("== Absender (scope=all), Summe")
    for s in get("/senders", scope="all"):
        print(f"  {s['name']:<45.45} {s['count']:>3}  aus {s.get('spent', s['total']):>12,.2f}  ein {s.get('received', 0):>10,.2f}")
    print("== Verträge (scope=all)")
    for k in get("/contracts", scope="all"):
        cb = (k.get("cancel_by") or {}).get("date")
        print(f"  {k['sender']:<36.36} {k['interval']:<15} {k['amount']:>9.2f}  /Monat {k['per_month']:>8.2f}  {k.get('source', '?'):<8} n={k['count']} nächste {k['next_expected']} überfällig={k['overdue']} kündbar bis {cb}")
    print("== Fristen")
    print(" ", get("/documents/due_count", scope="all"))
    for d in get("/documents", scope="all", due=1, limit=50):
        print(f"  {d.get('due_date')}  {d.get('due_note') or '':<28.28} {d['title'][:50]}")
    print("== Dubletten")
    for p in get("/duplicates"):
        print("  ", json.dumps(p, ensure_ascii=False)[:220])
    print("== Steuer-Paket 2025 (Tag Steuer)")
    for tags in ["Steuer", ""]:
        r = c.get("/export/package.zip", params={"year": 2025, "tags": tags, "scope": "all"}, headers=A)
        if r.status_code != 200:
            print("  ", tags or "(alle)", r.status_code, r.text[:100]); continue
        z = zipfile.ZipFile(io.BytesIO(r.content))
        sums = json.loads(z.read("summen.json"))
        print(f"  Tags={tags or '(alle 2025)'}: {sums['documents']} Dokumente, Ausgaben {sums.get('spent', sums['total']):,.2f}, "
              f"Einnahmen {sums.get('received', 0):,.2f}, § 35a {sums.get('tax_35a', 0):,.2f}, sonst {sums.get('other_currencies')}")
        print("   je Tag:", dict(list(sums["by_tag"].items())[:8]))

elif part == "ask":
    qs = sys.argv[3:] or [
        "Wie viel habe ich 2025 für Strom bezahlt?",
        "Was kostet mich das Auto im Jahr?",
        "Wie hoch war die Steuererstattung für 2024?",
        "Welche Rechnungen muss ich in den nächsten zwei Wochen bezahlen?",
        "Was haben wir 2025 insgesamt für Versicherungen ausgegeben?",
        "Welche haushaltsnahen Dienstleistungen kann ich für 2025 in der Steuererklärung angeben?",
        "Wie viel verdiene ich netto im Monat?",
        "Bis wann kann ich die Kfz-Versicherung kündigen?",
    ]
    for q in qs:
        r = c.post("/ask", json={"question": q, "scope": "all"}, headers=A)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:300]}
        print("??", q)
        print("  ", (j.get("answer") or j.get("detail") or j).__str__().replace("\n", "\n   ")[:900])
        if j.get("sources") or j.get("documents"):
            print("   Quellen:", [s.get("title") for s in (j.get("sources") or j.get("documents"))][:6])
        print()
