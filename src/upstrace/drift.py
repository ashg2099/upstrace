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
    baseline_text: str | None = None
    current_text: str | None = None  
    first_partition: str | None = None

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
    con.execute(f"""
        ALTER TABLE {METRICS_SCHEMA}.drift_signals
        ADD COLUMN IF NOT EXISTS baseline_text VARCHAR
    """)
    con.execute(f"""
        ALTER TABLE {METRICS_SCHEMA}.drift_signals
        ADD COLUMN IF NOT EXISTS current_text VARCHAR
    """)
    
    con.execute(f"""
        ALTER TABLE {METRICS_SCHEMA}.drift_signals
        ADD COLUMN IF NOT EXISTS first_partition VARCHAR
    """)


def latest_runs(con: duckdb.DuckDBPyConnection, n: int = 2) -> list[str]:
    rows = con.execute(
        f"select run_id from {METRICS_SCHEMA}.profile_runs order by started_at desc limit {n}"
    ).fetchall()
    return [r[0] for r in rows]

def run_volume(con: duckdb.DuckDBPyConnection, run_id: str) -> int:
    """Total rows this run actually measured, across all nodes.

    Every column of a node shares that node's row count, so taking the max per
    node and summing gives the volume without needing a schema change.
    """
    row = con.execute(
        f"""
        select coalesce(sum(rows), 0) from (
            select model_name, max(row_count) as rows
            from {METRICS_SCHEMA}.column_profiles
            where run_id = ?
            group by 1
        )
        """,
        [run_id],
    ).fetchone()
    return int(row[0] or 0)


def volume_mismatch(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    baseline_run_id: str,
    tolerance: float = 0.5,
) -> tuple[int, int] | None:
    """Were the two runs measuring comparable amounts of data?

    Comparing a 500,000-row sample against a 9.5M-row load produces confident
    nonsense: every metric moves, nothing is drift. This does not block the
    comparison - a genuine 60% growth in volume is itself worth reporting - but
    it refuses to let the tool sound certain when the two sides are not alike.
    """
    baseline = run_volume(con, baseline_run_id)
    current = run_volume(con, run_id)
    if baseline == 0 or current == 0:
        return None
    ratio = current / baseline
    if ratio > 1 + tolerance or ratio < 1 / (1 + tolerance):
        return baseline, current
    return None


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

def _text_value(column: str, value) -> str | None:
    """Render a min/max value for reporting, masked and bounded.

    Long values are truncated because a max_value can be an entire JSON blob,
    and the prompt has a budget.
    """
    if value is None:
        return None
    if get_settings().should_mask(column):
        return "<masked>"
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."

ROLLING_METRICS = ["row_count", "null_rate", "distinct_count", "mean_value"]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _robust_z(value: float, history: list[float]) -> tuple[float, float]:
    """How unusual is this value against its own recent history?

    Median and MAD rather than mean and standard deviation, because a fault that
    runs for thirty days corrupts the mean it would be measured against. The
    median tolerates up to half the window being wrong; the mean tolerates one
    bad day.

    0.6745 is the scale factor that makes MAD comparable to a standard deviation
    for normally distributed data, so the threshold reads like a familiar z.
    """
    med = _median(history)
    mad = _median([abs(v - med) for v in history])
    if mad == 0:
        # A perfectly flat history. Any movement at all is a departure, but
        # calling it infinitely unusual is useless, so score it by relative size.
        if abs(value - med) <= max(abs(med), 1e-9) * 1e-6:
            return 0.0, med
        scale = max(abs(med), 1e-9)
        return abs(value - med) / scale * 10, med
    return abs(0.6745 * (value - med) / mad), med


# row_count is a property of the table on that day, not of each column - every
# column of a node shares it. Evaluating it per column reports one fact thirty
# times and buries everything else.
COLUMN_METRICS = ["null_rate", "distinct_count", "mean_value"]
COLUMN_METRIC_INDEX = {"null_rate": 1, "distinct_count": 2, "mean_value": 3}


def _flag(
    points: list[tuple[str, float]],
    window: int,
    min_history: int,
    limit: float,
) -> list[tuple[str, float, float, float]]:
    """Walk a series and return every point unusual against the days before it."""
    flagged = []
    for i in range(min_history, len(points)):
        value = points[i][1]
        if value is None:
            continue
        history = [p[1] for p in points[max(0, i - window):i] if p[1] is not None]
        if len(history) < min_history:
            continue
        z, med = _robust_z(float(value), [float(h) for h in history])
        if z >= limit:
            flagged.append((points[i][0], z, float(value), med))
    return flagged


def _grade(z: float, limit: float, tiers: dict[str, float]) -> str:
    if z >= limit * tiers["critical"]:
        return "critical"
    if z >= limit * tiers["high"]:
        return "high"
    return "warning"


def _to_signal(model: str, column: str, metric: str, flagged: list) -> Signal | None:
    """Turn flagged partitions into a signal, if the movement is worth reporting.

    A z-score alone is not enough. A perfectly stable series has a tiny MAD, so a
    1.3% move scores as wildly unusual - statistically true, practically
    worthless, and the fastest way to teach people to ignore the tool. A signal
    has to clear both bars: unusual for this column, AND large enough that a
    human would care. The second bar is the threshold that already exists in
    config.
    """
    day, z, value, med = max(flagged, key=lambda f: f[1])

    if metric == "null_rate":
        practical = abs(value - med)          # percentage points, as configured
    else:
        practical = _relative(med, value)

    if practical < thresholds_for(model)[metric]:
        return None

    return Signal(
        model, column, metric,
        baseline=med,
        current=value,
        change=_relative(med, value),
        severity="warning",           # overwritten by the caller
        partitions=len(flagged),
        # The first flagged day answers "since when", which a two-run
        # comparison structurally cannot.
        first_partition=min(f[0] for f in flagged),
    )


