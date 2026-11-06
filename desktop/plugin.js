// Docket for Hermes Desktop: Jobs board and Price book pages, status-bar pipeline chip, ⌘K command, and an inline
// ::docket-quote{id="Q-1001"} card in chats. Talks only to Docket's own backend (/api/plugins/docket) through
// ctx.rest, and refreshes live from the plugin.docket.board.updated event (with a slow polling fallback).
import * as sdk from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const { cn, Codicon, host, Tip, useQuery, useQueryClient } = sdk

const PATH = '/docket'
const KEY = ['docket']
let REST = null // set in register(): the plugin-scoped ctx.rest
let CLIPBOARD = null // set in register(): ctx.os clipboard writer

const money = (value, currency) => {
  const n = Number(value || 0)
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD' }).format(n)
  } catch {
    return `$${n.toFixed(2)}`
  }
}

const day = iso => {
  if (!iso) return ''
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`)
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

const ago = iso => {
  if (!iso) return ''
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 48) return `${hrs}h ago`
  return `${Math.round(hrs / 24)}d ago`
}

const useBoard = (closed = false) =>
  useQuery({
    queryKey: [...KEY, 'board', closed],
    queryFn: () => REST(`/board${closed ? '?closed=true' : ''}`),
    refetchInterval: 20000
  })

const useQuote = id =>
  useQuery({
    queryKey: [...KEY, 'quote', id],
    queryFn: () => REST(`/quote/${id}`),
    enabled: id != null,
    refetchInterval: 20000
  })

const qtyLabel = line => {
  const q = Number(line.qty)
  const unit = line.unit && line.unit !== 'each' ? ` ${line.unit}` : ''
  if (!Number.isFinite(q) || (q === 1 && !unit) || line.kind === 'discount') return ''
  return ` × ${String(q)}${unit}`
}

const STATUS_TEXT = {
  draft: 'Draft', sent: 'Sent', viewed: 'Opened', accepted: 'Accepted', scheduled: 'Booked',
  in_progress: 'In progress', done: 'Done', declined: 'Declined', discarded: 'Discarded', cancelled: 'Cancelled',
  expired: 'Expired', invoiced: 'Invoiced', paid: 'Paid'
}

function Pill({ children, tone }) {
  const tones = {
    accent: 'border-(--ui-accent) text-(--ui-accent)',
    muted: 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary)',
    warn: 'border-(--ui-stroke-secondary) text-(--ui-text-secondary)'
  }
  return jsx('span', {
    className: cn('inline-flex items-center rounded-full border px-1.5 py-px text-[0.625rem] font-medium',
      tones[tone] || tones.muted),
    children
  })
}

function Card({ item, currency, selected, onOpen }) {
  const badges = []
  if (item.opened) badges.push(jsx(Pill, { tone: 'accent', children: 'opened' }, 'o'))
  if (!item.complete) badges.push(jsx(Pill, { tone: 'warn', children: 'needs price' }, 'n'))
  if (item.scheduled_for && !item.invoiced) badges.push(jsx(Pill, { tone: 'accent', children: day(item.scheduled_for) }, 's'))
  if (item.invoiced) badges.push(jsx(Pill, { tone: 'accent', children: 'invoiced' }, 'i'))
  return jsxs('button', {
    type: 'button',
    onClick: () => onOpen(item.id),
    className: cn(
      'flex w-full flex-col gap-1 rounded-md border px-2.5 py-2 text-left transition-colors',
      'border-(--ui-stroke-secondary) hover:border-(--ui-accent)',
      selected && 'border-(--ui-accent)'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('span', { className: 'truncate text-[0.8125rem] font-medium', children: item.customer }),
          jsx('span', { className: 'tabular-nums text-[0.8125rem]', children: money(item.total, currency) })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center justify-between gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { className: 'truncate', children: `${item.number} v${item.version}${item.site ? ' · ' + item.site : ''}` }),
          jsx('span', { className: 'shrink-0', children: ago(item.updated_at) })
        ]
      }),
      badges.length ? jsx('div', { className: 'flex flex-wrap gap-1', children: badges }) : null
    ]
  })
}

function Column({ column, currency, selectedId, onOpen, compact }) {
  return jsxs('div', {
    className: cn('flex flex-1 flex-col gap-2', compact ? 'min-w-[170px]' : 'min-w-[220px]'),
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2 whitespace-nowrap px-0.5 text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: `${column.label} · ${column.count}` }),
          jsx('span', { className: 'tabular-nums normal-case', children: column.count ? money(column.total, currency) : '' })
        ]
      }),
      jsx('div', {
        className: 'flex flex-col gap-1.5',
        children: column.items.length
          ? column.items.map(item =>
              jsx(Card, { item, currency, selected: item.id === selectedId, onOpen }, item.id))
          : jsx('div', {
              className: 'rounded-md border border-dashed border-(--ui-stroke-secondary) px-2 py-3 text-center text-[0.6875rem] text-(--ui-text-quaternary)',
              children: 'Nothing here'
            })
      })
    ]
  })
}

function Lines({ quote, currency }) {
  return jsxs('div', {
    className: 'flex flex-col',
    children: [
      ...quote.lines.map((line, i) =>
        jsxs('div', {
          className: 'flex flex-col gap-0.5 border-b border-(--ui-stroke-secondary) py-1.5',
          children: [
            jsxs('div', {
              className: 'flex justify-between gap-3 text-[0.8125rem]',
              children: [
                jsxs('span', {
                  children: [line.description,
                    jsx('span', { className: 'text-(--ui-text-tertiary)', children: qtyLabel(line) })]
                }),
                jsx('span', {
                  className: 'shrink-0 tabular-nums',
                  children: line.kind === 'needs_price' ? 'needs price' : money(line.amount, currency)
                })
              ]
            }),
            jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: line.rule })
          ]
        }, i)
      ),
      ...[
        ['Subtotal', quote.subtotal],
        [`${quote.tax_name} ${(Number(quote.tax_rate) * 100).toFixed(2).replace(/\.?0+$/, '')}%`, quote.tax]
      ].map(([label, value]) =>
        jsxs('div', {
          className: 'flex justify-between pt-1.5 text-[0.75rem] text-(--ui-text-secondary)',
          children: [jsx('span', { children: label }), jsx('span', { className: 'tabular-nums', children: money(value, currency) })]
        }, label)
      ),
      jsxs('div', {
        className: 'flex justify-between pt-1 text-[0.9375rem] font-semibold',
        children: [jsx('span', { children: 'Total' }), jsx('span', { className: 'tabular-nums', children: money(quote.total, currency) })]
      })
    ]
  })
}

function ActionButton({ label, onClick, primary, disabled }) {
  return jsx('button', {
    type: 'button',
    disabled,
    onClick,
    className: cn(
      'rounded-md border px-2.5 py-1 text-[0.75rem] font-medium transition-colors disabled:opacity-50',
      primary
        ? 'border-(--ui-accent) bg-(--ui-accent) text-(--ui-accent-foreground,white)'
        : 'border-(--ui-stroke-secondary) hover:border-(--ui-accent)'
    ),
    children: label
  })
}

function Detail({ id, currency, onClose }) {
  const { data, error, isLoading } = useQuote(id)
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [booking, setBooking] = useState('')

  const run = async (action, body) => {
    setBusy(true)
    try {
      const res = await REST(`/quote/${id}/${action}`, { method: 'POST', body: body || {} })
      host.notify({ kind: 'success', message: res.message || 'Done' })
      await qc.invalidateQueries({ queryKey: KEY })
    } catch (err) {
      host.notify({ kind: 'error', message: String(err?.message || err) })
    } finally {
      setBusy(false)
    }
  }

  if (isLoading) return jsx('div', { className: 'p-4 text-[0.75rem] text-(--ui-text-tertiary)', children: 'Loading…' })
  if (error || !data) return jsx('div', { className: 'p-4 text-[0.75rem]', children: 'Could not load this quote.' })

  const actions = data.actions.map(act => {
    if (act.id === 'book') {
      return jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('input', {
            type: 'date',
            value: booking,
            onChange: e => setBooking(e.target.value),
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-1.5 py-0.5 text-[0.75rem]'
          }),
          jsx(ActionButton, { label: act.label, primary: true, disabled: busy || !booking, onClick: () => run('book', { date: booking }) })
        ]
      }, act.id)
    }
    return jsx(ActionButton, {
      label: act.label, primary: ['send', 'done', 'invoice', 'paid'].includes(act.id), disabled: busy,
      onClick: () => run(act.id)
    }, act.id)
  })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-y-auto border-l border-(--ui-stroke-secondary) p-4',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsx('span', { className: 'text-[1rem] font-semibold', children: `${data.number} v${data.version}` }),
              jsx('span', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: [data.customer, data.site, data.zip].filter(Boolean).join(' · ') })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Pill, { tone: 'accent', children: STATUS_TEXT[data.status] || data.status }),
              jsx('button', { type: 'button', onClick: onClose, className: 'text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)', children: jsx(Codicon, { name: 'close' }) })
            ]
          })
        ]
      }),
      actions.length ? jsx('div', { className: 'flex flex-wrap items-center gap-2', children: actions }) : null,
      data.accept_url
        ? jsxs('div', {
            className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
            children: [
              jsx('span', { className: 'truncate', children: data.accept_url }),
              jsx('button', {
                type: 'button',
                className: 'shrink-0 text-(--ui-accent)',
                onClick: () => { CLIPBOARD?.(data.accept_url); host.notify({ kind: 'info', message: 'Customer link copied' }) },
                children: 'Copy link'
              })
            ]
          })
        : null,
      jsx(Lines, { quote: data.quote, currency }),
      jsxs('div', {
        className: 'flex flex-col gap-1 pt-2',
        children: [
          jsx('span', { className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'History' }),
          ...data.events.map((e, i) =>
            jsxs('div', {
              className: 'flex justify-between gap-2 text-[0.6875rem] text-(--ui-text-secondary)',
              children: [
                jsx('span', { children: `${STATUS_TEXT[e.to] || e.to} · ${e.actor}${e.surface ? ' via ' + e.surface : ''}${e.detail && e.to === 'declined' ? ' · "' + e.detail + '"' : ''}` }),
                jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: ago(e.at) })
              ]
            }, i)
          )
        ]
      })
    ]
  })
}

function JobsPage() {
  const [showClosed, setShowClosed] = useState(false)
  const [selected, setSelected] = useState(null)
  const { data, error, isLoading, refetch } = useBoard(showClosed)

  if (isLoading) return jsx('div', { className: 'p-6 text-[0.8125rem] text-(--ui-text-tertiary)', children: 'Loading jobs…' })
  if (error || !data) {
    return jsxs('div', {
      className: 'flex flex-col gap-2 p-6 text-[0.8125rem]',
      children: [
        jsx('span', { children: 'Docket’s backend isn’t reachable.' }),
        jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'Enable the Docket plugin (hermes plugins enable docket) and restart the gateway, then retry.' }),
        jsx('div', { children: jsx(ActionButton, { label: 'Retry', onClick: () => refetch() }) })
      ]
    })
  }

  return jsxs('div', {
    className: 'relative flex h-full min-h-0',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 flex-1 flex-col gap-4 overflow-auto p-5',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-baseline justify-between gap-3',
            children: [
              jsxs('div', {
                className: 'flex flex-col',
                children: [
                  jsx('span', { className: 'text-[1.125rem] font-semibold', children: 'Jobs' }),
                  jsx('span', {
                    className: 'text-[0.75rem] text-(--ui-text-tertiary)',
                    children: `${data.business} · ${data.open_count} open quote${data.open_count === 1 ? '' : 's'} worth ${money(data.open_value, data.currency)}`
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-3',
                children: [
                  jsxs('label', {
                    className: 'flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)',
                    children: [
                      jsx('input', { type: 'checkbox', checked: showClosed, onChange: e => setShowClosed(e.target.checked) }),
                      'Show closed'
                    ]
                  }),
                  jsx(ActionButton, { label: 'Price book', onClick: () => host.navigate(PB_PATH) })
                ]
              })
            ]
          }),
          jsx('div', {
            className: 'flex gap-3 overflow-x-auto pb-2',
            children: data.columns.map(col =>
              jsx(Column, { column: col, currency: data.currency, selectedId: selected, onOpen: setSelected }, col.key))
          }),
          showClosed && data.closed?.length
            ? jsx(Column, {
                column: { key: 'closed', label: 'Closed', count: data.closed.length, total: 0, items: data.closed },
                currency: data.currency, selectedId: selected, onOpen: setSelected
              })
            : null
        ]
      }),
      selected != null
        ? jsx('div', { className: 'absolute inset-y-0 right-0 z-10 w-[min(400px,92%)] bg-background shadow-xl', children: jsx(Detail, { id: selected, currency: data.currency, onClose: () => setSelected(null) }) })
        : null
    ]
  })
}

function PipelineChip() {
  const { data } = useBoard(false)
  if (!data || !data.open_count) return null
  return jsx(Tip, {
    label: `${data.open_count} open quote${data.open_count === 1 ? '' : 's'}. Open the Docket jobs board`,
    children: jsxs('button', {
      type: 'button',
      onClick: () => host.navigate(PATH),
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] tabular-nums transition-colors',
        'text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
      ),
      children: [jsx(Codicon, { name: 'notebook', size: '0.7rem' }), jsx('span', { children: `${data.open_count} · ${money(data.open_value, data.currency)}` })]
    })
  })
}

function QuoteDirective({ attrs, sessionId, streaming }) {
  const raw = String(attrs?.id || '').toUpperCase().replace(/^Q-?/, '')
  const id = /^\d{1,9}$/.test(raw) ? Number(raw) : null
  const { data, error } = useQuote(id)
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  if (id == null) return null
  if (error) return jsx('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: `Quote Q-${raw} not found` })
  if (!data) return jsx('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: `Loading Q-${raw}…` })
  const send = async () => {
    setBusy(true)
    try {
      const res = await REST(`/quote/${id}/send`, { method: 'POST', body: {} })
      host.notify({ kind: 'success', message: res.message })
      await qc.invalidateQueries({ queryKey: KEY })
    } catch (err) {
      host.notify({ kind: 'error', message: String(err?.message || err) })
    } finally {
      setBusy(false)
    }
  }
  const canSend = data.actions.some(a => a.id === 'send')
  return jsxs('div', {
    className: 'my-1 flex max-w-[520px] flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-[0.875rem] font-semibold', children: `${data.number} v${data.version} · ${data.customer}` }),
          jsx(Pill, { tone: 'accent', children: STATUS_TEXT[data.status] || data.status })
        ]
      }),
      jsx(Lines, { quote: data.quote, currency: data.quote.currency }),
      jsxs('div', {
        className: 'flex gap-2 pt-1',
        children: [
          canSend && !streaming ? jsx(ActionButton, { label: 'Send to customer', primary: true, disabled: busy, onClick: send }) : null,
          jsx(ActionButton, { label: 'Open jobs board', onClick: () => host.navigate(PATH) })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------- Price book page

const PB_PATH = '/docket/pricebook'

const usePricebook = () =>
  useQuery({ queryKey: [...KEY, 'pricebook'], queryFn: () => REST('/pricebook'), refetchInterval: 30000 })

const pct = v => `${(Number(v) * 100).toFixed(4).replace(/\.?0+$/, '')}%`
const hrs = v => `${Number(v)} h`

const EXAMPLE = `labor 145/hr
3/4" PEX $2.20/ft
Sump pump $289 each
Backflow test $95 flat
TOILET-REPLACE 2 hours`

function ChangeRow({ change }) {
  const sym = change.label.slice(0, 1)
  const tone = { '+': 'text-(--ui-accent)', '−': 'text-(--ui-text-tertiary) line-through', '★': 'text-(--ui-accent)' }[sym] || ''
  return jsxs('div', {
    className: 'flex gap-2 py-0.5 text-[0.8125rem]',
    children: [
      jsx('span', { className: cn('w-3 shrink-0 text-center font-semibold', sym === '−' ? 'text-(--ui-text-tertiary)' : 'text-(--ui-accent)'), children: sym }),
      jsx('span', { className: cn('min-w-0', tone), children: change.label.slice(1).trim() })
    ]
  })
}

function PlanView({ plan, busy, onSave, onCancel }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md border border-(--ui-accent) p-3',
    children: [
      jsx('span', {
        className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
        children: plan.changes.length ? `${plan.changes.length} change${plan.changes.length === 1 ? '' : 's'} · not saved yet` : 'No changes'
      }),
      plan.changes.length ? jsx('div', { className: 'flex flex-col', children: plan.changes.map((c, i) => jsx(ChangeRow, { change: c }, i)) }) : null,
      plan.skipped?.length
        ? jsxs('div', {
            className: 'flex flex-col gap-0.5 border-t border-(--ui-stroke-secondary) pt-2',
            children: [
              jsx('span', { className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'Not changed' }),
              ...plan.skipped.map((s, i) => jsx('span', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: s }, i))
            ]
          })
        : null,
      plan.summary ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: plan.summary }) : null,
      plan.changes.length
        ? jsxs('div', {
            className: 'flex gap-2 pt-1',
            children: [
              jsx(ActionButton, { label: 'Save changes', primary: true, disabled: busy, onClick: onSave }),
              jsx(ActionButton, { label: 'Cancel', disabled: busy, onClick: onCancel })
            ]
          })
        : null
    ]
  })
}

function Table({ title, note, columns, rows, empty }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2 px-0.5',
        children: [
          jsx('span', { className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: `${title} · ${rows.length}` }),
          note ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: note }) : null
        ]
      }),
      jsx('div', {
        className: 'overflow-x-auto rounded-md border border-(--ui-stroke-secondary)',
        children: jsxs('table', {
          className: 'w-full border-collapse text-[0.8125rem]',
          children: [
            jsx('thead', {
              children: jsx('tr', {
                children: columns.map(c => jsx('th', {
                  className: cn('whitespace-nowrap border-b border-(--ui-stroke-secondary) px-2.5 py-1.5 text-[0.6875rem] font-medium text-(--ui-text-tertiary)', c.right ? 'text-right' : 'text-left'),
                  children: c.label
                }, c.label))
              })
            }),
            jsx('tbody', {
              children: rows.length
                ? rows.map((r, i) => jsx('tr', {
                    className: 'group border-b border-(--ui-stroke-secondary) last:border-0',
                    children: columns.map(c => jsx('td', {
                      className: cn('px-2.5 py-1.5 align-top', c.right && 'text-right tabular-nums', c.muted && 'text-(--ui-text-tertiary)', c.nowrap && 'whitespace-nowrap'),
                      children: c.render(r)
                    }, c.label))
                  }, r.code || r.name || i))
                : jsx('tr', { children: jsx('td', { colSpan: columns.length, className: 'px-2.5 py-3 text-center text-[0.75rem] text-(--ui-text-quaternary)', children: empty || 'Nothing yet' }) })
            })
          ]
        })
      })
    ]
  })
}

function PriceBookPage() {
  const { data, error, isLoading, refetch } = usePricebook()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [plan, setPlan] = useState(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')

  const call = async (path, body, after) => {
    setBusy(true)
    try {
      const res = await REST(path, { method: 'POST', body: body || {} })
      await after?.(res)
      await qc.invalidateQueries({ queryKey: KEY })
    } catch (err) {
      host.notify({ kind: 'error', message: String(err?.message || err) })
    } finally {
      setBusy(false)
    }
  }
  const preview = body => call('/pricebook/preview', body, res => setPlan(res.plan))
  const save = token => call('/pricebook/save', { token }, res => {
    host.notify({ kind: 'success', message: res.message.split('\n')[0].replace(/:$/, '.') })
    setPlan(null)
    setText('')
  })
  const cancel = token => call('/pricebook/cancel', { token }, () => setPlan(null))
  const undo = () => call('/pricebook/undo', {}, res => { host.notify({ kind: 'info', message: res.message }); setPlan(null) })
  const loadFile = e => {
    const file = e.target.files?.[0]
    if (!file) return
    if (file.size > 500000) { host.notify({ kind: 'error', message: 'That file is over 500 KB.' }); return }
    file.text().then(t => setText(t))
    e.target.value = ''
  }

  if (isLoading) return jsx('div', { className: 'p-6 text-[0.8125rem] text-(--ui-text-tertiary)', children: 'Loading price book…' })
  if (error || !data) {
    return jsxs('div', {
      className: 'flex flex-col gap-2 p-6 text-[0.8125rem]',
      children: [
        jsx('span', { children: 'Docket’s backend isn’t reachable.' }),
        jsx('div', { children: jsx(ActionButton, { label: 'Retry', onClick: () => refetch() }) })
      ]
    })
  }

  const shown = plan || data.pending
  const cur = data.currency
  const q = filter.trim().toLowerCase()
  const match = r => !q || [r.code, r.name, ...(r.aliases || [])].some(v => String(v || '').toLowerCase().includes(q))
  const removeCol = {
    label: '', nowrap: true, right: true,
    render: r => jsx('button', {
      type: 'button', title: `Remove ${r.code}`, disabled: busy,
      className: 'text-(--ui-text-quaternary) opacity-0 transition-opacity hover:text-(--ui-text-secondary) group-hover:opacity-100',
      onClick: () => preview({ remove: [r.code] }),
      children: jsx(Codicon, { name: 'trash', size: '0.75rem' })
    })
  }
  const codeCol = { label: 'Code', nowrap: true, muted: true, render: r => r.code }
  const nameCol = {
    label: 'Name',
    render: r => jsxs('div', {
      className: 'flex flex-col',
      children: [
        jsx('span', { children: r.name }),
        r.aliases?.length ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `also: ${r.aliases.join(', ')}` }) : null
      ]
    })
  }

  return jsx('div', {
    className: 'flex h-full min-h-0 flex-col overflow-auto',
    children: jsxs('div', {
      className: 'flex w-full max-w-[980px] flex-col gap-5 p-5',
      children: [
        jsxs('div', {
          className: 'flex flex-wrap items-start justify-between gap-3',
          children: [
            jsxs('div', {
              className: 'flex flex-col gap-0.5',
              children: [
                jsxs('div', {
                  className: 'flex items-center gap-2',
                  children: [
                    jsx('span', { className: 'text-[1.125rem] font-semibold', children: 'Price book' }),
                    data.is_demo ? jsx(Pill, { tone: 'warn', children: 'demo' }) : null
                  ]
                }),
                data.errors?.length
                  ? jsx('span', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: `${data.errors.length} error(s): ${data.errors[0]}` })
                  : jsx('span', {
                      className: 'text-[0.75rem] text-(--ui-text-tertiary)',
                      children: [
                        data.business,
                        `labor ${money(data.labor_rate, cur)}/h (min ${hrs(data.labor_minimum)})`,
                        `${data.tax_name} ${pct(data.tax_rate)} on ${data.tax_applies_to.join(', ') || 'nothing'}`,
                        data.service_call ? `${data.service_call.name} ${money(data.service_call.amount, cur)}` : null
                      ].filter(Boolean).join(' · ')
                    }),
                jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: data.path })
              ]
            }),
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                data.can_undo ? jsx(ActionButton, { label: 'Undo last save', disabled: busy, onClick: undo }) : null,
                jsx(ActionButton, { label: 'Jobs board', onClick: () => host.navigate(PATH) })
              ]
            })
          ]
        }),
        jsxs('div', {
          className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-3',
          children: [
            jsxs('div', {
              className: 'flex items-baseline justify-between gap-2',
              children: [
                jsx('span', { className: 'text-[0.8125rem] font-medium', children: 'Add or change prices' }),
                jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Your own words, a pasted list, or a CSV. Nothing is saved until you confirm.' })
              ]
            }),
            jsx('textarea', {
              value: text, rows: 5, placeholder: EXAMPLE, spellCheck: false,
              onChange: e => setText(e.target.value),
              className: 'w-full resize-y rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2.5 py-2 font-mono text-[0.75rem] outline-none focus:border-(--ui-accent)'
            }),
            jsxs('div', {
              className: 'flex flex-wrap items-center gap-2',
              children: [
                jsx(ActionButton, { label: busy ? 'Reading…' : 'Preview changes', primary: true, disabled: busy || !text.trim(), onClick: () => preview({ text }) }),
                jsxs('label', {
                  className: 'cursor-pointer rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1 text-[0.75rem] font-medium hover:border-(--ui-accent)',
                  children: ['Load a file…', jsx('input', { type: 'file', accept: '.csv,.tsv,.txt', className: 'hidden', onChange: loadFile })]
                })
              ]
            }),
            shown ? jsx(PlanView, { plan: shown, busy, onSave: () => save(shown.token), onCancel: () => cancel(shown.token) }) : null
          ]
        }),
        jsx('input', {
          value: filter, onChange: e => setFilter(e.target.value), placeholder: 'Search codes, names and aliases',
          className: 'w-full max-w-[320px] rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2.5 py-1 text-[0.8125rem] outline-none focus:border-(--ui-accent)'
        }),
        jsx(Table, {
          title: 'Jobs', note: 'priced from labor hours plus parts',
          rows: (data.assemblies || []).filter(match),
          columns: [codeCol, nameCol,
            { label: 'Labor', right: true, nowrap: true, render: r => `${hrs(r.hours)}${r.unit !== 'each' ? '/' + r.unit : ''}` },
            { label: 'Parts', muted: true, render: r => r.parts.map(p => `${Number(p.qty) !== 1 ? Number(p.qty) + ' × ' : ''}${p.name}`).join(', ') || '—' },
            { label: 'From', right: true, nowrap: true, render: r => `${money(r.price, cur)}${r.unit !== 'each' ? '/' + r.unit : ''}` },
            removeCol]
        }),
        jsx(Table, {
          title: 'Flat-rate', note: 'one set price',
          rows: (data.fees || []).filter(match),
          columns: [codeCol, nameCol,
            { label: 'Price', right: true, nowrap: true, render: r => `${money(r.amount, cur)}${r.unit !== 'each' ? '/' + r.unit : ''}` },
            removeCol]
        }),
        jsx(Table, {
          title: 'Materials', note: 'price = cost × (1 + markup)',
          rows: (data.materials || []).filter(match),
          columns: [codeCol, nameCol,
            { label: 'Cost', right: true, nowrap: true, muted: true, render: r => money(r.cost, cur) },
            { label: 'Markup', right: true, nowrap: true, muted: true, render: r => pct(r.markup) },
            { label: 'Price', right: true, nowrap: true, render: r => `${money(r.price, cur)}/${r.unit}` },
            removeCol]
        }),
        jsx(Table, {
          title: 'Travel zones', note: 'by ZIP; any other ZIP needs a price',
          rows: data.zones || [], empty: 'No zones: travel is never charged',
          columns: [
            { label: 'Zone', nowrap: true, render: r => r.name },
            { label: 'Charge', right: true, nowrap: true, render: r => money(r.amount, cur) },
            { label: 'ZIPs', muted: true, render: r => r.zips.join(', ') }]
        })
      ]
    })
  })
}

export default {
  id: 'docket',
  name: 'Docket',
  description: 'Jobs board, pipeline chip and inline quote cards for Docket quotes.',
  register(ctx) {
    REST = ctx.rest
    CLIPBOARD = text => (ctx.os?.writeClipboard ? ctx.os.writeClipboard(text) : navigator.clipboard?.writeText(text))
    const refresh = () => sdk.queryClient?.invalidateQueries?.({ queryKey: KEY })
    ctx.onEvent('plugin.docket.board.updated', refresh)

    const contributions = []
    if (sdk.ROUTES_AREA) {
      contributions.push({ id: 'page', area: sdk.ROUTES_AREA, data: { path: PATH }, render: () => jsx(JobsPage, {}) })
      contributions.push({ id: 'pricebook', area: sdk.ROUTES_AREA, data: { path: PB_PATH }, render: () => jsx(PriceBookPage, {}) })
    }
    if (sdk.SIDEBAR_NAV_AREA) {
      contributions.push({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, order: 55, data: { codicon: 'notebook', label: 'Jobs', path: PATH } })
      contributions.push({ id: 'nav-pricebook', area: sdk.SIDEBAR_NAV_AREA, order: 56, data: { codicon: 'tag', label: 'Price book', path: PB_PATH } })
    }
    if (sdk.STATUSBAR_AREAS?.right) contributions.push({ id: 'chip', area: sdk.STATUSBAR_AREAS.right, order: 85, render: () => jsx(PipelineChip, {}) })
    if (sdk.PALETTE_AREA) {
      contributions.push({
        id: 'open', area: sdk.PALETTE_AREA,
        data: { id: 'docket.open', label: 'Docket: Open jobs board', keywords: ['docket', 'jobs', 'quotes', 'pipeline'], run: () => host.navigate(PATH) }
      })
      contributions.push({
        id: 'open-pricebook', area: sdk.PALETTE_AREA,
        data: { id: 'docket.pricebook', label: 'Docket: Open price book', keywords: ['docket', 'price', 'prices', 'rates', 'labor'], run: () => host.navigate(PB_PATH) }
      })
    }
    if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string') {
      contributions.push({
        id: 'quote-card', area: sdk.TRANSCRIPT_DIRECTIVE_AREA,
        data: { name: 'docket-quote', render: props => jsx(QuoteDirective, props) }
      })
    }
    ctx.registerMany(contributions)
  }
}
