"""Runtime configuration for Upstrace.

The engine is deliberately dataset-agnostic: it reads a dbt manifest, profiles
whatever columns exist, and walks whatever lineage it finds. Everything that is
genuinely project-specific - which warehouse, which models, what counts as a
partition, how much movement is too much - lives here, in one YAML file, so
pointing Upstrace at a different project never means editing Python.

Resolution order, lowest priority first:
    built-in defaults  ->  upstrace.yml  ->  UPSTRACE_* environment variables
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

CONFIG_FILENAME = "upstrace.yml"

DEFAULT_THRESHOLDS: dict[str, float] = {
    "row_count": 0.02,
    "null_rate": 0.01,
    "distinct_count": 0.10,
    "mean_value": 0.05,
}

# A signal is a warning at 1x the threshold, high at 2x, critical at 5x.
DEFAULT_SEVERITY: dict[str, float] = {"warning": 1.0, "high": 2.0, "critical": 5.0}

PartitionMode = Literal["auto", "fixed", "off"]


def _find_config(start: Path) -> Path | None:
    """Walk up from `start` looking for upstrace.yml, like git finds .git."""
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Settings:
    config_path: Path | None
    project_root: Path
    project: str
    warehouse: Path
    dbt_project_dir: Path
    data_dir: Path
    metrics_schema: str
    include: list[str]
    exclude: list[str]
    partition_columns: dict[str, str | None]
    max_partitions: int
    thresholds_default: dict[str, float]
    thresholds_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    severity: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SEVERITY))

    # ---------------------------------------------------------------- selection

    def matches(self, node: str) -> bool:
        """Should this node be profiled at all?

        Big dbt projects have hundreds of models; profiling every one of them on
        every run is a waste, and some (PII staging, scratch tables) should never
        be read. Exclude always wins over include.
        """
        if any(fnmatch.fnmatch(node, pattern) for pattern in self.exclude):
            return False
        return any(fnmatch.fnmatch(node, pattern) for pattern in self.include)

    # ---------------------------------------------------------------- partition

    def partition_override(self, node: str) -> tuple[PartitionMode, str | None]:
        """How to partition this node.

        Three states, and the difference matters:
          ("auto",  None) - not configured, fall back to the type heuristic
          ("fixed", col)  - profile per value of this column
          ("off",   None) - explicitly `null` in the YAML: whole-table only

        The heuristic (first DATE column, else first TIMESTAMP) is right often
        enough to be the default and wrong often enough to need an escape hatch:
        a table whose first date column is `customer_signup_date` would be
        profiled along entirely the wrong axis, and silently so.
        """
        if node not in self.partition_columns:
            return ("auto", None)
        value = self.partition_columns[node]
        if value is None:
            return ("off", None)
        return ("fixed", str(value))

    # --------------------------------------------------------------- thresholds

    def thresholds_for(self, node: str) -> dict[str, float]:
        """Per-node thresholds, falling back to the defaults.

        A 2% row-count threshold is sane for a fact table and useless for a
        marketing events table that naturally swings 40% day to day. Without
        this, the only way to stop the false alarms is to raise the threshold
        globally - which is how alerting systems get ignored.
        """
        resolved = dict(self.thresholds_default)
        for pattern, overrides in self.thresholds_overrides.items():
            if fnmatch.fnmatch(node, pattern):
                resolved.update(overrides)
        return resolved

    # -------------------------------------------------------------------- misc

    def describe(self) -> dict[str, Any]:
        return {
            "config file": str(self.config_path) if self.config_path else "(defaults, no upstrace.yml found)",
            "project": self.project,
            "warehouse": str(self.warehouse),
            "dbt project": str(self.dbt_project_dir),
            "metrics schema": self.metrics_schema,
            "include": ", ".join(self.include),
            "exclude": ", ".join(self.exclude) or "(none)",
            "partition overrides": ", ".join(
                f"{k}={v if v is not None else 'off'}" for k, v in self.partition_columns.items()
            ) or "(auto for every node)",
            "max partitions": str(self.max_partitions),
            "thresholds": ", ".join(f"{k}={v}" for k, v in self.thresholds_default.items()),
            "threshold overrides": ", ".join(self.thresholds_overrides) or "(none)",
        }


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_settings(config_path: Path | None = None) -> Settings:
    package_root = Path(__file__).resolve().parents[2]

    if config_path is None:
        env_path = os.getenv("UPSTRACE_CONFIG")
        config_path = Path(env_path).expanduser() if env_path else _find_config(Path.cwd())
        if config_path is None:
            candidate = package_root / CONFIG_FILENAME
            config_path = candidate if candidate.is_file() else None

    raw: dict[str, Any] = {}
    if config_path is not None:
        with open(config_path) as fh:
            raw = yaml.safe_load(fh) or {}

    base = config_path.parent if config_path is not None else package_root

    profile = raw.get("profile") or {}
    thresholds = raw.get("thresholds") or {}

    warehouse = _resolve(base, os.getenv("UPSTRACE_WAREHOUSE") or raw.get("warehouse") or "warehouse/upstrace.duckdb")
    dbt_dir = _resolve(base, os.getenv("UPSTRACE_DBT_PROJECT_DIR") or raw.get("dbt_project_dir") or "transform")
    data_dir = _resolve(base, raw.get("data_dir") or "data")

    return Settings(
        config_path=config_path,
        project_root=base,
        project=raw.get("project") or "upstrace",
        warehouse=warehouse,
        dbt_project_dir=dbt_dir,
        data_dir=data_dir,
        metrics_schema=os.getenv("UPSTRACE_METRICS_SCHEMA") or raw.get("metrics_schema") or "upstrace_meta",
        include=list(profile.get("include") or ["*"]),
        exclude=list(profile.get("exclude") or []),
        partition_columns=dict(profile.get("partition_column") or {}),
        max_partitions=int(profile.get("max_partitions") or 400),
        thresholds_default={**DEFAULT_THRESHOLDS, **(thresholds.get("default") or {})},
        thresholds_overrides=dict(thresholds.get("overrides") or {}),
        severity={**DEFAULT_SEVERITY, **(raw.get("severity") or {})},
    )


_CACHED: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = load_settings()
    return _CACHED


STARTER_YAML = """\
# Upstrace configuration.
# The engine reads your dbt manifest and profiles whatever it finds - this file
# is the only place anything project-specific lives.

project: my-project

warehouse: warehouse/upstrace.duckdb
dbt_project_dir: transform

profile:
  # Glob patterns matched against dbt node names. Exclude wins over include.
  include: ["*"]
  exclude: []
  #   - "*_scratch"
  #   - "stg_pii_*"

  # Per-day profiling axis. Omit a node to auto-detect (first DATE column, else
  # first TIMESTAMP). Set it explicitly when the heuristic would pick wrong.
  # Set it to null to profile that node whole-table only.
  partition_column: {}
  #   fct_orders: order_date
  #   dim_customers: null

  # Guard rail: refuse to profile a table with more partitions than this.
  max_partitions: 400

thresholds:
  # Relative change that counts as drift. null_rate is in percentage points.
  default:
    row_count: 0.02
    null_rate: 0.01
    distinct_count: 0.10
    mean_value: 0.05

  # Per-node overrides, glob-matched. Some tables are just spiky.
  overrides: {}
  #   agg_marketing_events:
  #     row_count: 0.40

# A signal is a warning at 1x its threshold, high at 2x, critical at 5x.
severity:
  warning: 1.0
  high: 2.0
  critical: 5.0
"""