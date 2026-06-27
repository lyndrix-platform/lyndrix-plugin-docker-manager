---
name: run-docker-manager
description: Run, launch, and screenshot the Docker Manager (/docker) plugin UI. This plugin has NO standalone run path — it only runs mounted inside the lyndrix-core dev stack and is rendered as a React bundle in the lyndrix-ui shell. Use to start/screenshot the Docker Manager hosts/containers UI or its Settings page, or to verify a docker-manager UI change in the actually-running app.
---

# Run the Docker Manager (/docker) plugin

This repo is a **lyndrix-core plugin**, not an app — no `main`, no server of its own.
Its UI is a **React bundle** (`app/ui/react/PluginApp.tsx` → `app/ui/static/ui_bundle.js`) that
the **lyndrix-ui** shell loads dynamically once the plugin is mounted in **lyndrix-core**
and **enabled**. So you run it by booting the shared dev stack and driving the React
shell at the plugin's route, **not** by launching this repo.

Manifest (`entrypoint.py`): `id="lyndrix.plugin.docker"`, `version="0.0.7"`,
`react_ui=True`, `ui_route="/docker"`, `settings_ui_route="/docker/settings"`.

The React shell reaches the plugin at `/apps/<safeId><route>`, where **safeId** is the
plugin id with dots→dashes: `lyndrix.plugin.docker` → **`lyndrix-plugin-docker`**. So:
- Main view:     `/apps/lyndrix-plugin-docker/docker`
- Settings view: `/apps/lyndrix-plugin-docker/docker/settings`

You drive it with the sibling **run-lyndrix-ui** CDP driver (logs in, seeds the token,
navigates, screenshots). Paths below are relative to this repo root
(`lyndrix-plugin-docker-manager/`); the UI repo is its sibling `../lyndrix-ui/`.

## Prerequisites

Same as `run-lyndrix-ui` — a headless `chromium` and the node `ws` module:

```bash
sudo apt-get install -y chromium
```

(`ws` ships in the `node-ws` system package; `node`/`curl`/`python3` are already present.)

## Bring up the shared stack (with this plugin mounted)

The plugin is already volume-mounted into the core dev compose (and the UI dev server
runs alongside). If `docker ps` already shows `lyndrix-core-dev` (:8081) and
`lyndrix-ui-dev` (:5173), **skip this**:

```bash
docker compose -f ../lyndrix-core/docker/docker-compose.dev.yml up -d --build   # core + DB + Vault
docker compose -f ../lyndrix-ui/docker/docker-compose.dev.yml up -d --build     # Vite UI on :5173
```

## Ensure the plugin is enabled

It only renders when active. Confirm it appears in `/api/health` (enable it in the
Plugin Manager `/plugins` otherwise — state persists in the DB volume):

```bash
curl -s http://localhost:8081/api/health | python3 -m json.tool | grep '"lyndrix.plugin.docker"'
# "lyndrix.plugin.docker": {
```

## Run (agent path) — screenshot the UI

The driver auto-reads the admin password from `../lyndrix-core/docker/.env.dev`. Takes
`<route> <outfile.png> [clickSelector]`:

```bash
# Main view (host list + the "Aktualisieren" / "Settings" header buttons):
node ../lyndrix-ui/.claude/skills/run-lyndrix-ui/driver.mjs \
  /apps/lyndrix-plugin-docker/docker /tmp/docker-main.png

# Settings view (reached via the header "Settings" button) — Docker Hosts list +
# "Host hinzufügen" form (Name / IP-Adresse / Port 2375 / Schema):
node ../lyndrix-ui/.claude/skills/run-lyndrix-ui/driver.mjs \
  /apps/lyndrix-plugin-docker/docker/settings /tmp/docker-settings.png
```

Then **open the PNG** (Read it). Verified this session: `/tmp/docker-main.png` shows the
**Docker Manager** header with `0 Hosts · 0/0 Container aktiv`, an "Aktualisieren" and a
"⚙ Settings" button, and "Keine Docker-Hosts konfiguriert." `/tmp/docker-settings.png`
shows "Docker Manager · Einstellungen · Docker-Hosts verwalten", an empty "Docker Hosts"
panel, and the "Host hinzufügen" form. A blank/login image means the stack isn't up or
the plugin isn't enabled.

> The driver's optional 3rd arg is a CSS selector clicked after load (see the
> run-lyndrix-ui skill). Deep-linking the `/settings` route directly (above) is
> simpler and is the path verified here.

## Alternative: the NiceGUI view from core

The plugin also exposes an in-process **NiceGUI** page at core `:8081/docker`. Drive it
with the core skill's Playwright driver instead of the React shell:

```bash
export LYNDRIX_ADMIN_PASSWORD=$(grep -E '^LYNDRIX_ADMIN_PASSWORD=' ../lyndrix-core/docker/.env.dev | cut -d= -f2- | tr -d '\r')
../lyndrix-core/.dev/run-venv/bin/python ../lyndrix-core/.claude/skills/run-lyndrix-core/driver.py --routes /docker --no-mobile
```

The React shell (`/apps/lyndrix-plugin-docker/docker`) is the current, verified path and
the one this repo's `app/ui/react/` changes affect; use the NiceGUI route only as a fallback.

## Gotchas

- **No standalone run.** No `python -m docker_manager`, no dev server in this repo. The
  UI only exists inside the lyndrix-ui shell (React) or core's NiceGUI page. `curl` of
  either route returns an empty SPA shell — always use a browser driver.
- **safeId keeps the id's own characters; only dots become dashes.** `lyndrix.plugin.docker`
  → `lyndrix-plugin-docker`. (Sibling plugins differ, e.g. the IaC one is
  `lyndrix-plugin-iac_orchestrator` — the underscore stays.) A wrong safeId silently
  bounces to the Dashboard.
- **Plugin must be enabled** or the `/apps/*` route renders the loading guard / dashboard
  and never the plugin. Confirm via `/api/health` (above).
- **Edit → rebuild the bundle.** Changing `app/ui/react/*.tsx` does nothing until you rebuild
  `app/ui/static/ui_bundle.js`: `node node_modules/vite/bin/vite.js build --config vite.ui.config.ts`.
  The shell loads the built bundle, not the TSX.
- **Password, not hardcoded.** The driver reads it from `../lyndrix-core/docker/.env.dev`
  (CRLF-encoded — the driver handles the `\r`).

## Build / typecheck the bundle (when changing the UI)

```bash
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json     # typecheck (ignore the pre-existing src/ui/index.tsx Window-cast error)
node node_modules/vite/bin/vite.js build --config vite.ui.config.ts  # → ui_static/ui_bundle.js (~13 kB)
```

## Test

```bash
pip install -r requirements-dev.txt 2>/dev/null; pytest    # if a suite exists
```
