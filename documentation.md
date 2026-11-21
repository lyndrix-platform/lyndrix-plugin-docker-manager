# Docker Manager — Documentation

## Overview

The Docker Manager enables central monitoring and control of Docker hosts via a remote proxy HTTP API (no direct socket access). For every registered host, containers can be displayed in real time and actions such as start, stop, restart, log retrieval and shell access can be executed.

Live updates run through the Lyndrix sockets layer (SSE). The plugin requires manual host configuration before first use (`auto_enable_on_install=False`).

---

## Architecture

```
lyndrix-plugin-docker-manager/
├── entrypoint.py              # Manifest + Lifecycle-Hooks
├── app/
│   ├── permissions.py         # Berechtigungs-IDs (docker:logs/control/hosts/exec/audit)
│   ├── api.py                 # FastAPI-Router (Hosts + Container-Aktionen)
│   ├── model/
│   │   ├── models.py          # Tabellen docker_manager_hosts, docker_manager_audit
│   │   └── database.py        # Cluster-sichere DB-Helfer (Bootstrap-Lock, READ COMMITTED, Retry)
│   ├── logic/
│   │   ├── targets.py         # Ziel-Validierung (SSRF, IP-Pinning)
│   │   ├── tls.py             # Pro-Host-TLS, Client-Zertifikate in Vault
│   │   ├── audit.py           # Audit-Trail (docker_manager_audit)
│   │   └── service.py         # DockerManagerService: Host-Persistenz, Docker-Proxy, SSRF-Validierung
│   └── ui/
│       ├── nicegui/
│       │   ├── overview.py    # Container-Übersicht + Laufzeitsteuerung
│       │   ├── settings.py    # Host-Verwaltung im Plugin-Manager
│       │   └── widget.py      # Kompaktes Dashboard-Widget
│       └── react/             # React-Frontend (kanonisches Migrationsziel)
```

**`DockerManagerService`** manages the Docker hosts, validates all inputs against SSRF attacks and forwards HTTP requests to the Docker proxy.

### Host Storage and Cluster Operation (from 1.2.0)

- The hosts are stored in the shared MariaDB table `docker_manager_hosts` (autoincrement ID, unique name, address, port, scheme, labels, source `manual`/`iac`, `ci_id`, check result). Every core instance behind the load balancer sees the same hosts and IDs; the in-memory list is only a cache (re-read after own changes, on the cluster event, and at the latest after 30 s).
- The table is created idempotently under the bootstrap lock `lyndrix_bootstrap` (during setup, if the DB is already connected, and on `db:connected`).
- Write accesses run row by row in short READ-COMMITTED transactions and are retried on MariaDB errors 1020/1205/1213 (snapshot isolation, lock timeout, deadlock) as well as on a lost insert race for the same name.
- After every change, the plugin sends `docker_manager:hosts_updated` with `cluster=True`; the other instances reload their cache when the copy carries `_origin`.
- **Migration:** The old Vault list `configured_hosts` is imported once. IDs are preserved if free (otherwise a new ID is assigned and logged); names that already exist win. The Vault marker `configured_hosts_migrated_at` prevents a repeated import (deleted hosts do not come back).
- **Reachability:** Exactly one instance checks the Docker proxies in the background (lease `docker_manager:prober`, interval `probe_interval_seconds`, default 60 s) and writes `reachable`, `checked_at` and `check_error` to the table. `health()` only reads these values (one query) and reports unreachable proxies as `degraded`, never as `error`. The prober uses its own pool (4 workers, 8 s limit per host). `health()` runs no DDL and reports `degraded` (`schema_not_ready`) until the bootstrap has completed.

---

## API Reference

All routes are reachable under `/api/plugins/lyndrix.plugin.docker/` and require valid authentication. Since 1.3.0, the core's base permission `api:read`/`api:write` is **no longer** sufficient for sensitive routes; they require the dedicated permissions listed below (see [Permissions and roles](#permissions-and-roles)).

