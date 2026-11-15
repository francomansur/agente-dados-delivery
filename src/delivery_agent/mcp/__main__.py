"""Run the MCP server on stdio: ``python -m delivery_agent.mcp``.

The client (Claude Code, Claude Desktop) starts this process and talks to it through stdin and
stdout, so nothing else may print to stdout. The ``.env`` and the audit log are relative to the
current folder, and a client may start the server in a folder of its own: ``--directory`` points to
the project.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from delivery_agent.assembly import open_sql_tools
from delivery_agent.core.settings import Settings
from delivery_agent.mcp.server import create_server

INVALID_SETTINGS = (
    "As configurações do banco estão incompletas ou inválidas. Rode o servidor na pasta do projeto, "
    "onde fica o .env, ou passe essa pasta em --directory."
)


def main(argv: Sequence[str] | None = None) -> None:
    """Serve the read-only tools on stdio until the client closes the connection.

    Args:
        argv: Command-line arguments; ``sys.argv[1:]`` when omitted.
    """
    parser = argparse.ArgumentParser(
        prog="python -m delivery_agent.mcp",
        description="Servidor MCP somente leitura da base de delivery, pelo stdio.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        help="pasta do projeto, com o .env e a pasta logs/ (padrão: a pasta atual)",
    )
    args = parser.parse_args(argv)

    if args.directory is not None:
        os.chdir(args.directory)

    try:
        settings = Settings()
    except ValidationError as error:
        parser.exit(1, f"{INVALID_SETTINGS}\n\n{error}\n")

    repository, tools = open_sql_tools(settings)

    try:
        create_server(tools).run()
    finally:
        repository.close()


if __name__ == "__main__":
    main()
