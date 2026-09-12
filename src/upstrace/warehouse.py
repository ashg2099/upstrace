import duckdb

from .config import METRICS_SCHEMA, WAREHOUSE_DB


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not WAREHOUSE_DB.exists():
        raise SystemExit(
            f"No warehouse at {WAREHOUSE_DB}.\n"
            "Check the 'warehouse:' path in upstrace.yml, then build it with dbt.\n"
            "For this repo's demo: python scripts/load_duckdb.py"
        )
    return duckdb.connect(str(WAREHOUSE_DB), read_only=read_only)


def ensure_metrics_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Upstrace ki apni tables banao agar abhi nahi hain.

    profile_runs    - har `upstrace profile` call ka ek row
    column_profiles - har run ke har column ka ek row. Yahi metric history hai.
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {METRICS_SCHEMA}")

    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {METRICS_SCHEMA}.profile_runs (
            run_id       VARCHAR PRIMARY KEY,
            started_at   TIMESTAMP,
            finished_at  TIMESTAMP,
            models_count INTEGER
        )
    """)

    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {METRICS_SCHEMA}.column_profiles (
            run_id          VARCHAR,
            profiled_at     TIMESTAMP,
            model_name      VARCHAR,
            column_name     VARCHAR,
            data_type       VARCHAR,
            row_count       BIGINT,
            null_count      BIGINT,
            null_rate       DOUBLE,
            distinct_count  BIGINT,
            distinct_rate   DOUBLE,
            min_value       VARCHAR,
            max_value       VARCHAR,
            mean_value      DOUBLE
        )
    """)
    
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {METRICS_SCHEMA}.partition_profiles (
            run_id          VARCHAR,
            profiled_at     TIMESTAMP,
            model_name      VARCHAR,
            partition_value DATE,
            column_name     VARCHAR,
            row_count       BIGINT,
            null_count      BIGINT,
            null_rate       DOUBLE,
            distinct_count  BIGINT,
            min_value       VARCHAR,
            max_value       VARCHAR,
            mean_value      DOUBLE
        )
    """)