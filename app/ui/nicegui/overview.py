"""Main overview page for Docker Manager."""
from __future__ import annotations

from nicegui import run, ui

from core.api import UIStyles

from ...logic.service import DockerManagerService

# Docker container state -> shared `.lx-badge` modifier (Theming v2 T0b).
# Keeps full state semantics: running/exited-dead/paused are distinct, anything
# else (created, restarting, removing, ...) falls back to "unknown" rather than
# collapsing onto the down/red badge.
_STATE_BADGE_CLASS = {
    "running": UIStyles.BADGE_STATE_UP,
    "exited": UIStyles.BADGE_STATE_DOWN,
    "dead": UIStyles.BADGE_STATE_DOWN,
    "paused": UIStyles.BADGE_STATE_PAUSED,
}


def _state_badge(state: str) -> None:
    badge_class = _STATE_BADGE_CLASS.get(state, UIStyles.BADGE_STATE_UNKNOWN)
    with ui.element("span").classes(badge_class):
        ui.element("span").classes("lx-dot")
        ui.label(state)


def render_overview_ui(ctx, service: DockerManagerService):
    with ui.row().classes("w-full justify-between items-center mb-4"):
        with ui.column().classes("gap-0"):
            ui.label("Docker Proxy Fleet").classes("text-2xl font-bold text-[var(--lx-text)]")
            ui.label("Runtime controls only: start/stop/restart, logs, shell snapshot.").classes(
                "text-xs text-[var(--lx-text-muted)]"
            )
        with ui.row().classes("items-center gap-2"):
            ui.button(
                "Settings",
                icon="settings",
                on_click=lambda: ui.navigate.to("/docker/settings"),
            ).props("flat")
            refresh_btn = ui.button("Refresh", icon="sync", color="primary").props("unelevated")

    container_wrapper = ui.column().classes("w-full gap-4")

    async def _do_action(action: str, host_id: int, container_id: str, container_name: str):
        try:
            refresh_btn.props("loading")
            await run.io_bound(service.container_action, host_id, container_id, action, container_name)
            ui.notify(f"{action.title()} sent to {container_name}.", type="positive")
            await refresh_containers()
        except Exception as exc:
            ui.notify(f"Action failed: {exc}", type="negative")
        finally:
            refresh_btn.props(remove="loading")

    async def _show_logs(host_id: int, container_id: str, container_name: str):
        try:
            logs = await run.io_bound(
                lambda: service.container_logs(
                    host_id,
                    container_id,
                    tail=200,
                    container_name=container_name,
                )
            )
        except Exception as exc:
            ui.notify(f"Could not load logs: {exc}", type="negative")
            return

        with ui.dialog() as dialog, ui.card().classes(f"w-[90vw] max-w-5xl h-[80vh] p-4 {UIStyles.MODAL_CONTAINER}"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Logs: {container_name}").classes("text-base font-semibold text-[var(--lx-text)]")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            ui.textarea(value=logs).props("readonly autogrow").classes(f"w-full h-full text-xs {UIStyles.LOG_PANEL}")
        dialog.open()

    async def _show_shell_snapshot(host_id: int, container_id: str, container_name: str):
        try:
            output = await run.io_bound(service.container_shell_snapshot, host_id, container_id, container_name)
        except Exception as exc:
            ui.notify(f"Shell snapshot failed: {exc}", type="negative")
            return

        with ui.dialog() as dialog, ui.card().classes(f"w-[90vw] max-w-4xl p-4 {UIStyles.MODAL_CONTAINER}"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Shell Snapshot: {container_name}").classes("text-base font-semibold text-[var(--lx-text)]")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            ui.textarea(value=output).props("readonly autogrow").classes(f"w-full text-xs {UIStyles.LOG_PANEL}")
        dialog.open()

    def _action_handler(action: str, host_id: int, container_id: str, container_name: str):
        async def _handler():
            await _do_action(action, host_id, container_id, container_name)

        return _handler

    def _logs_handler(host_id: int, container_id: str, container_name: str):
        async def _handler():
            await _show_logs(host_id, container_id, container_name)

        return _handler

    def _shell_handler(host_id: int, container_id: str, container_name: str):
        async def _handler():
            await _show_shell_snapshot(host_id, container_id, container_name)

        return _handler

    def _host_header(host_view: dict) -> tuple[str, str]:
        host = host_view["host"]
        containers = host_view["containers"]
        if host_view["error"]:
            return (
                f"{host['name']} (offline)",
                "!bg-[color-mix(in_srgb,var(--lx-state-down)_10%,var(--lx-surface))] text-[var(--lx-state-down)]",
            )
        running = sum(1 for c in containers if c.get("state") == "running")
        return (
            f"{host['name']} ({running}/{len(containers)} running)",
            "!bg-[var(--lx-surface)] text-[var(--lx-text)]",
        )

    async def refresh_containers():
        hosts = service.list_hosts()
        if not hosts:
            container_wrapper.clear()
            with container_wrapper:
                ui.label("No Docker hosts configured. Add hosts in Plugin Settings.").classes(
                    "text-[var(--lx-text-muted)] italic"
                )
            return

        refresh_btn.props("loading")
        data = await run.io_bound(service.fetch_all_containers_parallel)
        container_wrapper.clear()

        with container_wrapper:
            for host_view in data.values():
                header, color = _host_header(host_view)
                host = host_view["host"]
                with ui.expansion(header, icon="dns").classes(
                    f"w-full shadow-[var(--lx-shadow-sm)] border border-[var(--lx-border-soft)] "
                    f"rounded-[var(--lx-radius-2xl)] {color} overflow-hidden"
                ):
                    if host_view["error"]:
                        ui.label(str(host_view["error"])).classes("text-sm text-[var(--lx-state-down)] p-3")
                        continue

                    if not host_view["containers"]:
                        ui.label("No containers found.").classes("text-sm text-[var(--lx-text-muted)] p-3")
                        continue

                    with ui.column().classes("w-full gap-2 p-2"):
                        for container in host_view["containers"]:
                            with ui.card().classes(
                                "w-full p-3 bg-[var(--lx-elevated)] border border-[var(--lx-border-soft)]"
                            ):
                                with ui.row().classes("w-full justify-between items-center gap-3 flex-wrap"):
                                    with ui.column().classes("gap-0"):
                                        ui.label(container["name"]).classes("text-sm font-semibold")
                                        ui.label(f"{container['image']} | {container['status']}").classes(
                                            "text-xs text-[var(--lx-text-muted)]"
                                        )
                                    with ui.row().classes("items-center gap-2"):
                                        state = container.get("state") or "unknown"
                                        _state_badge(state)
                                        ui.button(
                                            "Start",
                                            icon="play_arrow",
                                            on_click=_action_handler("start", host["id"], container["raw_id"], container["name"]),
                                        ).props("dense flat color=positive")
                                        ui.button(
                                            "Stop",
                                            icon="stop",
                                            on_click=_action_handler("stop", host["id"], container["raw_id"], container["name"]),
                                        ).props("dense flat color=negative")
                                        ui.button(
                                            "Restart",
                                            icon="restart_alt",
                                            on_click=_action_handler("restart", host["id"], container["raw_id"], container["name"]),
                                        ).props("dense flat color=warning")
                                        ui.button(
                                            "Logs",
                                            icon="article",
                                            on_click=_logs_handler(host["id"], container["raw_id"], container["name"]),
                                        ).props("dense flat")
                                        ui.button(
                                            "Shell",
                                            icon="terminal",
                                            on_click=_shell_handler(host["id"], container["raw_id"], container["name"]),
                                        ).props("dense flat")

        refresh_btn.props(remove="loading")

    refresh_btn.on_click(refresh_containers)
    ui.timer(0.1, refresh_containers, once=True)
