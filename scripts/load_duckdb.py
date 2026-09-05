"""
Downloaded parquet files ko ek local DuckDB warehouse mein load karo.

DuckDB ek database hai jo disk pe ek single file mein rehta hai. Na server
start karna, na password, na Docker. Samjho "SQLite, but analytics ke liye".

Run:  python scripts/load_duckdb.py
"""

from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
WAREHOUSE_DIR = PROJECT_ROOT / "warehouse"
DB_PATH = WAREHOUSE_DIR / "upstrace.duckdb"


def main() -> None:
    files = sorted(DATA_DIR.glob("yellow_tripdata_*.parquet"))
    if not files:
        raise SystemExit(
            f"No parquet files in {DATA_DIR}. Run scripts/download_data.py first."
        )

    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    # "schema" matlab tables ka ek namespace. raw = bilkul untouched source data.
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")

    # DuckDB parquet ko seedha disk se padh leta hai - pandas ki zaroorat nahi.
    pattern = str(DATA_DIR / "yellow_tripdata_*.parquet")
    con.execute("DROP TABLE IF EXISTS raw.yellow_trips")
    con.execute(
        f"CREATE TABLE raw.yellow_trips AS SELECT * FROM read_parquet('{pattern}')"
    )

    rows = con.execute("SELECT count(*) FROM raw.yellow_trips").fetchone()[0]
    cols = con.execute("DESCRIBE raw.yellow_trips").fetchall()

    print(f"\nLoaded {rows:,} rows from {len(files)} file(s) into raw.yellow_trips")
    print(f"Warehouse file: {DB_PATH}\n")
    print(f"{len(cols)} columns:")
    for name, dtype, *_ in cols:
        print(f"  {name:<28} {dtype}")

    quality_report(con)
    con.close()


def quality_report(con: duckdb.DuckDBPyConnection) -> None:
    """Ek one-off, haath se likha hua check ki data kitna ganda hai.

    Yahi manual kaam Sentinel baad mein automatically karega. Ek baar haath se
    likhna zaroori hai: automate karne se pehle dard mehsoos karna padta hai.
    """
    checks = [
        ("rows with a negative fare", "fare_amount < 0"),
        ("rows with zero or null passengers", "passenger_count IS NULL OR passenger_count = 0"),
        ("rows with zero distance but fare > $50", "trip_distance = 0 AND fare_amount > 50"),
        ("rows where dropoff is before pickup", "tpep_dropoff_datetime < tpep_pickup_datetime"),
        ("rows outside 2024", "year(tpep_pickup_datetime) <> 2024"),
        ("rows with total_amount < fare_amount", "total_amount < fare_amount"),
    ]

    total = con.execute("SELECT count(*) FROM raw.yellow_trips").fetchone()[0]
    print("\nHand-written data quality check")
    print("-" * 56)
    for label, predicate in checks:
        try:
            n = con.execute(
                f"SELECT count(*) FROM raw.yellow_trips WHERE {predicate}"
            ).fetchone()[0]
        except duckdb.Error as exc:  # kisi month mein column missing ho sakta hai
            print(f"  {label:<42} skipped ({exc.__class__.__name__})")
            continue
        pct = (n / total * 100) if total else 0
        print(f"  {label:<42} {n:>9,}  ({pct:5.2f}%)")
    print("-" * 56)
    print("Every one of these rows passed the pipeline 'successfully'.\n")


if __name__ == "__main__":
    main()