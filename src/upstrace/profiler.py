import uuid
from datetime import datetime

import duckdb
import pandas as pd

from .config import METRICS_SCHEMA
from .manifest import Model, list_nodes
from .warehouse import ensure_metrics_tables
from .settings import get_settings

NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "DECIMAL", "REAL",
}


def _is_numeric(data_type: str) -> bool:
    return data_type.upper().split("(")[0] in NUMERIC_TYPES

FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL", "DECIMAL"}


def _is_float(data_type: str) -> bool:
    return data_type.upper().split("(")[0] in FLOAT_TYPES


def _distinct_expr(col: str, data_type: str) -> str:
    """Exact, and rounded for floats.

    approx_count_distinct is a HyperLogLog estimate. On two identical runs it
    returned counts up to 30% apart, which raised high-severity alerts on a
    control scenario where nothing had changed. Counting exactly removes that.

    Floats are rounded first for a separate reason: sum() and avg() run in
    parallel and floating-point addition is not associative, so two groups whose
    totals are mathematically equal can differ in the last bits from one run to
    the next. Rounding to six decimals counts values the way a person would, and
    still catches a column that was rounded to whole numbers.
    """
    if _is_float(data_type):
        return f"count(distinct round({col}, 6))"
    return f"count(distinct {col})"


def profile_column(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    column: str,
    data_type: str,
    row_count: int,
) -> dict:
    col = f'"{column}"'

    mean_expr = f"avg({col})::double" if _is_numeric(data_type) else "cast(null as double)"

    row = con.execute(f"""
        select
            count({col})                          as non_null_count,
            {_distinct_expr(col, data_type)}      as distinct_count,
            min({col})::varchar                   as min_value,
            max({col})::varchar                   as max_value,
            {mean_expr}                           as mean_value
        from {relation}
    """).fetchone()

    non_null, distinct, min_v, max_v, mean_v = row
    null_count = row_count - non_null

    return {
        "column_name": column,
        "data_type": data_type,
        "row_count": row_count,
        "null_count": null_count,
        "null_rate": (null_count / row_count) if row_count else 0.0,
        "distinct_count": distinct,
        "distinct_rate": (distinct / row_count) if row_count else 0.0,
        "min_value": min_v,
        "max_value": max_v,
        "mean_value": mean_v,
    }

DATE_LIKE = {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}

PARTITION_COLUMNS = [
    "run_id", "profiled_at", "model_name", "partition_value", "column_name",
    "row_count", "null_count", "null_rate", "distinct_count",
    "min_value", "max_value", "mean_value",
]


def partition_column(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    node: str | None = None,
) -> str | None:
    """Pick the column to slice by.

    Configuration wins over detection. upstrace.yml can name the column
    explicitly, or set it to null to opt a node out of per-day profiling
    entirely. Only when a node is not mentioned there do we fall back to the
    heuristic: a real DATE beats a TIMESTAMP, first wins.

    The heuristic is right often enough to be the default and wrong often enough
    to need an override. A table whose first date column is `customer_signup_date`
    would otherwise be profiled along entirely the wrong axis, and silently so -
    every day would look stable because the axis itself is meaningless.
    """
    if node is not None:
        mode, configured = get_settings().partition_override(node)
        if mode == "off":
            return None
        if mode == "fixed":
            names = {name for name, *_ in con.execute(f"DESCRIBE {relation}").fetchall()}
            if configured not in names:
                raise SystemExit(
                    f"upstrace.yml sets profile.partition_column for {node!r} to "
                    f"{configured!r}, which does not exist in {relation}."
                )
            return configured

    columns = con.execute(f"DESCRIBE {relation}").fetchall()
    dates = [name for name, dtype, *_ in columns if dtype.upper() == "DATE"]
    if dates:
        return dates[0]
    stamps = [name for name, dtype, *_ in columns if dtype.upper() in DATE_LIKE]
    return stamps[0] if stamps else None

