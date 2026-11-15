"""Security layer 3: validate model-written SQL before it reaches the database.

The validator parses the SQL with sqlglot, in the Postgres dialect, and accepts a single
read-only query. It is not the main barrier, which is the database role (layers 1 and 2):
its job is to reject early and tell the model what to fix. The SQL that runs is the
original text plus a LIMIT when it has none; sqlglot only analyzes it.
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokenizer_core import TokenType

from delivery_agent.errors import SQLRejected

MAX_ROWS = 100
ROW_LIMIT = MAX_ROWS + 1  # fetching one row more than we return tells us the result was cut

# sqlglot warns about every command it does not know. Those commands are rejected anyway.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# sqlglot does not decode Unicode escapes: for Postgres, U&"\0070g_sleep" is pg_sleep.
# They are rejected before parsing.
_UNICODE_ESCAPE = re.compile(r"\bu&['\"]", re.IGNORECASE)

_FORBIDDEN_NODES: dict[type[exp.Expr], str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.TruncateTable: "TRUNCATE",
    exp.Copy: "COPY",
    exp.Into: "SELECT ... INTO",
    exp.Lock: "FOR UPDATE ou FOR SHARE",
    exp.Set: "SET",
    exp.Grant: "GRANT",
    exp.Revoke: "REVOKE",
    exp.Analyze: "ANALYZE",
    exp.Transaction: "BEGIN",
    exp.Commit: "COMMIT",
    exp.Rollback: "ROLLBACK",
    exp.Command: "um comando que não é de leitura",
}

_DENIED_FUNCTION_PREFIXES = (
    "set_config",
    "pg_sleep",
    "pg_read_",
    "pg_ls_",
    "pg_stat_file",
    "lo_",
    "dblink",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_advisory_",
    "pg_try_advisory_",
)


def validate_sql(sql: str) -> str:
    """Check that the SQL is a single read-only query and cap the rows it returns.

    Args:
        sql: SQL written by the model.

    Returns:
        The SQL to run: the original text without trailing semicolons, plus ``LIMIT 101``
        on a new line when the outer query has no ``LIMIT`` or ``FETCH FIRST``.

    Raises:
        SQLRejected: The SQL is empty or unparseable, has more than one statement, is not
            a query, writes anywhere in its tree or calls a denied function. The message,
            in Portuguese, says what to fix.
    """
    if not sql.strip():
        raise SQLRejected("A consulta está vazia. Envie uma consulta SELECT.")

    if _UNICODE_ESCAPE.search(sql):
        raise SQLRejected(
            "Não use escapes Unicode (U&) em textos ou identificadores. Escreva os nomes diretamente."
        )

    try:
        statements = [statement for statement in sqlglot.parse(sql, read="postgres") if statement is not None]
    except SqlglotError as error:
        detail = str(error).splitlines()[0][:200]

        raise SQLRejected(
            f"Não consegui analisar o SQL ({detail}). Escreva uma consulta SELECT válida para o Postgres."
        ) from error

    if not statements:
        raise SQLRejected("A consulta está vazia. Envie uma consulta SELECT.")

    if len(statements) > 1:
        raise SQLRejected("Envie um único comando SQL por vez, sem ';' separando comandos.")

    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise SQLRejected(
            f"Só consultas de leitura (SELECT ou WITH ... SELECT) são permitidas, e recebi {_describe(tree)}."
        )

    forbidden = tree.find(*_FORBIDDEN_NODES)

    if forbidden is not None:
        raise SQLRejected(
            f"A consulta contém {_FORBIDDEN_NODES[type(forbidden)]}, que não é permitido. Use só leitura."
        )

    for function in tree.find_all(exp.Func):
        name = _denied_function_name(function)

        if name is not None:
            raise SQLRejected(
                f"A função {name} não é permitida. Use só funções de consulta, como COUNT, SUM e AVG."
            )

    sql_to_run = _strip_trailing_semicolons(sql)

    if _has_outer_limit(tree):
        return sql_to_run

    return f"{sql_to_run}\nLIMIT {ROW_LIMIT}"


def _describe(tree: exp.Expr) -> str:
    if isinstance(tree, exp.Command):
        return f"um comando {str(tree.this).upper()}"

    label = _FORBIDDEN_NODES.get(type(tree), tree.key.upper())

    return f"um comando {label}"


def _denied_function_name(function: exp.Func) -> str | None:
    names = {function.sql_name().lower()}

    if isinstance(function, exp.Anonymous):
        names.add(function.name.lower())

    return next((name for name in sorted(names) if name.startswith(_DENIED_FUNCTION_PREFIXES)), None)


def _has_outer_limit(tree: exp.Query) -> bool:
    return tree.args.get("limit") is not None or tree.args.get("fetch") is not None


def _strip_trailing_semicolons(sql: str) -> str:
    tokens = sqlglot.tokenize(sql, read="postgres")
    end = len(sql)

    while tokens and tokens[-1].token_type == TokenType.SEMICOLON:
        end = tokens.pop().start

    return sql[:end].rstrip()
