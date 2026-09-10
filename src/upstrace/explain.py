"""Hand a structured incident to a language model and ask what it means.

Everything before this file was measurement: SQL, arithmetic, graph traversal.
This is the one place a model is allowed to speak, and it is given only what we
already established - never the rows themselves.
"""

import json

from .lineage import graph
from .llm import ask_json
from .rca import Incident

SCHEMA = {
    "summary": "one sentence, plain English, what happened",
    "likely_causes": [
        {
            "cause": "specific and testable",
            "confidence": "high | medium | low",
            "reasoning": "why this fits the evidence",
        }
    ],
    "checks_to_add": ["a dbt test or SQL assertion that would catch this next time"],
    "who_is_affected": "which downstream consumers see wrong numbers, and how",
}

PROMPT_TEMPLATE = """\
You are a data reliability engineer. A monitoring tool detected drift in a dbt
warehouse and traced it to a root node. Explain what most likely happened.

ROOT NODE
  {root} ({kind})
  columns with drift: {columns}

EVIDENCE AT THE ROOT
{evidence}

LINEAGE (node <- its upstream dependencies)
{lineage}

DOWNSTREAM NODES THAT ALSO DRIFTED (node: the columns that drifted there)
{downstream}

RULES
- You have not seen any rows. Reason only from the names and numbers above.
- Column names carry meaning. A name that contradicts the values is a finding.
- Ratios matter. A mean multiplied by a familiar constant suggests a unit change.
- min_value and max_value are the actual smallest and largest values in the
  column, shown quoted. Compare them character by character: a change in casing,
  whitespace, or padding with no change in row count means the values were
  rewritten rather than replaced.
- A value shown as '<masked>' was withheld for privacy. Do not guess what it was.
- Downstream column names are renamed versions of the root column. If a downstream name states a unit and the values no longer fit it, say so
  explicitly and name both units.
- Metrics are computed over the whole table. If a change affected only part of
  the data, the blended ratio understates it, so a ratio below a familiar
  constant does not rule that constant out.
- If the evidence is thin, say low confidence. Do not invent detail.
- Never claim to know who deployed what; you have no commit history.

Reply with JSON only, matching this shape:
{schema}
"""


def _format_evidence(incident: Incident) -> str:
    lines = []
    for e in incident.evidence:
        if e.metric in ("min_value", "max_value"):
            # The values themselves, not just the fact that they moved. When a
            # column is renamed the clue is in the name; when values are
            # rewritten - a casing change, a trimmed string, a new code - the
            # clue is only visible here.
            if e.baseline_text is not None or e.current_text is not None:
                lines.append(
                    f"  {e.column_name}.{e.metric}: "
                    f"{e.baseline_text!r} -> {e.current_text!r}"
                )
            else:
                lines.append(f"  {e.column_name}.{e.metric}: changed")
            continue
        line = (
            f"  {e.column_name}.{e.metric}: {e.baseline:,.4f} -> {e.current:,.4f}"
            f"  ({e.change:.1%} change, severity {e.severity}"
        )
        if e.baseline:
            line += f", ratio x{e.current / e.baseline:.4f}"
        lines.append(line + ")")
    return "\n".join(lines)

def _format_downstream(incident: Incident) -> str:
    if not incident.blast_radius:
        return "  none"
    return "\n".join(
        f"  {node}: {', '.join(incident.downstream_columns.get(node, [])) or '(no columns listed)'}"
        for node in incident.blast_radius
    )

def _format_lineage(incident: Incident) -> str:
    _, parents = graph()
    lines = []
    for name in [incident.root] + incident.blast_radius:
        ups = parents.get(name, [])
        lines.append(f"  {name} <- {', '.join(ups) if ups else '(source)'}")
    return "\n".join(lines)


def build_prompt(incident: Incident) -> str:
    return PROMPT_TEMPLATE.format(
        root=incident.root,
        kind="source table" if incident.is_source else "model",
        columns=", ".join(incident.columns),
        evidence=_format_evidence(incident),
        lineage=_format_lineage(incident),
        downstream=_format_downstream(incident),
        schema=json.dumps(SCHEMA, indent=2),
    )


def explain(incident: Incident, use_cache: bool = True) -> dict:
    return ask_json(build_prompt(incident), use_cache=use_cache)