"""Settings UI for Docker host configuration."""
from __future__ import annotations

from nicegui import ui

from ...logic.service import DockerManagerService


def render_settings_ui(ctx, service: DockerManagerService):
    del ctx

    with ui.column().classes("w-full gap-4"):
        ui.label("Docker Hosts").classes("text-base font-semibold text-zinc-200")
        ui.label(
            "Manage Docker proxy hosts used by this plugin. Hosts are persisted in Vault, "
            "can be updated via API, and via event bus topics."
        ).classes("text-xs text-zinc-400")

        current_host = {"id": None}

        with ui.row().classes("w-full gap-4 flex-wrap lg:flex-nowrap items-start"):
            with ui.card().classes("w-full lg:w-2/3 p-4 bg-zinc-900 border border-zinc-700"):
                host_columns = [
                    {"name": "name", "label": "Host Name", "field": "name", "align": "left", "sortable": True},
                    {"name": "ip", "label": "IP Address", "field": "ip", "align": "left"},
                    {"name": "port", "label": "Port", "field": "port", "align": "left"},
                    {"name": "scheme", "label": "Scheme", "field": "scheme", "align": "left"},
                ]
                host_table = ui.table(
                    columns=host_columns,
                    rows=service.list_hosts(),
                    row_key="id",
                    selection="single",
                    pagination=15,
                ).classes("w-full no-shadow border border-zinc-700")

            with ui.card().classes("w-full lg:w-1/3 p-4 bg-zinc-900 border border-zinc-700"):
                form_title = ui.label("Add Docker Host").classes("text-sm font-semibold text-zinc-200")
                name_input = ui.input("Host Name").classes("w-full").props("outlined dense")
                ip_input = ui.input("IP Address").classes("w-full").props("outlined dense")
                with ui.row().classes("w-full gap-2 flex-nowrap"):
                    port_input = ui.number("Port", value=2375).classes("w-1/2").props("outlined dense")
                    scheme_input = ui.select(["http", "https"], value="http", label="Scheme").classes("w-1/2")

                def _refresh_table():
                    host_table.rows = service.list_hosts()
                    host_table.update()

                def _clear_form():
                    current_host["id"] = None
                    form_title.set_text("Add Docker Host")
                    name_input.value = ""
                    ip_input.value = ""
                    port_input.value = 2375
                    scheme_input.value = "http"
                    btn_delete.set_visibility(False)
                    host_table.selected.clear()
                    host_table.update()

                def _on_selection(_event):
                    if not host_table.selected:
                        _clear_form()
                        return
                    selected = host_table.selected[0]
                    current_host["id"] = selected["id"]
                    form_title.set_text("Edit Docker Host")
                    name_input.value = selected.get("name", "")
                    ip_input.value = selected.get("ip", "")
                    port_input.value = selected.get("port", 2375)
                    scheme_input.value = selected.get("scheme", "http")
                    btn_delete.set_visibility(True)

                def _save_host():
                    try:
                        payload = {
                            "id": current_host["id"],
                            "name": name_input.value,
                            "ip": ip_input.value,
                            "port": int(port_input.value or 2375),
                            "scheme": scheme_input.value or "http",
                        }
                        if payload["id"] is None:
                            payload.pop("id")
                        service.upsert_host(payload)
                        _refresh_table()
                        _clear_form()
                        ui.notify("Host saved.", type="positive")
                    except Exception as exc:
                        ui.notify(f"Failed to save host: {exc}", type="negative")

                def _delete_host():
                    host_id = current_host.get("id")
                    if host_id is None:
                        return
                    if service.delete_host(int(host_id)):
                        _refresh_table()
                        _clear_form()
                        ui.notify("Host removed.", type="info")
                    else:
                        ui.notify("Host not found.", type="warning")

                host_table.on("selection", _on_selection)

                with ui.row().classes("w-full justify-between items-center mt-2"):
                    btn_delete = ui.button(icon="delete", on_click=_delete_host, color="red").props("unelevated round")
                    btn_delete.set_visibility(False)
                    with ui.row().classes("gap-2"):
                        ui.button("Cancel", on_click=_clear_form).props("flat")
                        ui.button("Save", on_click=_save_host, color="primary").props("unelevated")

        with ui.card().classes("w-full p-4 bg-zinc-900 border border-zinc-700 gap-2"):
            ui.label("Automation Hooks").classes("text-sm font-semibold text-zinc-200")
            hooks = [
                (
                    "EventBus subscribe",
                    "docker_manager:hosts:set | docker_manager:host:upsert | docker_manager:host:delete | docker_manager:hosts:get",
                ),
                (
                    "EventBus emit",
                    "docker_manager:hosts:updated | docker_manager:hosts:state | docker_manager:container:action",
                ),
                (
                    "REST API",
                    "GET/POST /api/plugins/lyndrix.plugin.docker/hosts | "
                    "POST /api/plugins/lyndrix.plugin.docker/hosts/set | "
                    "DELETE /api/plugins/lyndrix.plugin.docker/hosts/{host_id}",
                ),
            ]
            for title, value in hooks:
                with ui.row().classes("w-full items-start gap-3 border-b border-zinc-800 py-1"):
                    ui.label(title).classes("text-xs text-zinc-500 w-40 shrink-0")
                    ui.label(value).classes("text-xs font-mono text-emerald-400")
