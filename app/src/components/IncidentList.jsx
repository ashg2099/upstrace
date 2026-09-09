export default function IncidentList({ incidents, selected, onSelect }) {
  if (incidents.length === 0) {
    return (
      <section className="panel">
        <h2>Incidents</h2>
        <div className="empty">
          <strong>Nothing to report</strong>
          No column moved beyond its threshold since the previous run.
        </div>
      </section>
    )
  }

  return (
    <section className="panel">
      <h2>Incidents ({incidents.length})</h2>
      {incidents.map((incident) => (
        <button
          key={incident.root}
          className="incident"
          aria-current={incident.root === selected}
          onClick={() => onSelect(incident.root)}
        >
          <span className={`sev ${incident.severity}`}>{incident.severity}</span>
          <span className="root">{incident.root}</span>
          <span className="meta">
            {incident.columns.length} column{incident.columns.length === 1 ? '' : 's'}
            {' · '}
            {incident.blast_radius.length} downstream
            {' · '}
            {incident.signal_count} signals
          </span>
        </button>
      ))}
    </section>
  )
}