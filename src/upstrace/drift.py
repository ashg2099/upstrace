from dataclasses import dataclass
from functools import lru_cache

import duckdb

from .config import METRICS_SCHEMA
from .settings import get_settings


@lru_cache(maxsize=None)
def thresholds_for(node: str) -> dict[str, float]:
    """How far a metric may move before we call it drift, for this node.

    Thresholds are per-node because a single global number cannot be right:
    2% row-count movement is alarming in a fact table and ordinary in a
    marketing events table that swings 40% between a Tuesday and a Saturday.
    Without per-node overrides the only way to silence those false alarms is to
    raise the threshold everywhere, which is how alerting systems get ignored.

    Cached because this is called once per metric per column per partition -
    tens of thousands of times on a ninety-day table. Call
    thresholds_for.cache_clear() if the config is reloaded mid-process.
    """
    return get_settings().thresholds_for(node)

@dataclass
class Signal:
    model_name: str
    column_name: str
    metric: str
    baseline: float | None
    current: float | None
    change: float
    severity: str
    partitions: int = 0  


def ensure_drift_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {METRICS_SCHEMA}.drift_signals (
            detected_at      TIMESTAMP,
            run_id           VARCHAR,
            baseline_run_id  VARCHAR,
            model_name       VARCHAR,
            column_name      VARCHAR,
            metric           VARCHAR,
            baseline_value   DOUBLE,
            current_value    DOUBLE,
            change           DOUBLE,
            severity         VARCHAR
        )
    """)
    # Added after the first release; existing warehouses migrate in place.
    con.execute(f"""
        ALTER TABLE {METRICS_SCHEMA}.drift_signals
        ADD COLUMN IF NOT EXISTS partitions INTEGER
    """)


def latest_runs(con: duckdb.DuckDBPyConnection, n: int = 2) -> list[str]:
    rows = con.execute(
        f"select run_id from {METRICS_SCHEMA}.profile_runs order by started_at desc limit {n}"
    ).fetchall()
    return [r[0] for r in rows]


def _relative(baseline: float | None, current: float | None) -> float:
    if baseline in (None, 0):
        return 0.0 if current in (None, 0) else 1.0
    if current is None:
        return 1.0
    return abs(current - baseline) / abs(baseline)


def _severity(metric: str, change: float, node: str) -> str:
    limit = thresholds_for(node)[metric]
    tiers = get_settings().severity
    if change >= limit * tiers["critical"]:
        return "critical"
    if change >= limit * tiers["high"]:
        return "high"
    return "warning"

def partition_signals(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    baseline_run_id: str,
) -> list[Signal]:
    """Compare each day against the same day in the baseline run.

    This is what a whole-table average cannot do. A fault confined to one day
    barely moves a 500,000-row mean, but it moves that day's mean hard.

    Note on what this measures: both runs profile the same seeded sample, so a
    day nobody touched is byte-identical and produces no signal by construction.
    Against live data you would compare each partition to its own history rather
    than to the same partition in a previous run.
    """
    rows = con.execute(
        f"""
        select
            b.model_name, b.column_name, b.partition_value,
            b.row_count, c.row_count,
            b.null_rate, c.null_rate,
            b.distinct_count, c.distinct_count,
            b.mean_value, c.mean_value
        from {METRICS_SCHEMA}.partition_profiles b
        join {METRICS_SCHEMA}.partition_profiles c
          on  b.model_name = c.model_name
         and  b.column_name = c.column_name
         and  b.partition_value = c.partition_value
        where b.run_id = ? and c.run_id = ?
        """,
        [baseline_run_id, run_id],
    ).fetchall()

    # (model, column, metric) -> [partitions over threshold, worst change,
    #                             baseline at worst, current at worst]
    worst: dict[tuple[str, str, str], list] = {}

    for (model, column, _partition,
         b_rows, c_rows, b_null, c_null,
         b_dist, c_dist, b_mean, c_mean) in rows:

        checks = [
            ("row_count", b_rows, c_rows, _relative(b_rows, c_rows)),
            ("null_rate", b_null, c_null, abs((c_null or 0) - (b_null or 0))),
            ("distinct_count", b_dist, c_dist, _relative(b_dist, c_dist)),
        ]
        if b_mean is not None or c_mean is not None:
            checks.append(("mean_value", b_mean, c_mean, _relative(b_mean, c_mean)))

        for metric, baseline, current, change in checks:
            if change < thresholds_for(model)[metric]:
                continue
            key = (model, column, metric)
            entry = worst.setdefault(key, [0, 0.0, None, None])
            entry[0] += 1
            if change > entry[1]:
                entry[1:] = [change, baseline, current]

    return [
        Signal(model, column, metric, baseline, current, change,
               _severity(metric, change, model), partitions=count)
        for (model, column, metric), (count, change, baseline, current) in worst.items()
    ]

def detect(
    con: duckdb.DuckDBPyConnection,
    run_id: str | None = None,
    baseline_run_id: str | None = None,
) -> list[Signal]:
    ensure_drift_table(con)

    if run_id is None or baseline_run_id is None:
        runs = latest_runs(con, 2)
        if len(runs) < 2:
            raise SystemExit(
                "Need at least two profile runs to compare. Run: upstrace profile"
            )
        run_id = run_id or runs[0]
        baseline_run_id = baseline_run_id or runs[1]

    rows = con.execute(
        f"""
        select
            b.model_name, b.column_name,
            b.row_count, c.row_count,
            b.null_rate, c.null_rate,
            b.distinct_count, c.distinct_count,
            b.mean_value, c.mean_value,
            b.min_value, c.min_value,
            b.max_value, c.max_value
        from {METRICS_SCHEMA}.column_profiles b
        join {METRICS_SCHEMA}.column_profiles c
          on b.model_name = c.model_name
         and b.column_name = c.column_name
        where b.run_id = ? and c.run_id = ?
        """,
        [baseline_run_id, run_id],
    ).fetchall()

    signals: list[Signal] = []

    for (
        model, column,
        b_rows, c_rows,
        b_null, c_null,
        b_distinct, c_distinct,
        b_mean, c_mean,
        b_min, c_min,
        b_max, c_max,
    ) in rows:

        checks = [
            ("row_count", b_rows, c_rows, _relative(b_rows, c_rows)),
            ("null_rate", b_null, c_null, abs((c_null or 0) - (b_null or 0))),
            ("distinct_count", b_distinct, c_distinct, _relative(b_distinct, c_distinct)),
        ]
        if b_mean is not None or c_mean is not None:
            checks.append(("mean_value", b_mean, c_mean, _relative(b_mean, c_mean)))

        limits = thresholds_for(model)
        for metric, baseline, current, change in checks:
            if change >= limits[metric]:
                signals.append(
                    Signal(model, column, metric, baseline, current,
                           change, _severity(metric, change, model))
                )

        # min/max strings hain: inka koi bhi badalna bolne layak hai.
        for metric, baseline, current in (("min_value", b_min, c_min),
                                          ("max_value", b_max, c_max)):
            if baseline != current:
                signals.append(
                    Signal(model, column, metric, None, None, 1.0, "warning")
                )

    signals += partition_signals(con, run_id, baseline_run_id)
    
    order = {"critical": 0, "high": 1, "warning": 2}
    signals.sort(key=lambda s: (order[s.severity], -s.change))

    con.execute(f"delete from {METRICS_SCHEMA}.drift_signals where run_id = ?", [run_id])
    for s in signals:
        con.execute(
            f"""INSERT INTO {METRICS_SCHEMA}.drift_signals
                (detected_at, run_id, baseline_run_id, model_name, column_name,
                 metric, baseline_value, current_value, change, severity, partitions)
                VALUES (now(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, baseline_run_id, s.model_name, s.column_name, s.metric,
             s.baseline, s.current, s.change, s.severity, s.partitions],
        )

    return signals