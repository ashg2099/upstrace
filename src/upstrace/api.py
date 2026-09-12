"""HTTP API for the dashboard.

Two constraints shape everything here:

1. DuckDB allows one writer at a time, and dbt runs as a separate process. So
   every mutating request takes a lock, opens a connection only for as long as
   it needs one, and closes it before dbt starts.
2. Injecting a fault means rebuild + reprofile, which takes seconds, not
   milliseconds. These endpoints are deliberately synchronous: the UI shows a
   spinner and waits. A job queue would be more correct at scale and is not
   worth the moving parts for a single-user demo.
"""

import os
import shutil
import subprocess
import threading
from datetime import date, datetime

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import drift as drift_mod
from . import explain as explain_mod
from . import faults as faults_mod
from . import rca as rca_mod
from .config import DBT_PROJECT_DIR, METRICS_SCHEMA, PROJECT_ROOT, WAREHOUSE_DB
from .lineage import graph
from .profiler import run_profile

app = FastAPI(title="Upstrace", docs_url="/api/docs", openapi_url="/api/openapi.json")

# One lock for the whole warehouse. Reads are cheap; writes must not overlap.
WAREHOUSE_LOCK = threading.Lock()

STATIC_DIR = PROJECT_ROOT / "app" / "static"
DEMO_SAMPLE = int(os.environ.get("UPSTRACE_DEMO_SAMPLE", "0")) or None


def _connect() -> duckdb.DuckDBPyConnection:
    if not WAREHOUSE_DB.exists():
        raise HTTPException(
            503,
            "No warehouse yet. Check the 'warehouse:' path in upstrace.yml, then "
            "build it with dbt. For this repo's demo: python scripts/load_duckdb.py",
        )
    return duckdb.connect(str(WAREHOUSE_DB))


def _dbt(*args: str) -> None:
    binary = shutil.which("dbt")
    if not binary:
        raise HTTPException(500, "dbt is not on PATH. Activate the venv first.")

    result = subprocess.run(
        [binary, *args],
        cwd=DBT_PROJECT_DIR,
        env={**os.environ, "DBT_PROFILES_DIR": "."},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HTTPException(500, f"dbt {' '.join(args)} failed: {result.stdout[-800:]}")


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------
# read endpoints
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "warehouse": WAREHOUSE_DB.exists(),
        "demo_sample": DEMO_SAMPLE,
    }


@app.get("/api/overview")
def overview() -> dict:
    con = _connect()
    try:
        runs = con.execute(f"""
            select run_id, started_at, models_count
            from {METRICS_SCHEMA}.profile_runs
            order by started_at desc limit 2
        """).fetchall()

        if not runs:
            return {"runs": [], "incidents": 0, "signals": 0, "nodes": 0}

        latest = runs[0][0]
        signals = con.execute(
            f"select count(*) from {METRICS_SCHEMA}.drift_signals where run_id = ?",
            [latest],
        ).fetchone()[0]
        incidents = con.execute(
            f"select count(*) from {METRICS_SCHEMA}.incidents where run_id = ?",
            [latest],
        ).fetchone()[0]
        rows = con.execute(f"""
            select sum(row_count) from (
                select model_name, max(row_count) as row_count
                from {METRICS_SCHEMA}.column_profiles
                where run_id = ? group by model_name
            )
        """, [latest]).fetchone()[0]
    finally:
        con.close()

    nodes, _ = graph()
    return {
        "runs": [
            {"run_id": r[0], "started_at": _json_safe(r[1]), "models": r[2]}
            for r in runs
        ],
        "latest_run": latest,
        "incidents": incidents,
        "signals": signals,
        "nodes": len(nodes),
        "rows_profiled": int(rows or 0),
    }


@app.get("/api/lineage")
def lineage() -> dict:
    con = _connect()
    try:
        affected = {
            row[0] for row in con.execute(f"""
                select distinct model_name from {METRICS_SCHEMA}.drift_signals
                where run_id = (
                    select run_id from {METRICS_SCHEMA}.profile_runs
                    order by started_at desc limit 1
                )
            """).fetchall()
        }
        roots = {
            row[0] for row in con.execute(f"""
                select root from {METRICS_SCHEMA}.incidents
                where run_id = (
                    select run_id from {METRICS_SCHEMA}.profile_runs
                    order by started_at desc limit 1
                )
            """).fetchall()
        }
    finally:
        con.close()

    nodes, parents = graph()
    return {
        "nodes": [
            {
                "name": name,
                "kind": "source" if node.is_source else "model",
                "materialized": node.materialized,
                "state": "root" if name in roots
                         else "affected" if name in affected
                         else "ok",
            }
            for name, node in nodes.items()
        ],
        "edges": [
            {"source": parent, "target": child}
            for child, ps in parents.items() for parent in ps
        ],
    }


