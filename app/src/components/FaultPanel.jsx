export default function FaultPanel({ faults, busy, onInject, onReset }) {
  return (
    <section className="panel">
      <h2>Break something on purpose</h2>
      <div className="body">
        {faults.map((fault) => (
          <button
            key={fault.key}
            className="fault"
            disabled={busy}
            onClick={() => onInject(fault.key)}
          >
            <span className="key">{fault.key}</span>
            <span className="what">{fault.description}</span>
          </button>
        ))}

        <div className="actions">
          <button className="btn" disabled={busy} onClick={onReset}>
            Reset warehouse
          </button>
        </div>

        <p className="note">
          Each of these rebuilds the pipeline and reprofiles every column, so it
          takes a few seconds. dbt reports success either way — that is the point.
        </p>
      </div>
    </section>
  )
}