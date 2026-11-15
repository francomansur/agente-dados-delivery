"""The MCP server: two read-only tools over the same SQL tools as the API."""

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent

from delivery_agent.assembly import open_sql_tools
from delivery_agent.core.audit import AuditLog
from delivery_agent.core.errors import DatabaseUnavailable, ExecutionError, QueryTimeout
from delivery_agent.core.settings import Settings
from delivery_agent.mcp.__main__ import main
from delivery_agent.mcp.server import create_server
from delivery_agent.services.sql_tools import SQLTools
from fakes import FakeRepository, FakeSQLTools

PROJECT = Path(__file__).parent.parent
COUNT_SQL = "SELECT count(*) AS n FROM orders"
WRITES = [
    "DELETE FROM orders",
    "UPDATE orders SET total = 0",
    "DROP TABLE reviews",
    "WITH gone AS (DELETE FROM orders RETURNING id) SELECT count(*) FROM gone",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def tools_with(repository: FakeRepository, tmp_path: Path) -> tuple[SQLTools, Path]:
    log_path = tmp_path / "audit.jsonl"

    return SQLTools(repository, AuditLog(log_path)), log_path  # type: ignore[arg-type]


def read_log(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def text_of(result: CallToolResult) -> str:
    [block] = result.content
    assert isinstance(block, TextContent)

    return block.text


async def call(
    tools: SQLTools | FakeSQLTools, tool: str, arguments: dict[str, str] | None = None
) -> CallToolResult:
    # An in-memory client, connected to the server as Claude would be, but without a subprocess.
    async with Client(create_server(tools), raise_exceptions=True) as client:  # type: ignore[arg-type]
        return await client.call_tool(tool, arguments)


@pytest.mark.anyio
async def test_only_the_two_read_only_tools_are_listed(tmp_path: Path) -> None:
    tools, _ = tools_with(FakeRepository(), tmp_path)

    async with Client(create_server(tools), raise_exceptions=True) as client:
        listed = (await client.list_tools()).tools

    assert sorted(tool.name for tool in listed) == ["describe_schema", "execute_sql"]

    for tool in listed:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False


@pytest.mark.anyio
async def test_execute_sql_runs_the_query_and_logs_it_as_mcp(tmp_path: Path) -> None:
    repository = FakeRepository()
    tools, log_path = tools_with(repository, tmp_path)
    result = await call(tools, "execute_sql", {"sql": COUNT_SQL})
    [record] = read_log(log_path)

    assert not result.is_error
    assert result.structured_content == {"columns": ["n"], "rows": [[1]], "truncated": False}
    assert json.loads(text_of(result)) == result.structured_content
    # The validator ran before the repository: it added the row limit.
    assert repository.selected == [(f"{COUNT_SQL}\nLIMIT 101", 100)]
    assert (record["source"], record["status"], record["question"]) == ("mcp", "ok", None)
    assert len(str(record["request_id"])) == 32


@pytest.mark.anyio
@pytest.mark.parametrize("sql", WRITES)
async def test_execute_sql_refuses_writes_and_logs_them(sql: str, tmp_path: Path) -> None:
    repository = FakeRepository()
    tools, log_path = tools_with(repository, tmp_path)
    result = await call(tools, "execute_sql", {"sql": sql})
    [record] = read_log(log_path)

    assert result.is_error
    assert result.structured_content is None
    assert repository.selected == []
    assert (record["source"], record["status"], record["sql"]) == ("mcp", "rejected", sql)
    # The client's model reads the validator's message, which says what to fix.
    assert text_of(result) == f"Error executing tool execute_sql: {record['error']}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        ExecutionError('Erro do Postgres: column "nope" does not exist'),
        QueryTimeout("A consulta passou de 5 s e foi cancelada."),
        DatabaseUnavailable("O banco de dados não respondeu: connection refused"),
    ],
)
async def test_execute_sql_reports_failures_to_the_model(error: Exception, tmp_path: Path) -> None:
    tools, _ = tools_with(FakeRepository(error=error), tmp_path)
    result = await call(tools, "execute_sql", {"sql": COUNT_SQL})

    assert result.is_error
    assert text_of(result) == f"Error executing tool execute_sql: {error}"


@pytest.mark.anyio
async def test_describe_schema_returns_the_text_of_the_sql_tools() -> None:
    tools = FakeSQLTools()
    result = await call(tools, "describe_schema")

    assert not result.is_error
    assert result.structured_content is None
    assert text_of(result) == tools.describe_schema()


@pytest.mark.anyio
async def test_describe_schema_reports_a_database_that_is_down() -> None:
    result = await call(FakeSQLTools(healthy=False), "describe_schema")

    assert result.is_error
    assert text_of(result) == "Error executing tool describe_schema: o banco não respondeu"


def test_without_settings_the_server_says_how_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(PROJECT)  # brings the tests back to this folder after main() moves to --directory
    monkeypatch.delenv("AGENT_DB_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(["--directory", str(tmp_path)])

    assert exit_info.value.code == 1
    assert Path.cwd() == tmp_path.resolve()
    assert "--directory" in capsys.readouterr().err


@pytest.mark.db
@pytest.mark.anyio
async def test_describe_schema_returns_the_description_the_api_uses(
    settings: Settings, tools: SQLTools
) -> None:
    # The fixture is the SQL tools the API builds; the server gets its own, from the shared assembly.
    repository, server_tools = open_sql_tools(settings)

    try:
        result = await call(server_tools, "describe_schema")
    finally:
        repository.close()

    assert text_of(result) == tools.describe_schema()


@pytest.mark.db
@pytest.mark.anyio
async def test_execute_sql_runs_as_the_read_only_role(tools: SQLTools) -> None:
    result = await call(tools, "execute_sql", {"sql": "SELECT current_user AS role"})

    assert result.structured_content == {"columns": ["role"], "rows": [["agent_reader"]], "truncated": False}


@pytest.mark.db
@pytest.mark.anyio
@pytest.mark.usefixtures("repository")
async def test_the_server_runs_on_stdio_from_another_folder(tmp_path: Path) -> None:
    # As Claude Desktop does: the process starts in a folder of its own, and --directory points here.
    log_path = tmp_path / "audit.jsonl"
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "delivery_agent.mcp", "--directory", str(PROJECT)],
        env={**os.environ, "AUDIT_LOG_PATH": str(log_path)},
        cwd=tmp_path,
    )

    async with Client(server) as client:
        listed = (await client.list_tools()).tools
        result = await client.call_tool("execute_sql", {"sql": COUNT_SQL})
        name = client.server_info.name

    assert name == "delivery-data"
    assert sorted(tool.name for tool in listed) == ["describe_schema", "execute_sql"]
    assert not result.is_error
    assert [record["source"] for record in read_log(log_path)] == ["mcp"]
