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

function stateLabel(state: string | null): string {
  if (!state) return 'Unbekannt'
  return state.charAt(0).toUpperCase() + state.slice(1)
}

// ─── Shared sub-components ────────────────────────────────────────────────────

function StateBadge({ state }: { state: string | null }) {
  const color = stateColor(state)
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      fontSize: '0.7rem',
      fontWeight: 600,
      color,
      background: `color-mix(in srgb, ${color} 12%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
      borderRadius: 'var(--lx-radius-sm)',
      padding: '2px 7px',
      letterSpacing: '0.04em',
      textTransform: 'uppercase',
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: color, flexShrink: 0 }} />
      {stateLabel(state)}
    </span>
  )
}

function ActionButton({
  label,
  onClick,
  disabled,
  variant = 'default',
}: {
  label: string
  onClick: () => void
  disabled?: boolean
  variant?: 'default' | 'danger' | 'primary'
}) {
  const accent =
    variant === 'danger' ? 'var(--lx-state-down)'
    : variant === 'primary' ? 'var(--lx-accent)'
    : 'var(--lx-accent)'
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: '3px 10px',
        fontSize: '0.7rem',
        fontWeight: 600,
        border: `1px solid color-mix(in srgb, ${accent} 40%, transparent)`,
        borderRadius: 'var(--lx-radius-sm)',
        background: variant === 'primary'
          ? `color-mix(in srgb, ${accent} 20%, transparent)`
          : `color-mix(in srgb, ${accent} 10%, transparent)`,
        color: disabled ? 'var(--lx-text-muted)' : accent,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'opacity 0.15s',
      }}
    >
      {label}
    </button>
  )
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
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: '0.75rem',
      padding: '0.5rem 0.75rem',
      borderBottom: '1px solid var(--lx-border-soft)',
    }}>
      <div style={{
        width: 3,
        height: 32,
        borderRadius: 2,
        background: stateColor(container.state),
        flexShrink: 0,
      }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: '0.8rem',
          fontWeight: 600,
          color: 'var(--lx-text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {container.name}
        </div>
        <div style={{
          fontSize: '0.65rem',
          color: 'var(--lx-text-muted)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {container.image ?? '—'} · {container.id}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexShrink: 0 }}>
        <StateBadge state={container.state} />
        {isRunning ? (
          <>
            <ActionButton label="Restart" disabled={busy} onClick={() => void act('restart')} />
            <ActionButton label="Stop" disabled={busy} onClick={() => void act('stop')} variant="danger" />
          </>
        ) : (
          <ActionButton label="Start" disabled={busy} onClick={() => void act('start')} />
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
    <div style={{
      background: 'var(--lx-surface)',
      border: '1px solid var(--lx-border-soft)',
      borderRadius: 'var(--lx-radius-md)',
      marginBottom: '1rem',
      overflow: 'hidden',
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0.75rem 1rem',
        borderBottom: '1px solid var(--lx-border-soft)',
        background: 'var(--lx-elevated)',
      }}>
        <div>
          <span style={{ fontSize: '0.875rem', fontWeight: 700, color: 'var(--lx-text)' }}>
            {host.name}
          </span>
          <span style={{ fontSize: '0.7rem', color: 'var(--lx-text-muted)', marginLeft: 8 }}>
            {host.scheme}://{host.ip}:{host.port}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          {!error && (
            <span style={{ fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>
              {runningCount}/{containers.length} aktiv
            </span>
          )}
          {error && (
            <span style={{ fontSize: '0.7rem', color: 'var(--lx-state-down)' }}>
              Verbindungsfehler
            </span>
          )}
        </div>
      </div>
      {error && (
        <div style={{ padding: '0.75rem 1rem', fontSize: '0.75rem', color: 'var(--lx-state-down)' }}>
          {error}
        </div>
      )}
      {!error && containers.length === 0 && (
        <div style={{ padding: '0.75rem 1rem', fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>
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

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.25rem' }}>
        <h1 style={{ margin: 0, fontSize: '1.125rem', fontWeight: 700, color: 'var(--lx-text)' }}>
          Docker Manager
        </h1>
        <button
          onClick={() => void fetchContainers()}
          disabled={loading}
          style={{
            padding: '0.375rem 0.75rem',
            borderRadius: 'var(--lx-radius-sm)',
            border: '1px solid var(--lx-border-soft)',
            background: 'var(--lx-surface)',
            color: loading ? 'var(--lx-text-muted)' : 'var(--lx-text)',
            fontSize: '0.75rem',
            cursor: loading ? 'not-allowed' : 'pointer',
          }}
        >
          {loading ? 'Lade…' : 'Aktualisieren'}
        </button>
      </div>

      {error && <ErrorBox msg={error} />}
      {actionError && <ErrorBox msg={actionError} />}

      {loading && !data && (
        <div style={{ color: 'var(--lx-text-muted)', fontSize: '0.875rem', textAlign: 'center', padding: '3rem 0' }}>
          Lade Container…
        </div>
      )}

      {!loading && data && entries.length === 0 && (
        <div style={{
          padding: '2rem',
          textAlign: 'center',
          color: 'var(--lx-text-muted)',
          fontSize: '0.875rem',
          background: 'var(--lx-surface)',
          border: '1px solid var(--lx-border-soft)',
          borderRadius: 'var(--lx-radius-md)',
        }}>
          Keine Docker-Hosts konfiguriert.
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
      padding: '0.75rem 1rem',
      borderRadius: 'var(--lx-radius-md)',
      background: `color-mix(in srgb, var(--lx-state-down) 10%, transparent)`,
      border: `1px solid color-mix(in srgb, var(--lx-state-down) 25%, transparent)`,
      color: 'var(--lx-state-down)',
      fontSize: '0.8rem',
      marginBottom: '1rem',
    }}>
      {msg}
    </div>
  )
}

function inputStyle(wider = false): React.CSSProperties {
  return {
    width: wider ? '100%' : undefined,
    padding: '0.35rem 0.6rem',
    fontSize: '0.8rem',
    borderRadius: 'var(--lx-radius-sm)',
    border: '1px solid var(--lx-border-soft)',
    background: 'var(--lx-elevated)',
    color: 'var(--lx-text)',
    outline: 'none',
    boxSizing: 'border-box',
  }
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
      const payload = editId !== null
        ? { ...form, id: editId }
        : form
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
    <div style={{ maxWidth: 860, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1.5rem' }}>
        <a
          href="#"
          onClick={(e) => { e.preventDefault(); window.history.back() }}
          style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)', textDecoration: 'none' }}
        >
          ← Zurück
        </a>
        <h1 style={{ margin: 0, fontSize: '1.125rem', fontWeight: 700, color: 'var(--lx-text)' }}>
          Docker Manager — Einstellungen
        </h1>
      </div>

      {error && <ErrorBox msg={error} />}
      {notice && (
        <div style={{
          padding: '0.6rem 1rem',
          borderRadius: 'var(--lx-radius-md)',
          background: `color-mix(in srgb, var(--lx-state-up) 10%, transparent)`,
          border: `1px solid color-mix(in srgb, var(--lx-state-up) 25%, transparent)`,
          color: 'var(--lx-state-up)',
          fontSize: '0.8rem',
          marginBottom: '1rem',
        }}>
          {notice}
        </div>
      )}

      <div style={{ display: 'flex', gap: '1.25rem', alignItems: 'flex-start', flexWrap: 'wrap' }}>

        {/* Host list */}
        <div style={{
          flex: '1 1 380px',
          background: 'var(--lx-surface)',
          border: '1px solid var(--lx-border-soft)',
          borderRadius: 'var(--lx-radius-md)',
          overflow: 'hidden',
        }}>
          <div style={{
            padding: '0.75rem 1rem',
            borderBottom: '1px solid var(--lx-border-soft)',
            background: 'var(--lx-elevated)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--lx-text)',
          }}>
            Docker Hosts
          </div>

          {loading && (
            <div style={{ padding: '1rem', fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>
              Lade…
            </div>
          )}

          {!loading && hosts.length === 0 && (
            <div style={{ padding: '1.25rem 1rem', fontSize: '0.8rem', color: 'var(--lx-text-muted)', textAlign: 'center' }}>
              Noch keine Hosts konfiguriert.
            </div>
          )}

          {hosts.map((host) => (
            <div
              key={host.id}
              onClick={() => startEdit(host)}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '0.6rem 1rem',
                borderBottom: '1px solid var(--lx-border-soft)',
                cursor: 'pointer',
                background: editId === host.id
                  ? `color-mix(in srgb, var(--lx-accent) 8%, transparent)`
                  : 'transparent',
              }}
            >
              <div>
                <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--lx-text)' }}>
                  {host.name}
                </div>
                <div style={{ fontSize: '0.7rem', color: 'var(--lx-text-muted)', fontFamily: 'monospace' }}>
                  {host.scheme}://{host.ip}:{host.port}
                </div>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); void deleteHost(host.id) }}
                disabled={busy}
                title="Löschen"
                style={{
                  background: 'none',
                  border: 'none',
                  cursor: busy ? 'not-allowed' : 'pointer',
                  color: 'var(--lx-state-down)',
                  fontSize: '1rem',
                  lineHeight: 1,
                  padding: '0.2rem 0.4rem',
                  borderRadius: 'var(--lx-radius-sm)',
                  opacity: busy ? 0.4 : 0.7,
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>

        {/* Add / edit form */}
        <div style={{
          flex: '0 0 260px',
          background: 'var(--lx-surface)',
          border: '1px solid var(--lx-border-soft)',
          borderRadius: 'var(--lx-radius-md)',
          overflow: 'hidden',
        }}>
          <div style={{
            padding: '0.75rem 1rem',
            borderBottom: '1px solid var(--lx-border-soft)',
            background: 'var(--lx-elevated)',
            fontSize: '0.8rem',
            fontWeight: 600,
            color: 'var(--lx-text)',
          }}>
            {isEditing ? 'Host bearbeiten' : 'Host hinzufügen'}
          </div>

          <div style={{ padding: '1rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
            <label style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
              Name
              <input
                style={{ ...inputStyle(true), marginTop: 3 }}
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="prod-server-01"
              />
            </label>

            <label style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
              IP-Adresse
              <input
                style={{ ...inputStyle(true), marginTop: 3 }}
                value={form.ip}
                onChange={(e) => setForm((f) => ({ ...f, ip: e.target.value }))}
                placeholder="192.168.1.10"
              />
            </label>

            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <label style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', flex: 1 }}>
                Port
                <input
                  type="number"
                  style={{ ...inputStyle(true), marginTop: 3 }}
                  value={form.port}
                  onChange={(e) => setForm((f) => ({ ...f, port: Number(e.target.value) }))}
                />
              </label>
              <label style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', flex: 1 }}>
                Schema
                <select
                  style={{ ...inputStyle(true), marginTop: 3 }}
                  value={form.scheme}
                  onChange={(e) => setForm((f) => ({ ...f, scheme: e.target.value }))}
                >
                  <option value="http">http</option>
                  <option value="https">https</option>
                </select>
              </label>
            </div>

            <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: 4 }}>
              {isEditing && (
                <ActionButton label="Abbrechen" onClick={cancelEdit} disabled={busy} />
              )}
              <ActionButton
                label={isEditing ? 'Speichern' : 'Hinzufügen'}
                onClick={() => void saveHost()}
                disabled={busy || !form.name.trim() || !form.ip.trim()}
                variant="primary"
              />
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
  return isSettings ? <SettingsView /> : <ContainerView />
}
