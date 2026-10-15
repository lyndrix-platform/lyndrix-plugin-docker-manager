"""lyndrix-docker-manager — plugin entrypoint.

Architecture
------------
  app/logic/service.py        — host persistence + Docker proxy runtime actions
  app/ui/nicegui/overview.py  — container overview and runtime controls
  app/ui/nicegui/settings.py  — host management for Plugin Manager settings
  app/ui/nicegui/widget.py    — compact dashboard widget
  app/ui/react/               — React frontend (canonical migration target)
  app/api.py                  — REST API for host and container runtime operations

  app/model/                  — docker_manager_hosts table + cluster-safe DB helpers

Event bus topics
----------------
  subscribe: docker_manager:hosts:set | docker_manager:host:upsert |
             docker_manager:host:delete | docker_manager:hosts:get |
             docker_manager:hosts_updated (cluster) | iac:inventory_updated
  emit:      docker_manager:hosts_updated (cluster=True) |
             docker_manager:hosts:updated (local, full list) |
             docker_manager:hosts:state | docker_manager:container:action

Cluster (core API >= 1.7): hosts live in the shared DB table, every instance
serves the REST API from it; the reachability probe runs on one instance under
the "docker_manager:prober" lease and health() only reads its results.
"""
import asyncio
import time

from nicegui import ui

from core.api import ModuleManifest, PluginHealthStatus, db_instance

try:
    from ui.layout import main_layout
except ImportError:
    def main_layout(_title):  # type: ignore
        def _decorator(fn):
            return fn
        return _decorator

from .app.api import build_plugin_router
from .app.logic.service import DEFAULT_PROBE_INTERVAL
from .app.logic.service import docker_manager_service as svc
from .app.ui.nicegui.overview import render_overview_ui
from .app.ui.nicegui.settings import render_settings_ui as _render_settings_ui
from .app.ui.nicegui.widget import render_dashboard_widget as _render_widget


# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id="lyndrix.plugin.docker",
    name="Docker Manager",
    version="1.2.0",
    description="Docker proxy monitoring with runtime controls (start/stop/restart/logs/shell).",
    author="Lyndrix",
    icon="view_in_ar",
    type="PLUGIN",
    min_core_version="0.3.0",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager",
    ui_route="/docker",
    react_ui=True,
    # i18next-shaped namespace served to the React UI; core auto-registers
    # locales/docker.<locale>.json and adds "docker" to the client allowlist.
    i18n_namespace="docker",
    react_routes=[
        {
            "path": "/docker",
            "label": "Docker Manager",
            "icon": "view_in_ar",
            "sidebar_visible": True,
        },
        {
            "path": "/docker/settings",
            "label": "Docker Manager Einstellungen",
            "icon": "settings",
            "sidebar_visible": False,
        },
    ],
    settings_ui_route="/docker/settings",
    # Remote Docker proxies over HTTP — no local socket, so any instance may serve.
    placement="any",
    settings_schema=[
        {"key": "probe_interval_seconds", "label": "Reachability probe interval (seconds)",
         "kind": "int", "category": "Probing", "default": str(DEFAULT_PROBE_INTERVAL),
         "description": "How often one instance (under the docker_manager:prober lease) checks every "
                        "Docker proxy and stores the result for health(). Minimum 10."},
    ],
    permissions={
        "subscribe": [
            "vault:ready_for_data",
            "db:connected",
            "plugin:settings_changed",
            "iac:inventory_updated",
            "docker_manager:hosts_updated",
            "docker_manager:hosts:set",
            "docker_manager:host:upsert",
            "docker_manager:host:delete",
            "docker_manager:hosts:get",
        ],
        "emit": [
            "docker_manager:hosts_updated",
            "docker_manager:hosts:updated",
            "docker_manager:hosts:state",
            "docker_manager:container:action",
        ],
    },
    # Identity & Permissions 2.0: dedicated exec permission (container shell
    # access is more sensitive than a plain write) + roles auto-mapped onto
    # groups. Role permissions are fully qualified (plugin:<id>:api:*) rather
    # than the bare global api:read/write, so granting them only ever affects
    # this plugin.
    custom_permissions=[
        {"id": "docker:exec", "label": "Container Shell Exec",
         "description": "Open an interactive shell session inside a managed container",
         "icon": "terminal"},
    ],
    roles=[
        {"id": "admin", "label": "Docker Manager Administrator",
         "permissions": [
             "plugin:lyndrix.plugin.docker:api:read",
             "plugin:lyndrix.plugin.docker:api:write",
             "plugin:lyndrix.plugin.docker:docker:exec",
         ],
         "auto_map_groups": ["INT_ADMIN"],
         "description": "Full control including container shell exec."},
        {"id": "operator", "label": "Docker Manager Operator",
         "permissions": ["plugin:lyndrix.plugin.docker:api:read", "plugin:lyndrix.plugin.docker:api:write"],
         "auto_map_groups": [],
         "description": "Manage hosts and containers, no shell exec."},
        {"id": "viewer", "label": "Docker Manager Viewer",
         "permissions": ["plugin:lyndrix.plugin.docker:api:read"],
         "auto_map_groups": [],
         "description": "Read-only access to hosts and containers."},
    ],
)


# ── Public plugin API ─────────────────────────────────────────────────────────

def render_settings_ui(ctx):
    _render_settings_ui(ctx, svc)


def render_dashboard_widget(ctx):
    _render_widget(ctx, svc)


# ── Health ────────────────────────────────────────────────────────────────────

