# Usage

Routes live under `/api/plugins/lyndrix.plugin.docker/`. Sensitive routes need a dedicated
permission (not satisfied by the core's `api:read`/`api:write`):

| Route | Permission |
|---|---|
| `GET /capabilities`, `GET /hosts`, `GET /containers` | `api:read` |
| `POST /hosts`, `POST /hosts/sync`, `POST /hosts/set`, `DELETE /hosts/{id}` | `docker:hosts` |
| `POST /containers/action` (start/stop/restart) | `docker:control` |
| `GET /containers/{host_id}/{container_id}/logs` | `docker:logs` |
| `POST /containers/{host_id}/{container_id}/shell` | `docker:exec` |
| `GET /audit` | `docker:audit` |

Roles: **admin** (all, auto-mapped to `INT_ADMIN`), **operator** (logs + control), **viewer**
(read only). Operator and viewer are not auto-mapped; an administrator assigns them. After
upgrading to 1.3.0, users outside `INT_ADMIN` lose logs, control and host-registry access until
a role is assigned.

Hosts can use `https` with `tls_verify`, `tls_ca_path` and a write-only client certificate;
every host reports its `security` state and insecure hosts carry a warning. The audit trail
(`GET /audit`) supports keyset paging (`before_id`) and the filters `host_id`, `action`, `actor`.

For the platform context this plugin runs in, see the
[Lyndrix Core documentation](https://docs.lyndrix.eu).
