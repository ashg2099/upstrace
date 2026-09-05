from dataclasses import dataclass

import duckdb

from .config import DATA_DIR

RAW_TABLE = "raw.yellow_trips"


@dataclass
class Fault:
    key: str
    description: str
    sql: str          # {since} injection ke time bhara jaata hai


FAULTS: dict[str, Fault] = {
    "unit-change": Fault(
        key="unit-change",
        description="trip_distance silently switches from miles to kilometres",
        sql=f"""
            update {RAW_TABLE}
            set trip_distance = round(trip_distance * 1.60934, 2)
            where tpep_pickup_datetime >= '{{since}}'
        """,
    ),
    "null-spike": Fault(
        key="null-spike",
        description="vendor 2 stops sending passenger_count",
        sql=f"""
            update {RAW_TABLE}
            set passenger_count = null
            where "VendorID" = 2 and tpep_pickup_datetime >= '{{since}}'
        """,
    ),
    "enum-drift": Fault(
        key="enum-drift",
        description="an undocumented payment_type 7 starts appearing",
        sql=f"""
            update {RAW_TABLE}
            set payment_type = 7
            where payment_type = 1
              and tpep_pickup_datetime >= '{{since}}'
              and hash(tpep_pickup_datetime) % 10 = 0
        """,
    ),
    "case-change": Fault(
        key="case-change",
        description="store_and_fwd_flag starts arriving lowercase",
        sql=f"""
            update {RAW_TABLE}
            set store_and_fwd_flag = lower(store_and_fwd_flag)
            where tpep_pickup_datetime >= '{{since}}'
        """,
    ),
}


def inject(con: duckdb.DuckDBPyConnection, key: str, since: str) -> int:
    if key not in FAULTS:
        raise SystemExit(
            f"Unknown fault {key!r}. Known: {', '.join(sorted(FAULTS))}"
        )

    before = con.execute(f"select count(*) from {RAW_TABLE}").fetchone()[0]
    con.execute(FAULTS[key].sql.format(since=since))
    after = con.execute(f"select count(*) from {RAW_TABLE}").fetchone()[0]
    return before - after if before != after else before


def reset(con: duckdb.DuckDBPyConnection) -> int:
    """Raw table ko parquet se dobara load karo. Har fault undo ho jaata hai."""
    pattern = str(DATA_DIR / "yellow_tripdata_*.parquet")
    con.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")
    con.execute(
        f"CREATE TABLE {RAW_TABLE} AS SELECT * FROM read_parquet('{pattern}')"
    )
    return con.execute(f"select count(*) from {RAW_TABLE}").fetchone()[0]