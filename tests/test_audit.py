"""DM-06: append-only audit trail (who, when, host, container, action, result),
leader-side retention prune, admin-only read permission."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from test_cluster import _FakeCtx, _service, engine  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _schema(engine):
    from app.model import database as db

    db.ensure_tables()


def _entries(svc, **kw):
    return svc.audit_entries(**kw)


def test_container_action_logs_and_exec_are_audited(engine, monkeypatch):
    svc = _service()
    host = svc.upsert_host({"name": "docker-atlas", "ip": "10.1.10.5"})
    monkeypatch.setattr(svc, "_container_action", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(svc, "_container_logs", lambda *a, **k: "line")
    monkeypatch.setattr(svc, "_container_shell_snapshot", lambda *a, **k: "root")

    svc.container_action(host["id"], "abc", "restart", "web", actor="alice")
    svc.container_logs(host["id"], "abc", container_name="web", actor="bob")
    svc.container_shell_snapshot(host["id"], "abc", "web", actor="carol")

    rows = list(reversed(_entries(svc)))
    assert [(r["actor"], r["action"], r["container"], r["result"], r["host_name"]) for r in rows] == [
        ("alice", "restart", "web", "ok", "docker-atlas"),
        ("bob", "logs", "web", "ok", "docker-atlas"),
        ("carol", "exec", "web", "ok", "docker-atlas"),
    ]
    assert rows[0]["at"].endswith("Z")


def test_failed_action_is_audited_and_still_raises(engine, monkeypatch):
    svc = _service()

    def _boom(*a, **k):
        raise ValueError("unknown host")

    monkeypatch.setattr(svc, "_container_logs", _boom)
    with pytest.raises(ValueError):
        svc.container_logs(7, "abc", actor="mallory")
    (row,) = _entries(svc)
    assert (row["actor"], row["action"], row["result"], row["detail"]) == (
        "mallory", "logs", "error", "unknown host")


def test_host_registry_writes_are_audited_but_internal_calls_are_not(engine):
    svc = _service()
    svc.upsert_host({"name": "internal", "ip": "10.1.10.6"})  # no actor: internal caller
    created = svc.upsert_host({"name": "docker-atlas", "ip": "10.1.10.5"}, actor="alice")
    svc.delete_host(created["id"], actor="alice")
    assert [(r["action"], r["host_name"]) for r in reversed(_entries(svc))] == [
        ("host_upsert", "docker-atlas"), ("host_delete", "docker-atlas")]


def test_entries_are_append_only_api_and_paged(engine):
    from app.logic import audit

    for i in range(5):
        audit.record(actor="a", action="logs", result="ok", container=f"c{i}")
    page = audit.list_entries(limit=2)
    assert [r["container"] for r in page] == ["c4", "c3"]
    assert [r["container"] for r in audit.list_entries(limit=2, before_id=page[-1]["id"])] == ["c2", "c1"]
    assert not any(hasattr(audit, name) for name in ("update", "update_entry", "delete", "delete_entry"))


def test_retention_prune_removes_only_old_entries(engine):
    from app.logic import audit
    from app.model import database as db
    from app.model.models import DockerAuditRecord

    audit.record(actor="a", action="logs", result="ok", container="old")
    audit.record(actor="a", action="logs", result="ok", container="new")
    with db.get_session() as s:
        row = s.query(DockerAuditRecord).filter_by(container="old").one()
        row.at = audit._now() - timedelta(days=100)
        s.commit()
    assert audit.prune(0) == 0  # 0 = keep forever
    assert audit.prune(90) == 1
    assert [r["container"] for r in audit.list_entries()] == ["new"]


def test_prune_runs_hourly_at_most(engine):
    svc = _service()
    svc._schema_ready = True
    svc._audit_retention_days = 90
    asyncio.run(svc.prune_audit())
    first = svc._audit_pruned_at
    assert first > 0
    asyncio.run(svc.prune_audit())
    assert svc._audit_pruned_at == first
