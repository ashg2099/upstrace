import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import FaultPanel from './components/FaultPanel'
import IncidentList from './components/IncidentList'
import IncidentDetail from './components/IncidentDetail'
import LineageGraph from './components/LineageGraph'
import MetricChart from './components/MetricChart'

export default function App() {
  const [overview, setOverview] = useState(null)
  const [lineage, setLineage] = useState(null)
  const [faults, setFaults] = useState([])
  const [incidents, setIncidents] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    const [ov, list, graph] = await Promise.all([
      api.overview(),
      api.incidents(),
      api.lineage(),
    ])
    setOverview(ov)
    setIncidents(list)
    setLineage(graph)
    // Keep the current selection if it still exists, else take the worst one.
    setSelected((current) =>
      list.some((i) => i.root === current) ? current : list[0]?.root ?? null,
    )
  }, [])

  useEffect(() => {
    api.faults().then(setFaults).catch((e) => setError(e.message))
    refresh().catch((e) => setError(e.message))
  }, [refresh])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .incident(selected)
      .then((d) => { if (!cancelled) setDetail(d) })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [selected])

  async function run(label, action) {
    setBusy(label)
    setError(null)
    try {
      await action()
      await refresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>Upstrace</h1>
          <p>
            When a data quality check fails, trace it upstream to the change
            that caused it.
          </p>
        </div>

        {overview && (
          <div className="stats">
            <div className="stat">
              <span className="value">{overview.nodes}</span>
              <span className="label">nodes</span>
            </div>
            <div className="stat">
              <span className="value">
                {overview.rows_profiled?.toLocaleString() ?? '—'}
              </span>
              <span className="label">rows profiled</span>
            </div>
            <div className="stat">
              <span className="value">{overview.signals}</span>
              <span className="label">signals</span>
            </div>
            <div className={`stat${overview.incidents ? ' alarm' : ''}`}>
              <span className="value">{overview.incidents}</span>
              <span className="label">incidents</span>
            </div>
          </div>
        )}
      </header>

      {busy && (
        <div className="banner working">
          {busy === 'reset'
            ? 'Reloading the raw data, rebuilding the pipeline, reprofiling every column…'
            : `Injecting ${busy}, rebuilding, reprofiling…`}
        </div>
      )}
      {error && <div className="banner">{error}</div>}

      <div className="columns">
        <div>
          <FaultPanel
            faults={faults}
            busy={Boolean(busy)}
            onInject={(key) => run(key, () => api.injectFault(key))}
            onReset={() => run('reset', () => api.reset())}
          />
          <IncidentList
            incidents={incidents}
            selected={selected}
            onSelect={setSelected}
          />
        </div>

        <div>
          {lineage && (
            <LineageGraph
              lineage={lineage}
              onSelect={(name) =>
                incidents.some((i) => i.root === name) && setSelected(name)
              }
            />
          )}

          {detail ? (
            <IncidentDetail key={detail.root} incident={detail} />
          ) : (
            <section className="panel">
              <h2>Incident detail</h2>
              <div className="empty">
                Select an incident, or inject a fault to create one.
              </div>
            </section>
          )}

          {detail && detail.columns.length > 0 && (
            <MetricChart
              key={`${detail.root}-${detail.columns[0]}`}
              model={detail.root}
              column={detail.columns[0]}
            />
          )}
        </div>
      </div>
    </div>
  )
}