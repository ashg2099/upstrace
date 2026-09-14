"""SQL dialects.

Everything that differs between warehouses lives in this file and nowhere else.
The rest of the engine talks to a Dialect; it never touches a driver directly.

The contract a dialect must honour is DuckDB's, not its own driver's:
execute() returns something you can call fetchone()/fetchall() on, and
parameters are qmark style. Adapting one driver to that contract is a small
class. Adapting a hundred call sites in the engine to a second driver's
contract is not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol

import pandas as pd


class Cursor(Protocol):
    """Whatever execute() hands back must support these two."""

    def fetchone(self) -> tuple | None: ...
    def fetchall(self) -> list[tuple]: ...


class Connection(Protocol):
    def execute(self, sql: str, params: list | None = None) -> Cursor: ...
    def close(self) -> None: ...


class Dialect(ABC):
    """One warehouse's worth of difference."""

    name: str
    double_type: str = "DOUBLE"

    # --- connection -----------------------------------------------------

    @abstractmethod
    def connect(self, read_only: bool = False) -> Connection: ...

    # --- introspection --------------------------------------------------

    @abstractmethod
    def list_columns(self, con: Connection, relation: str) -> list[tuple[str, str]]:
        """[(column_name, data_type)] for a schema-qualified relation."""

    @abstractmethod
    def is_numeric(self, data_type: str) -> bool: ...

    @abstractmethod
    def is_float(self, data_type: str) -> bool: ...

    # --- expressions ----------------------------------------------------

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def cast_date(self, col: str) -> str:
        return f"cast({col} as date)"

    @abstractmethod
    def distinct_expr(self, col: str, data_type: str) -> str: ...

    @abstractmethod
    def bound_expr(self, fn: str, col: str, data_type: str) -> str: ...

    @abstractmethod
    def mean_expr(self, col: str, data_type: str) -> str: ...

    # --- writes ---------------------------------------------------------

    @abstractmethod
    def insert_frame(self, con: Connection, table: str, frame: pd.DataFrame) -> None:
        """Bulk-insert a DataFrame whose columns match the table, in order."""


class DuckDBDialect(Dialect):
    name = "duckdb"
    double_type = "DOUBLE"

    NUMERIC_TYPES = {
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
        "FLOAT", "DOUBLE", "DECIMAL", "REAL",
    }
    FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL", "DECIMAL"}

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self, read_only: bool = False):
        import duckdb

        if not self.path.exists():
            raise SystemExit(
                f"No warehouse at {self.path}.\n"
                "Check the 'warehouse:' path in upstrace.yml, then build it with dbt.\n"
                "For this repo's demo: python scripts/load_duckdb.py"
            )
        return duckdb.connect(str(self.path), read_only=read_only)

    def list_columns(self, con, relation: str) -> list[tuple[str, str]]:
        return [
            (name, dtype)
            for name, dtype, *_ in con.execute(f"DESCRIBE {relation}").fetchall()
        ]

    def _base(self, data_type: str) -> str:
        # DECIMAL(18,3) -> DECIMAL
        return data_type.upper().split("(")[0].strip()

    def is_numeric(self, data_type: str) -> bool:
        return self._base(data_type) in self.NUMERIC_TYPES

    def is_float(self, data_type: str) -> bool:
        return self._base(data_type) in self.FLOAT_TYPES

    def distinct_expr(self, col: str, data_type: str) -> str:
        if self.is_float(data_type):
            return f"count(distinct round({col}, 6))"
        return f"count(distinct {col})"

    def bound_expr(self, fn: str, col: str, data_type: str) -> str:
        if self.is_float(data_type):
            return f"round({fn}({col}), 6)::varchar"
        return f"{fn}({col})::varchar"

    def mean_expr(self, col: str, data_type: str) -> str:
        if self.is_numeric(data_type):
            return f"avg({col})::double"
        return "cast(null as double)"

    def insert_frame(self, con, table: str, frame: pd.DataFrame) -> None:
        # DuckDB's replacement scan resolves the local name `frame` to the
        # DataFrame in this scope, so the data never crosses a parameter
        # boundary. Measured on 1,729 rows: 8.2s parameterised, 0.01s this way.
        # Renaming the local variable breaks it.
        con.execute(f"INSERT INTO {table} SELECT * FROM frame")


def get_dialect(settings=None) -> Dialect:
    from .settings import get_settings

    settings = settings or get_settings()
    name = getattr(settings, "dialect", "duckdb")

    if name == "duckdb":
        return DuckDBDialect(settings.warehouse)

    raise SystemExit(f"Unknown dialect {name!r}. Supported: duckdb.")