"""Read-only access to Postgres for the agent: run model-written SQL and describe the schema.

Every query runs as ``agent_reader`` (security layers 1 and 2) after the validator (layer 3),
and every execution, approved or rejected, goes to the audit log (layer 4).
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field, ValidationError

from delivery_agent.core.audit import AuditLog, AuditRecord, AuditStatus, QueryContext
from delivery_agent.core.errors import (
    DatabaseUnavailable,
    ExecutionError,
    QueryTimeout,
    SQLError,
    SQLRejected,
)
from delivery_agent.core.settings import Settings
from delivery_agent.services.sql_validator import MAX_ROWS, validate_sql

MAX_SQL_LENGTH = 10_000

Cell = str | int | float | bool | None

GLOSSARY_LINES = (
    "## Glossário de métricas",
    "",
    "- **Pedidos**, sem qualificação: todos os pedidos, inclusive os cancelados. "
    "**Pedidos entregues:** `status = 'delivered'`.",
    "- **Faturamento:** soma de `orders.total` dos pedidos entregues.",
    "- **Ticket médio:** média de `orders.total` dos pedidos entregues.",
    "- **Taxa de cancelamento:** pedidos cancelados divididos por todos os pedidos.",
    "- **Pedido atrasado:** pedido entregue com `delivery_minutes > promised_minutes`. "
    "**Taxa de atraso:** atrasados divididos pelos entregues.",
    "- **Tempo de entrega:** `orders.delivery_minutes`, só de pedidos entregues.",
    "- **Itens vendidos:** soma de `order_items.quantity` dos pedidos entregues.",
    "- **Cidade de um pedido:** a cidade do restaurante.",
    "- **Dia da semana:** `EXTRACT(DOW FROM created_at)`, em que 0 é domingo e 6 é sábado.",
)

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


class SQLInput(BaseModel):
    """Input of the ``execute_sql`` tool."""

    sql: str = Field(
        min_length=1,
        max_length=MAX_SQL_LENGTH,
        description="Uma única consulta SELECT no dialeto do Postgres.",
    )


class QueryResult(BaseModel):
    """Result of one query, ready to show to people or to send to the model."""

    sql: str
    columns: list[str]
    rows: list[list[Cell]]
    truncated: bool
    duration_ms: float


class Database:
    """Pool of read-only connections used by the agent."""

    def __init__(self, settings: Settings, audit_log: AuditLog) -> None:
        self._settings = settings
        self._audit_log = audit_log
        self._pool: ConnectionPool[psycopg.Connection[TupleRow]] = ConnectionPool(
            settings.agent_conninfo(),
            min_size=1,
            max_size=4,
            open=False,
            configure=_configure,
            timeout=10,
        )
        self._schema_description: str | None = None

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

    def __enter__(self) -> Database:
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

    def validate(self, sql_text: str, context: QueryContext) -> str:
        """Run the input checks and the validator, logging any rejection.

        Args:
            sql_text: SQL written by the model.
            context: Request that asked for the query, for the audit log.

        Returns:
            The SQL to run, with ``LIMIT 101`` when the outer query has no limit.

        Raises:
            SQLRejected: The input or the validator blocked the query.
        """
        started = time.monotonic()

        try:
            return validate_sql(_parse_input(sql_text).sql)
        except SQLRejected as error:
            self._log(context, sql_text, "rejected", started, row_count=None, error=str(error))

            raise

    def execute_sql(self, sql_text: str, context: QueryContext) -> QueryResult:
        """Validate and run one model-written query as the read-only role.

        Args:
            sql_text: SQL written by the model.
            context: Request that asked for the query, for the audit log.

        Returns:
            Columns, at most 100 rows, whether there were more rows, and how long the query took.

        Raises:
            SQLRejected: The input or the validator blocked the query.
            QueryTimeout: The query ran past the time limit.
            ExecutionError: Postgres refused or failed to run the query.
            DatabaseUnavailable: The database cannot be reached.
        """
        validated_sql = self.validate(sql_text, context)
        started = time.monotonic()

        try:
            result = self._run(validated_sql)
        except (SQLError, DatabaseUnavailable) as error:
            self._log(context, validated_sql, _audit_status(error), started, row_count=None, error=str(error))

            raise

        self._log(context, result.sql, "ok", started, row_count=len(result.rows), error=None)

        return result

    def describe_schema(self) -> str:
        """Describe what the agent can read, for the system prompt of the model.

        The text lists tables and views, columns, types, comments and sample values, and ends
        with the metrics glossary. It is built once per process and is deterministic: the same
        database always gives the same text.

        Returns:
            Text in Portuguese, with the identifiers in English.

        Raises:
            DatabaseUnavailable: The database cannot be reached.
        """
        if self._schema_description is None:
            self._schema_description = self._build_schema_description()

        return self._schema_description

    def _run(self, validated_sql: str) -> QueryResult:
        timeout = sql.SQL("SET LOCAL statement_timeout = {}").format(
            sql.Literal(f"{self._settings.statement_timeout_ms}ms")
        )
        started = time.monotonic()

        try:
            with self._pool.connection() as conn, conn.transaction(force_rollback=True):
                conn.execute(timeout)
                columns, rows, truncated = fetch_rows(conn, validated_sql, MAX_ROWS)
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
            sql=validated_sql, columns=columns, rows=rows, truncated=truncated, duration_ms=duration_ms
        )

    def _build_schema_description(self) -> str:
        parts = [
            "# Base de um marketplace de delivery (dados fictícios)",
            "",
            "Banco PostgreSQL: escreva as consultas no dialeto do Postgres. "
            "Estas são as tabelas e views que você pode consultar.",
        ]

        try:
            with self._pool.connection() as conn, conn.transaction(force_rollback=True):
                for oid, name, kind, comment in conn.execute(_RELATIONS_QUERY).fetchall():
                    parts += ["", f"## {name} ({'view' if kind == 'v' else 'tabela'})", comment]

                    for column, data_type, column_comment, category in conn.execute(
                        _COLUMNS_QUERY, (oid,)
                    ).fetchall():
                        details = _column_details(conn, name, column, category)
                        parts.append(f"- {column} ({data_type}): {column_comment}{details}")
        except (psycopg.Error, PoolTimeout) as error:
            raise DatabaseUnavailable(f"Não consegui ler o schema do banco: {error}") from error

        parts += ["", *GLOSSARY_LINES]

        return "\n".join(parts) + "\n"

    def _log(
        self,
        context: QueryContext,
        sql_text: str,
        status: AuditStatus,
        started: float,
        *,
        row_count: int | None,
        error: str | None,
    ) -> None:
        self._audit_log.record(
            AuditRecord(
                request_id=context.request_id,
                source=context.source,
                question=context.question,
                sql=sql_text,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                row_count=row_count,
                error=error,
            )
        )


def fetch_rows(
    conn: psycopg.Connection[Any], query: str, max_rows: int
) -> tuple[list[str], list[list[Cell]], bool]:
    """Run one statement and fetch at most ``max_rows`` rows.

    ``binary=True`` makes psycopg use the extended query protocol, in which Postgres refuses more
    than one statement per call: a second barrier behind the validator.

    Args:
        conn: Open connection, inside a transaction.
        query: SQL already approved by the validator.
        max_rows: Maximum number of rows to return.

    Returns:
        Column names, rows with JSON-friendly values, and whether there were more rows.

    Raises:
        psycopg.Error: Postgres refused or failed to run the statement.
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


