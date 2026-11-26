"""Öffentliche Freigabe-Seiten /s/… zweisprachig: Deutsch bei deutschem Browser, sonst Englisch; ohne Angabe die
Sprache des Servers (Standard Deutsch, wie bisher)."""
from urllib.parse import urlparse

import pytest
from starlette.requests import Request

from app import share


def _path(link):
    return urlparse(link["url"]).path


def _req(value):
    headers = [(b"accept-language", value.encode())] if value is not None else []
    return Request({"type": "http", "headers": headers})


@pytest.mark.parametrize("header,lang", [
    ("de-DE,de;q=0.9,en;q=0.8", "de"), ("de", "de"), ("en-US,en;q=0.9", "en"), ("fr-FR,fr;q=0.9,de;q=0.8", "en"),
    ("en;q=0.5,de;q=0.9", "de"), ("de-AT", "de"), ("nl", "en"), (None, "de"), ("", "de"), ("*", "de"),
])
def test_page_lang(header, lang):
    assert share.page_lang(_req(header)) == lang


def test_missing_header_follows_server_language(admin):
    admin.patch("/settings", json={"llm_language": "en"})
    assert share.page_lang(_req(None)) == "en"


def test_share_page_in_english(client, admin):
    doc = admin.upload("Mietvertrag", "Mietvertrag").json()
    admin.patch(f"/documents/{doc['id']}", json={"document_date": "2026-03-05"})
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "expires_days": 7}).json()
    client.cookies.clear()
    en = client.get(_path(link), headers={"Accept-Language": "en-GB,en;q=0.9"}).text
    assert '<html lang="en">' in en and "Download PDF" in en and "1 document" in en and "valid until" in en
    assert "5 Mar 2026" in en and "herunterladen" not in en
    de = client.get(_path(link), headers={"Accept-Language": "de-DE"}).text
    assert '<html lang="de">' in de and "PDF herunterladen" in de and "1 Dokument" in de and "05.03.2026" in de
    admin.delete(f"/links/{link['id']}")
    assert "Link no longer valid" in client.get(_path(link), headers={"Accept-Language": "en"}).text


def test_password_and_dropbox_pages_in_english(client, admin):
    doc = admin.upload("Steuer", "Steuer").json()
    link = admin.post("/links", json={"kind": "doc", "target": doc["id"], "password": "geheim1"}).json()
    client.cookies.clear()
    page = client.get(_path(link) + "?error=pw", headers={"Accept-Language": "en"}).text
    assert "password protected" in page and "Wrong password." in page and ">Open<" in page
    box = admin.post("/links", json={"kind": "upload", "name": "Tax advisor"}).json()
    page = client.get(_path(box), headers={"Accept-Language": "en-US"}).text
    assert "Drop in" in page and "Your name (optional)" in page and "Drop box" in page
    assert "Thank you! 2 files have arrived." in client.get(_path(box) + "?ok=2", headers={"Accept-Language": "en"}).text
    assert "This drop box is full." in client.get(_path(box) + "?error=full", headers={"Accept-Language": "en"}).text
    assert "Dieser Briefkasten ist voll." in client.get(_path(box) + "?error=full").text   # ohne Angabe: Deutsch
