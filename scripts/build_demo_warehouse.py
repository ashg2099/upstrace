"""Build the demo warehouse inside the Docker image.

Everything the Space needs is baked at build time so the container starts in
under a second: raw table, dbt models, and two clean profile runs. The second
run matters - it means the dashboard opens with a settled baseline and zero
drift rather than a false alarm on first load.
"""

import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from upstrace import config  # noqa: E402


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> None:
    config.WAREHOUSE_DB.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(config.DATA_DIR / "yellow_tripdata_*.parquet")

    con = duckdb.connect(str(config.WAREHOUSE_DB))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("DROP TABLE IF EXISTS raw.yellow_trips")
    con.execute(
        f"CREATE TABLE raw.yellow_trips AS SELECT * FROM read_parquet('{pattern}')"
    )
    rows = con.execute("SELECT count(*) FROM raw.yellow_trips").fetchone()[0]
    con.close()
    print(f"loaded {rows:,} rows into raw.yellow_trips")

    run(["dbt", "run", "--profiles-dir", "."], cwd=config.DBT_PROJECT_DIR)

    # Two runs over identical data. The first becomes the baseline, the second
    # the current state, and the comparison between them is empty by
    # construction - which is the determinism claim the eval makes, asserted
    # here at build time.
    run(["upstrace", "profile"])
    run(["upstrace", "profile"])

    print("\ndemo warehouse ready")


if __name__ == "__main__":
    main()