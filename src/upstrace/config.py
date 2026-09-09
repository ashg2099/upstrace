"""Paths and constants, now derived from settings rather than hardcoded.

This module used to be where the NYC-taxi-shaped assumptions lived. It is kept
as a thin façade so every existing `from upstrace import config` import keeps
working, but the values now come from upstrace.yml.
"""

from pathlib import Path

from dotenv import load_dotenv

from upstrace.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# .env is for secrets (API keys). upstrace.yml is for configuration.
# Keeping them separate is why upstrace.yml is safe to commit.
load_dotenv(PROJECT_ROOT / ".env", override=False)

SETTINGS = get_settings()

DATA_DIR = SETTINGS.data_dir
WAREHOUSE_DB = SETTINGS.warehouse
DBT_PROJECT_DIR = SETTINGS.dbt_project_dir
MANIFEST_PATH = DBT_PROJECT_DIR / "target" / "manifest.json"
METRICS_SCHEMA = SETTINGS.metrics_schema