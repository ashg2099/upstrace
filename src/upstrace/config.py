from pathlib import Path

from dotenv import load_dotenv

# config.py -> upstrace/ -> src/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Read .env into the environment before anything asks for a key.
# override=False so a real shell variable always beats the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)

DATA_DIR = PROJECT_ROOT / "data"
WAREHOUSE_DB = PROJECT_ROOT / "warehouse" / "upstrace.duckdb"
DBT_PROJECT_DIR = PROJECT_ROOT / "transform"
MANIFEST_PATH = DBT_PROJECT_DIR / "target" / "manifest.json"

# Upstrace apne metrics apne schema mein likhta hai, tumhare data ke saath kabhi mix nahi karta.
METRICS_SCHEMA = "upstrace_meta"