async def health(ctx) -> PluginHealthStatus:
    """Cheap health: reads the prober's last results from the DB (one query).

    Never probes — the leased background prober (one instance) does that, so a
    dashboard poll costs nothing and every instance reports the same view.
    Unreachable proxies → ``degraded``; a standby instance (no prober) is healthy.
    """
    start = time.perf_counter()
    try:
        result = await asyncio.to_thread(svc.health_snapshot)
    except Exception as exc:
        result = {"status": "degraded", "details": {"reason": "health_query_failed", "error": str(exc)}}
    return PluginHealthStatus(
        status=result["status"],
        details=result["details"],
        latency_ms=round((time.perf_counter() - start) * 1000, 1),
    )


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup(ctx):
    ctx.log.info("Docker Manager: setup started")
    svc.set_context(ctx)

    # Single auth'd router, mounted via the registry at
    # /api/plugins/lyndrix.plugin.docker/ on EVERY instance; hosts come from the
    # shared DB table (registry enforces authentication; routes add
    # api:read / api:write for authorization).
    ctx.register_routes(build_plugin_router(svc))

    create_task = getattr(ctx, "create_task", None) or asyncio.create_task
    if getattr(db_instance, "is_connected", False):
        # Plugin (re)loaded after boot: db:connected and vault:ready_for_data
        # already fired (the DB credentials come from the unsealed Vault).
        svc.mark_vault_ready()
        create_task(svc.ensure_ready(), name="docker_manager:bootstrap")
        create_task(asyncio.to_thread(svc.reload_settings), name="docker_manager:settings")

    # Reachability probe on exactly one instance (immediately in single mode).
    on_lease = getattr(ctx, "on_lease", None)
    if callable(on_lease):
        on_lease("docker_manager:prober", acquired=svc.start_prober, lost=svc.stop_prober)
    else:  # core < 1.7
        create_task(svc.start_prober(), name="docker_manager:prober-start")

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload=None):
        await svc.ensure_ready()

    @ctx.subscribe("vault:ready_for_data")
    async def _on_vault_ready(payload=None):
        svc.mark_vault_ready()
        await asyncio.to_thread(svc.reload_settings)
        await svc.ensure_ready()  # one-time import of the legacy Vault host list

    @ctx.subscribe("plugin:settings_changed")
    async def _on_settings_changed(payload=None):
        if (payload or {}).get("plugin_id") == manifest.id:
            await asyncio.to_thread(svc.reload_settings)

    @ctx.subscribe("docker_manager:hosts_updated")
    async def _on_hosts_updated(payload):
        await svc.on_hosts_updated(payload)  # peers only (``_origin``)

    @ctx.subscribe("iac:inventory_updated")
    async def _on_iac_inventory(payload):
        """Auto-register docker hosts published by the IaC Orchestrator."""
        try:
            stats = await svc.on_iac_inventory(payload)
        except Exception as exc:
            ctx.log.error(f"Docker Manager: IaC inventory sync failed: {exc}")
            return
        if stats:
            ctx.log.info(
                f"Docker Manager: IaC inventory sync — {stats['added']} added, "
                f"{stats['updated']} updated, {stats['total']} total."
            )

    @ctx.subscribe("docker_manager:hosts:set")
    async def _on_hosts_set(payload):
        hosts = payload.get("hosts") if isinstance(payload, dict) else payload
        try:
            await asyncio.to_thread(svc.replace_hosts, hosts if isinstance(hosts, list) else [])
            ctx.log.info("Docker Manager: host list replaced via EventBus")
        except Exception as exc:
            ctx.log.error(f"Docker Manager: hosts:set failed: {exc}")

    @ctx.subscribe("docker_manager:host:upsert")
    async def _on_host_upsert(payload):
        if not isinstance(payload, dict):
            ctx.log.warning("Docker Manager: host:upsert ignored non-dict payload")
            return
        try:
            await asyncio.to_thread(svc.upsert_host, payload)
            ctx.log.info("Docker Manager: host upserted via EventBus")
        except Exception as exc:
            ctx.log.error(f"Docker Manager: host:upsert failed: {exc}")

    @ctx.subscribe("docker_manager:host:delete")
    async def _on_host_delete(payload):
        host_id = payload.get("id") if isinstance(payload, dict) else None
        if host_id is None:
            ctx.log.warning("Docker Manager: host:delete needs a dict payload with 'id'")
            return
        try:
            removed = await asyncio.to_thread(svc.delete_host, int(host_id))
        except Exception as exc:
            ctx.log.error(f"Docker Manager: host:delete failed: {exc}")
            return
        if removed:
            ctx.log.info(f"Docker Manager: host {host_id} removed via EventBus")
        else:
            ctx.log.warning(f"Docker Manager: host {host_id} not found for deletion")

    @ctx.subscribe("docker_manager:hosts:get")
    async def _on_hosts_get(payload):
        response = {"hosts": await asyncio.to_thread(svc.current_hosts)}
        if isinstance(payload, dict) and "correlation_id" in payload:
            response["correlation_id"] = payload["correlation_id"]
        ctx.emit("docker_manager:hosts:state", response)

    @ui.page("/docker")
    @main_layout("Docker Manager")
    async def docker_page():
        render_overview_ui(ctx, svc)

    ctx.log.info("Docker Manager: setup complete")


async def teardown(ctx):
    """Plugin disabled/unloaded: stop the prober (the core also releases the lease)."""
    await svc.stop_prober()
