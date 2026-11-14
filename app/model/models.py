"""SQLAlchemy model for the Docker Manager host registry.

The table is the cluster-wide source of truth for configured Docker proxies
(1.2.0+). Every core instance reads it; the in-memory list in the service is
only a cache. The legacy Vault key ``configured_hosts`` is imported once.
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


HOST_SOURCES = ("manual", "iac")


class DockerHostRecord(_Base):
    __tablename__ = "docker_manager_hosts"
    # Ids are never reused (a stale UI must not act on a different host that
    # inherited a deleted id). MariaDB AUTO_INCREMENT already behaves so; the
    # flag gives SQLite (tests) the same semantics.
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    # The name is the natural key automated callers (IaC sync) upsert by.
    name = Column(String(255), nullable=False, unique=True)
    # Address of the Docker proxy (IP literal or hostname) — exposed as "ip"
    # in the REST/JSON shape for backwards compatibility.
    address = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, default=2375)
    scheme = Column(String(8), nullable=False, default="http")
    # TLS towards the proxy (scheme "https"). All three are nullable so rows
    # written before these columns existed — and by 1.2.0 instances during a
    # rolling upgrade — keep their meaning: NULL = verify against the system CA
    # store, no client certificate.
    tls_verify = Column(Boolean, nullable=True)
    # CA bundle (PEM file on the core instances) for proxies with a private CA.
    tls_ca_path = Column(String(1024), nullable=True)
    # True when a client certificate + key for this host is stored in Vault
    # (plugin secret ``host_tls_client_<id>``). The material itself never
    # touches this table or an API response.
    tls_client_auth = Column(Boolean, nullable=True)
    labels = Column(JSON, nullable=False, default=dict)
    # "manual" (UI / REST / bus) or "iac" (IaC Orchestrator inventory sync).
    source = Column(String(16), nullable=False, default="manual")
    # Entity Registry UUID of the configuration item, when known.
    ci_id = Column(String(36), nullable=True, index=True)
    # Last result of the leased background reachability probe.
    reachable = Column(Boolean, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    check_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
