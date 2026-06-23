const PLUGIN_ID_PREFIX = 'lyndrix.plugin.docker'
const TOKEN_KEY = 'lyndrix_token'

export interface DockerHost {
  id: number
  name: string
  ip: string
  port: number
  scheme: string
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers as Record<string, string> | undefined),
  }

  const res = await fetch(path, { ...init, headers })

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    window.location.href = '/login'
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
