import { useState } from 'react'
import { api } from '../api'

function formatValue(value) {
  if (value === null || value === undefined) return '—'
  const abs = Math.abs(value)
  if (abs >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
  return value.toFixed(4)
}

export default function IncidentDetail({ incident }) {
  const [explanation, setExplanation] = useState(incident.explanation)
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState(null)

  async function explain() {
    setAsking(true)
    setError(null)
    try {
      const full = await api.incident(incident.root, true)
      setExplanation(full.explanation)
    } catch (err) {
      setError(err.message)
    } finally {
      setAsking(false)
    }
  }

  return (
    <section className="panel">
      <div className="detail-head">
        <h3>{incident.root}</h3>
        <p className="sub">
          <span className={`sev ${incident.severity}`}>{incident.severity}</span>
          {' · '}
          {incident.is_source ? 'source table' : 'model'}
          {' · '}
          no upstream node drifted, so this is where it started
        </p>
      </div>

      <div className="facts">
        <div className="fact">
          <div className="k">Columns that moved</div>
          <div className="chips">
            {incident.columns.map((column) => (
              <span key={column} className="chip hot">{column}</span>
            ))}
          </div>
        </div>

        <div className="fact">
          <div className="k">Carried downstream to</div>
          {incident.blast_radius.length === 0 ? (
            <div className="v">nothing downstream</div>
          ) : (
            <div className="chips">
              {incident.blast_radius.map((node) => (
                <span key={node} className="chip">
                  {node}
                  {incident.downstream_columns?.[node]?.length
                    ? ` · ${incident.downstream_columns[node].join(', ')}`
                    : ''}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th>column</th>
              <th>metric</th>
              <th style={{ textAlign: 'right' }}>baseline</th>
              <th style={{ textAlign: 'right' }}>current</th>
              <th style={{ textAlign: 'right' }}>change</th>
              <th style={{ textAlign: 'right' }}>days</th>
            </tr>
          </thead>
          <tbody>
            {incident.evidence.map((e, i) => (
              <tr key={`${e.column}-${e.metric}-${i}`}>
                <td className="col">{e.column}</td>
                <td>{e.metric}</td>
                <td className="num">{formatValue(e.baseline)}</td>
                <td className="num">{formatValue(e.current)}</td>
                <td className="num">
                  {e.metric === 'min_value' || e.metric === 'max_value'
                    ? 'changed'
                    : `${(e.change * 100).toFixed(1)}%`}
                </td>
                <td className="num">{e.partitions || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="explain">
        {explanation ? (
          <>
            <p className="summary">{explanation.summary}</p>
            {(explanation.likely_causes ?? []).map((cause, i) => (
              <div className="cause" key={i}>
                <div className="conf">{cause.confidence} confidence</div>
                <div>{cause.cause}</div>
                {cause.reasoning && <div className="why">{cause.reasoning}</div>}
              </div>
            ))}
            {explanation.who_is_affected && (
              <div className="cause">
                <div className="conf">impact</div>
                <div className="why">{explanation.who_is_affected}</div>
              </div>
            )}
          </>
        ) : (
          <>
            <button className="btn primary" disabled={asking} onClick={explain}>
              {asking ? 'Asking the model…' : 'Explain this in English'}
            </button>
            <p className="note">
              The model sees the numbers above and the lineage, never the rows.
              Responses are cached by prompt hash, so this is reproducible.
            </p>
          </>
        )}
        {error && <div className="banner">{error}</div>}
      </div>
    </section>
  )
}