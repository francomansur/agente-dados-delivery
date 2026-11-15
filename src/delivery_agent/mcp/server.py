"""MCP server: another entry point to the database, beside the API, for Claude and other MCP clients.

Here the client's model writes the SQL. Both tools call the same ``SQLTools`` as the API, so every
query takes the agent's path: Pydantic checks the input, the validator checks the SQL, the
repository runs it as the read-only role and the audit log records it, with source ``mcp``. Both
tools are marked read-only, and the write action of extra EXT-01 is not exposed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from delivery_agent import __version__
from delivery_agent.core.audit import QueryContext
from delivery_agent.core.errors import DatabaseUnavailable, SQLError
from delivery_agent.services.sql_tools import SQLTools
from delivery_agent.services.sql_validator import MAX_ROWS

NAME = "delivery-data"

INSTRUCTIONS = (
    "Base fictícia de um marketplace de delivery, num PostgreSQL. Chame describe_schema antes de "
    "escrever o SQL: ela lista as tabelas e as colunas que podem ser lidas, com exemplos de valores "
    "e o glossário de métricas. Depois, rode consultas de leitura com execute_sql. Os dados pessoais "
    "dos clientes não estão disponíveis."
)

# Hints for the client, not the security barrier: that is the validator and the read-only role.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

DESCRIBE_SCHEMA = (
    "Descreve as tabelas e views que podem ser consultadas: as colunas, os tipos, o que cada uma "
    "guarda e exemplos de valores, mais o glossário de métricas (faturamento, ticket médio, taxa de "
    "atraso etc.). Chame antes de escrever o SQL."
)

EXECUTE_SQL = (
    "Executa uma consulta de leitura no PostgreSQL da base e devolve as colunas e as linhas. Aceita "
    "um único SELECT, que pode começar com WITH, sem escrita nem funções administrativas. A consulta "
    "passa por um validador, roda com um usuário somente leitura e com tempo limite, e fica "
    f"registrada na auditoria. Volta no máximo {MAX_ROWS} linhas: se truncated vier true, agregue ou "
    "filtre mais no SQL. Se a consulta for recusada ou falhar, a mensagem diz o que corrigir."
)


class QueryRows(BaseModel):
    """What ``execute_sql`` returns to the client: the columns and at most 100 rows."""

    # The schema goes to the client's model, so its description is in Portuguese, like the fields'.
    model_config = ConfigDict(json_schema_extra={"description": "Colunas e linhas que a consulta devolveu."})

    columns: list[str] = Field(description="Nomes das colunas, na ordem do SELECT.")
    rows: list[list[str | int | float | bool | None]] = Field(
        description=f"Linhas do resultado, no máximo {MAX_ROWS}. Datas e horários vêm como texto ISO."
    )
    truncated: bool = Field(description="true quando a consulta tinha mais linhas do que o limite.")


def create_server(tools: SQLTools) -> MCPServer:
    """Create the MCP server with the two read-only tools over the SQL tools.

    The SQL errors become a ``ToolError``, whose message, in Portuguese, tells the client's model
    what to fix. Any other exception reaches the client without its details.

    Args:
        tools: The SQL tools that validate, run and log every query: the same class as the API's.

    Returns:
        The server, ready to run on stdio. The tests connect a client to it in memory.
    """
    server = MCPServer(NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        title="Descrever a base",
        description=DESCRIBE_SCHEMA,
        annotations=READ_ONLY,
        structured_output=False,
    )
    def describe_schema() -> str:
        try:
            return tools.describe_schema()
        except DatabaseUnavailable as error:
            raise ToolError(str(error)) from error

    @server.tool(title="Executar SQL de leitura", description=EXECUTE_SQL, annotations=READ_ONLY)
    def execute_sql(
        sql: Annotated[str, Field(description="Uma única consulta SELECT no dialeto do Postgres.")],
    ) -> QueryRows:
        context = QueryContext(request_id=uuid.uuid4().hex, source="mcp")

        try:
            result = tools.execute_sql(sql, context)
        except (SQLError, DatabaseUnavailable) as error:
            raise ToolError(str(error)) from error

        return QueryRows(columns=result.columns, rows=result.rows, truncated=result.truncated)

    return server
