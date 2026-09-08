"""Run every scenario, score the analysis, and report the number honestly.

The loop per scenario:

    reset to a clean sample -> inject the fault -> rebuild -> profile
    -> detect drift against the clean baseline -> identify the root -> score

Two things this deliberately does NOT do:

  - It does not call a language model. The score measures the deterministic
    part - profiling, drift detection, graph traversal. Mixing a model into the
    headline number would make it unreproducible and unexplainable.
  - It does not hide the misses. A harness that only reports its wins is a
    screenshot, not an evaluation.
"""

import shutil
import subprocess
import time
from dataclasses import dataclass

from . import drift as drift_mod
from . import faults as faults_mod
from . import rca as rca_mod
from .config import DBT_PROJECT_DIR
from .profiler import run_profile
from .scenarios import Scenario, all_scenarios
from .warehouse import connect

DBT_ENV = {"DBT_PROFILES_DIR": "."}


@dataclass
class Result:
    scenario: Scenario
    detected: bool
    predicted_root: str | None
    root_correct: bool
    column_correct: bool
    n_signals: int
    n_incidents: int
    seconds: float

    @property
    def passed(self) -> bool:
        if self.scenario.layer == "control":
            return not self.detected
        return self.detected and self.root_correct and self.column_correct

def _dbt(*args: str) -> None:
    """Run dbt as a subprocess.

    DuckDB allows one writer at a time, so every caller must close its
    connection before this runs. That constraint is why the loop below keeps
    opening and closing connections instead of holding one open.
    """
    binary = shutil.which("dbt")
    if not binary:
        raise SystemExit("dbt is not on PATH. pip install -e . inside your venv.")

    import os
    env = {**os.environ, **DBT_ENV}
    result = subprocess.run(
        [binary, *args],
        cwd=DBT_PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"dbt {' '.join(args)} failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )


def _build_baseline(sample: int) -> str:
    """Clean sample, built and profiled once. Every scenario compares to this."""
    con = connect()
    faults_mod.reset(con, sample=sample)
    con.close()

    _dbt("run")

    con = connect()
    run_id = run_profile(con)
    con.close()
    return run_id


def _score(scenario: Scenario, incidents: list, n_signals: int) -> tuple:
    if scenario.layer == "control":
        # Not every signal is an alert. A control passes if nothing rose to
        # high or critical; warnings are informational by design, and min/max
        # changes are always warnings because any change at all trips them.
        alerting = [i for i in incidents if i.severity in ("critical", "high")]
        found = bool(alerting)
        return found, (alerting[0].root if found else None), not found, not found

    if not incidents:
        return False, None, False, False

    top = incidents[0]
    root_correct = top.root == scenario.expected_root

    if not scenario.expected_columns:
        # Row loss has no single guilty column; getting the root right is the test.
        column_correct = root_correct
    else:
        column_correct = any(
            expected in col
            for expected in scenario.expected_columns
            for col in top.columns
        )

    return True, top.root, root_correct, column_correct

def run_one(scenario: Scenario, baseline_run_id: str, sample: int) -> Result:
    started = time.time()

    con = connect()
    faults_mod.reset(con, sample=sample, seed=scenario.seed)
    con.close()

    if scenario.layer == "control":
        # No fault. Only the sample seed differs, the way real daily data
        # differs from one day to the next. Silence is the correct answer.
        _dbt("run")

    elif scenario.layer == "source":
        con = connect()
        con.execute(scenario.sql)
        con.close()
        _dbt("run")

    else:
        # A transformation bug: build cleanly first, break the model, then
        # rebuild only what sits below it.
        _dbt("run")
        con = connect()
        con.execute(scenario.sql)
        con.close()
        _dbt("run", "--select", "agg_daily_revenue", "agg_daily_quality")

    con = connect()
    run_id = run_profile(con)
    signals = drift_mod.detect(con, run_id=run_id, baseline_run_id=baseline_run_id)
    incidents = rca_mod.analyse(con, run_id=run_id)
    con.close()

    detected, predicted, root_ok, col_ok = _score(scenario, incidents, len(signals))

    return Result(
        scenario=scenario,
        detected=detected,
        predicted_root=predicted,
        root_correct=root_ok,
        column_correct=col_ok,
        n_signals=len(signals),
        n_incidents=len(incidents),
        seconds=round(time.time() - started, 1),
    )

def run_all(sample: int, limit: int | None = None, on_result=None) -> list[Result]:
    scenarios = all_scenarios()
    if limit:
        scenarios = scenarios[:limit]

    baseline_run_id = _build_baseline(sample)

    results = []
    for scenario in scenarios:
        result = run_one(scenario, baseline_run_id, sample)
        results.append(result)
        if on_result:
            on_result(result)

    return results

def summarise(results: list[Result]) -> dict:
    faults = [r for r in results if r.scenario.layer != "control"]
    controls = [r for r in results if r.scenario.layer == "control"]
    total = len(faults)
    return {
        "total": total,
        "detected": sum(r.detected for r in faults),
        "detection_rate": sum(r.detected for r in faults) / total if total else 0,
        "root_correct": sum(r.root_correct for r in faults),
        "column_correct": sum(r.column_correct for r in faults),
        "passed": sum(r.passed for r in faults),
        "pass_rate": sum(r.passed for r in faults) / total if total else 0,
        "controls": len(controls),
        "false_positives": sum(r.detected for r in controls),
    }

def breakdown(results: list[Result], attr: str) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = {}
    for r in results:
        key = getattr(r.scenario, attr)
        bucket = out.setdefault(key, [0, 0])
        bucket[0] += int(r.passed)
        bucket[1] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}

