"""DM-04: target validation (numeric forms, embedded IPv4, resolved names),
connect-time re-check with a pinned address, no redirects, strict refs."""
from __future__ import annotations

import ipaddress
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from app.logic import targets
from app.logic.service import DockerManagerService
from test_cluster import _H, _service, engine  # noqa: F401  (fixture)


def _resolver(mapping):
    def _resolve(name):
        if name not in mapping:
            raise targets.TargetError(f"Cannot resolve {name!r}")
        return [ipaddress.ip_address(a) for a in mapping[name]]
    return _resolve


# --- validation at write time ---------------------------------------------------------

@pytest.mark.parametrize("value", [
    "169.254.169.254",            # metadata, literal
    "2852039166",                 # … as one decimal number
    "0xa9fea9fe",                 # … hex
    "0251.0376.0251.0376",        # … octal
    "169.254.43518",              # … short form
    "0xa9.0xfe.0xa9.0xfe",
    "10.1.10.05",                 # leading zero = octal to the resolver
    "1",                          # 0.0.0.1
    "::ffff:169.254.169.254",     # IPv4-mapped IPv6
    "::ffff:a9fe:a9fe",
    "64:ff9b::a9fe:a9fe",         # NAT64
    "2002:a9fe:a9fe::1",          # 6to4
    "fe80::1",
    "fd00:ec2::254",              # AWS IPv6 metadata
    "100.100.100.200",
    "192.0.0.192",
    "0.0.0.0", "224.0.0.1", "240.0.0.1", "::",
    "host.0x1f", "host.123",      # numeric last label
    "-bad.example", "a b", "http://x", "x/y", "",
])
def test_disallowed_targets_are_rejected(value):
    with pytest.raises(ValueError):
        targets.validate_target(value)


@pytest.mark.parametrize("value", ["10.1.10.5", "192.168.1.10", "127.0.0.1", "fd12:3456::5", "docker-atlas",
                                   "atlas.int.example"])
def test_lan_targets_stay_allowed(value):
    targets.validate_target(value)  # hostnames: unresolvable in tests → accepted


def test_hostname_is_resolved_and_every_address_checked(monkeypatch):
    monkeypatch.setattr(targets, "resolve", _resolver({
        "good.example": ["10.1.10.5"],
        "meta.example": ["169.254.169.254"],
        "mixed.example": ["10.1.10.5", "::ffff:169.254.169.254"],
    }))
    targets.validate_target("good.example")
    for name in ("meta.example", "mixed.example"):
        with pytest.raises(ValueError, match="resolves to"):
            targets.validate_target(name)


def test_service_rejects_bypass_forms(engine, monkeypatch):
    svc = _service()
    for value in ("2852039166", "0xa9fea9fe", "0251.0376.0251.0376", "::ffff:169.254.169.254"):
        with pytest.raises(ValueError):
            svc.upsert_host({**_H, "name": f"h-{value}", "ip": value})
    monkeypatch.setattr(targets, "resolve", _resolver({"meta.example": ["169.254.169.254"]}))
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "ip": "meta.example"})
    assert svc.load_hosts() == []