def profile_partitions(
    con: duckdb.DuckDBPyConnection,
    model: Model,
) -> list[tuple]:
    """Profile every column once per day, in one query per column.

    Whole-table averages hide a fault confined to a short window: 500,000 rows
    absorb one bad day. Per-day profiles measure that day against its own
    history instead, which is the difference between noticing and not.
    """
    settings = get_settings()

    part_col = partition_column(con, model.relation, model.name)
    if part_col is None:
        return []

    part = f'cast("{part_col}" as date)'

    # Guard rail. A column that is date-like but effectively unique - an event
    # timestamp spanning ten years, an id that happened to parse as a date -
    # would write one profile row per partition per column and turn a two-second
    # run into a very long one. Better to skip per-day profiling loudly in
    # config than to discover it as a hang.
    partitions = con.execute(
        f"select count(distinct {part}) from {model.relation} where {part} is not null"
    ).fetchone()[0]
    if partitions > settings.max_partitions:
        return []

    rows: list[tuple] = []
    columns = con.execute(f"DESCRIBE {model.relation}").fetchall()

    for name, dtype, *_ in columns:
        col = f'"{name}"'
        mean_expr = f"avg({col})::double" if _is_numeric(dtype) else "cast(null as double)"
        for p, total, non_null, distinct, min_v, max_v, mean_v in con.execute(f"""
            select
                {part}                            as partition_value,
                count(*)                          as row_count,
                count({col})                      as non_null_count,
                {_distinct_expr(col, dtype)}      as distinct_count,
                min({col})::varchar               as min_value,
                max({col})::varchar               as max_value,
                {mean_expr}                       as mean_value
            from {model.relation}
            where {part} is not null
            group by 1
        """).fetchall():
            null_count = total - non_null
            rows.append((
                model.name, p, name, total, null_count,
                (null_count / total) if total else 0.0,
                distinct, min_v, max_v, mean_v,
            ))
    return rows

def profile_model(con: duckdb.DuckDBPyConnection, model: Model) -> list[dict]:
    columns = con.execute(f"DESCRIBE {model.relation}").fetchall()
    row_count = con.execute(f"select count(*) from {model.relation}").fetchone()[0]

    return [
        profile_column(con, model.relation, name, dtype, row_count)
        for name, dtype, *_ in columns
    ]


def run_profile(
    con: duckdb.DuckDBPyConnection,
    only_model: str | None = None,
    on_model=None,
) -> str:
    """Har model profile karo aur results append karo. run_id return karta hai."""
    ensure_metrics_tables(con)

    settings = get_settings()
    models = list_nodes()
    if only_model:
        # An explicit --model beats the config: if you named it, you meant it.
        models = [m for m in models if m.name == only_model]
        if not models:
            raise SystemExit(f"No model named {only_model!r} in the manifest.")
    else:
        models = [m for m in models if settings.matches(m.name)]
        if not models:
            raise SystemExit(
                "No nodes matched profile.include / profile.exclude in upstrace.yml."
            )

    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now()

    for model in models:
        if on_model:
            on_model(model)
            
        partition_rows = profile_partitions(con, model)
        if partition_rows:
            # One INSERT ... SELECT from a DataFrame, not thousands of
            # parameterised inserts. Measured on 1,729 rows: 8.2s -> 0.01s.
            frame = pd.DataFrame(
                [(run_id, started_at, *row) for row in partition_rows],
                columns=PARTITION_COLUMNS,
            )
            con.execute(
                f"INSERT INTO {METRICS_SCHEMA}.partition_profiles SELECT * FROM frame"
            )

        for profile in profile_model(con, model):
            con.execute(
                f"""
                INSERT INTO {METRICS_SCHEMA}.column_profiles VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id, started_at, model.name,
                    profile["column_name"], profile["data_type"],
                    profile["row_count"], profile["null_count"], profile["null_rate"],
                    profile["distinct_count"], profile["distinct_rate"],
                    profile["min_value"], profile["max_value"], profile["mean_value"],
                ],
            )

    con.execute(
        f"INSERT INTO {METRICS_SCHEMA}.profile_runs VALUES (?, ?, ?, ?)",
        [run_id, started_at, datetime.now(), len(models)],
    )

    return run_id