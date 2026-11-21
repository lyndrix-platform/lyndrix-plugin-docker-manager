# Configuration

The plugin has no environment variables. Plugin settings:

| Setting | Default | Description |
|---|---|---|
| `probe_interval_seconds` | `60` | Reachability probe interval (minimum 10 s) |
| `audit_retention_days` | `90` | Audit trail retention in days (`0` keeps entries forever); pruned hourly by the prober-lease holder |

Per-host TLS client certificates and keys are stored in the plugin's Vault namespace.

Secrets are stored in the plugin's own Vault namespace via `ctx.get_secret` /
`ctx.set_secret`; see [Plugin development → state and secrets](https://docs.lyndrix.eu/plugins/).
