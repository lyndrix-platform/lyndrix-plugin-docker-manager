"""REST API for Docker Manager.

A single router is mounted by core under ``/api/plugins/lyndrix.plugin.docker/``
via ``ctx.register_routes()``. The registry enforces authentication for every
route automatically; we additionally require ``api:read`` on reads and
``api:write`` on mutations so a merely-authenticated user cannot start/stop
containers or edit hosts without the write permission.
"""
from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from .logic.service import DockerManagerService


class DockerHostPayload(BaseModel):
    id: Optional[int] = Field(default=None)
    name: str
    ip: str
    port: int = Field(default=2375)
    scheme: str = Field(default="http")


class DockerHostsSetPayload(BaseModel):
    hosts: List[DockerHostPayload]


class ContainerActionPayload(BaseModel):
    host_id: int
    container_id: str
    action: Literal["start", "stop", "restart"]


def build_plugin_router(service: DockerManagerService) -> APIRouter:
    """The single Docker Manager router — core mounts it at /api/plugins/<id>/."""
    router = APIRouter(tags=["Docker Manager"])

    # ── Hosts ────────────────────────────────────────────────────────────────
    @router.get("/hosts")
    async def list_hosts(identity: ApiIdentity = Depends(require_permission("api:read"))):
        return {"hosts": service.list_hosts()}

    @router.post("/hosts")
    async def upsert_host(
        payload: DockerHostPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            # Drop a null id so the service treats this as a create (it requires
            # id to be absent, not None, for new hosts). Runs off the event loop
            # because it persists to Vault (blocking I/O).
            host = await asyncio.to_thread(
                service.upsert_host, payload.model_dump(exclude_none=True)
            )
            return {"host": host, "hosts": service.list_hosts()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hosts/set")
    async def set_hosts(
        payload: DockerHostsSetPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            hosts = await asyncio.to_thread(
                service.replace_hosts, [h.model_dump() for h in payload.hosts]
            )
            return {"hosts": hosts}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/hosts/{host_id}")
    async def delete_host(
        host_id: int,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        if not await asyncio.to_thread(service.delete_host, host_id):
            raise HTTPException(status_code=404, detail=f"Unknown host id: {host_id}")
        return {"ok": True, "hosts": service.list_hosts()}

    # ── Containers ───────────────────────────────────────────────────────────
    @router.get("/containers")
    async def list_containers(
        include_stopped: bool = True,
        identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        # fetch_all_containers_parallel blocks on synchronous network I/O
        # (ThreadPoolExecutor + requests); keep it off the shared event loop.
        return await asyncio.to_thread(
            service.fetch_all_containers_parallel, include_stopped=include_stopped
        )

    @router.post("/containers/action")
    async def container_action(
        payload: ContainerActionPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
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
        identity: ApiIdentity = Depends(require_permission("api:read")),
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
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        # SECURITY: this runs a Docker `exec` inside the target container, i.e.
        # in-container code execution across the reachable fleet. It is gated on
        # the generic `api:write` permission; the read-only fallback handles
        # socket proxies that block /exec (HTTP 403).
        # TODO(agent): gate behind a dedicated stronger permission (e.g.
        # "docker:exec") once the core permission registry supports plugin-scoped
        # permissions, rather than the generic api:write grant.
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
