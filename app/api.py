"""REST API for Docker Manager.

A single router is mounted by core under ``/api/plugins/lyndrix.plugin.docker/``
via ``ctx.register_routes()``. The registry enforces authentication for every
route automatically; we additionally require ``api:read`` on reads and
``api:write`` on mutations so a merely-authenticated user cannot start/stop
containers or edit hosts without the write permission.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from .controller.service import DockerManagerService


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
            host = service.upsert_host(payload.model_dump())
            return {"host": host, "hosts": service.list_hosts()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hosts/set")
    async def set_hosts(
        payload: DockerHostsSetPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            hosts = service.replace_hosts([h.model_dump() for h in payload.hosts])
            return {"hosts": hosts}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/hosts/{host_id}")
    async def delete_host(
        host_id: int,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        if not service.delete_host(host_id):
            raise HTTPException(status_code=404, detail=f"Unknown host id: {host_id}")
        return {"ok": True, "hosts": service.list_hosts()}

    # ── Containers ───────────────────────────────────────────────────────────
    @router.get("/containers")
    async def list_containers(
        include_stopped: bool = True,
        identity: ApiIdentity = Depends(require_permission("api:read")),
    ):
        return service.fetch_all_containers_parallel(include_stopped=include_stopped)

    @router.post("/containers/action")
    async def container_action(
        payload: ContainerActionPayload,
        identity: ApiIdentity = Depends(require_permission("api:write")),
    ):
        try:
            return service.container_action(payload.host_id, payload.container_id, payload.action)
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
                "logs": service.container_logs(host_id, container_id, tail=tail),
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
        try:
            return {
                "host_id": host_id,
                "container_id": container_id,
                "output": service.container_shell_snapshot(host_id, container_id),
            }
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
