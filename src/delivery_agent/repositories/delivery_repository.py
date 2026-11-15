"""Read-only access to the delivery database: the only module that runs SQL on Postgres.

The connections use ``agent_reader``, the read-only role (security layers 1 and 2), and each
transaction is read-only and has its own time limit. The repository runs the SQL it receives as it
is: checking the model's SQL and logging it are jobs of ``services.sql_tools``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel

from delivery_agent.core.errors import DatabaseUnavailable, ExecutionError, QueryTimeout
from delivery_agent.core.settings import Settings

Cell = str | int | float | bool | None

_RELATIONS_QUERY = """
    SELECT c.oid, c.relname, c.relkind, COALESCE(obj_description(c.oid, 'pg_class'), '')
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v') AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relname
"""

_COLUMNS_QUERY = """
    SELECT a.attname,
           format_type(a.atttypid, a.atttypmod),
           COALESCE(col_description(a.attrelid, a.attnum), ''),
           t.typcategory
    FROM pg_attribute a
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""


class QueryResult(BaseModel):
    """Result of one query, ready to show to people or to send to the model."""

    sql: str
    columns: list[str]
    rows: list[list[Cell]]
    truncated: bool
    duration_ms: float


@dataclass(frozen=True)
class Column:
    """One column of a table or view, as the Postgres catalog describes it."""

    name: str
    data_type: str
    comment: str
    category: str  # Postgres type category: S for text, D for dates and times, N for numbers


@dataclass(frozen=True)
class Relation:
    """A table or view that the read-only role can read."""

    name: str
    is_view: bool
    comment: str
    columns: list[Column]


