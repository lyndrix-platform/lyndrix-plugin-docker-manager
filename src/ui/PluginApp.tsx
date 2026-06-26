import React, { useState, useEffect, useCallback } from 'react'
import { pluginApi, dockerManagerApi } from './lib/api'
import type { DockerHost } from './lib/api'

// ─── Types ────────────────────────────────────────────────────────────────────

interface Container {
  id: string
  raw_id: string
  name: string
  image: string | null
  state: string | null
  status: string | null
}

interface HostEntry {
  host: DockerHost
  containers: Container[]
  error: string | null
}

type ContainersResponse = Record<string, HostEntry>

type ContainerAction = 'start' | 'stop' | 'restart'

// ─── Helpers ─────────────────────────────────────────────────────────────────

function stateColor(state: string | null): string {
  switch ((state ?? '').toLowerCase()) {
    case 'running': return 'var(--lx-state-up)'
    case 'exited':
    case 'dead':   return 'var(--lx-state-down)'
    case 'paused': return 'var(--lx-state-paused)'
    default:       return 'var(--lx-state-unknown)'
  }
}

function stateBadgeClass(state: string | null): string {
  switch ((state ?? '').toLowerCase()) {
    case 'running': return 'lx-badge--up'
    case 'exited':
    case 'dead':   return 'lx-badge--down'
    case 'paused': return 'lx-badge--paused'
    default:       return 'lx-badge--muted'
  }
}

function stateLabel(state: string | null): string {
  if (!state) return 'Unbekannt'
  return state.charAt(0).toUpperCase() + state.slice(1)
}

// ─── Shared sub-components ────────────────────────────────────────────────────

function MatIcon({ name, size = 18 }: { name: string; size?: number }) {
  return <span className="material-icons" style={{ fontSize: size }}>{name}</span>
}

function StateBadge({ state }: { state: string | null }) {
  return (
    <span className={`lx-badge ${stateBadgeClass(state)}`}>
      <span className="lx-dot" />
      {stateLabel(state)}
    </span>
  )
}

// ─── Responsive styles (injected once) ────────────────────────────────────────
// Inline styles can't carry media queries, so the layout-critical properties live
// here as classes with a phone breakpoint. Base = desktop; <=640px = mobile.

const DM_STYLES = `
.dm-page { max-width: 900px; margin: 0 auto; padding: 32px 24px 48px; }
.dm-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 24px; gap: 16px; }
.dm-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.dm-row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--lx-border-soft); transition: background 0.12s ease; }
.dm-row-main { flex: 1; min-width: 0; }
.dm-row-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.dm-grid { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.dm-host-list { flex: 1 1 380px; overflow: hidden; }
.dm-host-form { flex: 0 0 280px; overflow: hidden; }
@media (max-width: 640px) {
  .dm-page { padding: 18px 12px 40px; }
  .dm-header { flex-direction: column; align-items: stretch; gap: 12px; }
  .dm-actions { flex-wrap: wrap; }
  .dm-row { flex-wrap: wrap; }
  .dm-row-actions { width: 100%; justify-content: flex-end; flex-wrap: wrap; margin-top: 4px; }
  .dm-grid { flex-direction: column; }
  .dm-host-list, .dm-host-form { flex: 1 1 auto; width: 100%; }
}
`

function DMStyles() {
  return <style dangerouslySetInnerHTML={{ __html: DM_STYLES }} />
}

// ─── Container view ───────────────────────────────────────────────────────────

