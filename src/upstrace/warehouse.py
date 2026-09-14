from .config import METRICS_SCHEMA
from .dialect import get_dialect


def connect(read_only: bool = False):
    """Open the configured warehouse.

    The signature is unchanged from when this returned a DuckDB connection
    directly, so every caller in the engine keeps working. What comes back is
    whatever the dialect hands over - it only has to honour DuckDB's contract:
    execute() returns something you can fetch from.
    """
    return get_dialect().connect(read_only=read_only)


def ensure_metrics_tables(con) -> None:
    """Upstrace ki apni tables banao agar abhi nahi hain.

    profile_runs       - har `upstrace profile` call ka ek row
    column_profiles    - har run ke har column ka ek row. Yahi metric history hai.
    partition_profiles - wahi, par har din ka alag row
    """
    double = get_dialect().double_type

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
            null_rate       {double},
            distinct_count  BIGINT,
            distinct_rate   {double},
            min_value       VARCHAR,
            max_value       VARCHAR,
            mean_value      {double}
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
            null_rate       {double},
            distinct_count  BIGINT,
            min_value       VARCHAR,
            max_value       VARCHAR,
            mean_value      {double}
        )
    """)