def to_markdown(results: list[Result], sample: int) -> str:
    s = summarise(results)
    lines = [
        "# Evaluation: fault injection",
        "",
        f"{s['total']} seeded defects, each with a known root cause, plus "
        f"{s['controls']} controls where nothing was broken and the correct "
        "answer is silence. Every scenario runs against a repeatable "
        f"{sample:,}-row sample.",
        "",
        "No language model is involved in this score. It measures profiling, "
        "drift detection and graph traversal only.",
        "",
        "## Headline",
        "",
        "| metric | result |",
        "|---|---|",
        f"| drift detected | {s['detected']}/{s['total']} ({s['detection_rate']:.0%}) |",
        f"| correct root node | {s['root_correct']}/{s['total']} ({s['root_correct']/s['total']:.0%}) |",
        f"| correct column | {s['column_correct']}/{s['total']} ({s['column_correct']/s['total']:.0%}) |",
        f"| **fully correct** | **{s['passed']}/{s['total']} ({s['pass_rate']:.0%})** |",
        f"| false positives | {s['false_positives']}/{s['controls']} controls |",
        "",
        "Detection rate alone is not evidence of anything: a detector that fires "
        "on every run would score 100% on it. The false-positive row is what "
        "makes the rest meaningful.",
        "",
    ]

    for label, attr in (("magnitude", "magnitude"), ("window", "window"), ("layer", "layer")):
        lines += [f"## By {label}", "", f"| {label} | passed |", "|---|---|"]
        for key, (passed, count) in breakdown(results, attr).items():
            lines.append(f"| {key} | {passed}/{count} ({passed/count:.0%}) |")
        lines.append("")

    misses = [r for r in results if not r.passed]
    lines += ["## Everything that did not behave correctly", ""]
    if not misses:
        lines.append(
            "Nothing. With controls in the suite that is a stronger result than "
            "it was without them, but it still means the scenarios are not yet "
            "hard enough to discriminate between versions."
        )
    else:
        lines += ["| scenario | what it was | detected | root said |", "|---|---|---|---|"]
        for r in misses:
            lines.append(
                f"| `{r.scenario.id}` | {r.scenario.description} | "
                f"{'yes' if r.detected else 'no'} | {r.predicted_root or '-'} |"
            )
    lines.append("")
    return "\n".join(tail := lines)