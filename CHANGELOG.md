# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
Proposed version: **1.3.0** (security audit 2026-11; the manifest version is not bumped yet). Minimum core unchanged.
React bundle rebuild needed (permission-aware controls, TLS badge and fields, `audit` capability flag).
**Before the rollout:** users outside INT_ADMIN lose log, control and host-registry access until an administrator
assigns the Docker Manager Operator/Administrator role (or the single permissions). A scoped user API key that calls
`/hosts/sync` needs `docker:hosts`; the system API key is unaffected.

### Security
- Sensitive routes require custom permissions that the core's baseline `api:read`/`api:write` grant does not satisfy:
  `docker:logs` (container logs), `docker:control` (start/stop/restart), `docker:hosts` (host registry) next to the
  existing `docker:exec` (shell). Roles: admin = all, operator = logs + control, viewer = read. New
  `GET /capabilities` tells a UI what the caller may do (DM-01).
- Per-host TLS: new nullable columns `tls_verify`, `tls_ca_path`, `tls_client_auth` (idempotent ALTER under the
  bootstrap lock). A client certificate and key are accepted write-only on `POST /hosts` and stored in Vault. Every
  host reports `security{encrypted, server_verified, client_auth}`, `insecure` and `security_warning`; existing rows
  stay plain http and are flagged (DM-02).
- Viewer-level responses no longer contain internal host details (target URL, resolver and TLS details, CA path).
- Docker targets are validated robustly (numeric IPv4 notations, IPv6 forms that embed IPv4, metadata ranges, resolved
  addresses), every request connects to the checked IP (no DNS-rebinding window), redirects are never followed, and
  container references follow Docker's grammar (DM-04).
- Audit trail: new append-only table `docker_manager_audit` (created idempotently under the bootstrap lock) records
  container start/stop/restart, log reads, shell exec and user-driven host changes. `GET /audit` (keyset paging,
  filters `host_id`/`action`/`actor`) requires the new `docker:audit` permission (admin role). The bus event
  `docker_manager:container:action` carries `actor`. New setting `audit_retention_days` (default 90, 0 = keep), pruned
  hourly by the prober-lease holder (DM-06).
- The NiceGUI `/docker` page requires `docker:exec` (DM-07).

### Fixed
- The inventory sync no longer resets a manually configured host to `http:2375`: it updates address and CI id of `iac`
  rows only, `port`/`scheme` on `POST /hosts/sync` are optional, and unchanged hosts cause no write and no cluster
  event. Sync stats gain `unchanged`; `updated` counts real changes (DM-03).
- The prober uses its own 4-worker pool with an 8 s limit per host ("Probe timed out") and bounded DNS lookups, so
  unreachable hosts no longer occupy the thread pool every plugin shares (DM-05).
- `health()` no longer runs DDL; it reports degraded `schema_not_ready` until the bootstrap finished. A host payload
  (API, bus, `/hosts/set`) with an unknown id gets a fresh id instead of recreating a deleted host (DM-08).

## [1.2.0] - 2026-10-15
### Changed
- **Hosts live in the shared database.** New table `docker_manager_hosts`
  (autoincrement id, unique name, address, port, scheme, labels, source
  `manual`|`iac`, `ci_id`, probe columns, timestamps), created idempotently under
  `db_instance.advisory_lock("lyndrix_bootstrap")` at setup (DB already up) and on
  `db:connected`. Every core instance behind the load balancer now serves the same
  hosts and ids; the in-memory list is only a cache (refreshed after own writes,
  on the cluster event and after 30 s).
- One-time import of the 1.1 Vault list `configured_hosts`: ids are kept where
  free (otherwise re-assigned and logged), names already in the table win, the
  Vault marker `configured_hosts_migrated_at` prevents re-imports. An IaC
  inventory arriving during boot waits for the import.
- `health()` no longer probes Docker proxies on every poll. A background prober
  runs on exactly one instance under the lease `docker_manager:prober` (interval
  setting `probe_interval_seconds`, default 60 s, min 10) and stores
  `reachable` / `checked_at` / `check_error` per host; `health()` reads them with
  one query. Unreachable proxies report `degraded` (never `error`); a standby
  instance without the prober is healthy.
- Manifest: `placement="any"`, `settings_schema` (probe interval), new
  subscriptions `db:connected`, `plugin:settings_changed`,
  `docker_manager:hosts_updated`.

### Added
- Cluster event `docker_manager:hosts_updated` (`cluster=True`,
  `{"reason", "ids"}`) after every host change; peers reload their cache when the
  copy carries `_origin`, then re-emit the local `docker_manager:hosts:updated`.
- Host JSON gains the additive fields `source`, `ci_id`, `labels`, `reachable`,
  `checked_at`, `check_error`; the 1.1 keys (`id`, `name`, `ip`, `port`,
  `scheme`) are unchanged.
- Tests (`tests/test_cluster.py`, in-memory SQLite).

### Fixed
- Concurrent writes no longer lose hosts or hand out colliding ids
  (`max(id)+1` read-modify-write of the whole Vault list replaced by per-row
  upserts). Writers use READ COMMITTED sessions and retry on MariaDB
  1020/1205/1213; a lost insert race on the unique name (IntegrityError) is
  retried as an update.
- `iac:inventory_updated` ignores relayed copies (`_origin`) so a cluster-wide
  emit never syncs N times; new inventory hosts get `source: "iac"` and the
  inventory's `ci_id`.
- Blocking Vault/DB work in async handlers and NiceGUI host edits runs in
  worker threads; `docker_manager:container:action` and host events emitted from
  worker threads are handed to the event loop.
- `plugin:settings_changed` for this plugin re-reads the settings on every
  instance.
- REST host writes answer `503` instead of `500` while the database is not
  connected.
