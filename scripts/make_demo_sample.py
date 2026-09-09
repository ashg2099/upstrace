"""Cut a small, committable sample of the taxi data for the public demo.

The full 9.5M-row warehouse is 300+ MB and gitignored. A ~400k-row reservoir
sample keeps ~90 days of history (roughly 4,400 rows/day), which is plenty for
per-day profiling, and fits in git without LFS.

Run once:  python scripts/make_demo_sample.py
"""

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data"
OUT_DIR = SRC / "demo"
OUT = OUT_DIR / "yellow_tripdata_demo.parquet"

ROWS = 400_000


def main() -> None:
    files = sorted(SRC.glob("yellow_tripdata_2*.parquet"))
    if not files:
        raise SystemExit(f"No source parquet in {SRC}. Run scripts/download_data.py first.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pattern = str(SRC / "yellow_tripdata_2*.parquet")

    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT *
            FROM read_parquet('{pattern}')
            WHERE tpep_pickup_datetime >= DATE '2024-01-01'
              AND tpep_pickup_datetime <  DATE '2024-04-01'
            USING SAMPLE {ROWS} ROWS (reservoir, 42)
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    rows = con.execute(f"SELECT count(*) FROM read_parquet('{OUT}')").fetchone()[0]
    days = con.execute(
        f"SELECT count(DISTINCT CAST(tpep_pickup_datetime AS DATE)) FROM read_parquet('{OUT}')"
    ).fetchone()[0]
    size_mb = OUT.stat().st_size / 1_048_576

    print(f"\n{OUT}")
    print(f"  {rows:,} rows across {days} days, {size_mb:.1f} MB")
    print("  commit this file - the Docker image builds its warehouse from it.\n")


if __name__ == "__main__":
    main()