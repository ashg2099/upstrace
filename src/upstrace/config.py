from pathlib import Path

# config.py -> upstrace/ -> src/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

WAREHOUSE_DB = PROJECT_ROOT / "warehouse" / "upstrace.duckdb"
DBT_PROJECT_DIR = PROJECT_ROOT / "transform"
MANIFEST_PATH = DBT_PROJECT_DIR / "target" / "manifest.json"

# Upstrace apne metrics apne schema mein likhta hai, tumhare data ke saath kabhi mix nahi karta.
METRICS_SCHEMA = "upstrace_meta"