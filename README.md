# Lyndrix Docker Manager

**Plugin ID:** `lyndrix.plugin.docker` · **Route:** `/docker`

A [Lyndrix](https://github.com/lyndrix-platform/lyndrix-core) plugin for monitoring and
controlling Docker hosts from the dashboard. It talks to the Docker daemon through the
Lyndrix Core **sockets** layer (no direct socket access from the plugin), so all access is
mediated by core auth/permissions.

## Features

- **Container monitoring** — live view of containers and their state across managed hosts.
- **Runtime controls** — start / stop / restart, view **logs**, and open an interactive **shell**.
- **Host management** — register Docker hosts; connection details persisted in the plugin's
  Vault namespace.
- **Dashboard widget + settings UI** — a compact status widget and a settings page.
- **REST API** — container operations exposed under `/api/plugins/lyndrix.plugin.docker/`.

## Installation

Install **Docker Manager** from the Lyndrix **Plugin Manager**, or declare it for
reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager
```

The manifest sets `auto_enable_on_install=False` — enable it in the Plugin Manager after
configuring your Docker host(s).

## Project structure

```
entrypoint.py        # manifest + lifecycle wiring only
app/model/           # data structures / persistence helpers
app/controller/      # service singleton, Docker operations, event handlers
app/ui/              # NiceGUI pages, widget, settings
```

## Documentation

- Plugin docs: https://docker-manager.docs.lyndrix.eu
- Platform docs: https://docs.lyndrix.eu — see [Sockets](https://docs.lyndrix.eu/core-components/sockets/)
  and the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/).

## License

Apache-2.0 — see [LICENSE](LICENSE).
