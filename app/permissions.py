"""Permission ids of the Docker Manager plugin (one place for API, manifest, UIs).

Why custom permissions: the core grants ``plugin:<id>:api:read`` to INT_VIEWER
(every SSO user) and ``plugin:<id>:api:write`` to INT_USER for EVERY plugin, and
the global ``api:read``/``api:write`` satisfy any permission ending in
``:api:read``/``:api:write``. A route gated only on those is therefore open to
every logged-in user. Anything sensitive needs a permission whose id does NOT
end in ``:api:read``/``:api:write`` — those get no baseline grant and no global
fallback, so only the manifest roles (or an explicit grant) unlock them.
"""
from __future__ import annotations

PLUGIN_ID = "lyndrix.plugin.docker"
_PREFIX = f"plugin:{PLUGIN_ID}"

# Baseline (every viewer): list hosts and containers with their status.
PERM_READ = f"{_PREFIX}:api:read"
# Kept in the roles for compatibility; no route is gated on it alone any more.
PERM_WRITE = f"{_PREFIX}:api:write"

# Local suffixes as declared in ``manifest.custom_permissions``.
LOGS = "docker:logs"
CONTROL = "docker:control"
HOSTS = "docker:hosts"
EXEC = "docker:exec"

# Container logs and anything else that exposes env/inspect details.
PERM_LOGS = f"{_PREFIX}:{LOGS}"
# Container lifecycle: start/stop/restart (and future remove/pull/create).
PERM_CONTROL = f"{_PREFIX}:{CONTROL}"
# Host registry writes — decides where the core sends Docker API requests.
PERM_HOSTS = f"{_PREFIX}:{HOSTS}"
# In-container code execution (shell snapshot).
PERM_EXEC = f"{_PREFIX}:{EXEC}"

# Capability name (as served by GET /capabilities) → permission id.
CAPABILITIES = {
    "logs": PERM_LOGS,
    "control": PERM_CONTROL,
    "hosts": PERM_HOSTS,
    "exec": PERM_EXEC,
}
