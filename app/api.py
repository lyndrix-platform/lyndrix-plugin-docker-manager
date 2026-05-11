"""REST API for Docker Manager."""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


def build_router(service: DockerManagerService) -> APIRouter:
    router = APIRouter(prefix="/api/docker-manager", tags=["Docker Manager"])

    @router.get("/hosts")
    async def list_hosts():
        return {"hosts": service.list_hosts()}

    @router.post("/hosts")
    async def upsert_host(payload: DockerHostPayload):
        try:
            host = service.upsert_host(payload.model_dump())
            return {"host": host, "hosts": service.list_hosts()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hosts/set")
    async def set_hosts(payload: DockerHostsSetPayload):
        try:
            hosts = service.replace_hosts([h.model_dump() for h in payload.hosts])
            return {"hosts": hosts}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/hosts/{host_id}")
    async def delete_host(host_id: int):
        if not service.delete_host(host_id):
            raise HTTPException(status_code=404, detail=f"Unknown host id: {host_id}")
        return {"ok": True, "hosts": service.list_hosts()}

    @router.get("/containers")
    async def list_containers(include_stopped: bool = True):
        return service.fetch_all_containers_parallel(include_stopped=include_stopped)

    @router.post("/containers/action")
    async def container_action(payload: ContainerActionPayload):
        try:
            return service.container_action(payload.host_id, payload.container_id, payload.action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/containers/{host_id}/{container_id}/logs")
    async def container_logs(host_id: int, container_id: str, tail: int = 200):
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
    async def container_shell(host_id: int, container_id: str):
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


def register_api_routes(fastapi_app, router: APIRouter) -> None:
    api_prefix = "/api/docker-manager"
    routes = list(fastapi_app.router.routes)
    existing = [r for r in routes if getattr(r, "path", "").startswith(api_prefix)]

    if not existing:
        fastapi_app.include_router(router)
        routes = list(fastapi_app.router.routes)
        existing = [r for r in routes if getattr(r, "path", "").startswith(api_prefix)]

    if not existing:
        return

    remaining = [r for r in routes if r not in existing]
    root_idx = next(
        (i for i, r in enumerate(remaining) if getattr(r, "path", None) == ""),
        len(remaining),
    )
    fastapi_app.router.routes = remaining[:root_idx] + existing + remaining[root_idx:]
    fastapi_app.openapi_schema = None
