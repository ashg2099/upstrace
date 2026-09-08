"""The catalogue of things that can go wrong, with the right answer attached.

Each scenario is a deliberate break plus the root node and column that a correct
analysis should name. That pairing is what makes the harness an evaluation
rather than a demo: every run produces a score, not a screenshot.
"""

from dataclasses import dataclass

RAW = "raw.yellow_trips"
FCT = "main.fct_trips"

# window name -> (raw predicate, fct predicate)
WINDOWS = {
    "month": (
        "tpep_pickup_datetime >= '2024-03-01'",
        "pickup_at >= '2024-03-01'",
    ),
    "week": (
        "tpep_pickup_datetime >= '2024-03-01' and tpep_pickup_datetime < '2024-03-08'",
        "pickup_at >= '2024-03-01' and pickup_at < '2024-03-08'",
    ),
    "day": (
        "tpep_pickup_datetime >= '2024-03-01' and tpep_pickup_datetime < '2024-03-02'",
        "pickup_at >= '2024-03-01' and pickup_at < '2024-03-02'",
    ),
}


@dataclass
class Scenario:
    id: str
    layer: str                  # "source" or "model"
    description: str
    sql: str
    expected_root: str | None
    expected_columns: list[str]
    magnitude: str
    window: str
    seed: int = 42


def _pct(pct: int, ts_col: str) -> str:
    """Deterministic row sample: the same rows every run."""
    return f"hash({ts_col}) % 100 < {pct}"


SCALED = {
    "unit-change": ("trip_distance", {"large": 1.609, "medium": 1.25, "small": 1.05}),
    "fare-inflation": ("fare_amount", {"large": 1.5, "medium": 1.2, "small": 1.03}),
}

SPIKED = {
    "null-spike": ("passenger_count", "null", {"large": 100, "medium": 50, "small": 10}),
    "enum-drift": ("payment_type", "7", {"large": 100, "medium": 50, "small": 10}),
}


def _source_scenarios() -> list[Scenario]:
    out: list[Scenario] = []

    for window, (raw_where, _) in WINDOWS.items():
        for name, (column, factors) in SCALED.items():
            for magnitude, factor in factors.items():
                out.append(Scenario(
                    id=f"{name}-{magnitude}-{window}",
                    layer="source",
                    description=f"{column} multiplied by {factor} over one {window}",
                    sql=f"update {RAW} set {column} = {column} * {factor} where {raw_where}",
                    expected_root="yellow_trips",
                    expected_columns=[column],
                    magnitude=magnitude,
                    window=window,
                ))

        for name, (column, value, pcts) in SPIKED.items():
            for magnitude, pct in pcts.items():
                where = raw_where
                if pct < 100:
                    where += f" and {_pct(pct, 'tpep_pickup_datetime')}"
                out.append(Scenario(
                    id=f"{name}-{magnitude}-{window}",
                    layer="source",
                    description=f"{column} set to {value} for {pct}% of one {window}",
                    sql=f"update {RAW} set {column} = {value} where {where}",
                    expected_root="yellow_trips",
                    expected_columns=[column],
                    magnitude=magnitude,
                    window=window,
                ))

        for magnitude, pct in {"large": 50, "medium": 20, "small": 5}.items():
            out.append(Scenario(
                id=f"row-drop-{magnitude}-{window}",
                layer="source",
                description=f"{pct}% of rows in one {window} never arrived",
                sql=(f"delete from {RAW} where {raw_where} "
                     f"and {_pct(pct, 'tpep_pickup_datetime')}"),
                expected_root="yellow_trips",
                expected_columns=[],          # row loss is not one column's fault
                magnitude=magnitude,
                window=window,
            ))

        out.append(Scenario(
            id=f"case-change-large-{window}",
            layer="source",
            description=f"store_and_fwd_flag arrives lowercase for one {window}",
            sql=(f"update {RAW} set store_and_fwd_flag = lower(store_and_fwd_flag) "
                 f"where {raw_where}"),
            expected_root="yellow_trips",
            expected_columns=["store_and_fwd_flag"],
            magnitude="large",
            window=window,
        ))

        out.append(Scenario(
            id=f"precision-loss-medium-{window}",
            layer="source",
            description=f"trip_distance rounded to whole numbers for one {window}",
            sql=(f"update {RAW} set trip_distance = round(trip_distance, 0) "
                 f"where {raw_where}"),
            expected_root="yellow_trips",
            expected_columns=["trip_distance"],
            magnitude="medium",
            window=window,
        ))

    return out


def _model_scenarios() -> list[Scenario]:
    out: list[Scenario] = []
    _, fct_where = WINDOWS["month"]

    out.append(Scenario(
        id="flag-logic-break-large-month",
        layer="model",
        description="a change to fct_trips marks every trip clean",
        sql=f"update {FCT} set is_clean = true",
        expected_root="fct_trips",
        expected_columns=["is_clean"],
        magnitude="large",
        window="all",
    ))

    for magnitude, factor in {"large": 1.5, "medium": 1.2, "small": 1.05}.items():
        out.append(Scenario(
            id=f"mart-scaling-{magnitude}-month",
            layer="model",
            description=f"fct_trips inflates total_amount by {factor}, source is fine",
            sql=f"update {FCT} set total_amount = total_amount * {factor} where {fct_where}",
            expected_root="fct_trips",
            expected_columns=["total_amount"],
            magnitude=magnitude,
            window="month",
        ))

    out.append(Scenario(
        id="mart-null-large-month",
        layer="model",
        description="fct_trips drops passenger_count, source still has it",
        sql=f"update {FCT} set passenger_count = null where {fct_where}",
        expected_root="fct_trips",
        expected_columns=["passenger_count"],
        magnitude="large",
        window="month",
    ))

    return out

def _control_scenarios() -> list[Scenario]:
    """Changes that must not raise an alert.

    Two kinds:
      - null: nothing changed at all
      - sub-threshold: a real change, deliberately smaller than the configured
        thresholds (5% on means, 2% on row counts)

    The second kind is the one that matters. It asks whether the tool respects
    the thresholds it declares, rather than firing on any movement at all.

    An earlier version resampled the data with a different seed and called that
    a control. That was wrong: a fresh reservoir sample is a different dataset,
    not the same dataset a day later, and alerting on it is correct behaviour.
    """
    controls = [
        Scenario(
            id="control-null",
            layer="control",
            description="nothing was changed at all",
            sql="",
            expected_root=None,
            expected_columns=[],
            magnitude="none",
            window="none",
        )
    ]

    for name, column, factor in (
        ("tip", "tip_amount", 1.03),
        ("extra", "extra", 1.02),
        ("tip-near-limit", "tip_amount", 1.045),
    ):
        controls.append(Scenario(
            id=f"control-subthreshold-{name}",
            layer="control",
            description=(
                f"{column} up {(factor - 1) * 100:.1f}%, below the 5% mean threshold"
            ),
            sql=(f"update {RAW} set {column} = round({column} * {factor}, 2)"),
            expected_root=None,
            expected_columns=[],
            magnitude="none",
            window="none",
        ))

    return controls

def all_scenarios() -> list[Scenario]:
    return _source_scenarios() + _model_scenarios() + _control_scenarios()