| Method | Path | Permission | Description |
|---|---|---|---|
| `GET` | `/capabilities` | `api:read` | What the caller is allowed to do (`logs`, `control`, `hosts`, `exec`, `audit` as booleans); the UI uses this to hide controls |
| `GET` | `/hosts` | `api:read` | List of all hosts (without `labels`, `check_error`, `tls_ca_path`, as long as `docker:hosts` is missing) |
| `POST` | `/hosts` | `docker:hosts` | Create a host or update it (by name/ID): `name`, `ip`, `port`, `scheme`, TLS fields |
| `POST` | `/hosts/sync` | `docker:hosts` | Upsert hosts by name without removing others (IaC orchestrator, scripts); `port`/`scheme` optional; response with `unchanged` counter |
| `POST` | `/hosts/set` | `docker:hosts` | Replace the complete host list |
| `DELETE` | `/hosts/{host_id}` | `docker:hosts` | Remove a host |
| `GET` | `/containers` | `api:read` | Containers of all hosts (`include_stopped`); without `docker:hosts` no internal host details |
| `POST` | `/containers/action` | `docker:control` | Container action (`start` / `stop` / `restart`), body `host_id`, `container_id`, `action` |
| `GET` | `/containers/{host_id}/{container_id}/logs` | `docker:logs` | Container logs (`tail`, default 200) |
| `POST` | `/containers/{host_id}/{container_id}/shell` | `docker:exec` | Shell snapshot via Docker `exec` |
| `GET` | `/audit` | `docker:audit` | Audit trail, newest first; keyset paging (`limit`, `before_id` = `next_before_id` of the previous page), filters `host_id`, `action`, `actor` |

Container references are validated against the Docker grammar; the plugin never follows redirects.

---

## Permissions and Roles

The core grants `plugin:<id>:api:read` to every logged-in user and `api:write` to every user in the group INT_USER. For this reason, all sensitive functions have their own permissions (prefix `plugin:lyndrix.plugin.docker:`), which are satisfied neither by the base permissions nor by the global `api:read`/`api:write`:

| Permission | Protects |
|---|---|
| `docker:logs` | Container logs |
| `docker:control` | Start / stop / restart |
| `docker:hosts` | Host registry (create, change, delete, synchronize) |
| `docker:exec` | Shell in containers; also the NiceGUI page `/docker` |
| `docker:audit` | Read the audit trail |

| Role | Rights | Assignment |
|---|---|---|
| `admin` (Docker Manager Administrator) | all five permissions plus `api:read`/`api:write` | automatically for the group `INT_ADMIN` |
| `operator` (Docker Manager Operator) | read, logs, container control | not automatic; an administrator assigns it |
| `viewer` (Docker Manager Viewer) | hosts and containers with status | not automatic |

**Note on updating to 1.3.0:** Users outside `INT_ADMIN` lose log, control and host registry access until they are assigned the operator/administrator role (or the individual permissions). A restricted user API key that calls `/hosts/sync` needs `docker:hosts`; the system API key is not affected.

### Audit Trail

The append-only table `docker_manager_audit` (created idempotently under the bootstrap lock) records container start/stop/restart, log retrievals, shell exec and user-triggered host changes. The instance holding the prober lease deletes entries hourly after `audit_retention_days`. The bus event `docker_manager:container:action` additionally contains `actor`.

---

## Events

| Direction | Topic | Description |
|---|---|---|
| subscribe | `docker_manager:hosts:set` | Set the complete host list |
| subscribe | `docker_manager:host:upsert` | Create or update a single host |
| subscribe | `docker_manager:host:delete` | Remove a single host |
| subscribe | `docker_manager:hosts:get` | Request the current host list |
| subscribe | `iac:inventory_updated` | Take over Docker hosts from the IaC inventory (copies with `_origin` are ignored) |
| subscribe | `plugin:settings_changed` | Re-read settings |
| subscribe/emit | `docker_manager:hosts_updated` | Cluster event after every host change (`{"reason", "ids"}`, `cluster=True`) |
| emit | `docker_manager:hosts:updated` | Host list has changed (local, complete list) |
| emit | `docker_manager:hosts:state` | Current host status (polling result) |
| emit | `docker_manager:container:action` | Container action was executed (contains `actor`) |

