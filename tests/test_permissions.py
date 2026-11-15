"""DM-01: sensitive routes are gated on docker:* permissions that the core's
baseline grants (``…:api:read`` for every viewer, ``…:api:write`` for every
user) and the global ``api:read``/``api:write`` fallback can never satisfy."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import ApiIdentity

P = "plugin:lyndrix.plugin.docker"

# (method, path) → required permission. A new route must be added here on purpose.
EXPECTED = {
    ("GET", "/capabilities"): f"{P}:api:read",
    ("GET", "/hosts"): f"{P}:api:read",
    ("POST", "/hosts"): f"{P}:docker:hosts",
    ("POST", "/hosts/sync"): f"{P}:docker:hosts",
    ("POST", "/hosts/set"): f"{P}:docker:hosts",
    ("DELETE", "/hosts/{host_id}"): f"{P}:docker:hosts",
    ("GET", "/containers"): f"{P}:api:read",
    ("POST", "/containers/action"): f"{P}:docker:control",
    ("GET", "/containers/{host_id}/{container_id}/logs"): f"{P}:docker:logs",
    ("POST", "/containers/{host_id}/{container_id}/shell"): f"{P}:docker:exec",
    ("GET", "/audit"): f"{P}:docker:audit",
}


class _FakeService:
    """Records calls; a route that reaches it passed authorization."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append(name)
            return {} if name == "fetch_all_containers_parallel" else []
        return _call


def test_route_permission_table(monkeypatch):
    from app import api

    def _recording(permission):
        def dependency():  # pragma: no cover - never called
            raise AssertionError
        dependency.permission = permission
        return dependency

    monkeypatch.setattr(api, "require_permission", _recording)
    router = api.build_plugin_router(_FakeService())
    actual = {}
    for route in router.routes:
        perms = [d.call.permission for d in route.dependant.dependencies if hasattr(d.call, "permission")]
        assert len(perms) == 1, f"{route.path} must declare exactly one permission"
        for method in route.methods:
            actual[(method, route.path)] = perms[0]
    assert actual == EXPECTED


def test_sensitive_permissions_escape_the_global_fallback():
    for perm in EXPECTED.values():
        if perm.endswith(":api:read"):
            continue
        assert not perm.endswith((":api:read", ":api:write")), perm


def _client(monkeypatch, permissions):
    from app import api
    from core.api import security

    identity = ApiIdentity(method="session", username="alice", permissions=frozenset(permissions))
    monkeypatch.setattr(security, "authenticate_request", lambda request: identity)
    service = _FakeService()
    app = FastAPI()
    app.include_router(api.build_plugin_router(service))
    return TestClient(app), service


_SENSITIVE = [
    ("get", "/containers/1/abc/logs", None),
    ("post", "/containers/action", {"host_id": 1, "container_id": "abc", "action": "stop"}),
    ("post", "/containers/1/abc/shell", None),
    ("post", "/hosts", {"name": "x", "ip": "10.0.0.1"}),
    ("post", "/hosts/sync", {"hosts": []}),
    ("post", "/hosts/set", {"hosts": []}),
    ("delete", "/hosts/1", None),
    ("get", "/audit", None),
]


@pytest.mark.parametrize("granted", [
    # INT_VIEWER / INT_USER baseline and the global fallbacks.
    {"api:read", "api:write"},
    {f"{P}:api:read", f"{P}:api:write", f"plugin:{'lyndrix.plugin.docker'}"},
])
def test_baseline_user_can_look_but_not_touch(monkeypatch, granted):
    client, service = _client(monkeypatch, granted)
    assert client.get("/hosts").status_code == 200
    assert client.get("/containers").status_code == 200
    assert client.get("/capabilities").json() == {
        "logs": False, "control": False, "hosts": False, "exec": False, "audit": False}
    service.calls.clear()
    for method, path, body in _SENSITIVE:
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert service.calls == []  # nothing reached the service


def _load_entrypoint():
    """entrypoint.py uses relative imports — load it the way the core does, as
    a module of a package rooted at the repository."""
    import importlib
    import pathlib
    import sys
    import types

    if "dm_plugin" not in sys.modules:
        pkg = types.ModuleType("dm_plugin")
        pkg.__path__ = [str(pathlib.Path(__file__).resolve().parents[1])]
        sys.modules["dm_plugin"] = pkg
    return importlib.import_module("dm_plugin.entrypoint")


