"""DM-03 / DM-08: the IaC inventory sync only adds hosts and updates the fields
the inventory owns, never an operator's connection settings, and writes (and
broadcasts) only when a row actually changed."""
from __future__ import annotations

import asyncio

from test_cluster import _FakeCtx, _service, _topics, engine  # noqa: F401  (fixture)

_INV = {"hosts": [{"name": "docker-atlas", "ip": "10.1.10.5", "groups": ["docker_hosts"], "ci_id": "c1"}]}


def _by_name(svc):
    return {h["name"]: h for h in svc.load_hosts()}


def test_manual_host_keeps_connection_settings(engine):
    svc = _service()
    svc.upsert_host({"name": "docker-atlas", "ip": "atlas.mgmt.example", "port": 2376, "scheme": "https"})
    stats = asyncio.run(svc.on_iac_inventory(_INV))
    host = _by_name(svc)["docker-atlas"]
    # scheme/port/address are the operator's; only the CI link comes from the inventory.
    assert (host["scheme"], host["port"], host["ip"], host["source"]) == (
        "https", 2376, "atlas.mgmt.example", "manual")
    assert host["ci_id"] == "c1"
    assert stats == {"added": 0, "updated": 1, "unchanged": 0, "total": 1}
    # An explicit port/scheme from an automated caller does not override it either.
    svc.sync_orchestrator_hosts([{"name": "docker-atlas", "ip": "10.9.9.9", "port": 2375, "scheme": "http"}])
    host = _by_name(svc)["docker-atlas"]
    assert (host["scheme"], host["port"], host["ip"]) == ("https", 2376, "atlas.mgmt.example")


def test_iac_host_follows_address_but_keeps_port_and_scheme(engine):
    svc = _service()
    asyncio.run(svc.on_iac_inventory(_INV))
    created = _by_name(svc)["docker-atlas"]
    assert (created["port"], created["scheme"], created["source"]) == (2375, "http", "iac")
    # Operator moves the proxy behind TLS by editing the iac row.
    svc.upsert_host({"id": created["id"], "name": "docker-atlas", "ip": "10.1.10.5",
                     "port": 2376, "scheme": "https"})
    moved = {"hosts": [{**_INV["hosts"][0], "ip": "10.1.10.99"}]}
    assert asyncio.run(svc.on_iac_inventory(moved))["updated"] == 1
    host = _by_name(svc)["docker-atlas"]
    assert (host["ip"], host["port"], host["scheme"]) == ("10.1.10.99", 2376, "https")
    # An automated caller that states port/scheme owns them for iac rows.
    svc.sync_orchestrator_hosts([{"name": "docker-atlas", "ip": "10.1.10.99", "port": 2377}])
    assert _by_name(svc)["docker-atlas"]["port"] == 2377


def test_unchanged_inventory_writes_and_broadcasts_nothing(engine, monkeypatch):
    ctx = _FakeCtx()
    svc = _service(ctx)
    asyncio.run(svc.on_iac_inventory(_INV))
    ctx.events.clear()

    def _no_write(host):
        raise AssertionError(f"unexpected write for {host['name']}")

    monkeypatch.setattr(svc, "_sync_one_tx", _no_write)
    stats = asyncio.run(svc.on_iac_inventory(_INV))
    assert stats == {"added": 0, "updated": 0, "unchanged": 1, "total": 1}
    assert _topics(ctx) == []  # no cluster event, no reload on the peers


def test_only_changed_rows_are_written(engine, monkeypatch):
    svc = _service()
    inventory = {"hosts": [
        {"name": f"docker-{i}", "ip": f"10.1.10.{i}", "groups": ["docker_hosts"]} for i in range(1, 6)
    ]}
    asyncio.run(svc.on_iac_inventory(inventory))
    written: list[str] = []
    real = svc._sync_one_tx
    monkeypatch.setattr(svc, "_sync_one_tx", lambda host: (written.append(host["name"]), real(host))[1])
    inventory["hosts"][2]["ip"] = "10.1.10.33"
    stats = asyncio.run(svc.on_iac_inventory(inventory))
    assert written == ["docker-3"]
    assert stats == {"added": 0, "updated": 1, "unchanged": 4, "total": 5}


def test_inventory_entries_carry_no_invented_port_or_scheme():
    from app.logic.service import DockerManagerService

    entries = DockerManagerService.inventory_docker_hosts(_INV)
    assert entries == [{"name": "docker-atlas", "ip": "10.1.10.5", "source": "iac", "ci_id": "c1"}]
