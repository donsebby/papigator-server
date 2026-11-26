"""Bonjour/mDNS (app/discovery.py): Anmelden und Abmelden mit gemocktem Zeroconf, Abschalten per Env und Demo,
Fehler beim Start halten den Server nicht auf."""
import sys
import types

import pytest

from app import config, discovery


class FakeZeroconf:
    instances: list = []
    fail_register = False

    def __init__(self, interfaces=None, ip_version=None, **kw):
        self.interfaces, self.registered, self.unregistered, self.closed = interfaces, [], [], False
        FakeZeroconf.instances.append(self)

    def register_service(self, info, allow_name_change=False, **kw):
        if FakeZeroconf.fail_register:
            raise OSError("Port 5353 belegt")
        self.allow_name_change = allow_name_change
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


class FakeInfo:
    def __init__(self, type_, name, addresses=None, port=None, properties=None, server=None, **kw):
        self.type, self.name, self.addresses, self.port = type_, name, addresses, port
        self.properties, self.server = properties, server


@pytest.fixture
def fake_zeroconf(monkeypatch):
    mod = types.ModuleType("zeroconf")
    mod.Zeroconf, mod.ServiceInfo = FakeZeroconf, FakeInfo
    mod.IPVersion = types.SimpleNamespace(V4Only="v4")
    monkeypatch.setitem(sys.modules, "zeroconf", mod)
    monkeypatch.setenv("PAPIGATOR_DISCOVERY", "1")
    monkeypatch.delenv("PAPIGATOR_DISCOVERY_PORT", raising=False)
    monkeypatch.setattr(discovery, "local_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(discovery, "hostname", lambda: "Tower")
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"])
    FakeZeroconf.instances, FakeZeroconf.fail_register = [], False
    yield
    discovery.stop()


def _wait():
    t = discovery._thread
    if t is not None:
        t.join(5)


def test_start_registers_and_stop_unregisters(fake_zeroconf):
    discovery.start()
    _wait()
    [zc] = FakeZeroconf.instances
    [info] = zc.registered
    assert info.type == "_papigator._tcp.local."
    assert info.name == "Papigator (Tower)._papigator._tcp.local."
    assert info.port == 8000
    assert info.addresses == [bytes([192, 0, 2, 10])]
    # eigener Name fürs SRV-Ziel, nicht „tower.local.“ (gehört dem System)
    assert info.server == "papigator-tower.local."
    assert info.properties == {"version": discovery.VERSION, "path": "/", "name": "Tower"}
    assert zc.interfaces == ["192.0.2.10"] and zc.allow_name_change

    discovery.start()          # zweiter Aufruf meldet nicht doppelt an
    _wait()
    assert len(FakeZeroconf.instances) == 1

    discovery.stop()
    assert zc.unregistered == [info] and zc.closed
    assert discovery._zc is None


def test_off_by_env(fake_zeroconf, monkeypatch):
    monkeypatch.setenv("PAPIGATOR_DISCOVERY", "0")
    assert not discovery.enabled()
    discovery.start()
    _wait()
    assert FakeZeroconf.instances == []


def test_off_in_demo(fake_zeroconf, monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    assert not discovery.enabled()
    discovery.start()
    _wait()
    assert FakeZeroconf.instances == []


def test_tests_run_without_discovery():
    assert not discovery.enabled()     # conftest setzt PAPIGATOR_DISCOVERY=0


def test_register_error_is_only_logged(fake_zeroconf):
    FakeZeroconf.fail_register = True
    discovery.start()
    _wait()
    [zc] = FakeZeroconf.instances
    assert zc.closed and discovery._zc is None
    discovery.stop()               # nichts mehr zu tun, kein Fehler


def test_missing_package_is_only_logged(fake_zeroconf, monkeypatch):
    monkeypatch.setitem(sys.modules, "zeroconf", None)   # import zeroconf → ImportError
    discovery.start()
    _wait()
    assert discovery._zc is None
    discovery.stop()


def test_no_network_is_only_logged(fake_zeroconf, monkeypatch):
    monkeypatch.setattr(discovery, "local_ip", lambda: None)
    discovery.start()
    _wait()
    assert FakeZeroconf.instances == []


def test_port_from_argv_and_env(monkeypatch):
    monkeypatch.delenv("PAPIGATOR_DISCOVERY_PORT", raising=False)
    monkeypatch.delenv("UVICORN_PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.main:app", "--port=8123"])
    assert discovery.port() == 8123
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.main:app"])
    assert discovery.port() == 8000
    monkeypatch.setenv("PAPIGATOR_DISCOVERY_PORT", "9000")
    assert discovery.port() == 9000
    monkeypatch.setenv("PAPIGATOR_DISCOVERY_PORT", "kaputt")
    assert discovery.port() == 8000


def test_server_label_and_hostname(monkeypatch):
    assert discovery._server_label("papigator") == "papigator"
    assert discovery._server_label("Tower") == "papigator-Tower"
    monkeypatch.setattr(discovery.socket, "gethostname", lambda: "mein rechner.fritz.box")
    assert discovery.hostname() == "mein-rechner"
    monkeypatch.setattr(discovery.socket, "gethostname", lambda: "")
    assert discovery.hostname() == "papigator"


def test_share_app_does_not_announce():
    import inspect
    from app import share_app
    assert "discovery" not in inspect.getsource(share_app)
    assert share_app.app.router.on_startup == []


def test_loopback_only_is_not_announced(fake_zeroconf, monkeypatch):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["/venv/bin/uvicorn", "app.main:app", "--port", "8765"])   # uvicorn-Standard 127.0.0.1
    assert discovery.loopback_only()
    discovery.start()
    _wait()
    assert FakeZeroconf.instances == []
    monkeypatch.setattr(sys, "argv", ["/venv/bin/uvicorn", "app.main:app", "--host=0.0.0.0"])
    assert not discovery.loopback_only()
    monkeypatch.setattr(sys, "argv", ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app"])
    assert not discovery.loopback_only()
