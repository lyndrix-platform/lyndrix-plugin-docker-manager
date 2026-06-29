"""lyndrix-docker-manager — plugin entrypoint.

Architecture
------------
  app/logic/service.py        — host persistence + Docker proxy runtime actions
  app/ui/nicegui/overview.py  — container overview and runtime controls
  app/ui/nicegui/settings.py  — host management for Plugin Manager settings
  app/ui/nicegui/widget.py    — compact dashboard widget
  app/ui/react/               — React frontend (canonical migration target)
  app/api.py                  — REST API for host and container runtime operations

Event bus topics
----------------
  subscribe: docker_manager:hosts:set | docker_manager:host:upsert |
             docker_manager:host:delete | docker_manager:hosts:get
  emit:      docker_manager:hosts:updated | docker_manager:hosts:state |
             docker_manager:container:action
"""
import asyncio
import time

from nicegui import ui

from core.api import ModuleManifest, PluginHealthStatus

try:
    from ui.layout import main_layout
except ImportError:
    def main_layout(_title):  # type: ignore
        def _decorator(fn):
            return fn
        return _decorator

from .app.api import build_plugin_router
from .app.logic.service import docker_manager_service as svc
from .app.ui.nicegui.overview import render_overview_ui
from .app.ui.nicegui.settings import render_settings_ui as _render_settings_ui
from .app.ui.nicegui.widget import render_dashboard_widget as _render_widget


# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id="lyndrix.plugin.docker",
    name="Docker Manager",
    version="0.4.0",
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
    permissions={
        "subscribe": [
            "vault:ready_for_data",
            "iac:inventory_updated",
            "docker_manager:hosts:set",
            "docker_manager:host:upsert",
            "docker_manager:host:delete",
            "docker_manager:hosts:get",
        ],
        "emit": [
            "docker_manager:hosts:updated",
            "docker_manager:hosts:state",
            "docker_manager:container:action",
        ],
    },
)


# ── Public plugin API ─────────────────────────────────────────────────────────

def render_settings_ui(ctx):
    _render_settings_ui(ctx, svc)


def render_dashboard_widget(ctx):
    _render_widget(ctx, svc)


# ── Health ────────────────────────────────────────────────────────────────────

async def health(ctx) -> PluginHealthStatus:
    """Functional health probe.

    This plugin's whole job is proxying remote Docker daemons, so a meaningful
    probe actually *talks to* them: it issues the same ``/containers/json`` call
    the UI uses (2 s timeout) against every configured host, in parallel, and
    grades on real reachability. "Setup ran" is not enough — a host that is
    down or mis-addressed must show up here. The sync ``requests`` calls are
    offloaded so the probe never blocks the event loop.
    """
    start = time.perf_counter()

    hosts = svc.list_hosts()
    if not hosts:
        # Nothing to manage yet — the plugin is inert, not broken.
        return PluginHealthStatus(
            status="degraded",
            details={"reason": "no_hosts_configured", "hosts_total": 0},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )

    async def _probe(host: dict):
        label = host.get("name") or f"{host.get('ip')}:{host.get('port')}"
        try:
            await asyncio.to_thread(svc.fetch_single_host, host)
            return label, True, None
        except Exception as exc:
            return label, False, str(exc)

    results = await asyncio.gather(*(_probe(h) for h in hosts))
    reachable = [name for name, ok, _ in results if ok]
    unreachable = {name: err for name, ok, err in results if not ok}
    latency = round((time.perf_counter() - start) * 1000, 1)

    details = {
        "hosts_total": len(hosts),
        "hosts_reachable": len(reachable),
        "unreachable": unreachable,
    }
    if not reachable:
        return PluginHealthStatus(
            status="error",
            details={**details, "reason": "all_hosts_unreachable"},
            latency_ms=latency,
        )
    if unreachable:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "some_hosts_unreachable"},
            latency_ms=latency,
        )
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)


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

    @ctx.subscribe("iac:inventory_updated")
    async def _on_iac_inventory(payload):
        """Auto-register docker hosts published by the IaC Orchestrator.

        Filtering for docker hosts lives here, not in the orchestrator, so both
        plugins stay fully independent — the orchestrator broadcasts generic host
        data; this plugin decides what to do with it.
        """
        hosts = payload.get("hosts") or [] if isinstance(payload, dict) else []
        docker_hosts = [
            {"name": h["name"], "ip": h["ip"], "port": 2375, "scheme": "http"}
            for h in hosts
            if h.get("ip") and (
                "docker_hosts" in (h.get("groups") or [])
                or "docker" in (h.get("baseline_roles") or [])
            )
        ]
        if not docker_hosts:
            return
        try:
            stats = await asyncio.to_thread(svc.sync_orchestrator_hosts, docker_hosts)
            ctx.log.info(
                f"Docker Manager: IaC inventory sync — "
                f"{stats['added']} added, {stats['updated']} updated, "
                f"{stats['total']} total."
            )
        except Exception as exc:
            ctx.log.error(f"Docker Manager: IaC inventory sync failed: {exc}")

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