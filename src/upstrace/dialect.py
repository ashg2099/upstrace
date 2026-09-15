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
import os


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

def _qmark_to_pyformat(sql: str) -> str:
    """DuckDB's ? placeholders into psycopg's %s, ignoring string literals.

    The engine writes qmark SQL because that is what DuckDB takes. Rather than
    rewrite every call site for a second paramstyle, translate here.
    """
    out = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class _PgConnection:
    """Makes a psycopg connection behave like a DuckDB one.

    psycopg 3's execute() already returns a cursor you can fetch from, which is
    most of the contract. The gap is the parameter style, closed above. Anything
    else - cursor(), commit(), close() - falls through to the real connection.
    """

    def __init__(self, raw) -> None:
        self._raw = raw

    def execute(self, sql: str, params=None):
        if params is None:
            return self._raw.execute(sql)
        return self._raw.execute(_qmark_to_pyformat(sql), params)

    def close(self) -> None:
        self._raw.close()

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _split_relation(relation: str) -> tuple[str | None, str]:
    """(schema, table) from whatever the manifest called the relation.

    dbt writes anything from `table` to `"db"."schema"."table"` depending on
    adapter and quoting config. Take the last two parts and drop the quotes.
    """
    parts = [p.strip('"') for p in relation.split(".")]
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


class PostgresDialect(Dialect):
    name = "postgres"
    double_type = "DOUBLE PRECISION"

    NUMERIC_TYPES = {
        "smallint", "integer", "bigint",
        "decimal", "numeric", "real", "double precision",
    }
    FLOAT_TYPES = {"real", "double precision", "numeric", "decimal"}

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self, read_only: bool = False):
        import psycopg

        # autocommit because the engine never opens a transaction of its own -
        # it was written against DuckDB, where every execute() is committed.
        raw = psycopg.connect(self.dsn, autocommit=True)
        if read_only:
            raw.execute("SET default_transaction_read_only = on")
        return _PgConnection(raw)

    def list_columns(self, con, relation: str) -> list[tuple[str, str]]:
        schema, table = _split_relation(relation)
        if schema is None:
            rows = con.execute(
                """
                select column_name, data_type
                from information_schema.columns
                where table_name = ?
                order by ordinal_position
                """,
                [table],
            ).fetchall()
        else:
            rows = con.execute(
                """
                select column_name, data_type
                from information_schema.columns
                where table_schema = ? and table_name = ?
                order by ordinal_position
                """,
                [schema, table],
            ).fetchall()
        return [(name, dtype) for name, dtype in rows]

    def _base(self, data_type: str) -> str:
        return data_type.lower().split("(")[0].strip()

    def is_numeric(self, data_type: str) -> bool:
        return self._base(data_type) in self.NUMERIC_TYPES

    def is_float(self, data_type: str) -> bool:
        return self._base(data_type) in self.FLOAT_TYPES

    def distinct_expr(self, col: str, data_type: str) -> str:
        # Postgres has no round(double precision, int) - only round(numeric, int).
        if self.is_float(data_type):
            return f"count(distinct round({col}::numeric, 6))"
        return f"count(distinct {col})"

    def bound_expr(self, fn: str, col: str, data_type: str) -> str:
        if self.is_float(data_type):
            return f"round({fn}({col})::numeric, 6)::varchar"
        return f"{fn}({col})::varchar"

    def mean_expr(self, col: str, data_type: str) -> str:
        if self.is_numeric(data_type):
            return f"avg({col})::double precision"
        return "cast(null as double precision)"

    def insert_frame(self, con, table: str, frame: pd.DataFrame) -> None:
        # COPY is Postgres's bulk path, and the reason this is a dialect method
        # at all: DuckDB reads the DataFrame straight out of local scope, which
        # has no equivalent here.
        columns = ", ".join(self.quote(c) for c in frame.columns)
        clean = frame.astype(object).where(pd.notnull(frame), None)
        with con.cursor().copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            for row in clean.itertuples(index=False, name=None):
                copy.write_row(row)
                
def get_dialect(settings=None) -> Dialect:
    from .settings import get_settings

    settings = settings or get_settings()
    name = (os.getenv("UPSTRACE_DIALECT") or getattr(settings, "dialect", "duckdb")).lower()

    if name == "duckdb":
        return DuckDBDialect(settings.warehouse)

    if name == "postgres":
        dsn = os.getenv("UPSTRACE_DSN") or str(settings.warehouse)
        return PostgresDialect(dsn)

    raise SystemExit(f"Unknown dialect {name!r}. Supported: duckdb, postgres.")