import json
from dataclasses import dataclass, field

from .config import MANIFEST_PATH


@dataclass
class Model:
    unique_id: str          # e.g. model.upstrace.fct_trips
    name: str               # e.g. fct_trips
    relation: str           # e.g. "upstrace"."main"."fct_trips"  (query this)
    materialized: str       # view / table / source
    depends_on: list[str] = field(default_factory=list)   # upstream unique_ids
    is_source: bool = False

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


def list_sources(manifest: dict | None = None) -> list[Model]:
    """Sources are where the graph starts. Profile them too, or the root cause
    always looks like the first model instead of the data that arrived."""
    manifest = manifest or load_manifest()
    sources = []

    for unique_id, node in manifest.get("sources", {}).items():
        sources.append(
            Model(
                unique_id=unique_id,
                name=node["name"],
                relation=node["relation_name"],
                materialized="source",
                depends_on=[],
                is_source=True,
            )
        )

    return sorted(sources, key=lambda m: m.name)


def list_nodes(manifest: dict | None = None) -> list[Model]:
    """Sources and models together - everything Upstrace can profile."""
    manifest = manifest or load_manifest()
    return list_sources(manifest) + list_models(manifest)


def build_lineage(manifest: dict | None = None) -> dict[str, list[str]]:
    """node name -> list of upstream node names.

    This dict is the graph the root-cause search walks.
    """
    return {m.name: m.parents for m in list_nodes(manifest)}