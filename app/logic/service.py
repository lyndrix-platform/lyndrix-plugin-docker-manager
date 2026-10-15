"""Service layer for Docker Manager (host registry + Docker proxy runtime)."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..model import database as db
from ..model.models import HOST_SOURCES, DockerHostRecord

# Cluster-wide "the host table changed, re-read it" (relayed copies carry _origin).
EVENT_HOSTS_UPDATED = "docker_manager:hosts_updated"
# 1.1 local topic with the full host list, kept for existing subscribers.
EVENT_HOSTS_UPDATED_LOCAL = "docker_manager:hosts:updated"

# Legacy (<= 1.1) Vault storage and the marker written after the one-time import.
LEGACY_VAULT_KEY = "configured_hosts"
LEGACY_MARKER_KEY = "configured_hosts_migrated_at"

CACHE_TTL_SECONDS = 30.0
DEFAULT_PROBE_INTERVAL = 60
MIN_PROBE_INTERVAL = 10

# Hostnames per RFC 1123 (labels of [a-z0-9-], no leading/trailing hyphen).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _validate_host_target(value: str) -> None:
    """Reject SSRF-prone or malformed Docker host targets.

    A host's ``ip`` is operator-supplied and the core process then issues
    GET/POST requests to it, so we validate it is a sane IP/hostname and block
    the cloud-metadata / link-local range (169.254.0.0/16, fd00:ec2::/…) and
    other non-routable categories. Private/loopback addresses stay allowed
    because Docker proxies commonly live on the LAN or the host itself.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        # Not a literal IP — accept it only if it is a syntactically valid hostname.
        if not _HOSTNAME_RE.match(value):
            raise ValueError(f"Invalid host address: {value!r}")
        return

    if (
        addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError(
            f"Disallowed host address (link-local/metadata/reserved): {value!r}"
        )


class DockerManagerService:
    """Host registry (DB-backed, cluster-safe) + Docker proxy runtime actions.

    Since 1.2.0 the ``docker_manager_hosts`` table is the source of truth so every
    core instance behind the load balancer sees the same hosts and ids. ``_hosts``
    is a per-instance cache, refreshed after own writes, on the cluster event
    ``docker_manager:hosts_updated`` from a peer and after ``CACHE_TTL_SECONDS``.
    """

    def __init__(self) -> None:
        self._ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._hosts: List[Dict[str, Any]] = []
        self._loaded_at = 0.0  # monotonic time of the last DB read; 0 = never
        self._schema_ready = False
        self._vault_ready = False
        self._legacy_checked = False
        self._bootstrap_lock: asyncio.Lock | None = None
        self._probe_task: asyncio.Task | None = None
        self._probe_interval = DEFAULT_PROBE_INTERVAL

    @property
    def is_ready(self) -> bool:
        return self._ctx is not None

    @property
    def is_prober(self) -> bool:
        return self._probe_task is not None and not self._probe_task.done()

    def set_context(self, ctx) -> None:
        self._ctx = ctx
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _log(self, level: str, message: str) -> None:
        if self._ctx is not None:
            getattr(self._ctx.log, level)(message)

    # ── Normalisation ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_host(raw_host: Dict[str, Any]) -> Dict[str, Any]:
        """Validate one host payload; ``id`` is kept only when supplied."""
        if not isinstance(raw_host, dict):
            raise ValueError("Host must be an object")

        name = str(raw_host.get("name", "")).strip()
        ip = str(raw_host.get("ip", "")).strip()
        if not name or not ip:
            raise ValueError("Host requires 'name' and 'ip'")
        if len(name) > 255:
            raise ValueError("Host name too long (max 255)")
        # Validate the target to reduce the SSRF surface (operator-supplied value
        # that the core process will issue HTTP requests to).
        _validate_host_target(ip)

        try:
            port = int(raw_host.get("port", 2375) or 2375)
        except Exception as exc:
            raise ValueError("Port must be numeric") from exc
        if not 0 < port < 65536:
            raise ValueError("Port out of range")

        scheme = str(raw_host.get("scheme", "http") or "http").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "http"

        host: Dict[str, Any] = {"name": name, "ip": ip, "port": port, "scheme": scheme}
        if raw_host.get("id") is not None:
            try:
                host["id"] = int(raw_host["id"])
            except Exception as exc:
                raise ValueError("Host id must be numeric") from exc
        source = raw_host.get("source")
        if source is not None:
            if source not in HOST_SOURCES:
                raise ValueError(f"source must be one of {', '.join(HOST_SOURCES)}")
            host["source"] = source
        if raw_host.get("ci_id"):
            host["ci_id"] = str(raw_host["ci_id"])[:36]
        if isinstance(raw_host.get("labels"), dict):
            host["labels"] = raw_host["labels"]
        return host

    @staticmethod
    def _row_to_dict(row: DockerHostRecord) -> Dict[str, Any]:
        """REST/JSON shape: the 1.1 keys (id/name/ip/port/scheme) + additive fields."""
        return {
            "id": int(row.id),
            "name": row.name,
            "ip": row.address,
            "port": int(row.port or 2375),
            "scheme": row.scheme or "http",
            "source": row.source or "manual",
            "ci_id": row.ci_id,
            "labels": row.labels or {},
            "reachable": row.reachable,
            "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            "check_error": row.check_error,
        }

    @staticmethod
    def _apply(row: DockerHostRecord, host: Dict[str, Any]) -> None:
        row.name = host["name"]
        row.address = host["ip"]
        row.port = host["port"]
        row.scheme = host["scheme"]
        if "source" in host:
            row.source = host["source"]
        if "ci_id" in host:
            row.ci_id = host["ci_id"]
        if "labels" in host:
            row.labels = host["labels"]

    def _new_row(self, host: Dict[str, Any], *, source: str = "manual") -> DockerHostRecord:
        row = DockerHostRecord(source=host.get("source", source), labels={})
        if host.get("id") is not None:
            row.id = host["id"]
        self._apply(row, host)
        return row

    # ── Events ───────────────────────────────────────────────────────────────

    def _emit(self, topic: str, payload: Dict[str, Any], *, cluster: bool = False) -> None:
        """Emit on the event loop — the bus schedules tasks, so worker threads
        (``asyncio.to_thread`` writers) hand the call over thread-safely."""
        ctx = self._ctx
        if ctx is None:
            return

        def _do() -> None:
            try:
                if cluster:
                    try:
                        ctx.emit(topic, payload, cluster=True)
                    except TypeError:  # core < 1.7: no cluster bridge
                        ctx.emit(topic, payload)
                else:
                    ctx.emit(topic, payload)
            except Exception as exc:
                self._log("warning", f"Docker Manager: emit {topic} failed: {exc}")

        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        loop = self._loop
        if in_loop or loop is None or loop.is_closed():
            _do()
        else:
            loop.call_soon_threadsafe(_do)

    def _after_change(self, reason: str, ids: List[int]) -> None:
        """Refresh this instance's cache and tell every instance to do the same."""
        try:
            self.load_hosts()
        except Exception as exc:
            self._log("warning", f"Docker Manager: cache refresh after {reason} failed: {exc}")
            self.invalidate_cache()
        hosts = self.list_hosts()
        self._emit(EVENT_HOSTS_UPDATED_LOCAL, {"hosts": hosts})
        self._emit(EVENT_HOSTS_UPDATED, {"reason": reason, "ids": sorted(ids)}, cluster=True)

    async def on_hosts_updated(self, payload: Dict[str, Any] | None) -> None:
        """Cluster event handler: a peer changed hosts → re-read the table."""
        if not isinstance(payload, dict) or not payload.get("_origin"):
            return  # own emit — the cache is already fresh
        self.invalidate_cache()
        try:
            await asyncio.to_thread(self.load_hosts)
        except Exception as exc:
            self._log("warning", f"Docker Manager: reload after peer change failed: {exc}")
            return
        self._emit(EVENT_HOSTS_UPDATED_LOCAL, {"hosts": self.list_hosts()})

    # ── Bootstrap: schema, legacy Vault import, cache ───────────────────────

    def _require_db(self) -> None:
        if not db.is_connected():
            raise RuntimeError("Database not connected")
        if not self._schema_ready:
            db.ensure_tables()
            self._schema_ready = True

    def mark_vault_ready(self) -> None:
        self._vault_ready = True

    async def ensure_ready(self) -> None:
        """Tables → one-time legacy import → cache. Safe to call repeatedly
        (setup when the DB is already up, ``db:connected``, ``vault:ready_for_data``)."""
        if not db.is_connected():
            return
        if self._bootstrap_lock is None:
            self._bootstrap_lock = asyncio.Lock()
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        async with self._bootstrap_lock:
            try:
                await asyncio.to_thread(self._require_db)
                if self._vault_ready and not self._legacy_checked:
                    imported = await asyncio.to_thread(self.import_legacy_hosts)
                    if imported:
                        self._log("info", f"Docker Manager: imported {imported} host(s) from Vault")
                count = len(await asyncio.to_thread(self.load_hosts))
                self._log("info", f"Docker Manager: {count} host(s) loaded from the database")
            except Exception as exc:
                self._log("error", f"Docker Manager: bootstrap failed: {exc}")

    def import_legacy_hosts(self) -> int:
        """Import the 1.1 Vault list ``configured_hosts`` into the table, once.

        Ids are kept when free; a taken or invalid id is re-assigned and logged.
        Names already in the table are skipped (the table wins). A Vault marker
        stops later boots from resurrecting hosts deleted after the import.
        Runs under the bootstrap advisory lock so two booting instances cannot
        import twice. Blocking.
        """
        ctx = self._ctx
        if ctx is None or not self._vault_ready:
            return 0
        self._require_db()
        with db.bootstrap_lock():
            if ctx.get_secret(LEGACY_MARKER_KEY):
                self._legacy_checked = True
                return 0
            raw = ctx.get_secret(LEGACY_VAULT_KEY)
            if not raw:
                self._log("info", "Docker Manager: no legacy Vault host list to import")
                self._legacy_checked = True
                return 0
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("configured_hosts must be a list")
            except Exception as exc:
                self._log("error", f"Docker Manager: legacy Vault host list unreadable: {exc}")
                self._legacy_checked = True
                return 0

            imported = 0
            with db.get_session() as s:
                taken_ids = {r for (r,) in s.query(DockerHostRecord.id).all()}
                taken_names = {r for (r,) in s.query(DockerHostRecord.name).all()}
                for raw_host in parsed:
                    try:
                        host = self._normalize_host(raw_host)
                    except ValueError as exc:
                        self._log("warning", f"Docker Manager: legacy host skipped ({exc}): {raw_host!r}")
                        continue
                    if host["name"] in taken_names:
                        self._log("info", f"Docker Manager: legacy host '{host['name']}' already in the table")
                        continue
                    old_id = host.get("id")
                    if old_id is None or old_id <= 0 or old_id in taken_ids:
                        host.pop("id", None)
                        self._log(
                            "warning",
                            f"Docker Manager: legacy host '{host['name']}' (id {old_id}) gets a new id",
                        )
                    row = self._new_row(host)
                    s.add(row)
                    s.flush()
                    taken_ids.add(int(row.id))
                    taken_names.add(row.name)
                    imported += 1
                s.commit()
            ctx.set_secret(LEGACY_MARKER_KEY, datetime.now(timezone.utc).isoformat())
        self._log("info", f"Docker Manager: legacy Vault host list checked ({imported} imported)")
        self._legacy_checked = True
        if imported:
            self._after_change("legacy_import", [])
        return imported

    # ── Reads (cache) ────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def load_hosts(self) -> List[Dict[str, Any]]:
        """Re-read the table into the cache (blocking — call via ``to_thread``)."""
        self._require_db()
        with db.get_session() as s:
            rows = [self._row_to_dict(r) for r in s.query(DockerHostRecord).order_by(DockerHostRecord.id).all()]
        with self._lock:
            self._hosts = rows
            self._loaded_at = time.monotonic()
        return self.list_hosts()

    def list_hosts(self) -> List[Dict[str, Any]]:
        """Cached host list — never touches the DB (safe on the event loop)."""
        with self._lock:
            return sorted([dict(host) for host in self._hosts], key=lambda h: h["id"])

    def current_hosts(self) -> List[Dict[str, Any]]:
        """Cache, re-read when older than ``CACHE_TTL_SECONDS`` (blocking)."""
        with self._lock:
            stale = time.monotonic() - self._loaded_at > CACHE_TTL_SECONDS
        if stale and db.is_connected():
            try:
                return self.load_hosts()
            except Exception as exc:
                self._log("warning", f"Docker Manager: host reload failed, serving cache: {exc}")
        return self.list_hosts()

    def _resolve_host(self, host_id: int) -> Dict[str, Any]:
        """Host by id; a cache miss re-reads the table (a peer may have added it)."""
        for attempt in range(2):
            with self._lock:
                for host in self._hosts:
                    if int(host["id"]) == int(host_id):
                        return dict(host)
            if attempt == 0 and db.is_connected():
                self.load_hosts()
        raise ValueError(f"Unknown host id: {host_id}")

    # ── Writes (DB, per-row, retried) ────────────────────────────────────────

    def _upsert_tx(self, host: Dict[str, Any]) -> Dict[str, Any]:
        with db.get_session() as s:
            row = s.get(DockerHostRecord, host["id"]) if host.get("id") is not None else None
            by_name = s.query(DockerHostRecord).filter(DockerHostRecord.name == host["name"]).one_or_none()
            if row is not None:
                if by_name is not None and by_name.id != row.id:
                    raise ValueError(f"Host name already in use: {host['name']}")
                self._apply(row, host)
            elif host.get("id") is not None:
                # Explicit id that does not exist (bus/legacy callers): create it.
                if by_name is not None:
                    raise ValueError(f"Host name already in use: {host['name']}")
                row = self._new_row(host)
                s.add(row)
            elif by_name is not None:
                row = by_name  # upsert by name
                self._apply(row, host)
            else:
                row = self._new_row(host)
                s.add(row)
            s.commit()
            s.refresh(row)
            return self._row_to_dict(row)

    def upsert_host(self, host_payload: Dict[str, Any], *, emit: bool = True) -> Dict[str, Any]:
        """Create or update one host (by id when given, else by name). Blocking."""
        if not isinstance(host_payload, dict):
            raise ValueError("host payload must be an object")
        host = self._normalize_host(host_payload)
        self._require_db()
        try:
            result = db.retry_write(lambda: self._upsert_tx(host), integrity=True)
        except IntegrityError as exc:
            raise ValueError(f"Host conflicts with an existing host: {host['name']}") from exc
        if emit:
            self._after_change("upsert", [result["id"]])
        else:
            self.invalidate_cache()
        return result

    def _replace_tx(self, hosts: List[Dict[str, Any]]) -> List[int]:
        with db.get_session() as s:
            rows = s.query(DockerHostRecord).all()
            by_id = {r.id: r for r in rows}
            by_name = {r.name: r for r in rows}
            plan: List[tuple[Dict[str, Any], DockerHostRecord | None]] = []
            keep: set[int] = set()
            for host in hosts:
                row = by_id.get(host["id"]) if host.get("id") is not None else by_name.get(host["name"])
                plan.append((host, row))
                if row is not None:
                    keep.add(row.id)
            changed = [r.id for r in rows if r.id not in keep]
            for row in rows:
                if row.id not in keep:
                    s.delete(row)
            s.flush()
            for host, row in plan:
                if row is None:
                    row = self._new_row(host)
                    s.add(row)
                else:
                    self._apply(row, host)
                s.flush()
                changed.append(int(row.id))
            s.commit()
            return changed

    def replace_hosts(self, hosts: List[Dict[str, Any]], *, emit: bool = True) -> List[Dict[str, Any]]:
        """Make the table equal ``hosts`` (matched by id, else name). Blocking."""
        if not isinstance(hosts, list):
            raise ValueError("hosts must be a list")
        by_key: Dict[Any, Dict[str, Any]] = {}
        for raw in hosts:
            host = self._normalize_host(raw)
            by_key[host.get("id", f"name:{host['name']}")] = host  # last one wins, as in 1.1
        normalized = list(by_key.values())
        names = [h["name"] for h in normalized]
        if len(names) != len(set(names)):
            raise ValueError("Host names must be unique")
        self._require_db()
        try:
            ids = db.retry_write(lambda: self._replace_tx(normalized))
        except IntegrityError as exc:
            raise ValueError(f"Host list conflicts with existing hosts: {exc.orig}") from exc
        if emit:
            self._after_change("replace", ids)
        else:
            self.invalidate_cache()
        return self.list_hosts() if emit else self.load_hosts()

    def _sync_one_tx(self, host: Dict[str, Any]) -> tuple[str, int]:
        with db.get_session() as s:
            row = s.query(DockerHostRecord).filter(DockerHostRecord.name == host["name"]).one_or_none()
            if row is None:
                row = self._new_row({**host, "source": host.get("source", "iac")})
                s.add(row)
                outcome = "added"
            else:
                # Connection fields (and the CI id, when known) come from the
                # inventory; id, source and labels of the existing row are kept.
                row.address = host["ip"]
                row.port = host["port"]
                row.scheme = host["scheme"]
                if host.get("ci_id"):
                    row.ci_id = host["ci_id"]
                outcome = "updated"
            s.commit()
            return outcome, int(row.id)

    def sync_orchestrator_hosts(self, incoming: list[dict]) -> dict:
        """Upsert hosts by name without removing unrecognized entries. Blocking.

        Intended for automated callers (IaC Orchestrator). One short transaction
        per host, retried on 1020/1205/1213 and on a lost insert race, so two
        instances syncing the same inventory converge instead of colliding.

        Returns ``{"added": n, "updated": n, "total": n}``.
        """
        self._require_db()
        added = updated = 0
        ids: List[int] = []
        for raw in incoming or []:
            if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
                continue
            try:
                host = self._normalize_host({**raw, "id": None})
            except ValueError as exc:
                self._log("warning", f"Docker Manager: sync skipped {raw.get('name')!r}: {exc}")
                continue
            host.pop("id", None)
            try:
                outcome, host_id = db.retry_write(lambda h=host: self._sync_one_tx(h), integrity=True)
            except Exception as exc:
                self._log("error", f"Docker Manager: sync of '{host['name']}' failed: {exc}")
                continue
            ids.append(host_id)
            if outcome == "added":
                added += 1
            else:
                updated += 1
        self._after_change("sync", ids)
        return {"added": added, "updated": updated, "total": len(self.list_hosts())}

    def _delete_tx(self, host_id: int) -> bool:
        with db.get_session() as s:
            row = s.get(DockerHostRecord, int(host_id))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def delete_host(self, host_id: int, *, emit: bool = True) -> bool:
        self._require_db()
        changed = db.retry_write(lambda: self._delete_tx(host_id))
        if changed and emit:
            self._after_change("delete", [int(host_id)])
        elif changed:
            self.invalidate_cache()
        return changed

    # ── IaC inventory ────────────────────────────────────────────────────────

    @staticmethod
    def inventory_docker_hosts(payload: Any) -> List[Dict[str, Any]]:
        """Pick the Docker hosts out of a generic ``iac:inventory_updated`` payload.

        Filtering lives here, not in the orchestrator, so both plugins stay
        independent — the orchestrator broadcasts generic host data.
        """
        hosts = (payload.get("hosts") or []) if isinstance(payload, dict) else []
        result = []
        for h in hosts:
            if not isinstance(h, dict) or not h.get("ip") or not h.get("name"):
                continue
            if "docker_hosts" in (h.get("groups") or []) or "docker" in (h.get("baseline_roles") or []):
                entry = {"name": h["name"], "ip": h["ip"], "port": 2375, "scheme": "http", "source": "iac"}
                if h.get("ci_id"):
                    entry["ci_id"] = h["ci_id"]
                result.append(entry)
        return result

    async def on_iac_inventory(self, payload: Any) -> Dict[str, int] | None:
        """``iac:inventory_updated`` handler. The orchestrator emits it on the
        runner instance; that instance writes the table and the cluster event
        refreshes the peers. A relayed copy (``_origin``) is ignored so the
        sync never runs N times."""
        if isinstance(payload, dict) and payload.get("_origin"):
            return None
        docker_hosts = self.inventory_docker_hosts(payload)
        if not docker_hosts:
            return None
        # Let a pending legacy Vault import finish first so imported hosts keep
        # their 1.1 ids instead of losing them to freshly synced rows.
        if self._vault_ready and not self._legacy_checked:
            await self.ensure_ready()
        elif self._bootstrap_lock is not None and self._bootstrap_lock.locked():
            async with self._bootstrap_lock:
                pass
        return await asyncio.to_thread(self.sync_orchestrator_hosts, docker_hosts)

    # ── Settings ─────────────────────────────────────────────────────────────

    def reload_settings(self) -> None:
        """Re-read operator settings (blocking Vault read — use ``to_thread``)."""
        get_setting = getattr(self._ctx, "get_setting", None)
        if not callable(get_setting):
            return
        try:
            value = int(get_setting("probe_interval_seconds", DEFAULT_PROBE_INTERVAL) or DEFAULT_PROBE_INTERVAL)
        except (TypeError, ValueError):
            value = DEFAULT_PROBE_INTERVAL
        self._probe_interval = max(MIN_PROBE_INTERVAL, value)

    # ── Leased reachability probe ────────────────────────────────────────────

    async def start_prober(self) -> None:
        """``on_lease("docker_manager:prober")`` acquired → probe in the background."""
        if self.is_prober:
            return
        create_task = getattr(self._ctx, "create_task", None) or asyncio.create_task
        self._probe_task = create_task(self._probe_loop(), name="docker_manager:prober")
        self._log("info", "Docker Manager: reachability prober started")

    async def stop_prober(self) -> None:
        """Lease lost / plugin unloaded → stop probing at once."""
        task, self._probe_task = self._probe_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._log("info", "Docker Manager: reachability prober stopped")

    async def _probe_loop(self) -> None:
        while True:
            try:
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one failed sweep never kills the prober
                self._log("warning", f"Docker Manager: probe sweep failed: {exc}")
            await asyncio.sleep(self._probe_interval)

    def _probe_host(self, host: Dict[str, Any]) -> tuple[int, bool, str | None]:
        try:
            self.fetch_single_host(host, include_stopped=False)
            return int(host["id"]), True, None
        except Exception as exc:
            return int(host["id"]), False, str(exc)[:1000]

    async def probe_once(self) -> List[tuple[int, bool, str | None]]:
        """Probe every host once and persist reachable/checked_at/check_error."""
        if not db.is_connected():
            return []
        hosts = await asyncio.to_thread(self.load_hosts)
        if not hosts:
            return []
        results = await asyncio.gather(*(asyncio.to_thread(self._probe_host, h) for h in hosts))
        await asyncio.to_thread(self._store_probe_results, list(results))
        return list(results)

    def _store_probe_results(self, results: List[tuple[int, bool, str | None]]) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for host_id, ok, error in results:
            def _tx(host_id=host_id, ok=ok, error=error) -> None:
                with db.get_session() as s:
                    s.execute(
                        update(DockerHostRecord)
                        .where(DockerHostRecord.id == host_id)
                        # updated_at tracks config edits, not probe results.
                        .values(reachable=ok, checked_at=now, check_error=error,
                                updated_at=DockerHostRecord.updated_at)
                    )
                    s.commit()

            try:
                db.retry_write(_tx)
            except Exception as exc:  # a lost write only delays this host's status
                self._log("warning", f"Docker Manager: storing probe result of host {host_id} failed: {exc}")

    # ── Health (reads the probe results; never probes) ───────────────────────

    def health_snapshot(self) -> Dict[str, Any]:
        """One small query over the probe columns. Blocking.

        Unreachable proxies make the plugin ``degraded`` (never ``error``): the
        plugin itself works, and reachability is judged by the prober instance,
        not by whichever instance the dashboard happened to ask.
        """
        if not db.is_connected():
            return {"status": "degraded", "details": {"reason": "db_unavailable"}}
        self._require_db()
        with db.get_session() as s:
            rows = s.query(
                DockerHostRecord.name, DockerHostRecord.reachable,
                DockerHostRecord.checked_at, DockerHostRecord.check_error,
            ).all()
        details: Dict[str, Any] = {"hosts_total": len(rows), "prober": self.is_prober}
        if not rows:
            return {"status": "degraded", "details": {**details, "reason": "no_hosts_configured"}}
        unreachable = {name: err for name, ok, _, err in rows if ok is False}
        checked = [at for _, ok, at, _ in rows if at is not None]
        details.update(
            hosts_reachable=sum(1 for _, ok, _, _ in rows if ok is True),
            hosts_unchecked=sum(1 for _, ok, _, _ in rows if ok is None),
            unreachable=unreachable,
            last_checked_at=max(checked).isoformat() if checked else None,
        )
        if unreachable:
            reason = "all_hosts_unreachable" if len(unreachable) == len(rows) else "some_hosts_unreachable"
            return {"status": "degraded", "details": {**details, "reason": reason}}
        return {"status": "ok", "details": details}

    @staticmethod
    def _base_url(host: Dict[str, Any]) -> str:
        return f"{host['scheme']}://{host['ip']}:{host['port']}"

    @staticmethod
    def _container_to_row(container: Dict[str, Any]) -> Dict[str, Any]:
        name = (container.get("Names") or ["/unknown"])[0].lstrip("/")
        return {
            "id": (container.get("Id") or "")[:12],
            "raw_id": container.get("Id", ""),
            "name": name,
            "image": container.get("Image"),
            "state": container.get("State"),
            "status": container.get("Status"),
        }

    def fetch_single_host(self, host: Dict[str, Any], *, include_stopped: bool = True) -> List[Dict[str, Any]]:
        params = {"all": 1 if include_stopped else 0}
        url = f"{self._base_url(host)}/containers/json"
        response = requests.get(url, params=params, timeout=2.0)
        response.raise_for_status()
        return [self._container_to_row(item) for item in response.json()]

    def fetch_all_containers_parallel(self, *, include_stopped: bool = True) -> Dict[str, Any]:
        hosts = self.current_hosts()
        if not hosts:
            return {}

        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(15, max(1, len(hosts)))) as executor:
            futures = {
                executor.submit(self.fetch_single_host, host, include_stopped=include_stopped): host
                for host in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                host_key = f"{host['name']}#{host['id']}"
                try:
                    containers = future.result()
                    results[host_key] = {
                        "host": host,
                        "containers": containers,
                        "error": None,
                    }
                except Exception as exc:
                    results[host_key] = {
                        "host": host,
                        "containers": [],
                        "error": str(exc),
                    }

        return dict(sorted(results.items(), key=lambda item: item[1]["host"]["name"].lower()))

    _REF_RE = re.compile(r"[A-Za-z0-9._-]+")

    @classmethod
    def _validate_ref(cls, ref: str) -> str:
        # These refs are interpolated directly into Docker agent URLs
        # (/containers/{ref}/...) — reject anything that isn't a plain
        # container id/name to prevent path/URL injection.
        if not cls._REF_RE.fullmatch(ref):
            raise ValueError(f"Invalid container reference: {ref!r}")
        return ref

    @classmethod
    def _container_refs(cls, container_id: str, container_name: str | None = None) -> List[str]:
        refs: List[str] = []
        raw_id = (container_id or "").strip()
        if raw_id:
            refs.append(cls._validate_ref(raw_id))
            if len(raw_id) > 12:
                refs.append(raw_id[:12])
        raw_name = (container_name or "").strip().lstrip("/")
        if raw_name:
            refs.append(cls._validate_ref(raw_name))

        # keep order while removing duplicates
        seen = set()
        unique: List[str] = []
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                unique.append(ref)
        return unique

    def _post_container_endpoint(
        self,
        host: Dict[str, Any],
        refs: List[str],
        endpoint: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout: float = 4.0,
    ) -> tuple[requests.Response, str]:
        if not refs:
            raise ValueError("Missing container reference")

        last_404: requests.Response | None = None
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/{endpoint}"
            response = requests.post(url, params=params, json={}, timeout=timeout)
            if response.status_code == 404:
                last_404 = response
                continue
            return response, ref

        if last_404 is not None:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")
        raise RuntimeError("Docker API request failed")

    def _container_running(self, host: Dict[str, Any], refs: List[str]) -> bool:
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/json"
            response = requests.get(url, timeout=3.0)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            state = (response.json().get("State") or {}).get("Running")
            return bool(state)
        raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")

    def container_action(
        self,
        host_id: int,
        container_id: str,
        action: str,
        container_name: str | None = None,
    ) -> Dict[str, Any]:
        action_map = {
            "start": ("start", {}),
            "stop": ("stop", {"t": 10}),
            "restart": ("restart", {"t": 10}),
        }
        if action not in action_map:
            raise ValueError(f"Unsupported action: {action}")

        host = self._resolve_host(host_id)
        refs = self._container_refs(container_id, container_name)
        endpoint, params = action_map[action]
        response, used_ref = self._post_container_endpoint(host, refs, endpoint, params=params, timeout=6.0)
        if response.status_code not in (200, 204, 304):
            raise RuntimeError(f"Docker API error {response.status_code}: {response.text[:300]}")

        # Some environments immediately restart stopped containers via policies.
        # If stop did not effectively stop the container, send kill as fallback.
        if action == "stop":
            still_running = False
            try:
                for _ in range(5):
                    still_running = self._container_running(host, refs)
                    if not still_running:
                        break
                    time.sleep(0.2)
            except (requests.RequestException, RuntimeError) as exc:
                # A transient Docker API error during the liveness check must not
                # silently mask a still-running container; log and skip the kill
                # fallback rather than assert the container is stopped.
                if self._ctx:
                    self._ctx.log.warning(
                        f"Docker Manager: liveness check after stop failed: {exc}"
                    )
                still_running = False

            if still_running:
                kill_resp, _ = self._post_container_endpoint(host, refs, "kill", timeout=4.0)
                if kill_resp.status_code not in (200, 204, 304, 409):
                    raise RuntimeError(
                        f"Docker API stop/kill fallback failed {kill_resp.status_code}: {kill_resp.text[:300]}"
                    )

        # Runs in a worker thread (REST via to_thread) — _emit hands it to the loop.
        self._emit(
            "docker_manager:container:action",
            {
                "host_id": host_id,
                "container_id": container_id,
                "container_ref": used_ref,
                "action": action,
                "status_code": response.status_code,
            },
        )

        return {
            "ok": True,
            "action": action,
            "host_id": host_id,
            "container_id": container_id,
            "container_ref": used_ref,
            "status_code": response.status_code,
        }

    def container_logs(
        self,
        host_id: int,
        container_id: str,
        *,
        tail: int = 200,
        container_name: str | None = None,
    ) -> str:
        host = self._resolve_host(host_id)
        refs = self._container_refs(container_id, container_name)

        last_404 = False
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/logs"
            response = requests.get(
                url,
                params={"stdout": 1, "stderr": 1, "tail": max(1, min(int(tail), 2000))},
                timeout=6.0,
            )
            if response.status_code == 404:
                last_404 = True
                continue
            response.raise_for_status()
            if response.text:
                return response.text
            if response.content:
                return response.content.decode(errors="replace")
            return "(no logs returned)"

        if last_404:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")
        return "(no logs returned)"

    def container_shell_snapshot(
        self,
        host_id: int,
        container_id: str,
        container_name: str | None = None,
    ) -> str:
        host = self._resolve_host(host_id)
        base_url = self._base_url(host)
        refs = self._container_refs(container_id, container_name)

        commands = [
            ["sh", "-lc", "whoami && pwd && ls -la | head -50"],
            ["/bin/sh", "-lc", "whoami && pwd && ls -la | head -50"],
            ["bash", "-lc", "whoami && pwd && ls -la | head -50"],
            ["/bin/bash", "-lc", "whoami && pwd && ls -la | head -50"],
        ]

        errors: List[str] = []
        saw_exec_forbidden = False

        for ref in refs:
            for cmd in commands:
                create_payload = {
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": True,
                    "Cmd": cmd,
                }
                create_resp = requests.post(
                    f"{base_url}/containers/{ref}/exec",
                    json=create_payload,
                    timeout=5.0,
                )
                if create_resp.status_code == 404:
                    errors.append(f"{ref}: not found")
                    break
                if create_resp.status_code == 409:
                    raise RuntimeError("Container is not running. Start it before opening shell snapshot.")
                if create_resp.status_code >= 400:
                    errors.append(f"{ref}/{cmd[0]}: create failed {create_resp.status_code}")
                    continue

                exec_id = create_resp.json().get("Id")
                if not exec_id:
                    errors.append(f"{ref}/{cmd[0]}: exec id missing")
                    continue

                start_resp = requests.post(
                    f"{base_url}/exec/{exec_id}/start",
                    json={"Detach": False, "Tty": True},
                    timeout=8.0,
                )
                if start_resp.status_code == 403:
                    saw_exec_forbidden = True
                    errors.append(f"{ref}/{cmd[0]}: exec forbidden (403)")
                    continue
                if start_resp.status_code >= 400:
                    errors.append(f"{ref}/{cmd[0]}: start failed {start_resp.status_code}")
                    continue

                if start_resp.text:
                    return start_resp.text
                if start_resp.content:
                    return start_resp.content.decode(errors="replace")
                return "(shell command returned no output)"

        if saw_exec_forbidden:
            return self._read_only_runtime_snapshot(host, refs)

        details = "; ".join(errors[-6:]) if errors else "unknown error"
        raise RuntimeError(f"Shell snapshot failed: {details}")

    def _read_only_runtime_snapshot(self, host: Dict[str, Any], refs: List[str]) -> str:
        """Fallback when /exec is blocked by a Docker socket proxy.

        Uses only read-only endpoints to provide useful diagnostics.
        """
        lines: List[str] = []
        lines.append("Docker proxy denied exec (HTTP 403).")
        lines.append("Showing read-only runtime snapshot instead.\n")

        selected_ref = refs[0] if refs else ""
        inspect_data = None
        for ref in refs:
            inspect_resp = requests.get(f"{self._base_url(host)}/containers/{ref}/json", timeout=4.0)
            if inspect_resp.status_code == 404:
                continue
            inspect_resp.raise_for_status()
            inspect_data = inspect_resp.json()
            selected_ref = ref
            break

        if inspect_data is None:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")

        name = (inspect_data.get("Name") or "").lstrip("/") or selected_ref
        image = inspect_data.get("Config", {}).get("Image", "unknown")
        state = inspect_data.get("State", {})
        status = state.get("Status", "unknown")
        running = state.get("Running", False)
        started_at = state.get("StartedAt", "unknown")
        pid = state.get("Pid", "unknown")

        lines.append(f"Container: {name}")
        lines.append(f"Reference: {selected_ref}")
        lines.append(f"Image: {image}")
        lines.append(f"Status: {status} (running={running})")
        lines.append(f"PID: {pid}")
        lines.append(f"StartedAt: {started_at}\n")

        top_resp = requests.get(
            f"{self._base_url(host)}/containers/{selected_ref}/top",
            params={"ps_args": "aux"},
            timeout=5.0,
        )
        if top_resp.status_code in (200, 201):
            data = top_resp.json() if top_resp.content else {}
            titles = data.get("Titles") or []
            processes = data.get("Processes") or []
            if titles and processes:
                lines.append("Processes:")
                lines.append(" | ".join(titles))
                for row in processes[:30]:
                    lines.append(" | ".join(str(item) for item in row))
            else:
                lines.append("Processes: no process data returned")
        elif top_resp.status_code == 404:
            lines.append("Processes: container not found for /top")
        elif top_resp.status_code == 403:
            lines.append("Processes: /top also forbidden by proxy (403)")
        else:
            lines.append(f"Processes: /top failed with HTTP {top_resp.status_code}")

        return "\n".join(lines)


docker_manager_service = DockerManagerService()
