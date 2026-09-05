import uuid
from datetime import datetime

import duckdb

from .config import METRICS_SCHEMA
from .manifest import Model, list_models
from .warehouse import ensure_metrics_tables

NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "DECIMAL", "REAL",
}


def _is_numeric(data_type: str) -> bool:
    return data_type.upper().split("(")[0] in NUMERIC_TYPES


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
            count({col})                    as non_null_count,
            approx_count_distinct({col})    as distinct_count,
            min({col})::varchar             as min_value,
            max({col})::varchar             as max_value,
            {mean_expr}                     as mean_value
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

    models = list_models()
    if only_model:
        models = [m for m in models if m.name == only_model]
        if not models:
            raise SystemExit(f"No model named {only_model!r} in the manifest.")

    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now()

    for model in models:
        if on_model:
            on_model(model)
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