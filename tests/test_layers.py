"""The layers depend in one direction only: api and mcp -> services -> (repositories, llm) -> core."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "src" / "delivery_agent"

# The layers each one may import, besides itself and the version in the package root. The entry
# points put the layers together, so they are not checked: main.py, assembly.py and mcp/__main__.py.
ALLOWED = {
    "api": {"services", "core"},
    "mcp": {"services", "core"},
    "services": {"repositories", "llm", "core"},
    "repositories": {"core"},
    "llm": {"core"},
    "core": set(),
}
ENTRY_POINTS = {"__main__.py"}


def imported_modules(path: Path) -> set[str]:
    """Every module that one file imports, such as ``psycopg`` or ``delivery_agent.core.errors``."""
    found: set[str] = set()

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)

    return found


def project_imports(path: Path) -> set[str]:
    """Modules of the project that one file imports, such as ``delivery_agent.core.errors``."""
    return {module for module in imported_modules(path) if module.startswith("delivery_agent")}


def layer_of(module: str) -> str:
    """Layer of a module: ``delivery_agent.core.errors`` is in ``core``, and the package root in ``""``."""
    return module.split(".")[1] if "." in module else ""


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_each_layer_imports_only_the_layers_below(layer: str) -> None:
    files = sorted(path for path in (PACKAGE / layer).glob("*.py") if path.name not in ENTRY_POINTS)
    wrong = [
        f"{path.name} imports {module}"
        for path in files
        for module in sorted(project_imports(path))
        if layer_of(module) not in {"", layer, *ALLOWED[layer]}
    ]

    assert files
    assert wrong == []


@pytest.mark.parametrize("name", ["delivery_repository.py", "review_repository.py"])
def test_the_repositories_import_neither_the_validator_nor_the_audit_log(name: str) -> None:
    imports = project_imports(PACKAGE / "repositories" / name)

    assert "delivery_agent.services.sql_validator" not in imports
    assert "delivery_agent.core.audit" not in imports


def test_the_mcp_server_has_no_shortcut_to_the_database() -> None:
    # Its tools reach Postgres only through the SQL tools: no repository, no database driver and no
    # write action, which stays out of the MCP.
    imports = {module for path in (PACKAGE / "mcp").glob("*.py") for module in imported_modules(path)}
    forbidden = ("psycopg", "delivery_agent.repositories", "delivery_agent.services.review_actions")
    shortcuts = [module for module in imports if module.startswith(forbidden)]

    assert shortcuts == []


def test_the_data_generator_stands_alone() -> None:
    # The Postgres image runs seed.py by itself, without the rest of the package.
    assert project_imports(PACKAGE / "seed.py") == set()
