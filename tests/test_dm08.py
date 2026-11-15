"""DM-08: health never runs DDL; unknown ids never resurrect deleted hosts."""
from __future__ import annotations

from test_cluster import _service, engine  # noqa: F401  (fixture)


def test_health_does_not_run_ddl(engine, monkeypatch):
    from app.model import database as db

    def _no_ddl():
        raise AssertionError("health ran DDL")

    monkeypatch.setattr(db, "ensure_tables", _no_ddl)
    svc = _service()
    assert svc.health_snapshot() == {"status": "degraded", "details": {"reason": "schema_not_ready"}}


def test_upsert_with_deleted_id_gets_a_fresh_id(engine):
    svc = _service()
    gone = svc.upsert_host({"name": "old", "ip": "10.1.10.5"})
    svc.delete_host(gone["id"])
    again = svc.upsert_host({"id": gone["id"], "name": "stale", "ip": "10.1.10.6"})
    assert again["id"] != gone["id"]


def test_replace_with_deleted_id_gets_a_fresh_id(engine):
    svc = _service()
    keep = svc.upsert_host({"name": "keep", "ip": "10.1.10.5"})
    gone = svc.upsert_host({"name": "gone", "ip": "10.1.10.6"})
    svc.delete_host(gone["id"])
    result = svc.replace_hosts([{"id": keep["id"], "name": "keep", "ip": "10.1.10.5"},
                                {"id": gone["id"], "name": "stale", "ip": "10.1.10.7"}])
    ids = {h["name"]: h["id"] for h in result}
    assert ids["keep"] == keep["id"] and ids["stale"] not in (keep["id"], gone["id"])