def test_operator_role_gets_logs_and_control_only(monkeypatch):
    entrypoint = _load_entrypoint()

    roles = {r.id: r for r in entrypoint.manifest.roles}
    assert roles["admin"].auto_map_groups == ["INT_ADMIN"]
    assert roles["operator"].auto_map_groups == [] and roles["viewer"].auto_map_groups == []
    assert set(roles["viewer"].permissions) == {f"{P}:api:read"}
    assert {f"{P}:docker:hosts", f"{P}:docker:exec", f"{P}:docker:audit"}.isdisjoint(roles["operator"].permissions)
    assert {p.id for p in entrypoint.manifest.custom_permissions} == {
        "docker:logs", "docker:control", "docker:hosts", "docker:exec", "docker:audit"}

    client, _ = _client(monkeypatch, roles["operator"].permissions)
    assert client.get("/capabilities").json() == {
        "logs": True, "control": True, "hosts": False, "exec": False, "audit": False}
    assert client.get("/containers/1/abc/logs").status_code == 200
    assert client.post("/containers/action", json={
        "host_id": 1, "container_id": "abc", "action": "stop"}).status_code == 200
    assert client.post("/containers/1/abc/shell").status_code == 403
    assert client.delete("/hosts/1").status_code == 403
    assert client.get("/audit").status_code == 403

    admin, _ = _client(monkeypatch, roles["admin"].permissions)
    assert admin.get("/capabilities").json() == {
        "logs": True, "control": True, "hosts": True, "exec": True, "audit": True}


def test_container_rows_expose_status_fields_only():
    """Viewer-level listing: no env, labels, mounts or other inspect details."""
    from app.logic.service import DockerManagerService

    row = DockerManagerService._container_to_row({
        "Id": "a" * 64, "Names": ["/vault"], "Image": "hashicorp/vault", "State": "running",
        "Status": "Up 2 days", "Labels": {"com.example.token": "dummy-token-not-real"},
        "Mounts": [{"Source": "/export/docker/vault"}], "Command": "vault server",
        "NetworkSettings": {}, "Ports": [],
    })
    assert set(row) == {"id", "raw_id", "name", "image", "state", "status"}


def test_viewer_gets_no_internal_host_details(monkeypatch):
    """labels, check_error, tls_ca_path and raw probe errors need docker:hosts."""
    host = {"id": 1, "name": "atlas", "ip": "10.1.10.5", "port": 2375, "scheme": "http",
            "reachable": False, "labels": {"rack": "b2"}, "tls_ca_path": "/data/ca.pem",
            "check_error": "HTTPConnectionPool(host='10.1.10.5', port=2375): refused"}

    class _Service:
        def current_hosts(self):
            return [dict(host)]

        def fetch_all_containers_parallel(self, include_stopped=True):
            return {"atlas#1": {"host": dict(host), "containers": [], "error": host["check_error"]},
                    "hydra#2": {"host": {**host, "id": 2, "name": "hydra"}, "containers": [], "error": None}}

    from app import api
    from core.api import security

    def _get(perms):
        identity = ApiIdentity(method="session", username="alice", permissions=frozenset(perms))
        monkeypatch.setattr(security, "authenticate_request", lambda request: identity)
        app = FastAPI()
        app.include_router(api.build_plugin_router(_Service()))
        client = TestClient(app)
        return client.get("/hosts").json()["hosts"][0], client.get("/containers").json()

    viewer_host, viewer_containers = _get({"api:read"})
    assert {"labels", "check_error", "tls_ca_path"}.isdisjoint(viewer_host)
    assert viewer_host["reachable"] is False and viewer_host["name"] == "atlas"
    entry = viewer_containers["atlas#1"]
    assert entry["error"] == "Host unreachable" and "check_error" not in entry["host"]
    assert viewer_containers["hydra#2"]["error"] is None
    assert "10.1.10.5', port" not in str(viewer_containers)

    admin_host, admin_containers = _get({f"{P}:api:read", f"{P}:docker:hosts"})
    assert admin_host["labels"] == {"rack": "b2"} and admin_host["check_error"]
    assert "refused" in admin_containers["atlas#1"]["error"]