def test_resolution_is_time_bounded(monkeypatch):
    monkeypatch.undo()  # the real resolve(), with a resolver that hangs
    release = threading.Event()
    monkeypatch.setattr(targets, "_getaddrinfo", lambda name: release.wait(5) and [])
    monkeypatch.setattr(targets, "RESOLVE_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(targets.TargetError, match="timed out"):
            targets.resolve("slow.example")
    finally:
        release.set()
        targets.shutdown()


# --- connect time -----------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Host")))
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_server():
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _host(server, name, scheme="http", **extra):
    return {"id": 1, "name": "t", "ip": name, "port": server.server_address[1], "scheme": scheme, **extra}


def test_request_goes_to_the_checked_address_with_host_header(http_server, monkeypatch):
    calls = []

    def _resolve(name):
        calls.append(name)
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(targets, "resolve", _resolve)
    svc = DockerManagerService()
    port = http_server.server_address[1]
    assert svc._http("GET", _host(http_server, "proxy.example"), "/containers/json", timeout=2).status_code == 200
    # Resolved once (by us); the HTTP client got the IP and did no lookup of its own.
    assert calls == ["proxy.example"]
    assert _Handler.seen == [("/containers/json", f"proxy.example:{port}")]


def test_rebound_name_is_blocked_before_any_connection(http_server, monkeypatch):
    monkeypatch.setattr(targets, "resolve", _resolver({"proxy.example": ["169.254.169.254"]}))
    svc = DockerManagerService()
    with pytest.raises(targets.TargetError, match="resolves to 169.254.169.254"):
        svc._http("GET", _host(http_server, "proxy.example"), "/containers/json", timeout=2)
    with pytest.raises(targets.TargetError):  # a stored legacy literal is re-checked too
        svc._http("GET", _host(http_server, "169.254.169.254"), "/containers/json", timeout=2)
    with pytest.raises(targets.TargetError):
        svc._http("GET", _host(http_server, "2852039166"), "/containers/json", timeout=2)
    assert _Handler.seen == []


def test_redirects_are_not_followed(http_server):
    svc = DockerManagerService()
    response = svc._http("GET", _host(http_server, "127.0.0.1"), "/redirect", timeout=2)
    assert response.status_code == 302 and len(_Handler.seen) == 1


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI needed for a throwaway certificate")
def test_https_pins_the_address_and_still_verifies_the_hostname(tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-keyout", str(key),
         "-out", str(cert), "-subj", "/CN=proxy.example", "-addext", "subjectAltName=DNS:proxy.example"],
        check=True, capture_output=True,
    )
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    sni: list = []
    context.sni_callback = lambda sock, name, ctx: sni.append(name)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(targets, "resolve", _resolver({
            "proxy.example": ["127.0.0.1"], "other.example": ["127.0.0.1"]}))
        svc = DockerManagerService()
        port = server.server_address[1]
        ok = svc._http("GET", _host(server, "proxy.example", "https", tls_ca_path=str(cert)),
                       "/containers/json", timeout=3)
        assert ok.status_code == 200
        assert sni == ["proxy.example"] and _Handler.seen == [("/containers/json", f"proxy.example:{port}")]
        # Same address under another name: the certificate does not match → refused.
        with pytest.raises(requests.exceptions.SSLError):
            svc._http("GET", _host(server, "other.example", "https", tls_ca_path=str(cert)),
                      "/containers/json", timeout=3)
    finally:
        server.shutdown()
        server.server_close()


# --- container references ---------------------------------------------------------------

@pytest.mark.parametrize("ref", [".", "..", "../x", "a/b", "a%2f..", ".hidden", "-x", "_x", "a b", "a?b", "a#b",
                                 "x" * 129, "ä"])
def test_invalid_container_refs_are_rejected(ref):
    with pytest.raises(ValueError):
        DockerManagerService._container_refs(ref)
    with pytest.raises(ValueError):
        DockerManagerService._container_refs("abc", ref)


def test_valid_container_refs():
    full = "a1b2c3d4e5f6" + "0" * 52
    assert DockerManagerService._container_refs(full, "/aac-vault_server.1") == [
        full, full[:12], "aac-vault_server.1"]


def test_exec_id_from_the_proxy_is_validated(engine, monkeypatch):
    from types import SimpleNamespace

    svc = _service()
    host = svc.upsert_host(_H)
    paths: list[str] = []

    def _http(method, host, path, **kwargs):
        paths.append(path)
        return SimpleNamespace(status_code=201, json=lambda: {"Id": "../../containers/x/kill"}, text="", content=b"")

    monkeypatch.setattr(svc, "_http", _http)
    with pytest.raises(RuntimeError, match="exec id missing or malformed"):
        svc.container_shell_snapshot(host["id"], "abc123")
    assert all(path == "/containers/abc123/exec" for path in paths)
