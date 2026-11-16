"""Test bootstrap.

Importing ``core.api`` cold trips a circular import inside lyndrix-core
(core.api → plugins → ui.layout → notifications → core.api). The running
platform never hits it because main.py loads core.services first, so mirror
that order here before any plugin module pulls in core.api.
"""
# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Tests never touch the network: hostnames do not resolve unless a test
    installs its own resolver (``monkeypatch.setattr(targets, "resolve", ...)``)."""
    from app.logic import targets

    def _unresolvable(name):
        raise targets.TargetError(f"Cannot resolve {name!r} (test default)")

    monkeypatch.setattr(targets, "resolve", _unresolvable)
