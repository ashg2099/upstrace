import { useEffect, useMemo, useRef, useState } from 'react'

const METRICS = [
  { key: 'mean_value', label: 'mean value' },
  { key: 'null_rate', label: 'null rate' },
  { key: 'distinct_count', label: 'distinct count' },
  { key: 'row_count', label: 'row count' },
]

/* The API has gone through a couple of shapes while we built this, so we
   normalise defensively instead of trusting one. Returns [{day, baseline, current}]. */
function normalise(raw) {
  if (!raw) return []
  if (Array.isArray(raw.points)) {
    return raw.points.map((p) => ({
      day: String(p.day ?? p.partition ?? p.date ?? ''),
      baseline: num(p.baseline ?? p.before ?? p.baseline_value),
      current: num(p.current ?? p.after ?? p.current_value),
    }))
  }
  if (Array.isArray(raw.baseline) || Array.isArray(raw.current)) {
    const map = new Map()
    const put = (rows, field) => {
      ;(rows || []).forEach((r) => {
        const day = String(r.day ?? r.partition ?? r.date ?? '')
        const row = map.get(day) || { day, baseline: null, current: null }
        row[field] = num(r.value ?? r.metric_value ?? r[field])
        map.set(day, row)
      })
    }
    put(raw.baseline, 'baseline')
    put(raw.current, 'current')
    return [...map.values()].sort((a, b) => a.day.localeCompare(b.day))
  }
  if (Array.isArray(raw)) {
    const map = new Map()
    raw.forEach((r) => {
      const day = String(r.day ?? r.partition ?? r.date ?? '')
      const row = map.get(day) || { day, baseline: null, current: null }
      const field = (r.run ?? r.run_label ?? '').toString().includes('base')
        ? 'baseline'
        : 'current'
      row[field] = num(r.value ?? r.metric_value)
      map.set(day, row)
    })
    return [...map.values()].sort((a, b) => a.day.localeCompare(b.day))
  }
  return []
}