---

## Configuration & Settings

The plugin has no external environment variables. The host configuration is done via the plugin settings page, the REST API or automatically from the IaC inventory.

| Setting | Default value | Description |
|---|---|---|
| `probe_interval_seconds` | `60` | Interval of the reachability check (minimum 10 s) |
| `audit_retention_days` | `90` | Retention of the audit trail in days (`0` = unlimited); cleanup hourly by the prober instance |

**Important:** `auto_enable_on_install=False` — the plugin must be activated manually after installation, since no meaningful function is possible without at least one configured host.

### Host Fields

| Field | Type | Default value | Description |
|---|---|---|---|
| `name` | string | — | Display name of the host |
| `ip` | string | — | IP address or hostname of the Docker proxy |
| `port` | integer | `2375` | TCP port of the Docker proxy |
| `scheme` | string | `http` | `http` or `https` |
| `source` | string | `manual` | `manual` or `iac` (read-only in the UI) |
| `ci_id` | string | — | Entity Registry UUID, if known |
| `tls_verify` | boolean | — | Verify the server certificate (`https` only) |
| `tls_ca_path` | string | — | CA bundle file on the core; `null` removes it |
| `tls_client_cert_pem` / `tls_client_key_pem` | string | — | Client certificate and key (write-only, stored in Vault); the response only contains `tls_client_auth` |
| `tls_client_auth` | boolean | — | `false` removes the stored client certificate |
| `security` / `insecure` / `security_warning` | — | — | Read-only: `security{encrypted, server_verified, client_auth}`; existing hosts remain plaintext `http` and are marked |
| `reachable` / `checked_at` / `check_error` | — | — | Last result of the reachability check (read-only) |

---

## Security Notes

The method `_validate_host_target` in `app/logic/service.py` protects against SSRF attacks (Server-Side Request Forgery). The following address types are **rejected**:

- Link-local addresses (`169.254.0.0/16`, IPv6 link-local)
- Cloud metadata addresses
- Multicast and reserved address ranges
- Numeric IPv4 notations and IPv6 forms that embed IPv4, as well as resolved addresses in these ranges

Every request connects to the checked IP (no DNS rebinding window); redirects are never followed. Since 1.3.0 the check lives in `app/logic/targets.py`, TLS in `app/logic/tls.py`, and the audit trail in `app/logic/audit.py`.

Private (`10.0.0.0/8`, `192.168.0.0/16`, `172.16.0.0/12`) and loopback addresses (`127.0.0.1`) are **allowed**, since Docker proxies are typically operated in the local network or on the host itself.

---

## Internationalization

The plugin registers the i18n namespace `docker`. Translation files are located under `locales/docker.<locale>.json` and are automatically picked up by the Lyndrix catalog when loaded. The React client loads them via the standard i18n endpoint (`GET /api/i18n/{locale}?ns=docker`).

---

## Development & Testing

```bash
# From the plugin directory (lyndrix-plugin-docker-manager/)
pip install -r requirements-dev.txt

# Run tests
pytest

# Type checking
mypy .

# Linter
ruff check .

# Check formatter
black --check .
```

The service layer (`app/logic/service.py`) and the API module (`app/api.py`) are testable independently of a running Lyndrix core. For lifecycle hook tests, `ModuleContext` can be mocked.

The cluster tests (`tests/test_cluster.py`) run against an SQLite in-memory database and need the core on the path (`core.services` is imported first in `tests/conftest.py`) — simplest in the container `lyndrix-core-dev` with `PYTHONPATH=/app` and `LYNDRIX_CLUSTER_MODE=single`.
