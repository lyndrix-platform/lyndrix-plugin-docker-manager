"""DM-05: the prober has its own bounded pool and a per-host timeout, so a
sweep over slow hosts cannot occupy the shared default executor."""
from __future__ import annotations

import asyncio
import threading
import time

from app.logic import service as service_module
from test_cluster import _H, _service, engine  # noqa: F401  (fixture)


def _hosts(svc, count):
    return [svc.upsert_host({**_H, "name": f"h{i}", "ip": f"10.1.10.{i}"}) for i in range(1, count + 1)]


def test_probe_concurrency_is_bounded_and_off_the_default_executor(engine, monkeypatch):
    svc = _service()
    _hosts(svc, 20)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    threads: set[str] = set()

    def _fetch(host, include_stopped=True):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            threads.add(threading.current_thread().name)
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return []

    monkeypatch.setattr(svc, "fetch_single_host", _fetch)
    results = asyncio.run(svc.probe_once())
    svc.close()
    assert len(results) == 20 and all(ok for _, ok, _ in results)
    assert 1 < state["peak"] <= service_module.PROBE_WORKERS
    assert all(name.startswith("docker-manager-probe") for name in threads)


def test_hanging_host_times_out_without_blocking_the_sweep(engine, monkeypatch):
    svc = _service()
    slow, fast = _hosts(svc, 2)
    release = threading.Event()

    def _fetch(host, include_stopped=True):
        if host["id"] == slow["id"]:
            release.wait(5)
        return []

    monkeypatch.setattr(svc, "fetch_single_host", _fetch)
    monkeypatch.setattr(service_module, "PROBE_HOST_TIMEOUT", 0.1)
    started = time.monotonic()
    try:
        results = dict((hid, (ok, err)) for hid, ok, err in asyncio.run(svc.probe_once()))
    finally:
        release.set()
        svc.close()
    assert time.monotonic() - started < 2
    assert results[fast["id"]] == (True, None)
    assert results[slow["id"]][0] is False and "timed out" in results[slow["id"]][1]
    stored = {h["id"]: h for h in svc.load_hosts()}
    assert stored[slow["id"]]["reachable"] is False and stored[fast["id"]]["reachable"] is True
