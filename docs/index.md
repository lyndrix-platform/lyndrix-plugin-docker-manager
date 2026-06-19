# Lyndrix Docker Manager

Docker monitoring with runtime controls (start/stop/restart/logs/shell), built on the Lyndrix Core sockets layer.

- **Repository:** [https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager](https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager)
- **Platform docs:** [Lyndrix Core](https://docs.lyndrix.eu) · [Plugin ecosystem](https://docs.lyndrix.eu/ecosystem/)

This plugin builds on the Lyndrix Core [sockets](https://docs.lyndrix.eu/core-components/sockets/) extension point.

## Features

- Container monitoring and runtime control
- Host management with Vault-backed persistence
- Dashboard widget and settings UI
- REST API for container operations

## Installation

Install **Docker Manager** from the Lyndrix **Plugin Manager**, or declare it for
reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager
```

See the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/) for the plugin model and
lifecycle, and [Usage](usage.md) / [Configuration](configuration.md) for details.
