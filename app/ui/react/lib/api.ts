const PLUGIN_ID_PREFIX = 'lyndrix.plugin.docker'
const TOKEN_KEY = 'lyndrix_token'

// Cap every request so a backend stalled on an unreachable Docker host (see the
// blocking-I/O path on the server) can never hang the UI indefinitely.
const REQUEST_TIMEOUT_MS = 15000

export interface DockerHost {
  id: number
  name: string
  ip: string
  port: number
  scheme: string
}

// In-SPA redirect that the shell's BrowserRouter picks up — NOT a full reload.
// A hard window.location reload cold-loads the SPA before the dynamic plugin
// routes are registered, which bounces the user / breaks plugin navigation.
function spaRedirect(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers as Record<string, string> | undefined),
  }

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  let res: Response
  try {
    res = await fetch(path, { ...init, headers, signal: controller.signal })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('Zeitüberschreitung der Anfrage')
    }
    throw err
  } finally {
    window.clearTimeout(timer)
  }

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    spaRedirect('/login')
    throw new Error('Nicht autorisiert')
  }

  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }

  return res.json() as Promise<T>
}

export const pluginApi = {
  get: <T>(subpath: string) =>
    apiFetch<T>(`/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`),

  post: <T>(subpath: string, body?: unknown) =>
    apiFetch<T>(`/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`, {
      method: 'POST',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),

  del: <T>(subpath: string) =>
    apiFetch<T>(`/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`, { method: 'DELETE' }),
}

// Hosts are served by the plugin's single auth'd router at /api/plugins/<id>/,
// the same path as containers — one client, one surface.
export const dockerManagerApi = {
  getHosts: () => pluginApi.get<{ hosts: DockerHost[] }>('hosts'),

  upsertHost: (host: Omit<DockerHost, 'id'> & { id?: number }) =>
    pluginApi.post<{ host: DockerHost; hosts: DockerHost[] }>('hosts', host),

  deleteHost: (id: number) =>
    pluginApi.del<{ ok: boolean; hosts: DockerHost[] }>(`hosts/${id}`),
}
