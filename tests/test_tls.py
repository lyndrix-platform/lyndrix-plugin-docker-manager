"""DM-02: per-host TLS settings; client certificate material lives in Vault
only; plain http stays the default and is flagged per host."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_cluster import _FakeCtx, _H, _engine, _fake_db, _service, engine  # noqa: F401  (fixture)

_CERT = "-----BEGIN CERTIFICATE-----\nZHVtbXktbm90LXJlYWw=\n-----END CERTIFICATE-----"
_KEY = "-----BEGIN PRIVATE KEY-----\nZHVtbXkta2V5LW5vdC1yZWFs\n-----END PRIVATE KEY-----"


def _vault_service():
    ctx = _FakeCtx()
    svc = _service(ctx)
    svc.mark_vault_ready()
    return svc, ctx


def test_existing_http_host_is_flagged_but_keeps_working(engine):
    svc = _service()
    host = svc.upsert_host(_H)
    assert host["scheme"] == "http" and host["insecure"] is True
    assert host["security_warning"] == "unencrypted_unauthenticated"
    assert host["security"] == {"encrypted": False, "server_verified": False, "client_auth": False}
    assert svc._tls_kwargs(host) == {}


def test_https_verify_ca_bundle_and_warnings(engine):
    svc = _service()
    host = svc.upsert_host({**_H, "scheme": "https", "port": 2376})
    assert (host["tls_verify"], host["insecure"], host["security_warning"]) == (True, False, "tls_no_client_auth")
    assert svc._tls_kwargs(host) == {"verify": True}

    host = svc.upsert_host({**_H, "scheme": "https", "tls_ca_path": "/data/ca.pem"})
    assert svc._tls_kwargs(host) == {"verify": "/data/ca.pem"}
    # A later edit that does not mention TLS keeps the settings.
    host = svc.upsert_host({**_H, "scheme": "https", "port": 2377})
    assert host["tls_ca_path"] == "/data/ca.pem"

    host = svc.upsert_host({**_H, "scheme": "https", "tls_verify": False, "tls_ca_path": None})
    assert svc._tls_kwargs(host) == {"verify": False}
    assert (host["insecure"], host["security_warning"], host["tls_ca_path"]) == (True, "tls_unverified", None)

    for bad in ("relative/ca.pem", "x" * 2000):
        with pytest.raises(ValueError):
            svc.upsert_host({**_H, "tls_ca_path": bad})
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "tls_verify": "yes"})


def test_client_certificate_goes_to_vault_only(engine):
    svc, ctx = _vault_service()
    host = svc.upsert_host({**_H, "scheme": "https", "tls_client_cert_pem": _CERT, "tls_client_key_pem": _KEY})
    assert host["tls_client_auth"] is True and host["security_warning"] is None
    # Not in the API shape, not in any cache entry, not in the table, not on the bus.
    blob = json.dumps([host, svc.list_hosts(), [p for _, p, _ in ctx.events]])
    assert "PRIVATE KEY" not in blob and "BEGIN CERTIFICATE" not in blob
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM docker_manager_hosts")).fetchall()
    assert "PRIVATE KEY" not in repr(rows) and "BEGIN CERTIFICATE" not in repr(rows)
    assert "PRIVATE KEY" in ctx.secrets[f"host_tls_client_{host['id']}"]

    kwargs = svc._tls_kwargs(host)
    cert_file, key_file = kwargs["cert"]
    assert open(key_file).read().strip() == _KEY
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(key_file)).st_mode & 0o777) == "0o700"

    # Removal: flag off, Vault blanked; teardown removes the files.
    host = svc.upsert_host({**_H, "scheme": "https", "tls_client_auth": False})
    assert host["tls_client_auth"] is False and ctx.secrets[f"host_tls_client_{host['id']}"] == ""
    assert "cert" not in svc._tls_kwargs(host)
    svc.close()
    assert not os.path.exists(key_file)


def test_client_auth_host_is_never_contacted_without_its_certificate(engine):
    svc, ctx = _vault_service()
    host = svc.upsert_host({**_H, "scheme": "https", "tls_client_cert_pem": _CERT, "tls_client_key_pem": _KEY})
    ctx.secrets.clear()
    svc._client_certs.invalidate()
    with pytest.raises(RuntimeError, match="not available in Vault"):
        svc._tls_kwargs(host)


def test_client_certificate_validation_and_vault_requirement(engine):
    svc = _service()  # Vault not ready
    with pytest.raises(RuntimeError, match="Vault is not ready"):
        svc.upsert_host({**_H, "tls_client_cert_pem": _CERT, "tls_client_key_pem": _KEY})
    assert svc.load_hosts()[0]["tls_client_auth"] is False
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "tls_client_cert_pem": _CERT})  # key missing
    with pytest.raises(ValueError):
        svc.upsert_host({**_H, "tls_client_cert_pem": "nope", "tls_client_key_pem": _KEY})
    with pytest.raises(ValueError, match="per host"):
        svc.replace_hosts([{**_H, "tls_client_cert_pem": _CERT, "tls_client_key_pem": _KEY}])


def test_delete_blanks_the_vault_secret(engine):
    svc, ctx = _vault_service()
    host = svc.upsert_host({**_H, "scheme": "https", "tls_client_cert_pem": _CERT, "tls_client_key_pem": _KEY})
    svc.delete_host(host["id"])
    assert ctx.secrets[f"host_tls_client_{host['id']}"] == ""


def test_requests_carry_the_tls_options(engine, monkeypatch):
    from app.logic import service as service_module

    svc = _service()
    host = svc.upsert_host({**_H, "scheme": "https", "tls_ca_path": "/data/ca.pem"})
    seen = {}

    def _request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: [])

    monkeypatch.setattr(service_module.requests, "request", _request)
    assert svc.fetch_single_host(host) == []
    assert seen["url"].startswith("https://10.1.10.5:2375/") and seen["verify"] == "/data/ca.pem"


def test_tls_columns_are_added_to_a_1_2_0_table(monkeypatch):
    """Upgrade path: the 1.2.0 table has no TLS columns; rows keep working."""
    from app.model import database

    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE docker_manager_hosts (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL UNIQUE,"
            " address VARCHAR(255) NOT NULL, port INTEGER NOT NULL, scheme VARCHAR(8) NOT NULL, labels JSON NOT NULL,"
            " source VARCHAR(16) NOT NULL, ci_id VARCHAR(36), reachable BOOLEAN, checked_at DATETIME, check_error TEXT,"
            " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"))
        conn.execute(text(
            "INSERT INTO docker_manager_hosts (name, address, port, scheme, labels, source, created_at, updated_at)"
            " VALUES ('atlas', '10.1.10.5', 2375, 'http', '{}', 'manual', '2026-10-15 00:00:00', '2026-10-15 00:00:00')"))
    _fake_db(monkeypatch, eng)
    database.ensure_tables()
    database.ensure_tables()  # idempotent
    host = _service().load_hosts()[0]
    assert (host["name"], host["scheme"], host["tls_verify"], host["tls_client_auth"]) == ("atlas", "http", True, False)
