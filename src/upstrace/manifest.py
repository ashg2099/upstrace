import json
from dataclasses import dataclass, field

from .config import MANIFEST_PATH


@dataclass
class Model:
    unique_id: str          # e.g. model.upstrace.fct_trips
    name: str               # e.g. fct_trips
    relation: str           # e.g. "upstrace"."main"."fct_trips"  (query this)
    materialized: str       # view / table
    depends_on: list[str] = field(default_factory=list)   # upstream unique_ids

    @property
    def parents(self) -> list[str]:
        """Upstream node ke naam, bina model./source. prefix ke."""
        return [dep.split(".")[-1] for dep in self.depends_on]


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise SystemExit(
            f"No manifest at {MANIFEST_PATH}.\n"
            "Run this first:  cd transform && dbt run"
        )
    with open(MANIFEST_PATH) as fh:
        return json.load(fh)


def list_models(manifest: dict | None = None) -> list[Model]:
    manifest = manifest or load_manifest()
    models = []

    for unique_id, node in manifest["nodes"].items():
        if node.get("resource_type") != "model":
            continue
        models.append(
            Model(
                unique_id=unique_id,
                name=node["name"],
                relation=node["relation_name"],
                materialized=node["config"]["materialized"],
                depends_on=node.get("depends_on", {}).get("nodes", []),
            )
        )

    return sorted(models, key=lambda m: m.name)


def build_lineage(manifest: dict | None = None) -> dict[str, list[str]]:
    """model name -> upstream node names ki list.

    Ye chhota sa dict hi wo graph ka beej hai jispe RCA agent chalega.
    """
    return {m.name: m.parents for m in list_models(manifest)}