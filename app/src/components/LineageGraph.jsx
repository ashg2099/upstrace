import { useMemo } from 'react'

// dbt projects are shallow and mostly linear, so a layered left-to-right layout
// beats a force simulation: it is deterministic, needs no library, and puts the
// source on the left where people look first.
function layout(nodes, edges) {
  const parents = new Map(nodes.map((n) => [n.name, []]))
  edges.forEach((e) => parents.get(e.target)?.push(e.source))

  const depth = new Map()
  const resolve = (name, seen = new Set()) => {
    if (depth.has(name)) return depth.get(name)
    if (seen.has(name)) return 0                 // cycles should not exist, but do not hang
    seen.add(name)
    const ups = parents.get(name) ?? []
    const value = ups.length === 0 ? 0 : 1 + Math.max(...ups.map((u) => resolve(u, seen)))
    depth.set(name, value)
    return value
  }
  nodes.forEach((n) => resolve(n.name))

  const columns = new Map()
  nodes.forEach((n) => {
    const d = depth.get(n.name)
    if (!columns.has(d)) columns.set(d, [])
    columns.get(d).push(n)
  })

  const COL_W = 190
  const ROW_H = 78
  const BOX_W = 150
  const BOX_H = 46

  const placed = new Map()
  const tallest = Math.max(...[...columns.values()].map((c) => c.length))

  ;[...columns.entries()]
    .sort((a, b) => a[0] - b[0])
    .forEach(([d, group]) => {
      const offset = (tallest - group.length) / 2
      group.forEach((node, i) => {
        placed.set(node.name, {
          ...node,
          x: 20 + d * COL_W,
          y: 20 + (offset + i) * ROW_H,
          w: BOX_W,
          h: BOX_H,
        })
      })
    })

  return {
    placed,
    width: 40 + columns.size * COL_W,
    height: 40 + tallest * ROW_H,
  }
}

export default function LineageGraph({ lineage, onSelect }) {
  const { placed, width, height } = useMemo(
    () => layout(lineage.nodes, lineage.edges),
    [lineage],
  )

  return (
    <section className="panel">
      <h2>Lineage</h2>
      <div className="graphwrap">
        <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} role="img"
             aria-label="Pipeline lineage graph">
          {lineage.edges.map((edge, i) => {
            const from = placed.get(edge.source)
            const to = placed.get(edge.target)
            if (!from || !to) return null
            const x1 = from.x + from.w
            const y1 = from.y + from.h / 2
            const x2 = to.x
            const y2 = to.y + to.h / 2
            const mid = (x1 + x2) / 2
            return (
              <path
                key={i}
                className={`edge${to.state !== 'ok' ? ' hot' : ''}`}
                d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
                fill="none"
              />
            )
          })}

          {[...placed.values()].map((node) => (
            <g
              key={node.name}
              className={`node ${node.state}`}
              transform={`translate(${node.x} ${node.y})`}
              onClick={() => onSelect?.(node.name)}
            >
              <rect width={node.w} height={node.h} rx="5" />
              <text x="11" y="19">{node.name}</text>
              <text x="11" y="34" className="sub">
                {node.kind === 'source' ? 'source' : node.materialized}
                {node.state === 'root' ? ' · root cause' : ''}
                {node.state === 'affected' ? ' · affected' : ''}
              </text>
            </g>
          ))}
        </svg>
      </div>
      <p className="note graphnote">
        Read left to right. Red is where the analysis says it started; amber
        carried it downstream.
      </p>
    </section>
  )
}