@app.get("/api/incidents")
def incidents() -> list[dict]:
    con = _connect()
    try:
        rows = con.execute(f"""
            select detected_at, root, is_source, severity, columns,
                   blast_radius, signal_count
            from {METRICS_SCHEMA}.incidents
            where run_id = (
                select run_id from {METRICS_SCHEMA}.profile_runs
                order by started_at desc limit 1
            )
            order by case severity
                when 'critical' then 0 when 'high' then 1 else 2 end
        """).fetchall()
    finally:
        con.close()

    return [
        {
            "detected_at": _json_safe(r[0]),
            "root": r[1],
            "is_source": r[2],
            "severity": r[3],
            "columns": [c for c in (r[4] or "").split(", ") if c],
            "blast_radius": [c for c in (r[5] or "").split(", ") if c],
            "signal_count": r[6],
        }
        for r in rows
    ]


@app.get("/api/incidents/{root}")
def incident_detail(root: str, explain: bool = False) -> dict:
    con = _connect()
    try:
        found = [i for i in rca_mod.analyse(con) if i.root == root]
        if not found:
            raise HTTPException(404, f"No current incident rooted at {root!r}")
        incident = found[0]
    finally:
        con.close()

    payload = {
        "root": incident.root,
        "is_source": incident.is_source,
        "severity": incident.severity,
        "columns": incident.columns,
        "blast_radius": incident.blast_radius,
        "downstream_columns": incident.downstream_columns,
        "evidence": [
            {
                "column": e.column_name,
                "metric": e.metric,
                "baseline": e.baseline,
                "current": e.current,
                "change": e.change,
                "severity": e.severity,
                "partitions": e.partitions,
            }
            for e in incident.evidence
        ],
        "explanation": None,
    }

    if explain:
        # Cached by prompt hash, so this is usually free and always reproducible.
        try:
            payload["explanation"] = explain_mod.explain(incident)
        except SystemExit as exc:
            payload["explanation"] = {"error": str(exc)}

    return payload


@app.get("/api/metrics/{model}/{column}")
def metric_history(model: str, column: str, metric: str = "mean_value") -> dict:
    allowed = {"mean_value", "null_rate", "distinct_count", "row_count"}
    if metric not in allowed:
        raise HTTPException(400, f"metric must be one of {sorted(allowed)}")

    con = _connect()
    try:
        runs = [
            r[0] for r in con.execute(f"""
                select run_id from {METRICS_SCHEMA}.profile_runs
                order by started_at desc limit 2
            """).fetchall()
        ]
        if not runs:
            raise HTTPException(404, "No profile runs yet")

        series = {}
        for label, run_id in zip(("current", "baseline"), runs):
            series[label] = [
                {"date": _json_safe(r[0]), "value": r[1]}
                for r in con.execute(f"""
                    select partition_value, {metric}
                    from {METRICS_SCHEMA}.partition_profiles
                    where run_id = ? and model_name = ? and column_name = ?
                    order by partition_value
                """, [run_id, model, column]).fetchall()
            ]
    finally:
        con.close()

    return {"model": model, "column": column, "metric": metric, **series}


@app.get("/api/faults")
def list_faults() -> list[dict]:
    return [
        {"key": f.key, "description": f.description}
        for f in faults_mod.FAULTS.values()
    ]


# --------------------------------------------------------------------------
# write endpoints - these rebuild the warehouse, so they take the lock
# --------------------------------------------------------------------------

def _profile_and_analyse() -> dict:
    con = _connect()
    try:
        run_id = run_profile(con)
        signals = drift_mod.detect(con, run_id=run_id)
        incidents = rca_mod.analyse(con, run_id=run_id)
    finally:
        con.close()

    return {
        "run_id": run_id,
        "signals": len(signals),
        "incidents": [
            {"root": i.root, "severity": i.severity, "columns": i.columns}
            for i in incidents
        ],
    }


def _rebuild_and_analyse() -> dict:
    _dbt("run")
    return _profile_and_analyse()


@app.post("/api/faults/{key}")
def inject_fault(key: str, since: str = "2024-03-01") -> dict:
    if key not in faults_mod.FAULTS:
        raise HTTPException(404, f"Unknown fault {key!r}")

    with WAREHOUSE_LOCK:
        con = _connect()
        try:
            faults_mod.reset(con, sample=DEMO_SAMPLE)
            faults_mod.inject(con, key, since)
        finally:
            con.close()

        result = _rebuild_and_analyse()

    return {"injected": key, "since": since, **result}


@app.post("/api/reset")
def reset_warehouse() -> dict:
    with WAREHOUSE_LOCK:
        con = _connect()
        try:
            rows = faults_mod.reset(con, sample=DEMO_SAMPLE)
        finally:
            con.close()

        _rebuild_and_analyse()
        # Profile a second time so the two most recent runs are both clean.
        # Without this the dashboard would show the repair itself as drift.
        result = _profile_and_analyse()

    return {"reset": True, "rows": rows, **result}


# --------------------------------------------------------------------------
# the built React app, when it exists
# --------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """Every non-API path returns index.html so client-side routing works."""
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "UI not built. Run: cd app && npm run build")
        return FileResponse(index)