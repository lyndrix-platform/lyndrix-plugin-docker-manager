"""REST API for Docker Manager.

A single router is mounted by core under ``/api/plugins/lyndrix.plugin.docker/``
via ``ctx.register_routes()``. The registry enforces authentication for every
route automatically. Authorization (see ``app/permissions.py`` for the why):

  api:read        list hosts and containers (status only) — every viewer
  docker:logs     container logs
  docker:control  start / stop / restart
  docker:hosts    host registry writes
  docker:exec     shell snapshot

The four ``docker:*`` permissions get no baseline grant and no global
``api:read``/``api:write`` fallback, so a merely-authenticated user can look
but not read logs, control containers or redirect the proxy targets.
"""
from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from .logic.service import DockerManagerService
from .permissions import (  # noqa: F401  (PLUGIN_ID/PERM_WRITE re-exported for callers)
    CAPABILITIES,
    PERM_CONTROL,
    PERM_EXEC,
    PERM_HOSTS,
    PERM_LOGS,
    PERM_READ,
    PERM_WRITE,
    PLUGIN_ID,
)


class DockerHostPayload(BaseModel):
    id: Optional[int] = Field(default=None)
    name: str
    ip: str
    port: int = Field(default=2375)
    scheme: str = Field(default="http")


class DockerHostsSetPayload(BaseModel):
    hosts: List[DockerHostPayload]


class DockerHostSyncEntry(BaseModel):
    """Minimal host descriptor used for automated upserts (no id field).

    ``port``/``scheme`` are optional on purpose: omitted means "not mine" —
    new hosts get 2375/http, existing hosts keep what they have.
    """
    name: str
    ip: str
    port: Optional[int] = Field(default=None)
    scheme: Optional[str] = Field(default=None)


class DockerHostSyncPayload(BaseModel):
    hosts: List[DockerHostSyncEntry]


class ContainerActionPayload(BaseModel):
    host_id: int
    container_id: str
    action: Literal["start", "stop", "restart"]


def build_plugin_router(service: DockerManagerService) -> APIRouter:
    """The single Docker Manager router — core mounts it at /api/plugins/<id>/."""
    router = APIRouter(tags=["Docker Manager"])

    # ── Capabilities ─────────────────────────────────────────────────────────
    @router.get("/capabilities")
    async def capabilities(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        """What the caller may do beyond reading — lets a UI hide or disable
        controls instead of offering buttons that answer 403."""
        return {name: bool(identity.allows(perm)) for name, perm in CAPABILITIES.items()}

    # ── Hosts ────────────────────────────────────────────────────────────────
    @router.get("/hosts")
    async def list_hosts(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        # Cached per instance; re-read from the shared table when stale.
        return {"hosts": await asyncio.to_thread(service.current_hosts)}

    @router.post("/hosts")
    async def upsert_host(
        payload: DockerHostPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_HOSTS)),
    ):
        try:
            # Without an id the service upserts by name. Runs off the event
            # loop because it writes the shared DB table (blocking I/O).
            host = await asyncio.to_thread(
                service.upsert_host, payload.model_dump(exclude_none=True)
            )
            return {"host": host, "hosts": service.list_hosts()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:  # database not connected
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hosts/sync")
    async def sync_hosts(
        payload: DockerHostSyncPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_HOSTS)),
    ):
        """Upsert hosts by name without removing unrecognized entries.

        Intended for automated callers (IaC Orchestrator, Ansible, scripts).
        Hosts present in the current list but absent from ``payload.hosts``
        are left untouched — unlike ``/hosts/set`` which replaces everything.
        New hosts get ``source: "iac"``. Existing ``iac`` hosts follow the
        address (and an explicitly stated port/scheme); manually configured
        hosts keep all their connection settings.
        """
        try:
            stats = await asyncio.to_thread(
                service.sync_orchestrator_hosts,
                [h.model_dump(exclude_none=True) for h in payload.hosts],
            )
            return {"ok": True, **stats, "hosts": service.list_hosts()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/hosts/set")
    async def set_hosts(
        payload: DockerHostsSetPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_HOSTS)),
    ):
        try:
            hosts = await asyncio.to_thread(
                service.replace_hosts, [h.model_dump() for h in payload.hosts]
            )
            return {"hosts": hosts}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.delete("/hosts/{host_id}")
    async def delete_host(
        host_id: int,
        identity: ApiIdentity = Depends(require_permission(PERM_HOSTS)),
    ):
        try:
            removed = await asyncio.to_thread(service.delete_host, host_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail=f"Unknown host id: {host_id}")
        return {"ok": True, "hosts": service.list_hosts()}

    # ── Containers ───────────────────────────────────────────────────────────
    @router.get("/containers")
    async def list_containers(
        include_stopped: bool = True,
        identity: ApiIdentity = Depends(require_permission(PERM_READ)),
    ):
        # fetch_all_containers_parallel blocks on synchronous network I/O
        # (ThreadPoolExecutor + requests); keep it off the shared event loop.
        return await asyncio.to_thread(
            service.fetch_all_containers_parallel, include_stopped=include_stopped
        )

    @router.post("/containers/action")
    async def container_action(
        payload: ContainerActionPayload,
        identity: ApiIdentity = Depends(require_permission(PERM_CONTROL)),
    ):
        try:
            return await asyncio.to_thread(
                service.container_action,
                payload.host_id,
                payload.container_id,
                payload.action,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/containers/{host_id}/{container_id}/logs")
    async def container_logs(
        host_id: int,
        container_id: str,
        tail: int = 200,
        identity: ApiIdentity = Depends(require_permission(PERM_LOGS)),
    ):
        try:
            return {
                "host_id": host_id,
                "container_id": container_id,
                "tail": tail,
                "logs": await asyncio.to_thread(
                    service.container_logs, host_id, container_id, tail=tail
                ),
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/containers/{host_id}/{container_id}/shell")
    async def container_shell(
        host_id: int,
        container_id: str,
        identity: ApiIdentity = Depends(require_permission(PERM_EXEC)),
    ):
        # SECURITY: this runs a Docker `exec` inside the target container, i.e.
        # in-container code execution across the reachable fleet. Gated on the
        # dedicated docker:exec permission — the core's global api:write compat
        # fallback only covers plain :api:read/:api:write suffixes, so holding
        # the generic api:write permission alone does NOT satisfy this check.
        try:
            return {
                "host_id": host_id,
                "container_id": container_id,
                "output": await asyncio.to_thread(
                    service.container_shell_snapshot, host_id, container_id
                ),
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
