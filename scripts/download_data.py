"""
Download NYC Taxi (yellow cab) trip data as parquet files.

Parquet ek columnar file format hai - CSV jaisa, but compressed aur typed,
isliye 3 million rows ~50 MB mein aa jaate hain, 600 MB ki jagah.

Run:  python scripts/download_data.py
      python scripts/download_data.py 2024-01 2024-02 2024-03
"""

import sys
from pathlib import Path

import requests

BASE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data"
DEFAULT_MONTHS = ["2024-01", "2024-02", "2024-03"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


def download(month: str) -> Path:
    filename = f"yellow_tripdata_{month}.parquet"
    target = DATA_DIR / filename

    if target.exists():
        size_mb = target.stat().st_size / 1_048_576
        print(f"  {filename}  already here ({size_mb:.1f} MB), skipping")
        return target

    url = f"{BASE_URL}/{filename}"
    print(f"  {filename}  downloading...", end="", flush=True)

    response = requests.get(url, stream=True, timeout=120)
    if response.status_code != 200:
        print(f" FAILED (HTTP {response.status_code})")
        raise SystemExit(
            f"\nCould not fetch {url}\n"
            "The NYC TLC sometimes moves these files. Open\n"
            "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page\n"
            "right-click a 'Yellow Taxi Trip Records' link, copy the address,\n"
            "and update BASE_URL in this script."
        )

    # Chunks mein likho taaki 50 MB file poori memory mein na baithe.
    tmp = target.with_suffix(".parquet.part")
    with open(tmp, "wb") as fh:
        for chunk in response.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
    tmp.rename(target)

    size_mb = target.stat().st_size / 1_048_576
    print(f" done ({size_mb:.1f} MB)")
    return target


def main() -> None:
    months = sys.argv[1:] or DEFAULT_MONTHS
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nDownloading {len(months)} month(s) into {DATA_DIR}")
    for month in months:
        download(month)

    total_mb = sum(f.stat().st_size for f in DATA_DIR.glob("*.parquet")) / 1_048_576
    print(f"\n{total_mb:.1f} MB on disk. Next: python scripts/load_duckdb.py\n")


if __name__ == "__main__":
    main()