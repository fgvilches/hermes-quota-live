/**
 * hermes-quota-live — panel lateral "Quota & Usage" para Hermes Desktop.
 *
 * Port de la UI de opencode-quota-gui al SDK de plugins de Hermes: no embebe
 * el dashboard original (tiene frame-ancestors 'none' y su panel nativo depende
 * de OpenCode Desktop). Todos los datos vienen del backend Python hermano
 * (/api/plugins/hermes-quota-live/snapshot y /refresh) a traves de ctx.rest.
 *
 * ESM plano, cargado SIN compilar: UI con jsx()/jsxs(), nunca sintaxis JSX.
 * Unicos imports: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { cn, haptic, host, icons, Tip, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-quota-live'
const POLL_MS = 30000
const RANGES = [
  { id: '24h', label: '24 h' },
  { id: '7d', label: '7 d' },
  { id: '30d', label: '30 d' },
  { id: 'lifetime', label: 'Todo' }
]
const TABS = [
  { id: 'quota', label: 'Cuota' },
  { id: 'analytics', label: 'Análisis' },
  { id: 'models', label: 'Modelos' },
  { id: 'about', label: 'Info' }
]

const EMPTY = {
  generatedAt: null,
  source: 'hermes',
  opencode: { connected: false },
  quota: { cards: [] },
  analytics: { range: '7d', totalTokens: 0, cacheRead: 0, input: 0, reasoning: 0, output: 0, daily: [], projects: [] },
  models: [],
  notes: []
}

// -- helpers -----------------------------------------------------------------

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

/** 1230 -> 1.2K, 1500000 -> 1.5M. */
function fmt(value) {
  const n = num(value)
  const abs = Math.abs(n)
  if (abs >= 1e9) return `${(n / 1e9).toFixed(1).replace(/\.0$/, '')}B`
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`
  return String(Math.round(n))
}

/** remainingPct puede venir en 0-100 (contract). Si hay used/limit, se prefiere
 *  el calculo explicito, que es mas confiable que un porcentaje precomputado. */
function pctOf(window) {
  if (!window || typeof window !== 'object') return null
  const used = num(window.used)
  const limit = num(window.limit)
  if (limit > 0 && Number.isFinite(used)) {
    return Math.max(0, Math.min(100, (1 - used / limit) * 100))
  }
  const raw = Number(window.remainingPct)
  if (!Number.isFinite(raw)) return null
  return Math.max(0, Math.min(100, raw))
}

/** Porcentaje consumido: complemento exacto de pctOf (que es el remanente). */
function pctUsed(window) {
  const free = pctOf(window)
  return free === null ? null : Math.max(0, Math.min(100, 100 - free))
}

function statusOf(window) {
  const s = typeof window?.status === 'string' ? window.status.toLowerCase() : ''
  if (/exhaust|deplet|limit|agot/.test(s)) return 'Agotado'
  if (/low|warn|baj/.test(s)) return 'Bajo'
  const p = pctOf(window)
  if (p === null) return 'Sin dato'
  if (p <= 0) return 'Agotado'
  if (p <= 20) return 'Bajo'
  return 'Disponible'
}

function countdown(iso, now) {
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const ms = t - now
  if (ms <= 0) return 'ya venció'
  const mins = Math.floor(ms / 60000)
  const days = Math.floor(mins / 1440)
  const hours = Math.floor((mins % 1440) / 60)
  const minutes = mins % 60
  if (days > 0) return `en ${days} d ${hours} h`
  if (hours > 0) return `en ${hours} h ${minutes} min`
  return `en ${minutes} min`
}

function clock(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' })
}

/** Iniciales del proveedor, sin colores hardcoded. */
function ProviderMark({ provider }) {
  const name = String(provider || '?').trim()
  const initials = name
    .split(/[\s._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part[0].toUpperCase())
    .join('') || '?'
  return jsx('span', {
    'aria-hidden': true,
    className: 'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[0.625rem] font-semibold',
    style: { background: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-primary)' },
    children: initials
  })
}

function SectionTitle({ children, right }) {
  return jsxs('div', {
    className: 'mb-2 flex items-center justify-between gap-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-semibold uppercase tracking-wide', style: { color: 'var(--ui-text-tertiary)' }, children }),
      right || null
    ]
  })
}

function Empty({ children }) {
  return jsx('div', {
    className: 'rounded-lg border border-dashed px-3 py-5 text-center text-xs leading-relaxed',
    style: { borderColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' },
    children
  })
}

function Bar({ pct }) {
  const p = pct === null ? 0 : pct
  return jsx('div', {
    className: 'h-1.5 w-full overflow-hidden rounded-full',
    style: { background: 'var(--ui-stroke-secondary)' },
    children: jsx('div', {
      className: 'h-full rounded-full transition-[width] duration-300',
      style: { width: `${p}%`, background: 'var(--ui-accent)' }
    })
  })
}

function RangePicker({ value, onChange }) {
  return jsxs('div', {
    className: 'inline-flex items-center gap-0.5 rounded-md px-0.5 py-0.5',
    style: { border: '1px solid var(--ui-stroke-secondary)' },
    children: RANGES.map(r => jsx('button', {
      key: r.id,
      type: 'button',
      title: `Rango: ${r.label}`,
      onClick: () => onChange(r.id),
      className: cn(
        'rounded px-1.5 py-0.5 text-[0.625rem] transition-colors',
        value === r.id ? 'font-semibold' : 'hover:text-(--ui-text-secondary)'
      ),
      style: { color: value === r.id ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)' },
      children: r.label
    }))
  })
}

// -- pestaña: Cuota ----------------------------------------------------------

function WindowRow({ window, now, mode }) {
  const free = pctOf(window)
  const p = mode === 'used' ? pctUsed(window) : free
  const status = statusOf(window)
  const usedLimit = Number.isFinite(num(window.used)) && num(window.limit) > 0
    ? `${fmt(window.used)} / ${fmt(window.limit)}${window.unit ? ` ${window.unit}` : ''}`
    : ''
  const reset = countdown(window.resetAt, now)
  return jsxs('div', {
    className: 'mt-2.5',
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-baseline justify-between gap-2 text-[0.6875rem]',
        children: [
          jsx('span', { className: 'truncate', style: { color: 'var(--ui-text-secondary)' }, children: window.label || 'Ventana' }),
          jsx('span', {
            className: 'shrink-0 font-semibold tabular-nums',
            title: mode === 'used' ? 'Porcentaje usado' : 'Porcentaje libre',
            style: { color: free !== null && free <= 20 ? 'var(--ui-accent)' : 'var(--ui-text-primary)' },
            children: p === null ? '—' : `${Math.round(p)}% ${mode === 'used' ? 'usado' : 'libre'}`
          })
        ]
      }),
      jsx(Bar, { pct: p }),
      jsxs('div', {
        className: 'mt-1 flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5 text-[0.625rem]',
        style: { color: 'var(--ui-text-tertiary)' },
        children: [
          jsx('span', { children: usedLimit || status }),
          jsxs('span', { className: 'inline-flex items-center gap-1.5', children: [
            jsx('span', { children: status }),
            reset ? jsx('span', { children: `· ${reset}` }) : null
          ] })
        ]
      })
    ]
  })
}

function QuotaCard({ card, now, mode }) {
  return jsxs('div', {
    className: 'rounded-lg border p-3',
    style: { borderColor: 'var(--ui-stroke-secondary)' },
    children: [
      jsxs('div', { className: 'flex items-start gap-2.5', children: [
        jsx(ProviderMark, { provider: card.provider }),
        jsxs('div', { className: 'min-w-0 flex-1', children: [
          jsx('div', { className: 'truncate text-xs font-semibold', children: card.label || card.provider || 'Proveedor' }),
          jsxs('div', { className: 'mt-0.5 flex flex-wrap gap-x-2 text-[0.625rem]', style: { color: 'var(--ui-text-tertiary)' }, children: [
            card.account ? jsx('span', { children: card.account }) : null,
            card.plan ? jsx('span', { children: `· ${card.plan}` }) : null,
            card.active === false ? jsx('span', { children: '· inactiva' }) : null
          ] })
        ] })
      ] }),
      (card.windows || []).map(w => jsx(WindowRow, { key: w.id || w.label || 'window', window: w, now, mode })),
      !card.windows || card.windows.length === 0
        ? jsx('div', { className: 'mt-2 text-[0.625rem]', style: { color: 'var(--ui-text-tertiary)' }, children: 'Sin ventanas informadas.' })
        : null,
      card.error ? jsx('div', {
        className: 'mt-2 rounded px-2 py-1 text-[0.625rem]',
        style: { background: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)' },
        children: `Error: ${card.error}`
      }) : null
    ]
  })
}

function QuotaTab({ data, now, mode, onMode }) {
  const cards = Array.isArray(data.quota?.cards) ? data.quota.cards : []
  const noQuotaSource = data.source !== 'opencode' && data.source !== 'mixed'
  const opencodeDown = data.opencode && data.opencode.connected === false

  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
      jsx('span', { className: 'text-[0.625rem] uppercase tracking-wide', style: { color: 'var(--ui-text-tertiary)' }, children: 'Porcentaje' }),
      jsxs('div', { className: 'inline-flex rounded-md p-0.5', style: { background: 'var(--ui-stroke-secondary)' }, children: [
        jsx('button', {
          type: 'button',
          title: 'Mostrar cuánto se consumió de cada ventana',
          onClick: () => onMode('used'),
          className: cn('rounded px-2 py-0.5 text-[0.625rem] transition-colors', mode === 'used' ? 'font-semibold' : ''),
          style: mode === 'used' ? { background: 'var(--ui-accent)', color: 'var(--ui-text-primary)' } : { color: 'var(--ui-text-tertiary)' },
          children: 'Usado'
        }),
        jsx('button', {
          type: 'button',
          title: 'Mostrar cuánto queda disponible en cada ventana',
          onClick: () => onMode('free'),
          className: cn('rounded px-2 py-0.5 text-[0.625rem] transition-colors', mode === 'free' ? 'font-semibold' : ''),
          style: mode === 'free' ? { background: 'var(--ui-accent)', color: 'var(--ui-text-primary)' } : { color: 'var(--ui-text-tertiary)' },
          children: 'Libre'
        })
      ] })
    ] }),
    noQuotaSource
      ? jsx(Empty, { children: jsxs('span', { children: [
          'Sin fuente de cuota conectada. ',
          opencodeDown && data.opencode.error
            ? `OpenCode: ${data.opencode.error}. `
            : 'OpenCode no está disponible. ',
          'Se muestra solo el uso local de Hermes (si hay datos).'
        ] }) })
      : null,
    cards.length === 0
      ? jsx(Empty, { children: 'Todavía no hay tarjetas de cuota. Las cuentas detectadas aparecerán acá cuando el backend las informe.' })
      : cards.map(card => jsx(QuotaCard, { key: card.id || card.provider || 'card', card, now, mode }))
  ] })
}

// -- pestaña: Análisis --------------------------------------------------------

function Stat({ label, value }) {
  return jsxs('div', { className: 'flex-1 rounded-lg border px-2 py-2', style: { borderColor: 'var(--ui-stroke-secondary)' }, children: [
    jsx('div', { className: 'text-[0.625rem]', style: { color: 'var(--ui-text-tertiary)' }, children: label }),
    jsx('div', { className: 'mt-0.5 text-xs font-semibold tabular-nums', children: fmt(value) })
  ] })
}

function DailyChart({ daily }) {
  const rows = Array.isArray(daily) ? daily : []
  if (rows.length === 0) return jsx(Empty, { children: 'Sin datos diarios para el rango elegido.' })
  const max = Math.max(...rows.map(r => num(r.tokens)), 1)
  return jsxs('div', {
    className: 'flex h-24 items-end gap-1',
    children: rows.map((r, i) => jsx('div', {
      key: r.date || i,
      className: 'flex min-w-0 flex-1 flex-col items-center justify-end gap-1',
      title: `${r.date}: ${fmt(r.tokens)} tokens`,
      children: [
        jsxs('div', {
          className: 'w-full rounded-t-sm',
          style: { height: `${Math.max(3, (num(r.tokens) / max) * 100)}%`, background: 'var(--ui-accent)' }
        }),
        jsx('span', {
          className: 'w-full truncate text-center text-[0.5rem]',
          style: { color: 'var(--ui-text-tertiary)' },
          children: String(r.date || '').slice(5)
        })
      ]
    }))
  })
}

function AnalyticsTab({ data, range, onRange }) {
  const a = data.analytics || EMPTY.analytics
  const projects = Array.isArray(a.projects) ? a.projects : []
  const shownRange = a.range || range
  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs(SectionTitle, { right: jsx(RangePicker, { value: range, onChange: onRange }), children: `Análisis · ${shownRange}` }),
    jsxs('div', { className: 'flex gap-1.5', children: [
      jsx(Stat, { label: 'Tokens', value: a.totalTokens }),
      jsx(Stat, { label: 'Cache', value: a.cacheRead })
    ] }),
    jsxs('div', { className: 'flex gap-1.5', children: [
      jsx(Stat, { label: 'Input', value: a.input }),
      jsx(Stat, { label: 'Razonamiento', value: a.reasoning }),
      jsx(Stat, { label: 'Output', value: a.output })
    ] }),
    jsx('div', {
      className: 'rounded-lg border p-2.5',
      style: { borderColor: 'var(--ui-stroke-secondary)' },
      children: jsx(DailyChart, { daily: a.daily })
    }),
    jsx(SectionTitle, { children: 'Proyectos' }),
    projects.length === 0
      ? jsx(Empty, { children: 'Sin proyectos con actividad en este rango.' })
      : projects.map(p => jsxs('div', {
          key: p.id || p.label,
          className: 'flex items-center justify-between gap-2 border-b py-1.5 text-[0.6875rem] last:border-b-0',
          style: { borderColor: 'var(--ui-stroke-secondary)' },
          children: [
            jsxs('span', { className: 'min-w-0 truncate', children: [
              p.label || p.id || 'Proyecto',
              p.sessions ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: ` · ${p.sessions} sesiones` }) : null
            ] }),
            jsx('span', { className: 'shrink-0 tabular-nums', style: { color: 'var(--ui-text-secondary)' }, children: fmt(p.tokens) })
          ]
        }))
  ] })
}

// -- pestaña: Modelos ---------------------------------------------------------

function ModelsTab({ data, range, onRange }) {
  const models = Array.isArray(data.models) ? data.models : []
  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs(SectionTitle, { right: jsx(RangePicker, { value: range, onChange: onRange }), children: 'Modelos' }),
    models.length === 0
      ? jsx(Empty, { children: 'Sin consumo de modelos registrado para este rango.' })
      : jsxs('div', {
          className: 'overflow-hidden rounded-lg border',
          style: { borderColor: 'var(--ui-stroke-secondary)' },
          children: [
            jsxs('div', {
              className: 'grid grid-cols-[1fr_3.2rem] gap-2 px-2 py-1.5 text-[0.5625rem] font-semibold uppercase tracking-wide',
              style: { color: 'var(--ui-text-tertiary)', borderBottom: '1px solid var(--ui-stroke-secondary)' },
              children: [jsx('span', { children: 'Proveedor / modelo' }), jsx('span', { className: 'text-right', children: 'Tokens' })]
            }),
            models.map((m, i) => jsxs('div', {
              key: `${m.provider || ''}:${m.model || ''}:${i}`,
              className: 'grid grid-cols-[1fr_3.2rem] gap-2 px-2 py-1.5 text-[0.6875rem]',
              style: i % 2 ? { background: 'var(--ui-stroke-secondary)' } : undefined,
              children: [
                jsxs('div', { className: 'min-w-0', children: [
                  jsx('div', { className: 'truncate', children: m.model || 'Modelo' }),
                  jsx('div', { className: 'truncate text-[0.5625rem]', style: { color: 'var(--ui-text-tertiary)' }, children: m.provider || '' })
                ] }),
                jsx('div', {
                  className: 'text-right tabular-nums',
                  title: `input ${fmt(m.input)} · output ${fmt(m.output)} · cache ${fmt(m.cacheRead)} · razonamiento ${fmt(m.reasoning)}`,
                  children: fmt(m.tokens)
                })
              ]
            }))
          ]
        })
  ] })
}

// -- pestaña: Info ------------------------------------------------------------

function InfoRow({ label, value }) {
  return jsxs('div', {
    className: 'flex items-start justify-between gap-3 py-1.5 text-[0.6875rem]',
    style: { borderBottom: '1px solid var(--ui-stroke-secondary)' },
    children: [
      jsx('span', { className: 'shrink-0', style: { color: 'var(--ui-text-tertiary)' }, children: label }),
      jsx('span', { className: 'min-w-0 break-words text-right', children: value === undefined || value === null || value === '' ? '—' : String(value) })
    ]
  })
}

function AboutTab({ data, gateway }) {
  const notes = Array.isArray(data.notes) ? data.notes : []
  const sourceLabel = { opencode: 'OpenCode', hermes: 'Hermes', mixed: 'Mixto' }[data.source] || String(data.source || '—')
  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs('div', { children: [
      jsx(SectionTitle, { children: 'Origen de datos' }),
      jsx(InfoRow, { label: 'Fuente', value: sourceLabel }),
      jsx(InfoRow, { label: 'OpenCode conectado', value: data.opencode?.connected ? 'sí' : 'no' }),
      jsx(InfoRow, { label: 'Versión de OpenCode', value: data.opencode?.version }),
      jsx(InfoRow, { label: 'Error de OpenCode', value: data.opencode?.error }),
      jsx(InfoRow, { label: 'Gateway de Hermes', value: gateway || '—' }),
      jsx(InfoRow, { label: 'Actualizado', value: data.generatedAt ? clock(data.generatedAt) : '—' })
    ] }),
    notes.length > 0
      ? jsxs('div', { children: [
          jsx(SectionTitle, { children: 'Notas del backend' }),
          jsxs('ul', { className: 'flex flex-col gap-1 text-[0.6875rem] leading-relaxed', style: { color: 'var(--ui-text-secondary)' },
            children: notes.map((note, i) => jsx('li', { key: i, children: `• ${note}` })) })
        ] })
      : null,
    jsx('p', {
      className: 'text-[0.625rem] leading-relaxed',
      style: { color: 'var(--ui-text-tertiary)' },
      children: 'Panel local: réplica de la UI de opencode-quota-gui dentro de Hermes Desktop. Sin servicios externos ni datos enviados a la nube.'
    })
  ] })
}

// -- panel principal ----------------------------------------------------------

function QuotaPane({ ctx }) {
  const [tab, setTab] = useState('quota')
  const [range, setRange] = useState('7d')
  const [pctMode, setPctMode] = useState('used')
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const qc = useQueryClient()
  const gateway = useValue(host.state.gateway)

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), POLL_MS)
    return () => clearInterval(timer)
  }, [])

  const snap = useQuery({
    queryKey: [ID, 'snapshot', range],
    queryFn: () => ctx.rest(`/snapshot?range=${encodeURIComponent(range)}`),
    refetchInterval: POLL_MS,
    staleTime: 15000,
    retry: 1,
    refetchOnWindowFocus: false
  })
  const data = snap.data || EMPTY
  const generatedAt = data.generatedAt ? clock(data.generatedAt) : '—'

  const refresh = async () => {
    if (busy) return
    setBusy(true)
    try {
      await ctx.rest('/refresh', { method: 'POST' })
      await qc.invalidateQueries({ queryKey: [ID, 'snapshot'] })
      haptic('tap')
    } catch (error) {
      if (typeof host.notifyError === 'function') {
        host.notifyError(error, 'No se pudo actualizar la cuota')
      } else {
        host.notify({ kind: 'error', message: `No se pudo actualizar la cuota: ${error?.message || error}` })
      }
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', { className: 'flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
      jsx('div', { className: 'inline-flex flex-wrap gap-0.5 rounded-md p-0.5', style: { background: 'var(--ui-stroke-secondary)' },
        children: TABS.map(t => jsx('button', {
          key: t.id,
          type: 'button',
          onClick: () => setTab(t.id),
          className: cn(
            'rounded px-2 py-1 text-[0.6875rem] transition-colors',
            tab === t.id ? 'font-semibold' : 'text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
          ),
          style: tab === t.id ? { background: 'var(--ui-accent)', color: 'var(--ui-text-primary)' } : undefined,
          children: t.label
        }))
      }),
      jsx('button', {
        type: 'button',
        title: 'Actualizar ahora',
        'aria-label': 'Actualizar ahora',
        disabled: busy || Boolean(snap.isFetching),
        onClick: refresh,
        className: 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors hover:text-(--ui-accent) disabled:opacity-50',
        style: { color: 'var(--ui-text-tertiary)' },
        children: jsx(icons.RefreshCw, { size: 13, className: busy || snap.isFetching ? 'animate-spin' : undefined })
      })
    ] }),
    jsxs('div', {
      className: 'flex items-center justify-between gap-2 text-[0.625rem]',
      style: { color: 'var(--ui-text-tertiary)' },
      children: [
        jsx('span', { children: snap.isFetching ? 'Actualizando…' : `Actualizado ${generatedAt}` }),
        jsx('span', { children: `Fuente: ${data.source || '—'}` })
      ]
    }),
    snap.isError
      ? jsxs('div', { className: 'flex flex-col items-start gap-2 rounded-lg border p-3 text-xs', style: { borderColor: 'var(--ui-stroke-secondary)' }, children: [
          jsx('div', { style: { color: 'var(--ui-text-secondary)' }, children: `No se pudo leer el snapshot: ${snap.error?.message || snap.error || 'error desconocido'}` }),
          jsx('button', {
            type: 'button',
            onClick: () => snap.refetch(),
            className: 'rounded px-2 py-1 text-[0.6875rem]',
            style: { color: 'var(--ui-accent)', border: '1px solid var(--ui-stroke-secondary)' },
            children: 'Reintentar'
          })
        ] })
      : snap.isLoading
        ? jsx('div', { className: 'py-6 text-center text-xs', style: { color: 'var(--ui-text-tertiary)' }, children: 'Cargando cuota y uso…' })
        : tab === 'quota'
          ? jsx(QuotaTab, { data, now, mode: pctMode, onMode: setPctMode })
          : tab === 'analytics'
            ? jsx(AnalyticsTab, { data, range, onRange: setRange })
            : tab === 'models'
              ? jsx(ModelsTab, { data, range, onRange: setRange })
              : jsx(AboutTab, { data, gateway })
  ] })
}

// -- chip de la barra de estado ----------------------------------------------

function QuotaChip({ ctx }) {
  const snap = useQuery({
    queryKey: [ID, 'snapshot', '7d'],
    queryFn: () => ctx.rest('/snapshot?range=7d'),
    refetchInterval: POLL_MS,
    staleTime: 15000,
    retry: 1,
    refetchOnWindowFocus: false
  })
  const data = snap.data || EMPTY
  const cards = Array.isArray(data.quota?.cards) ? data.quota.cards : []
  const values = cards.flatMap(c => Array.isArray(c.windows) ? c.windows.map(pctUsed) : []).filter(v => v !== null)
  const top = values.length ? Math.max(...values) : null
  const label = top === null ? 'Uso —' : `Uso ${Math.round(top)}%`
  const tip = top === null
    ? `Sin datos de cuota (fuente: ${data.source || '—'}). Click para abrir Quota & Usage.`
    : `Mayor consumo: ${Math.round(top)}% · ${cards.length} proveedor(es). Click para abrir Quota & Usage.`

  return jsx(Tip, { label: tip, children: jsx('button', {
    type: 'button',
    className: cn(
      'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
      'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)'
    ),
    onClick: () => {
      haptic('tap')
      if (typeof host.revealPane === 'function') host.revealPane(`${ID}:pane`)
    },
    children: label
  }) })
}

export default {
  id: ID,
  name: 'Quota & Usage',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Quota & Usage',
      data: { placement: 'right', width: '360px' },
      render: () => jsx(QuotaPane, { ctx })
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(QuotaChip, { ctx })
    })
  }
}