def _parse_input(sql_text: str) -> SQLInput:
    try:
        return SQLInput(sql=sql_text)
    except ValidationError as error:
        limit = f"{MAX_SQL_LENGTH:,}".replace(",", ".")

        raise SQLRejected(
            f"A consulta precisa ter entre 1 e {limit} caracteres. Escreva uma consulta mais curta."
        ) from error


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


def _audit_status(error: Exception) -> AuditStatus:
    if isinstance(error, SQLRejected):
        return "rejected"

    if isinstance(error, QueryTimeout):
        return "timeout"

    return "error"


def _column_details(conn: psycopg.Connection[TupleRow], table: str, column: str, category: str) -> str:
    """Sample values of one column: every value of short text lists, a few examples, or the date range."""
    table_id, column_id = sql.Identifier(table), sql.Identifier(column)

    if category == "S":
        query = sql.SQL("SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL ORDER BY 1 LIMIT 16").format(
            c=column_id, t=table_id
        )
        values = [_quote(str(row[0])) for row in conn.execute(query).fetchall()]

        if len(values) <= 15:
            return f" Valores: {', '.join(values)}."

        return f" Exemplos: {', '.join(values[:3])}."

    if category == "D":
        query = sql.SQL("SELECT MIN({c}), MAX({c}) FROM {t}").format(c=column_id, t=table_id)
        first, last = conn.execute(query).fetchone() or (None, None)

        return f" De {_to_cell(first)} a {_to_cell(last)}."

    return ""


def _quote(value: str) -> str:
    escaped = value.replace("'", "''")

    return f"'{escaped}'"
