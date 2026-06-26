"""lyndrix-docker-manager — plugin entrypoint.

Architecture
------------
  app/controller/service.py — host persistence + Docker proxy runtime actions
  app/ui/overview.py        — container overview and runtime controls
  app/ui/settings.py        — host management for Plugin Manager settings
  app/ui/widget.py          — compact dashboard widget
  app/api.py                — REST API for host and container runtime operations

Event bus topics
----------------
  subscribe: docker_manager:hosts:set | docker_manager:host:upsert |
             docker_manager:host:delete | docker_manager:hosts:get
  emit:      docker_manager:hosts:updated | docker_manager:hosts:state |
             docker_manager:container:action
"""
from nicegui import ui

from core.components.plugins.logic.models import ModuleManifest

try:
    from ui.layout import main_layout
except ImportError:
    def main_layout(_title):  # type: ignore
        def _decorator(fn):
            return fn
        return _decorator

from .app.api import build_plugin_router
from .app.controller.service import docker_manager_service as svc
from .app.ui.overview import render_overview_ui
from .app.ui.settings import render_settings_ui as _render_settings_ui
from .app.ui.widget import render_dashboard_widget as _render_widget


# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id="lyndrix.plugin.docker",
    name="Docker Manager",
    version="0.1.0",
    description="Docker proxy monitoring with runtime controls (start/stop/restart/logs/shell).",
    author="Lyndrix",
    icon="view_in_ar",
    type="PLUGIN",
    min_core_version="0.1.1",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager",
    ui_route="/docker",
    react_ui=True,
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
    permissions={
        "subscribe": [
            "vault:ready_for_data",
            "docker_manager:hosts:set",
            "docker_manager:host:upsert",
            "docker_manager:host:delete",
            "docker_manager:hosts:get",
        ],
        "emit": [
            "docker_manager:hosts:updated",
            "docker_manager:hosts:state",
            "docker_manager:container:action",
            "messaging:outbound",
        ],
    },
)


# ── Public plugin API ─────────────────────────────────────────────────────────

def render_settings_ui(ctx):
    _render_settings_ui(ctx, svc)


def render_dashboard_widget(ctx):
    _render_widget(ctx, svc)


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup(ctx):
    ctx.log.info("Docker Manager: setup started")
    svc.set_context(ctx)

    # Single auth'd router, mounted via the registry at
    # /api/plugins/lyndrix.plugin.docker/ (registry enforces authentication;
    # routes add api:read / api:write for authorization).
    ctx.register_routes(build_plugin_router(svc))

    @ctx.subscribe("vault:ready_for_data")
    async def _on_vault_ready(payload=None):
        del payload
        count = svc.load_hosts_from_vault()
        ctx.log.info(f"Docker Manager: loaded {count} host(s) from Vault")

    @ctx.subscribe("docker_manager:hosts:set")
    async def _on_hosts_set(payload):
        hosts = payload.get("hosts") if isinstance(payload, dict) else payload
        try:
            svc.replace_hosts(hosts if isinstance(hosts, list) else [])
            ctx.log.info("Docker Manager: host list replaced via EventBus")
        except Exception as exc:
            ctx.log.error(f"Docker Manager: hosts:set failed: {exc}")

    @ctx.subscribe("docker_manager:host:upsert")
    async def _on_host_upsert(payload):
        if not isinstance(payload, dict):
            ctx.log.warning("Docker Manager: host:upsert ignored non-dict payload")
            return
        try:
            svc.upsert_host(payload)
            ctx.log.info("Docker Manager: host upserted via EventBus")
        except Exception as exc:
            ctx.log.error(f"Docker Manager: host:upsert failed: {exc}")

    @ctx.subscribe("docker_manager:host:delete")
    async def _on_host_delete(payload):
        if not isinstance(payload, dict):
            ctx.log.warning("Docker Manager: host:delete ignored non-dict payload")
            return
        host_id = payload.get("id")
        if host_id is None:
            ctx.log.warning("Docker Manager: host:delete missing 'id'")
            return
        removed = svc.delete_host(int(host_id))
        if removed:
            ctx.log.info(f"Docker Manager: host {host_id} removed via EventBus")
        else:
            ctx.log.warning(f"Docker Manager: host {host_id} not found for deletion")

    @ctx.subscribe("docker_manager:hosts:get")
    async def _on_hosts_get(payload):
        response = {"hosts": svc.list_hosts()}
        if isinstance(payload, dict) and "correlation_id" in payload:
            response["correlation_id"] = payload["correlation_id"]
        ctx.emit("docker_manager:hosts:state", response)

    @ui.page("/docker")
    @main_layout("Docker Manager")
    async def docker_page():
        render_overview_ui(ctx, svc)

    ctx.log.info("Docker Manager: setup complete")