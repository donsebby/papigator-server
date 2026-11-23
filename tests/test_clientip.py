"""Weitergeleitete IP-Angaben zählen nur vom vertrauten Proxy – sonst ließe sich die Anmeldesperre umgehen."""
from types import SimpleNamespace

from app import clientip, config


def _req(peer, **headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={k.replace("_", "-"): v for k, v in headers.items()})


def test_direct_client_cannot_fake_its_ip(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", "192.0.2.10/32")
    clientip._nets.cache_clear()
    assert clientip.client_ip(_req("203.0.113.5", x_forwarded_for="1.2.3.4")) == "203.0.113.5"
    assert clientip.client_ip(_req("203.0.113.5", x_real_ip="1.2.3.4")) == "203.0.113.5"


def test_proxy_chain_is_read_from_the_right(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", "192.0.2.0/24")
    clientip._nets.cache_clear()
    # Client fälscht „1.2.3.4“ vorne, der Proxy hängt die echte Adresse an
    assert clientip.client_ip(_req("192.0.2.10", x_forwarded_for="1.2.3.4, 198.51.100.7")) == "198.51.100.7"
    assert clientip.client_ip(_req("192.0.2.10", x_forwarded_for="198.51.100.7, 192.0.2.11")) == "198.51.100.7"
    assert clientip.client_ip(_req("192.0.2.10", x_real_ip="198.51.100.9")) == "198.51.100.9"
    assert clientip.client_ip(_req("192.0.2.10")) == "192.0.2.10"


def test_default_trusts_private_networks_only(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
    clientip._nets.cache_clear()
    assert clientip.client_ip(_req("10.0.0.61", x_forwarded_for="198.51.100.7")) == "198.51.100.7"
    assert clientip.client_ip(_req("198.51.100.7", x_forwarded_for="10.0.0.1")) == "198.51.100.7"
