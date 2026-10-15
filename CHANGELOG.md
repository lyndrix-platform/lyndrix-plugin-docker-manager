# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
