import typer
from rich.console import Console
from rich.table import Table

from . import drift as drift_mod
from . import faults as faults_mod
from . import rca as rca_mod
from . import explain as explain_mod
from .config import METRICS_SCHEMA
from .manifest import list_models
from .profiler import run_profile
from .warehouse import connect

app = typer.Typer(
    add_completion=False,
    help="Trace a failed data quality check upstream to the change that caused it.",
)
console = Console()


@app.command()
def models() -> None:
    """List the dbt models Upstrace can see, and what each depends on."""
    table = Table(title="Models in the manifest")
    table.add_column("model", style="bold")
    table.add_column("materialized")
    table.add_column("depends on")

    for model in list_models():
        table.add_row(
            model.name,
            model.materialized,
            ", ".join(model.parents) or "-",
        )

    console.print(table)


@app.command()
def profile(
    model: str = typer.Option(None, "--model", "-m", help="Profile one model only."),
) -> None:
    """Measure every column of every model and append to the metric history."""
    con = connect()

    def announce(m):
        console.print(f"  profiling [bold]{m.name}[/bold] ...")

    run_id = run_profile(con, only_model=model, on_model=announce)

    count = con.execute(
        f"select count(*) from {METRICS_SCHEMA}.column_profiles where run_id = ?",
        [run_id],
    ).fetchone()[0]

    console.print(f"\nrun [bold]{run_id}[/bold] wrote {count} column profiles")
    con.close()


@app.command()
def history(
    model: str = typer.Option(..., "--model", "-m"),
    column: str = typer.Option(..., "--column", "-c"),
) -> None:
    """Show how one column's measurements have moved across runs."""
    con = connect(read_only=True)

    rows = con.execute(
        f"""
        select profiled_at, row_count, null_rate, distinct_count, min_value, max_value
        from {METRICS_SCHEMA}.column_profiles
        where model_name = ? and column_name = ?
        order by profiled_at
        """,
        [model, column],
    ).fetchall()

    if not rows:
        console.print(f"No profiles yet for {model}.{column}. Run: upstrace profile")
        raise typer.Exit(1)

    table = Table(title=f"{model}.{column}")
    for header in ("profiled at", "rows", "null rate", "distinct", "min", "max"):
        table.add_column(header)

    for profiled_at, row_count, null_rate, distinct, min_v, max_v in rows:
        table.add_row(
            str(profiled_at)[:19],
            f"{row_count:,}",
            f"{null_rate:.4f}",
            f"{distinct:,}",
            str(min_v)[:24],
            str(max_v)[:24],
        )

    console.print(table)
    con.close()

SEVERITY_STYLE = {"critical": "bold red", "high": "red", "warning": "yellow"}


@app.command()
def fault(
    name: str = typer.Argument(..., help="Fault key, or 'list', or 'reset'."),
    since: str = typer.Option("2024-03-01", "--since", help="Apply from this date onward."),
) -> None:
    """Break the raw data on purpose, or put it back."""
    if name == "list":
        table = Table(title="Available faults")
        table.add_column("key", style="bold")
        table.add_column("what breaks")
        for f in faults_mod.FAULTS.values():
            table.add_row(f.key, f.description)
        console.print(table)
        return

    con = connect()

    if name == "reset":
        rows = faults_mod.reset(con)
        console.print(f"raw.yellow_trips reloaded from parquet: {rows:,} rows")
    else:
        faults_mod.inject(con, name, since)
        console.print(
            f"injected [bold red]{name}[/bold red] from {since} onward\n"
            f"[dim]{faults_mod.FAULTS[name].description}[/dim]"
        )

    con.close()
    console.print("\nNow rebuild and look:")
    console.print("  cd transform && dbt run && dbt test; cd ..")
    console.print("  upstrace profile")
    console.print("  upstrace drift")


@app.command()
def drift() -> None:
    """Compare the two most recent profile runs and report what moved."""
    con = connect()
    signals = drift_mod.detect(con)

    if not signals:
        console.print("No drift above threshold. Nothing moved.")
        con.close()
        return

    table = Table(title=f"{len(signals)} drift signals")
    table.add_column("severity")
    table.add_column("model", style="bold")
    table.add_column("column", style="bold")
    table.add_column("metric")
    table.add_column("baseline", justify="right")
    table.add_column("current", justify="right")
    table.add_column("change", justify="right")

    for s in signals:
        fmt = lambda v: "-" if v is None else (f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.0f}")
        table.add_row(
            f"[{SEVERITY_STYLE[s.severity]}]{s.severity}[/{SEVERITY_STYLE[s.severity]}]",
            s.model_name, s.column_name, s.metric,
            fmt(s.baseline), fmt(s.current),
            "changed" if s.metric in ("min_value", "max_value") else f"{s.change:.1%}",
        )

    console.print(table)
    con.close()
    
@app.command()
def rca() -> None:
    """Turn the drift signals from the latest run into root causes."""
    con = connect()
    incidents = rca_mod.analyse(con)

    if not incidents:
        console.print("No drift signals to explain. Run: upstrace drift")
        con.close()
        return

    for i, inc in enumerate(incidents, 1):
        kind = "source" if inc.is_source else "model"
        style = SEVERITY_STYLE[inc.severity]

        console.print(
            f"\n[{style}]INCIDENT {i} - {inc.severity.upper()}[/{style}]  "
            f"root: [bold]{inc.root}[/bold] ({kind})"
        )
        console.print(f"  columns affected : {', '.join(inc.columns)}")
        console.print(
            f"  also drifted     : {', '.join(inc.blast_radius) or 'nothing downstream'}"
        )
        console.print("  evidence:")
        for e in inc.evidence:
            if e.metric in ("min_value", "max_value"):
                console.print(f"    - {e.column_name}.{e.metric} changed")
            else:
                console.print(
                    f"    - {e.column_name}.{e.metric}: "
                    f"{e.baseline:,.4f} -> {e.current:,.4f} ({e.change:.1%})"
                )

    console.print(
        f"\n{len(incidents)} root cause(s) explain "
        f"{sum(1 + len(i.blast_radius) for i in incidents)} affected node(s)."
    )
    con.close()
    
@app.command()
def explain(
    no_cache: bool = typer.Option(False, "--no-cache", help="Force a fresh model call."),
) -> None:
    """Ask a language model what the root-cause incidents actually mean."""
    con = connect()
    incidents = rca_mod.analyse(con)
    con.close()

    if not incidents:
        console.print("No incidents to explain. Run: upstrace drift")
        return

    for i, inc in enumerate(incidents, 1):
        verdict = explain_mod.explain(inc, use_cache=not no_cache)

        console.print(
            f"\n[bold]INCIDENT {i}[/bold]  root: [bold]{inc.root}[/bold]  "
            f"({inc.severity})"
        )

        # markup=False everywhere the model's own words are printed: rich reads
        # square brackets as style tags, and a model will happily emit them.
        console.print(f"\n  {verdict.get('summary', '(no summary)')}\n", markup=False)

        console.print("  Likely causes:")
        for cause in verdict.get("likely_causes", []):
            console.print(
                f"    ({cause.get('confidence', '?')}) {cause.get('cause', '')}",
                markup=False,
            )
            if cause.get("reasoning"):
                console.print(f"        {cause['reasoning']}", markup=False)

        checks = verdict.get("checks_to_add", [])
        if checks:
            console.print("\n  Checks worth adding:")
            for check in checks:
                console.print(f"    - {check}", markup=False)

        if verdict.get("who_is_affected"):
            console.print(f"\n  Impact: {verdict['who_is_affected']}", markup=False)

if __name__ == "__main__":
    app()