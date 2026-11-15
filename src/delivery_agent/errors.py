"""Errors raised while validating or running model-written SQL.

The messages are in Portuguese because they go back to the model, which uses them to fix the SQL.
"""


class SQLError(Exception):
    """Expected failure of a model-written query; the message says how to fix it."""


class SQLRejected(SQLError):
    """The validator blocked the query before it reached the database."""


class QueryTimeout(SQLError):
    """The query ran past the time limit and was canceled by the database."""


class ExecutionError(SQLError):
    """Postgres refused or failed to run the query."""


class DatabaseUnavailable(Exception):
    """The database cannot be reached. An infrastructure failure, not a problem with the query."""