function ContainerRow({
  container,
  hostId,
  onAction,
}: {
  container: Container
  hostId: number
  onAction: (hostId: number, containerId: string, action: ContainerAction) => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const isRunning = (container.state ?? '').toLowerCase() === 'running'

  async function act(action: ContainerAction) {
    setBusy(true)
    try {
      await onAction(hostId, container.id, action)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="dm-row"
      onMouseEnter={(e) => (e.currentTarget.style.background =
        'color-mix(in srgb, var(--lx-elevated) 55%, transparent)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <span style={{ width: 3, height: 34, borderRadius: 2, background: stateColor(container.state), flexShrink: 0 }} />
      <div className="dm-row-main">
        <div style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--lx-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {container.name}
        </div>
        <div className="lx-mono" style={{ fontSize: '0.6875rem', color: 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: 2 }}>
          {container.image ?? '—'} · {container.id}
        </div>
      </div>
      <div className="dm-row-actions">
        <StateBadge state={container.state} />
        {isRunning ? (
          <>
            <button className="lx-btn lx-btn--secondary lx-btn--sm" disabled={busy} onClick={() => void act('restart')}>
              <MatIcon name="restart_alt" size={15} />Restart
            </button>
            <button className="lx-btn lx-btn--danger lx-btn--sm" disabled={busy} onClick={() => void act('stop')}>
              <MatIcon name="stop" size={15} />Stop
            </button>
          </>
        ) : (
          <button className="lx-btn lx-btn--primary lx-btn--sm" disabled={busy} onClick={() => void act('start')}>
            <MatIcon name="play_arrow" size={15} />Start
          </button>
        )}
      </div>
    </div>
  )
}

function HostCard({
  entry,
  onAction,
  onRefresh,
}: {
  entry: HostEntry
  onAction: (hostId: number, containerId: string, action: ContainerAction) => Promise<void>
  onRefresh: () => void
}) {
  const { host, containers, error } = entry
  const runningCount = containers.filter((c) => (c.state ?? '').toLowerCase() === 'running').length

  return (
    <div className="lx-card" style={{ marginBottom: 16, overflow: 'hidden' }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '14px 16px',
        borderBottom: '1px solid var(--lx-border-soft)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, minWidth: 0 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 'var(--lx-radius-sm)', flexShrink: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'color-mix(in srgb, var(--lx-accent) 12%, transparent)', color: 'var(--lx-accent)',
          }}>
            <MatIcon name="dns" size={20} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--lx-text)' }}>{host.name}</div>
            <div className="lx-mono" style={{ fontSize: '0.6875rem', color: 'var(--lx-text-muted)', marginTop: 2 }}>
              {host.scheme}://{host.ip}:{host.port}
            </div>
          </div>
        </div>
        {error ? (
          <span className="lx-badge lx-badge--down"><span className="lx-dot" />Verbindungsfehler</span>
        ) : (
          <span className="lx-badge lx-badge--muted">{runningCount}/{containers.length} aktiv</span>
        )}
      </div>

      {error && (
        <div style={{ padding: '14px 16px', fontSize: '0.8125rem', color: 'var(--lx-state-down)' }}>{error}</div>
      )}
      {!error && containers.length === 0 && (
        <div style={{ padding: '20px 16px', fontSize: '0.8125rem', color: 'var(--lx-text-muted)', textAlign: 'center' }}>
          Keine Container gefunden.
        </div>
      )}
      {!error && containers.map((c) => (
        <ContainerRow
          key={c.id || c.name}
          container={c}
          hostId={host.id}
          onAction={async (hid, cid, action) => {
            await onAction(hid, cid, action)
            onRefresh()
          }}
        />
      ))}
    </div>
  )
}

function goSettings() {
  const p = window.location.pathname.replace(/\/+$/, '')
  const target = p.endsWith('/docker') ? `${p}/settings` : `${p}/docker/settings`
  window.location.assign(target)
}

function ContainerView() {
  const [data, setData] = useState<ContainersResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const fetchContainers = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await pluginApi.get<ContainersResponse>('containers')
      setData(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Fehler beim Laden')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void fetchContainers() }, [fetchContainers])

  async function handleAction(hostId: number, containerId: string, action: ContainerAction) {
    setActionError(null)
    try {
      await pluginApi.post('containers/action', { host_id: hostId, container_id: containerId, action })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Aktion '${action}' fehlgeschlagen`)
    }
  }

  const entries = data ? Object.values(data) : []
  const totalContainers = entries.reduce((n, e) => n + e.containers.length, 0)
  const totalRunning = entries.reduce(
    (n, e) => n + e.containers.filter((c) => (c.state ?? '').toLowerCase() === 'running').length, 0)

  return (
    <div className="dm-page">
      <div className="dm-header">
        <div>
          <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 600, letterSpacing: '-0.01em', color: 'var(--lx-text)' }}>
            Docker Manager
          </h1>
          <p style={{ margin: '6px 0 0', fontSize: '0.8125rem', color: 'var(--lx-text-muted)' }}>
            {entries.length} Host{entries.length !== 1 ? 's' : ''} · {totalRunning}/{totalContainers} Container aktiv
          </p>
        </div>
        <div className="dm-actions">
          <button className="lx-btn lx-btn--secondary lx-btn--sm" onClick={() => void fetchContainers()} disabled={loading}>
            <MatIcon name="refresh" size={15} />{loading ? 'Lade…' : 'Aktualisieren'}
          </button>
          <button className="lx-btn lx-btn--secondary lx-btn--sm" onClick={goSettings}>
            <MatIcon name="settings" size={15} />Settings
          </button>
        </div>
      </div>

      {error && <ErrorBox msg={error} />}
      {actionError && <ErrorBox msg={actionError} />}

      {loading && !data && (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
          <div className="lx-spinner" />
        </div>
      )}

      {!loading && data && entries.length === 0 && (
        <div className="lx-card lx-empty">
          <MatIcon name="dns" size={34} />
          <p style={{ margin: 0, fontSize: '0.875rem' }}>Keine Docker-Hosts konfiguriert.</p>
        </div>
      )}

      {entries.map((entry) => (
        <HostCard
          key={entry.host.id}
          entry={entry}
          onAction={handleAction}
          onRefresh={() => void fetchContainers()}
        />
      ))}
    </div>
  )
}

// ─── Settings view ────────────────────────────────────────────────────────────

const EMPTY_FORM = { name: '', ip: '', port: 2375, scheme: 'http' }

function ErrorBox({ msg }: { msg: string }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      padding: '12px 16px',
      borderRadius: 'var(--lx-radius-md)',
      background: 'color-mix(in srgb, var(--lx-state-down) 10%, transparent)',
      border: '1px solid color-mix(in srgb, var(--lx-state-down) 25%, transparent)',
      color: 'var(--lx-state-down)',
      fontSize: '0.8125rem',
      marginBottom: 16,
    }}>
      <MatIcon name="error_outline" size={17} />{msg}
    </div>
  )
}

function CardHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="lx-section-title" style={{
      padding: '14px 16px',
      borderBottom: '1px solid var(--lx-border-soft)',
    }}>
      {children}
    </div>
  )
}

function SettingsView() {
  const [hosts, setHosts] = useState<DockerHost[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [editId, setEditId] = useState<number | null>(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [busy, setBusy] = useState(false)

  const loadHosts = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await dockerManagerApi.getHosts()
      setHosts(res.hosts)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Fehler beim Laden')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadHosts() }, [loadHosts])

  function startEdit(host: DockerHost) {
    setEditId(host.id)
    setForm({ name: host.name, ip: host.ip, port: host.port, scheme: host.scheme })
    setNotice(null)
  }

  function cancelEdit() {
    setEditId(null)
    setForm(EMPTY_FORM)
    setNotice(null)
  }

  async function saveHost() {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const payload = editId !== null ? { ...form, id: editId } : form
      const res = await dockerManagerApi.upsertHost(payload)
      setHosts(res.hosts)
      cancelEdit()
      setNotice('Host gespeichert.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Speichern fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  async function deleteHost(id: number) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const res = await dockerManagerApi.deleteHost(id)
      setHosts(res.hosts)
      if (editId === id) cancelEdit()
      setNotice('Host entfernt.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Löschen fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const isEditing = editId !== null

  return (
    <div className="dm-page">
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 }}>
        <button
          className="lx-icon-btn"
          onClick={() => window.history.back()}
          title="Zurück"
        >
          <MatIcon name="arrow_back" size={18} />
        </button>
        <div>
          <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 600, letterSpacing: '-0.01em', color: 'var(--lx-text)' }}>
            Docker Manager
          </h1>
          <p style={{ margin: '4px 0 0', fontSize: '0.8125rem', color: 'var(--lx-text-muted)' }}>
            Einstellungen · Docker-Hosts verwalten
          </p>
        </div>
      </div>

      {error && <ErrorBox msg={error} />}
      {notice && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '12px 16px',
          borderRadius: 'var(--lx-radius-md)',
          background: 'color-mix(in srgb, var(--lx-state-up) 10%, transparent)',
          border: '1px solid color-mix(in srgb, var(--lx-state-up) 25%, transparent)',
          color: 'var(--lx-state-up)',
          fontSize: '0.8125rem',
          marginBottom: 16,
        }}>
          <MatIcon name="check_circle" size={17} />{notice}
        </div>
      )}

      <div className="dm-grid">

        {/* Host list */}
        <div className="lx-card dm-host-list">
          <CardHeader>Docker Hosts</CardHeader>

          {loading && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '32px 0' }}>
              <div className="lx-spinner" />
            </div>
          )}

          {!loading && hosts.length === 0 && (
            <div className="lx-empty" style={{ padding: '40px 16px' }}>
              <MatIcon name="dns" size={34} />
              <p style={{ margin: 0, fontSize: '0.8125rem' }}>Noch keine Hosts konfiguriert.</p>
            </div>
          )}

          {hosts.map((host) => {
            const active = editId === host.id
            return (
              <div
                key={host.id}
                onClick={() => startEdit(host)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '12px 16px',
                  borderBottom: '1px solid var(--lx-border-soft)',
                  cursor: 'pointer',
                  background: active ? 'color-mix(in srgb, var(--lx-accent) 8%, transparent)' : 'transparent',
                  borderLeft: active ? '2px solid var(--lx-accent)' : '2px solid transparent',
                  transition: 'background 0.12s ease',
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: '0.8125rem', fontWeight: 600, color: active ? 'var(--lx-accent)' : 'var(--lx-text)' }}>
                    {host.name}
                  </div>
                  <div className="lx-mono" style={{ fontSize: '0.6875rem', color: 'var(--lx-text-muted)', marginTop: 2 }}>
                    {host.scheme}://{host.ip}:{host.port}
                  </div>
                </div>
                <button
                  className="lx-icon-btn lx-icon-btn--danger"
                  onClick={(e) => { e.stopPropagation(); void deleteHost(host.id) }}
                  disabled={busy}
                  title="Löschen"
                >
                  <MatIcon name="delete_outline" size={17} />
                </button>
              </div>
            )
          })}
        </div>

        {/* Add / edit form */}
        <div className="lx-card dm-host-form">
          <CardHeader>{isEditing ? 'Host bearbeiten' : 'Host hinzufügen'}</CardHeader>

          <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <label className="lx-label">Name</label>
              <input
                className="lx-input"
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="prod-server-01"
              />
            </div>

            <div>
              <label className="lx-label">IP-Adresse</label>
              <input
                className="lx-input"
                value={form.ip}
                onChange={(e) => setForm((f) => ({ ...f, ip: e.target.value }))}
                placeholder="192.168.1.10"
              />
            </div>

            <div style={{ display: 'flex', gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label className="lx-label">Port</label>
                <input
                  type="number"
                  className="lx-input"
                  value={form.port}
                  onChange={(e) => setForm((f) => ({ ...f, port: Number(e.target.value) }))}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label className="lx-label">Schema</label>
                <select
                  className="lx-select"
                  value={form.scheme}
                  onChange={(e) => setForm((f) => ({ ...f, scheme: e.target.value }))}
                >
                  <option value="http">http</option>
                  <option value="https">https</option>
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
              {isEditing && (
                <button className="lx-btn lx-btn--secondary lx-btn--sm" onClick={cancelEdit} disabled={busy}>
                  Abbrechen
                </button>
              )}
              <button
                className="lx-btn lx-btn--primary lx-btn--sm"
                onClick={() => void saveHost()}
                disabled={busy || !form.name.trim() || !form.ip.trim()}
              >
                {isEditing ? 'Speichern' : 'Hinzufügen'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Root — path-based routing ────────────────────────────────────────────────

export default function PluginApp() {
  const isSettings = window.location.pathname.endsWith('/settings')
  return (
    <>
      <DMStyles />
      {isSettings ? <SettingsView /> : <ContainerView />}
    </>
  )
}
