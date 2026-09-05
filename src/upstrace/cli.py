"""The upstrace command line."""

import typer
from rich.console import Console
from rich.table import Table

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


if __name__ == "__main__":
    app()