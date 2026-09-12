from dataclasses import dataclass, field

import duckdb

from .config import METRICS_SCHEMA
from .lineage import ancestors, descendants, graph

@dataclass
class Evidence:
    model_name: str
    column_name: str
    metric: str
    baseline: float | None
    current: float | None
    change: float
    severity: str
    partitions: int = 0
    baseline_text: str | None = None
    current_text: str | None = None
    first_partition: str | None = None

@dataclass
class Incident:
    root: str
    is_source: bool
    columns: list[str]
    blast_radius: list[str]           # downstream nodes that also drifted
    evidence: list[Evidence] = field(default_factory=list)
    downstream_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        order = ["critical", "high", "warning"]
        found = {e.severity for e in self.evidence}
        for level in order:
            if level in found:
                return level
        return "warning"


def ensure_incident_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {METRICS_SCHEMA}.incidents (
            detected_at   TIMESTAMP,
            run_id        VARCHAR,
            root          VARCHAR,
            is_source     BOOLEAN,
            severity      VARCHAR,
            columns       VARCHAR,
            blast_radius  VARCHAR,
            signal_count  INTEGER
        )
    """)


def load_signals(con: duckdb.DuckDBPyConnection, run_id: str) -> list[Evidence]:
    rows = con.execute(
        f"""
        select model_name, column_name, metric, baseline_value,
               current_value, change, severity, coalesce(partitions, 0),
               baseline_text, current_text, first_partition
        from {METRICS_SCHEMA}.drift_signals
        where run_id = ?
        """,
        [run_id],
    ).fetchall()
    return [Evidence(*row) for row in rows]


def latest_run(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute(
        f"select run_id from {METRICS_SCHEMA}.profile_runs order by started_at desc limit 1"
    ).fetchone()
    if not row:
        raise SystemExit("No profile runs yet. Run: upstrace profile")
    return row[0]


def analyse(con: duckdb.DuckDBPyConnection, run_id: str | None = None) -> list[Incident]:
    ensure_incident_table(con)
    run_id = run_id or latest_run(con)

    signals = load_signals(con, run_id)
    if not signals:
        return []

    nodes, parents = graph()
    affected = {s.model_name for s in signals}

    incidents: list[Incident] = []

    for name in sorted(affected):
        upstream = ancestors(name, parents)
        if upstream & affected:
            continue                      # upar bhi kuch drift hua - ye root nahi hai

        node_signals = [s for s in signals if s.model_name == name]
        downstream = sorted(descendants(name, parents) & affected)

        incidents.append(
            Incident(
                root=name,
                is_source=nodes[name].is_source if name in nodes else False,
                columns=sorted({s.column_name for s in node_signals}),
                blast_radius=downstream,
                downstream_columns={
                    node: sorted({s.column_name for s in signals if s.model_name == node})
                    for node in downstream
                },
                evidence=sorted(
                    node_signals,
                    key=lambda e: {"critical": 0, "high": 1, "warning": 2}[e.severity],
                ),
            )
        )

    order = {"critical": 0, "high": 1, "warning": 2}
    incidents.sort(key=lambda i: (order[i.severity], -len(i.blast_radius)))

    con.execute(f"delete from {METRICS_SCHEMA}.incidents where run_id = ?", [run_id])
    for inc in incidents:
        con.execute(
            f"INSERT INTO {METRICS_SCHEMA}.incidents VALUES (now(), ?, ?, ?, ?, ?, ?, ?)",
            [run_id, inc.root, inc.is_source, inc.severity,
             ", ".join(inc.columns), ", ".join(inc.blast_radius), len(inc.evidence)],
        )

    return incidents