class DeliveryRepository:
    """Pool of read-only connections to the delivery database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: ConnectionPool[psycopg.Connection[TupleRow]] = ConnectionPool(
            settings.agent_conninfo(),
            min_size=1,
            max_size=4,
            open=False,
            configure=_configure,
            timeout=10,
        )

    def open(self, *, wait: bool = True) -> None:
        """Open the connection pool.

        Args:
            wait: Wait for the first connection. The API passes ``False`` so it can start, and
                report the problem on ``/health``, while the database is down.

        Raises:
            DatabaseUnavailable: The database did not answer in time.
        """
        try:
            self._pool.open(wait=wait, timeout=10)
        except PoolTimeout as error:
            raise DatabaseUnavailable(f"O banco de dados não respondeu: {error}") from error

    def close(self) -> None:
        """Close every connection of the pool."""
        self._pool.close()

    def __enter__(self) -> DeliveryRepository:
        self.open()

        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def ping(self) -> bool:
        """Check that the database answers a trivial query.

        Returns:
            Whether the database answered.
        """
        try:
            with self._pool.connection(timeout=2) as conn, conn.transaction(force_rollback=True):
                conn.execute("SELECT 1")
        except (psycopg.Error, PoolTimeout):
            return False

        return True

    def select(self, query: str, *, max_rows: int) -> QueryResult:
        """Run one read-only query and fetch at most ``max_rows`` rows.

        The query runs as it is: the caller checks it first. Even so, the transaction is read-only,
        has a time limit, and Postgres accepts a single statement per call.

        Args:
            query: One SQL statement.
            max_rows: Maximum number of rows to return.

        Returns:
            The query, its columns, at most ``max_rows`` rows, whether there were more rows, and how
            long it took.

        Raises:
            QueryTimeout: The query ran past the time limit.
            ExecutionError: Postgres refused or failed to run the query.
            DatabaseUnavailable: The database cannot be reached.
        """
        timeout = sql.SQL("SET LOCAL statement_timeout = {}").format(
            sql.Literal(f"{self._settings.statement_timeout_ms}ms")
        )
        started = time.monotonic()

        try:
            with self._pool.connection() as conn, conn.transaction(force_rollback=True):
                conn.execute(timeout)
                columns, rows, truncated = _fetch_rows(conn, query, max_rows)
        except psycopg.errors.QueryCanceled as error:
            seconds = f"{self._settings.statement_timeout_ms / 1000:g}".replace(".", ",")

            raise QueryTimeout(
                f"A consulta passou de {seconds} s e foi cancelada. "
                "Simplifique a consulta ou filtre mais os dados."
            ) from error
        except (psycopg.OperationalError, PoolTimeout) as error:
            raise DatabaseUnavailable(f"O banco de dados não respondeu: {error}") from error
        except psycopg.Error as error:
            raise ExecutionError(f"Erro do Postgres: {_postgres_message(error)}") from error

        duration_ms = round((time.monotonic() - started) * 1000, 1)

        return QueryResult(
            sql=query, columns=columns, rows=rows, truncated=truncated, duration_ms=duration_ms
        )

    def list_relations(self) -> list[Relation]:
        """List the tables and views that the read-only role can read, with their columns.

        Returns:
            The relations in alphabetical order, each with its comment and its columns in table order.

        Raises:
            DatabaseUnavailable: The database cannot be reached.
        """
        relations: list[Relation] = []

        try:
            with self._pool.connection() as conn, conn.transaction(force_rollback=True):
                for oid, name, kind, comment in conn.execute(_RELATIONS_QUERY).fetchall():
                    columns = [Column(*row) for row in conn.execute(_COLUMNS_QUERY, (oid,)).fetchall()]
                    relations.append(
                        Relation(name=name, is_view=kind == "v", comment=comment, columns=columns)
                    )
        except (psycopg.Error, PoolTimeout) as error:
            raise _schema_unavailable(error) from error

        return relations

    def distinct_values(self, relation: str, column: str, *, limit: int) -> list[str]:
        """Read the first distinct values of a column, in order and without nulls.

        Args:
            relation: Table or view.
            column: Column of that relation.
            limit: Maximum number of values.

        Returns:
            The values, as text.

        Raises:
            DatabaseUnavailable: The database cannot be reached.
        """
        query = sql.SQL("SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL ORDER BY 1 LIMIT {n}").format(
            c=sql.Identifier(column), t=sql.Identifier(relation), n=sql.Literal(limit)
        )

        return [str(row[0]) for row in self._read_catalog(query)]

    def value_range(self, relation: str, column: str) -> tuple[Cell, Cell]:
        """Read the smallest and the largest value of a column.

        Args:
            relation: Table or view.
            column: Column of that relation.

        Returns:
            The minimum and the maximum, with dates as ISO text.

        Raises:
            DatabaseUnavailable: The database cannot be reached.
        """
        query = sql.SQL("SELECT MIN({c}), MAX({c}) FROM {t}").format(
            c=sql.Identifier(column), t=sql.Identifier(relation)
        )
        first, last = self._read_catalog(query)[0]

        return _to_cell(first), _to_cell(last)

    def _read_catalog(self, query: sql.Composed) -> list[TupleRow]:
        try:
            with self._pool.connection() as conn, conn.transaction(force_rollback=True):
                return conn.execute(query).fetchall()
        except (psycopg.Error, PoolTimeout) as error:
            raise _schema_unavailable(error) from error


def _fetch_rows(
    conn: psycopg.Connection[Any], query: str, max_rows: int
) -> tuple[list[str], list[list[Cell]], bool]:
    """Run one statement and fetch at most ``max_rows`` rows, plus one to know whether there were more.

    ``binary=True`` makes psycopg use the extended query protocol, in which Postgres refuses more
    than one statement per call: a second barrier behind the validator.
    """
    with conn.cursor() as cursor:
        # The SQL comes from the model by design: it was validated and runs read-only with minimal privileges.
        cursor.execute(query, binary=True)
        columns = [column.name for column in cursor.description or []]
        fetched = cursor.fetchmany(max_rows + 1)

    rows = [[_to_cell(value) for value in row] for row in fetched[:max_rows]]

    return columns, rows, len(fetched) > max_rows


def _configure(conn: psycopg.Connection[TupleRow]) -> None:
    conn.read_only = True


def _to_cell(value: object) -> Cell:
    if value is None or isinstance(value, str | bool | int | float):
        return value

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return str(value)


def _postgres_message(error: psycopg.Error) -> str:
    message = error.diag.message_primary or str(error).strip()

    if error.diag.message_hint:
        message += f" Dica do Postgres: {error.diag.message_hint}"

    return message


def _schema_unavailable(error: Exception) -> DatabaseUnavailable:
    return DatabaseUnavailable(f"Não consegui ler o schema do banco: {error}")
