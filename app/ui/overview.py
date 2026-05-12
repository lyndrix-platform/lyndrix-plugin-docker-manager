"""Main overview page for Docker Manager."""
from __future__ import annotations

from nicegui import run, ui

from ..controller.service import DockerManagerService


def render_overview_ui(ctx, service: DockerManagerService):
    with ui.row().classes("w-full justify-between items-center mb-4"):
        with ui.column().classes("gap-0"):
            ui.label("Docker Proxy Fleet").classes("text-2xl font-bold dark:text-zinc-100")
            ui.label("Runtime controls only: start/stop/restart, logs, shell snapshot.").classes(
                "text-xs text-zinc-500"
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

        with ui.dialog() as dialog, ui.card().classes("w-[90vw] max-w-5xl h-[80vh] p-4 bg-zinc-900 border border-zinc-700"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Logs: {container_name}").classes("text-base font-semibold text-zinc-200")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            ui.textarea(value=logs).props("readonly autogrow").classes("w-full h-full font-mono text-xs")
        dialog.open()

    async def _show_shell_snapshot(host_id: int, container_id: str, container_name: str):
        try:
            output = await run.io_bound(service.container_shell_snapshot, host_id, container_id, container_name)
        except Exception as exc:
            ui.notify(f"Shell snapshot failed: {exc}", type="negative")
            return

        with ui.dialog() as dialog, ui.card().classes("w-[90vw] max-w-4xl p-4 bg-zinc-900 border border-zinc-700"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"Shell Snapshot: {container_name}").classes("text-base font-semibold text-zinc-200")
                ui.button(icon="close", on_click=dialog.close).props("flat round")
            ui.textarea(value=output).props("readonly autogrow").classes("w-full font-mono text-xs")
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
                "!bg-red-50 dark:!bg-red-900/10 text-red-600 dark:text-red-400",
            )
        running = sum(1 for c in containers if c.get("state") == "running")
        return (
            f"{host['name']} ({running}/{len(containers)} running)",
            "!bg-white dark:!bg-zinc-900 text-slate-800 dark:text-zinc-200",
        )

    async def refresh_containers():
        hosts = service.list_hosts()
        if not hosts:
            container_wrapper.clear()
            with container_wrapper:
                ui.label("No Docker hosts configured. Add hosts in Plugin Settings.").classes("text-zinc-500 italic")
            return

        refresh_btn.props("loading")
        data = await run.io_bound(service.fetch_all_containers_parallel)
        container_wrapper.clear()

        with container_wrapper:
            for host_view in data.values():
                header, color = _host_header(host_view)
                host = host_view["host"]
                with ui.expansion(header, icon="dns").classes(
                    f"w-full shadow-sm border border-slate-200 dark:border-zinc-800 rounded-2xl {color} overflow-hidden"
                ):
                    if host_view["error"]:
                        ui.label(str(host_view["error"])).classes("text-sm text-red-500 p-3")
                        continue

                    if not host_view["containers"]:
                        ui.label("No containers found.").classes("text-sm text-zinc-500 p-3")
                        continue

                    with ui.column().classes("w-full gap-2 p-2"):
                        for container in host_view["containers"]:
                            with ui.card().classes("w-full p-3 bg-zinc-900/20 dark:bg-zinc-900 border border-zinc-700"):
                                with ui.row().classes("w-full justify-between items-center gap-3 flex-wrap"):
                                    with ui.column().classes("gap-0"):
                                        ui.label(container["name"]).classes("text-sm font-semibold")
                                        ui.label(f"{container['image']} | {container['status']}").classes("text-xs text-zinc-500")
                                    with ui.row().classes("items-center gap-2"):
                                        state = container.get("state") or "unknown"
                                        chip_color = "positive" if state == "running" else "negative"
                                        ui.chip(state, color=chip_color).props("text-color=white dense")
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
