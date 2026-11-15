"""The graph's tools over the database: describe the schema and run model-written SQL.

Every query follows the same path: Pydantic checks the input, the validator checks the SQL
(security layer 3), the repository runs it as the read-only role (layers 1 and 2) and the audit log
records the outcome, approved or rejected (layer 4).
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field, ValidationError

from delivery_agent.core.audit import AuditLog, AuditRecord, AuditStatus, QueryContext
from delivery_agent.core.errors import DatabaseUnavailable, QueryTimeout, SQLError, SQLRejected
from delivery_agent.repositories.delivery_repository import Column, DeliveryRepository, QueryResult
from delivery_agent.services.sql_validator import MAX_ROWS, validate_sql

MAX_SQL_LENGTH = 10_000

# Text columns with up to 15 distinct values list them all; the others show 3 examples.
MAX_LISTED_VALUES = 15
EXAMPLES = 3

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


class SQLInput(BaseModel):
    """Input of the ``execute_sql`` tool."""

    sql: str = Field(
        min_length=1,
        max_length=MAX_SQL_LENGTH,
        description="Uma única consulta SELECT no dialeto do Postgres.",
    )


class SQLTools:
    """What the graph uses to read the database: the schema description, validation and execution."""

    def __init__(self, repository: DeliveryRepository, audit_log: AuditLog) -> None:
        self._repository = repository
        self._audit_log = audit_log
        self._schema_description: str | None = None

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
            result = self._repository.select(validated_sql, max_rows=MAX_ROWS)
        except (SQLError, DatabaseUnavailable) as error:
            self._log(context, validated_sql, _audit_status(error), started, row_count=None, error=str(error))

            raise

        self._log(context, result.sql, "ok", started, row_count=len(result.rows), error=None)

        return result

    def _build_schema_description(self) -> str:
        parts = [
            "# Base de um marketplace de delivery (dados fictícios)",
            "",
            "Banco PostgreSQL: escreva as consultas no dialeto do Postgres. "
            "Estas são as tabelas e views que você pode consultar.",
        ]

        for relation in self._repository.list_relations():
            parts += [
                "",
                f"## {relation.name} ({'view' if relation.is_view else 'tabela'})",
                relation.comment,
            ]

            for column in relation.columns:
                details = self._column_details(relation.name, column)
                parts.append(f"- {column.name} ({column.data_type}): {column.comment}{details}")

        parts += ["", *GLOSSARY_LINES]

        return "\n".join(parts) + "\n"

    def _column_details(self, relation: str, column: Column) -> str:
        """Sample values of one column: every value of short text lists, a few examples, or the date range."""
        if column.category == "S":
            found = self._repository.distinct_values(relation, column.name, limit=MAX_LISTED_VALUES + 1)
            values = [_quote(value) for value in found]

            if len(values) <= MAX_LISTED_VALUES:
                return f" Valores: {', '.join(values)}."

            return f" Exemplos: {', '.join(values[:EXAMPLES])}."

        if column.category == "D":
            first, last = self._repository.value_range(relation, column.name)

            return f" De {first} a {last}."

        return ""

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


def _parse_input(sql_text: str) -> SQLInput:
    try:
        return SQLInput(sql=sql_text)
    except ValidationError as error:
        limit = f"{MAX_SQL_LENGTH:,}".replace(",", ".")

        raise SQLRejected(
            f"A consulta precisa ter entre 1 e {limit} caracteres. Escreva uma consulta mais curta."
        ) from error


def _audit_status(error: Exception) -> AuditStatus:
    if isinstance(error, SQLRejected):
        return "rejected"

    if isinstance(error, QueryTimeout):
        return "timeout"

    return "error"


def _quote(value: str) -> str:
    escaped = value.replace("'", "''")

    return f"'{escaped}'"