def rolling_signals(con: duckdb.DuckDBPyConnection, run_id: str) -> list[Signal]:
    """Judge each partition against the days before it, within a single run.

    This is the detector a real deployment wants. Comparing to the previous run
    assumes yesterday's data never changes and that every day is like every
    other; neither is true. Comparing to a trailing window asks a better
    question - is today unusual for this column - and needs no baseline run at
    all, so it works the first time it is ever run.
    """
    settings = get_settings()
    window = settings.baseline_window
    min_history = settings.baseline_min_history
    limit = settings.baseline_z
    share = settings.baseline_min_partition_share
    tiers = settings.severity

    signals: list[Signal] = []

    # ---- volume, once per node ------------------------------------------
    volume: dict[str, list[tuple[str, float]]] = {}
    for model, day, rows in con.execute(
        f"""
        select model_name, cast(partition_value as varchar), max(row_count)
        from {METRICS_SCHEMA}.partition_profiles
        where run_id = ?
        group by 1, 2
        order by 1, 2
        """,
        [run_id],
    ).fetchall():
        volume.setdefault(model, []).append((day, float(rows)))

    for model, points in volume.items():
        if len(points) <= min_history:
            continue
        flagged = _flag(points, window, min_history, limit)
        if flagged:
            signal = _to_signal(model, "(table)", "row_count", flagged)
            if signal:
                signal.severity = _grade(max(f[1] for f in flagged), limit, tiers)
                if len(flagged) < 2:
                    signal.severity = "warning"
                signals.append(signal)

    # ---- column metrics, skipping days that are not really days ----------
    rows = con.execute(
        f"""
        select model_name, column_name, cast(partition_value as varchar),
               row_count, null_rate, distinct_count, mean_value
        from {METRICS_SCHEMA}.partition_profiles
        where run_id = ?
        order by model_name, column_name, partition_value
        """,
        [run_id],
    ).fetchall()

    series: dict[tuple[str, str], list[tuple]] = {}
    for model, column, day, *values in rows:
        series.setdefault((model, column), []).append((day, values))

    for (model, column), points in series.items():
        if len(points) <= min_history:
            continue

        # A day holding 2 rows out of a typical 117,000 is a gap in the data,
        # not a day with unusual values. Judging its mean against a normal day's
        # is meaningless, and it produces one alarm per metric per column -
        # dozens of signals for a single underlying fact, which the volume check
        # above already reports once.
        typical = _median([float(p[1][0]) for p in points if p[1][0] is not None] or [0.0])
        floor = typical * share
        dense = [p for p in points if p[1][0] is not None and float(p[1][0]) >= floor]
        if len(dense) <= min_history:
            continue

        for metric in COLUMN_METRICS:
            index = COLUMN_METRIC_INDEX[metric]
            flagged = _flag(
                [(p[0], p[1][index]) for p in dense], window, min_history, limit
            )
            if not flagged:
                continue
            signal = _to_signal(model, column, metric, flagged)
            if signal is None:
                continue
            signal.severity = _grade(max(f[1] for f in flagged), limit, tiers)
            if len(flagged) < 2:
                # One unusual day is weather, a holiday, an outage upstream that
                # already fixed itself. A shift that persists is a pipeline
                # problem. Both are worth recording; only one is worth waking
                # someone for.
                signal.severity = "warning"
            signals.append(signal)

    return signals

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

def _persist(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    baseline_run_id: str | None,
    signals: list[Signal],
) -> list[Signal]:
    order = {"critical": 0, "high": 1, "warning": 2}
    signals.sort(key=lambda s: (order[s.severity], -s.change))

    con.execute(f"delete from {METRICS_SCHEMA}.drift_signals where run_id = ?", [run_id])
    for s in signals:
        con.execute(
            f"""INSERT INTO {METRICS_SCHEMA}.drift_signals
                (detected_at, run_id, baseline_run_id, model_name, column_name,
                 metric, baseline_value, current_value, change, severity, partitions,
                 baseline_text, current_text, first_partition)
                VALUES (now(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [run_id, baseline_run_id, s.model_name, s.column_name, s.metric,
             s.baseline, s.current, s.change, s.severity, s.partitions,
             s.baseline_text, s.current_text, s.first_partition],
        )
    return signals

def detect(
    con: duckdb.DuckDBPyConnection,
    run_id: str | None = None,
    baseline_run_id: str | None = None,
) -> list[Signal]:
    ensure_drift_table(con)
    
    settings = get_settings()

    if settings.baseline_mode == "rolling":
        runs = latest_runs(con, 1)
        if not runs:
            raise SystemExit("No profile runs yet. Run: upstrace profile")
        run_id = run_id or runs[0]
        signals = rolling_signals(con, run_id)
        return _persist(con, run_id, None, signals)

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
                    Signal(model, column, metric, None, None, 1.0, "warning",
                           baseline_text=_text_value(column, baseline),
                           current_text=_text_value(column, current))
                )

    signals += partition_signals(con, run_id, baseline_run_id)
    return _persist(con, run_id, baseline_run_id, signals)