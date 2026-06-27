"""Dashboard widget for Docker Manager."""
from __future__ import annotations

from nicegui import ui

from ...logic.service import DockerManagerService


def render_dashboard_widget(_ctx, service: DockerManagerService):
    hosts = service.list_hosts()
    with ui.column().classes("gap-2 w-full"):
        ui.label("Docker Manager").classes("text-base font-bold text-slate-200")
        ui.separator().classes("my-1 opacity-20")
        with ui.row().classes("w-full justify-between items-center"):
            ui.label("Configured Hosts").classes("text-xs text-slate-400")
            ui.label(str(len(hosts))).classes("text-xs font-mono text-slate-200")
        ui.button(
            "Open Docker Manager",
            icon="open_in_new",
            on_click=lambda: ui.navigate.to("/docker"),
        ).props("flat dense size=sm color=primary")
