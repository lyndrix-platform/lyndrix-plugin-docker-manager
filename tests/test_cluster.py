"""Cluster readiness (1.2.0): DB-backed host registry shared by every instance,
idempotent locked DDL, 1020/IntegrityError-tolerant writers, cluster event with
``_origin``, one-time legacy Vault import, leased probe and read-only health.
Runs against in-memory SQLite."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level):
        return lambda msg, *a, **kw: self.lines.append((level, str(msg)))


class _FakeCtx:
    def __init__(self, secrets: dict | None = None, settings: dict | None = None) -> None:
        self.events: list[tuple[str, dict, bool]] = []
        self.secrets: dict[str, str] = secrets if secrets is not None else {}
        self.settings = settings or {}
        self.log = _Log()

    def emit(self, topic, payload, cluster=False):
        self.events.append((topic, payload, cluster))

    def set_secret(self, key, value):
        self.secrets[key] = value
        return True

    def get_secret(self, key):
        return self.secrets.get(key)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _fake_db(monkeypatch, engine, *, lock=None):
    from app.model import database

    fake = SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine),
                           is_connected=True, advisory_lock=lock)
    monkeypatch.setattr(database, "db_instance", fake)
    return fake


def _service(ctx=None):
    from app.logic.service import DockerManagerService

    svc = DockerManagerService()
    svc.set_context(ctx or _FakeCtx())
    return svc


@pytest.fixture()
def engine(monkeypatch):
    eng = _engine()
    _fake_db(monkeypatch, eng)
    return eng


@pytest.fixture()
def svc(engine):
    return _service()


_H = {"name": "atlas", "ip": "10.1.10.5", "port": 2375, "scheme": "http"}


def _topics(ctx):
    return [(t, c) for t, _, c in ctx.events]


# --- bootstrap DDL ---------------------------------------------------------------

def test_ensure_tables_runs_under_bootstrap_lock_and_is_idempotent(monkeypatch):
    from app.model.database import ensure_tables

    taken: list[str] = []

    @contextmanager
    def _lock(name):
        taken.append(name)
        yield True

    _fake_db(monkeypatch, _engine(), lock=_lock)
    ensure_tables()
    ensure_tables()
    assert taken == ["lyndrix_bootstrap", "lyndrix_bootstrap"]


def test_create_all_race_is_retried(monkeypatch):
    from app.model import database

    _fake_db(monkeypatch, _engine())
    real = database._Base.metadata.create_all
    calls = {"n": 0}

    def _racy(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("(1050, \"Table 'docker_manager_hosts' already exists\")")
        return real(*a, **kw)

    monkeypatch.setattr(database._Base.metadata, "create_all", _racy)
    database.ensure_tables()
    assert calls["n"] == 2


def test_writes_without_db_raise_runtime_error(monkeypatch):
    from app.model import database

    monkeypatch.setattr(database, "db_instance", SimpleNamespace(is_connected=False, engine=None))
    svc = _service()
    with pytest.raises(RuntimeError):
        svc.upsert_host(_H)
    assert svc.current_hosts() == []


# --- CRUD + ids ----------------------------------------------------------------------

def test_upsert_assigns_db_ids_and_upserts_by_name(svc):
    a = svc.upsert_host(_H)
    b = svc.upsert_host({**_H, "name": "hydra", "ip": "10.1.10.6"})
    assert (a["id"], b["id"]) == (1, 2)
    assert a["source"] == "manual" and a["ip"] == "10.1.10.5"
    again = svc.upsert_host({**_H, "ip": "10.1.10.50"})  # same name, no id → update
    assert again["id"] == 1 and again["ip"] == "10.1.10.50"
    assert [h["id"] for h in svc.list_hosts()] == [1, 2]
    # 1.1 JSON keys are still all there.
    assert {"id", "name", "ip", "port", "scheme"} <= set(svc.list_hosts()[0])


def test_update_by_id_and_name_conflict(svc):
    a = svc.upsert_host(_H)
    svc.upsert_host({**_H, "name": "hydra"})
    renamed = svc.upsert_host({**_H, "id": a["id"], "name": "atlas-2"})
    assert renamed["id"] == a["id"] and renamed["name"] == "atlas-2"
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "id": a["id"], "name": "hydra"})


def test_validation_still_rejects_metadata_address(svc):
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "ip": "169.254.169.254"})


def test_delete_host(svc):
    a = svc.upsert_host(_H)
    assert svc.delete_host(a["id"]) is True
    assert svc.delete_host(a["id"]) is False
    assert svc.list_hosts() == []


def test_replace_hosts_keeps_ids_and_removes_the_rest(svc):
    a = svc.upsert_host(_H)
    svc.upsert_host({**_H, "name": "hydra"})
    result = svc.replace_hosts([{**_H, "id": a["id"], "ip": "10.1.10.9"}, {**_H, "name": "titan"}])
    assert {h["name"]: h["id"] for h in result} == {"atlas": a["id"], "titan": 3}
    assert result[0]["ip"] == "10.1.10.9"
    with pytest.raises(ValueError):
        svc.replace_hosts([{**_H, "id": 1}, {**_H, "id": 2}])  # one name, two ids


# --- multi-instance view -----------------------------------------------------------

def test_second_instance_sees_writes_of_the_first(engine):
    a, b = _service(), _service()
    host = a.upsert_host(_H)
    # B's cache is empty, but resolving an unknown id re-reads the shared table.
    assert b._resolve_host(host["id"])["name"] == "atlas"
    with pytest.raises(ValueError):
        b._resolve_host(999)


def test_change_emits_cluster_event_and_peer_reloads(engine):
    ctx_a, ctx_b = _FakeCtx(), _FakeCtx()
    a, b = _service(ctx_a), _service(ctx_b)
    b.load_hosts()
    host = a.upsert_host(_H)
    assert ("docker_manager:hosts_updated", True) in _topics(ctx_a)
    assert ("docker_manager:hosts:updated", False) in _topics(ctx_a)
    cluster_payload = next(p for t, p, c in ctx_a.events if c)
    assert cluster_payload == {"reason": "upsert", "ids": [host["id"]]}

    # Own emit (no _origin) is ignored; the relayed copy reloads the cache.
    asyncio.run(b.on_hosts_updated(dict(cluster_payload)))
    assert b.list_hosts() == []
    asyncio.run(b.on_hosts_updated({**cluster_payload, "_origin": "core-a"}))
    assert [h["name"] for h in b.list_hosts()] == ["atlas"]
    assert ("docker_manager:hosts:updated", False) in _topics(ctx_b)


def test_upsert_retries_on_1020(svc, monkeypatch):
    from app.model import database

    monkeypatch.setattr(database.time, "sleep", lambda _s: None)
    real = svc._upsert_tx
    calls = {"n": 0}

    def _flaky(host):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("(1020, 'Record has changed since last read')")
        return real(host)

    monkeypatch.setattr(svc, "_upsert_tx", _flaky)
    assert svc.upsert_host(_H)["id"] == 1 and calls["n"] == 2


# --- IaC inventory -------------------------------------------------------------------

_INVENTORY = {"hosts": [
    {"name": "docker-atlas", "ip": "10.1.10.20", "groups": ["docker_hosts"], "ci_id": "c1"},
    {"name": "docker-hydra", "ip": "10.1.10.21", "baseline_roles": ["docker"]},
    {"name": "dc-01", "ip": "10.1.10.2", "groups": ["dcs"]},
]}


def test_iac_sync_adds_updates_and_keeps_manual_hosts(svc):
    manual = svc.upsert_host(_H)
    stats = asyncio.run(svc.on_iac_inventory(_INVENTORY))
    assert stats == {"added": 2, "updated": 0, "total": 3}
    by_name = {h["name"]: h for h in svc.list_hosts()}
    assert by_name["docker-atlas"]["source"] == "iac" and by_name["docker-atlas"]["ci_id"] == "c1"
    assert by_name["atlas"]["id"] == manual["id"] and by_name["atlas"]["source"] == "manual"

    moved = {"hosts": [{**_INVENTORY["hosts"][0], "ip": "10.1.10.99"}]}
    assert asyncio.run(svc.on_iac_inventory(moved))["updated"] == 1
    assert {h["name"]: h["ip"] for h in svc.list_hosts()}["docker-atlas"] == "10.1.10.99"


def test_iac_relayed_copy_is_ignored(svc):
    assert asyncio.run(svc.on_iac_inventory({**_INVENTORY, "_origin": "runner-1"})) is None
    assert svc.current_hosts() == []


def test_iac_sync_survives_lost_insert_race(svc, monkeypatch):
    """A peer inserted the same name between our read and our insert."""
    from app.model import database

    monkeypatch.setattr(database.time, "sleep", lambda _s: None)
    real = svc._sync_one_tx
    calls = {"n": 0}

    def _racy(host):
        calls["n"] += 1
        if calls["n"] == 1:
            svc._upsert_tx({**host, "source": "iac"})  # the "peer" wins the insert
            raise IntegrityError("INSERT", {}, Exception("(1062, 'Duplicate entry')"))
        return real(host)

    monkeypatch.setattr(svc, "_sync_one_tx", _racy)
    stats = svc.sync_orchestrator_hosts([{"name": "docker-atlas", "ip": "10.1.10.20"}])
    assert stats == {"added": 0, "updated": 1, "total": 1}


# --- legacy Vault import -------------------------------------------------------------

def _legacy(hosts):
    return {"configured_hosts": json.dumps(hosts)}


def test_legacy_import_keeps_ids_and_runs_once(engine):
    ctx = _FakeCtx(secrets=_legacy([
        {"id": 3, "name": "atlas", "ip": "10.1.10.5", "port": 2375, "scheme": "http"},
        {"id": 7, "name": "hydra", "ip": "10.1.10.6", "port": 2376, "scheme": "https"},
        {"id": 9, "name": "broken", "ip": "169.254.169.254"},
    ]))
    svc = _service(ctx)
    svc.mark_vault_ready()
    asyncio.run(svc.ensure_ready())
    assert {h["name"]: h["id"] for h in svc.list_hosts()} == {"atlas": 3, "hydra": 7}
    assert "configured_hosts_migrated_at" in ctx.secrets

    # Deleting after the import must not resurrect the host on the next boot.
    svc.delete_host(3)
    other = _service(ctx)
    other.mark_vault_ready()
    asyncio.run(other.ensure_ready())
    assert [h["name"] for h in other.list_hosts()] == ["hydra"]
    # New hosts continue after the imported ids.
    assert other.upsert_host({**_H, "name": "titan"})["id"] == 8


def test_legacy_import_reassigns_taken_ids(engine):
    first = _service()
    first.upsert_host(_H)  # id 1, e.g. created by the IaC sync before Vault was ready
    ctx = _FakeCtx(secrets=_legacy([
        {"id": 1, "name": "hydra", "ip": "10.1.10.6"},
        {"id": 2, "name": "atlas", "ip": "10.1.10.77"},  # name already in the table
    ]))
    svc = _service(ctx)
    svc.mark_vault_ready()
    assert svc.import_legacy_hosts() == 1
    by_name = {h["name"]: h for h in svc.load_hosts()}
    assert by_name["hydra"]["id"] == 2 and by_name["atlas"]["ip"] == "10.1.10.5"
    assert any("gets a new id" in msg for _, msg in ctx.log.lines)


def test_legacy_import_waits_for_vault(engine):
    ctx = _FakeCtx(secrets=_legacy([{"id": 1, "name": "atlas", "ip": "10.1.10.5"}]))
    svc = _service(ctx)
    asyncio.run(svc.ensure_ready())
    assert svc.list_hosts() == []
    svc.mark_vault_ready()
    asyncio.run(svc.ensure_ready())
    assert [h["id"] for h in svc.list_hosts()] == [1]


# --- leased probe + health -----------------------------------------------------------

def test_probe_writes_results_and_health_reads_them(svc, monkeypatch):
    a = svc.upsert_host(_H)
    b = svc.upsert_host({**_H, "name": "hydra", "ip": "10.1.10.6"})
    assert svc.health_snapshot()["status"] == "ok"  # unchecked hosts are not a failure
    before = svc.load_hosts()[0]

    def _fetch(host, include_stopped=True):
        if host["name"] == "hydra":
            raise ConnectionError("refused")
        return []

    monkeypatch.setattr(svc, "fetch_single_host", _fetch)
    results = asyncio.run(svc.probe_once())
    assert sorted(results) == [(a["id"], True, None), (b["id"], False, "refused")]

    health = svc.health_snapshot()
    assert health["status"] == "degraded"
    assert health["details"]["reason"] == "some_hosts_unreachable"
    assert health["details"]["unreachable"] == {"hydra": "refused"}
    assert health["details"]["hosts_reachable"] == 1

    monkeypatch.setattr(svc, "fetch_single_host", lambda h, include_stopped=True: (_ for _ in ()).throw(OSError("x")))
    asyncio.run(svc.probe_once())
    health = svc.health_snapshot()
    assert health["status"] == "degraded" and health["details"]["reason"] == "all_hosts_unreachable"
    # Probe results do not count as config edits.
    after = svc.load_hosts()[0]
    assert after["reachable"] is False and after["checked_at"]
    assert before["id"] == after["id"]


def test_health_without_hosts_or_db(svc, monkeypatch):
    assert svc.health_snapshot()["details"]["reason"] == "no_hosts_configured"
    from app.model import database

    monkeypatch.setattr(database, "db_instance", SimpleNamespace(is_connected=False, engine=None))
    assert svc.health_snapshot() == {"status": "degraded", "details": {"reason": "db_unavailable"}}


def test_prober_start_stop(svc, monkeypatch):
    calls = {"n": 0}

    async def _probe():
        calls["n"] += 1
        return []

    monkeypatch.setattr(svc, "probe_once", _probe)

    async def _run():
        await svc.start_prober()
        await svc.start_prober()  # idempotent
        await asyncio.sleep(0)
        assert svc.is_prober
        await svc.stop_prober()
        assert not svc.is_prober

    asyncio.run(_run())
    assert calls["n"] == 1


def test_reload_settings_clamps_interval(engine):
    svc = _service(_FakeCtx(settings={"probe_interval_seconds": "3"}))
    svc.reload_settings()
    assert svc._probe_interval == 10
    svc = _service(_FakeCtx(settings={"probe_interval_seconds": "120"}))
    svc.reload_settings()
    assert svc._probe_interval == 120
    svc = _service(_FakeCtx(settings={"probe_interval_seconds": "nope"}))
    svc.reload_settings()
    assert svc._probe_interval == 60


def test_iac_sync_waits_for_legacy_import(engine):
    """Boot race: the inventory arrives before the legacy import ran."""
    ctx = _FakeCtx(secrets=_legacy([{"id": 5, "name": "docker-atlas", "ip": "10.1.10.20"}]))
    svc = _service(ctx)
    svc.mark_vault_ready()
    asyncio.run(svc.on_iac_inventory(_INVENTORY))
    by_name = {h["name"]: h for h in svc.list_hosts()}
    assert by_name["docker-atlas"]["id"] == 5  # legacy id survived
    assert by_name["docker-atlas"]["ci_id"] == "c1"  # and the inventory still applied
