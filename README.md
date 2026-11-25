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
- **Per-host TLS** — `https` with server verification, optional CA bundle and client certificate
  (stored write-only in Vault); insecure hosts are flagged.
- **Audit trail** — append-only log of container actions, log reads, shell exec and host changes
  (`GET /audit`, retention setting `audit_retention_days`, default 90).

## Permissions

Sensitive functions need dedicated permissions that the core's baseline `api:read`/`api:write`
grant does not satisfy: `docker:logs`, `docker:control`, `docker:hosts`, `docker:exec`,
`docker:audit`. Roles: **admin** (all; auto-mapped to `INT_ADMIN`), **operator** (logs + control)
and **viewer** (read only); operator and viewer are assigned manually by an administrator.
See [`documentation.md`](documentation.md) and the [Changelog](CHANGELOG.md) (1.3.0 upgrade note).

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
app/logic/           # service singleton, Docker operations, event handlers
app/api.py           # REST router (mounted at /api/plugins/<id>/)
app/ui/nicegui/      # NiceGUI pages, widget, settings
app/ui/react/        # React frontend sources (built to app/ui/static/)
```

## Documentation

- Plugin docs: https://docker-manager.docs.lyndrix.eu
- Platform docs: https://docs.lyndrix.eu — see [Sockets](https://docs.lyndrix.eu/core-components/sockets/)
  and the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/).

## License

Apache-2.0 — see [LICENSE](LICENSE).