function num(v) {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function quantile(sorted, q) {
  if (!sorted.length) return 0
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  if (lo === hi) return sorted[lo]
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

/* The whole point of this chart is the step between baseline and current.
   One freak day can be 20x the median and flatten that step into a hairline,
   so by default we clamp the view to p2..p98 and mark whatever falls outside. */
function domainFor(points, clip) {
  const all = []
  const base = []
  const curr = []
  points.forEach((p) => {
    if (p.baseline !== null) { all.push(p.baseline); base.push(p.baseline) }
    if (p.current !== null) { all.push(p.current); curr.push(p.current) }
  })
  if (!all.length) return { lo: 0, hi: 1, clipped: 0 }

  const sorted = [...all].sort((a, b) => a - b)
  let lo = clip ? quantile(sorted, 0.02) : sorted[0]
  let hi = clip ? quantile(sorted, 0.98) : sorted[sorted.length - 1]

  // never clip away the thing we came to see: keep both series' medians visible
  ;[base, curr].forEach((s) => {
    if (!s.length) return
    const med = quantile([...s].sort((a, b) => a - b), 0.5)
    lo = Math.min(lo, med)
    hi = Math.max(hi, med)
  })

  if (hi === lo) { hi = lo + Math.abs(lo || 1) * 0.1; lo -= Math.abs(lo || 1) * 0.1 }
  const pad = (hi - lo) * 0.08
  lo -= pad
  hi += pad
  const clipped = all.filter((v) => v < lo || v > hi).length
  return { lo, hi, clipped }
}

const W = 820
const H = 260
const PAD = { l: 62, r: 16, t: 14, b: 34 }

export default function MetricChart({ model, column }) {
  const [metric, setMetric] = useState('mean_value')
  const [points, setPoints] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [clip, setClip] = useState(true)
  const [hover, setHover] = useState(null) // { i, px }
  const svgRef = useRef(null)

  useEffect(() => {
    if (!model || !column) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setHover(null)
    fetch(`/api/metrics/${encodeURIComponent(model)}/${encodeURIComponent(column)}?metric=${metric}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((raw) => { if (!cancelled) setPoints(normalise(raw)) })
      .catch((e) => { if (!cancelled) setError(e.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [model, column, metric])

  const { lo, hi, clipped } = useMemo(() => domainFor(points, clip), [points, clip])

  const x = (i) =>
    PAD.l + (points.length < 2 ? 0 : (i * (W - PAD.l - PAD.r)) / (points.length - 1))
  const y = (v) => {
    const t = (v - lo) / (hi - lo)
    return PAD.t + (1 - Math.min(1, Math.max(0, t))) * (H - PAD.t - PAD.b)
  }
  const inView = (v) => v !== null && v >= lo && v <= hi

  const path = (field) => {
    let d = ''
    let open = false
    points.forEach((p, i) => {
      if (!inView(p[field])) { open = false; return }
      d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[field]).toFixed(1)} `
      open = true
    })
    return d.trim()
  }

  const outliers = []
  points.forEach((p, i) => {
    ;['baseline', 'current'].forEach((field) => {
      const v = p[field]
      if (v === null || inView(v)) return
      outliers.push({ i, field, above: v > hi, value: v })
    })
  })

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => lo + t * (hi - lo))
  const fmt = (v) =>
    v === null || v === undefined ? '—'
      : Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : Math.abs(v) >= 1 ? v.toFixed(4)
      : v.toFixed(5)

  const labelEvery = Math.max(1, Math.ceil(points.length / 8))

  /* Native <title> tooltips only fire on the exact glyph and lag a second, which
     is no use on a 9px marker. So we capture mousemove over the whole plot,
     snap to the nearest day, and render our own tooltip. */
  function onMove(e) {
    if (!points.length || !svgRef.current) return
    const rect = svgRef.current.getBoundingClientRect()
    const vx = ((e.clientX - rect.left) / rect.width) * W
    const span = W - PAD.l - PAD.r
    const step = points.length < 2 ? span : span / (points.length - 1)
    let i = Math.round((vx - PAD.l) / step)
    i = Math.max(0, Math.min(points.length - 1, i))
    setHover({ i, px: (x(i) / W) * rect.width, width: rect.width })
  }

  const hp = hover ? points[hover.i] : null

  return (
    <div className="card" style={{ marginTop: '1rem' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '.5rem', alignItems: 'center', justifyContent: 'space-between' }}>
        <strong style={{ fontSize: '.9rem' }}>{model}.{column}</strong>
        <div style={{ display: 'flex', gap: '.35rem', flexWrap: 'wrap' }}>
          {METRICS.map((m) => (
            <button
              key={m.key}
              onClick={() => setMetric(m.key)}
              style={{
                fontSize: '.72rem', padding: '.2rem .55rem', borderRadius: 999,
                cursor: 'pointer', border: '1px solid var(--border, #d0d0d8)',
                background: metric === m.key ? 'var(--accent, #4f46e5)' : 'transparent',
                color: metric === m.key ? '#0b0b0f' : 'inherit',
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {loading && <p className="muted" style={{ fontSize: '.8rem' }}>loading…</p>}
      {error && <p style={{ color: '#dc2626', fontSize: '.8rem' }}>could not load metric history: {error}</p>}
      {!loading && !error && points.length === 0 && (
        <p className="muted" style={{ fontSize: '.8rem' }}>no per-day history for this column yet.</p>
      )}

      {points.length > 0 && (
        <>
          <div style={{ position: 'relative', marginTop: '.5rem' }}>
            <svg
              ref={svgRef}
              viewBox={`0 0 ${W} ${H}`}
              style={{ width: '100%', height: 'auto', display: 'block' }}
              onMouseMove={onMove}
              onMouseLeave={() => setHover(null)}
            >
              {ticks.map((t, i) => {
                const yy = y(t)
                return (
                  <g key={i}>
                    <line x1={PAD.l} x2={W - PAD.r} y1={yy} y2={yy} stroke="currentColor" strokeOpacity=".12" />
                    <text x={PAD.l - 8} y={yy + 3.5} textAnchor="end" fontSize="10" fill="currentColor" fillOpacity=".6">
                      {Math.abs(t) >= 1000 ? Math.round(t).toLocaleString() : t.toFixed(2)}
                    </text>
                  </g>
                )
              })}

              {points.map((p, i) =>
                i % labelEvery === 0 ? (
                  <text key={p.day} x={x(i)} y={H - PAD.b + 16} textAnchor="middle" fontSize="9.5" fill="currentColor" fillOpacity=".55">
                    {p.day.slice(5)}
                  </text>
                ) : null
              )}

              {hover && (
                <line
                  x1={x(hover.i)} x2={x(hover.i)} y1={PAD.t} y2={H - PAD.b}
                  stroke="currentColor" strokeOpacity=".3" strokeDasharray="3 3"
                />
              )}

              <path d={path('baseline')} fill="none" stroke="#8b98ab" strokeWidth="1.8" strokeLinejoin="round" />
              <path d={path('current')} fill="none" stroke="#e11d48" strokeWidth="2.1" strokeLinejoin="round" />

              {outliers.map((o, k) => {
                const cy = o.above ? PAD.t + 4 : H - PAD.b - 4
                const tip = o.above ? cy - 5 : cy + 5
                const base = o.above ? cy + 3 : cy - 3
                return (
                  <polygon
                    key={k}
                    points={`${x(o.i)},${tip} ${x(o.i) - 4.5},${base} ${x(o.i) + 4.5},${base}`}
                    fill={o.field === 'current' ? '#e11d48' : '#8b98ab'}
                    fillOpacity=".8"
                  />
                )
              })}

              {hp && ['baseline', 'current'].map((field) =>
                inView(hp[field]) ? (
                  <circle
                    key={field}
                    cx={x(hover.i)} cy={y(hp[field])} r="3.6"
                    fill={field === 'current' ? '#e11d48' : '#8b98ab'}
                    stroke="var(--bg, #0b0b0f)" strokeWidth="1.4"
                  />
                ) : null
              )}
            </svg>

            {hp && (
              <div
                style={{
                  position: 'absolute',
                  top: 4,
                  left: Math.min(Math.max(hover.px + 12, 0), Math.max(0, hover.width - 190)),
                  pointerEvents: 'none',
                  background: 'var(--card, #14141b)',
                  border: '1px solid var(--border, #2a2a36)',
                  borderRadius: 6,
                  padding: '.45rem .6rem',
                  fontSize: '.72rem',
                  lineHeight: 1.5,
                  minWidth: 170,
                  boxShadow: '0 6px 20px rgba(0,0,0,.35)',
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: '.2rem' }}>{hp.day}</div>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
                  <span style={{ color: '#8b98ab' }}>baseline</span>
                  <span>{fmt(hp.baseline)}{hp.baseline !== null && !inView(hp.baseline) ? ' ▲' : ''}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
                  <span style={{ color: '#e11d48' }}>current</span>
                  <span>{fmt(hp.current)}{hp.current !== null && !inView(hp.current) ? ' ▲' : ''}</span>
                </div>
                {hp.baseline && hp.current ? (
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', opacity: .7, marginTop: '.2rem' }}>
                    <span>ratio</span>
                    <span>{(hp.current / hp.baseline).toFixed(4)}×</span>
                  </div>
                ) : null}
              </div>
            )}
          </div>

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', alignItems: 'center', fontSize: '.75rem', marginTop: '.35rem' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '.35rem' }}>
              <span style={{ width: 14, height: 2, background: '#8b98ab', display: 'inline-block' }} /> baseline
            </span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '.35rem' }}>
              <span style={{ width: 14, height: 2, background: '#e11d48', display: 'inline-block' }} /> current
            </span>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: '.35rem', cursor: 'pointer', marginLeft: 'auto' }}>
              <input type="checkbox" checked={clip} onChange={(e) => setClip(e.target.checked)} />
              clip outliers (p2–p98)
            </label>
            {clip && clipped > 0 && (
              <span className="muted">{clipped} point{clipped === 1 ? '' : 's'} outside view ▲</span>
            )}
          </div>
        </>
      )}
    </div>
  )
}