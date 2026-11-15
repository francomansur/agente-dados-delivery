"""Assembly of the paths from the settings to the database, in one place.

``open_sql_tools`` builds the read-only path: a pool of connections of the read-only role, and the
SQL tools over it, which validate every query and write it to the audit log. Both entry points, the
api (``main.py``) and the MCP server (``mcp/__main__.py``), use it. ``open_review_actions`` builds the
only write, the review action, which only the api uses: the MCP server stays read-only.
"""

from __future__ import annotations

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import DeliveryRepository
from delivery_agent.repositories.review_repository import ReviewRepository
from delivery_agent.services.review_actions import ReviewActions
from delivery_agent.services.sql_tools import SQLTools


def open_sql_tools(settings: Settings) -> tuple[DeliveryRepository, SQLTools]:
    """Open the connection pool of the read-only role and create the SQL tools over it.

    The pool opens without waiting for the database, so an entry point starts even while Postgres
    is down and reports the problem on each call.

    Args:
        settings: Application settings.

    Returns:
        The repository, which the caller closes when it stops, and the SQL tools, which log every
        query to the audit log of the settings.
    """
    repository = DeliveryRepository(settings)
    repository.open(wait=False)

    return repository, SQLTools(repository, AuditLog(settings.audit_log_path))


def open_review_actions(settings: Settings, reader: DeliveryRepository) -> ReviewActions | None:
    """Create the review action: the rules read with the read-only role, and the write uses ``review_writer``.

    Args:
        settings: Application settings.
        reader: The read-only repository, for the rules.

    Returns:
        The review action, or ``None`` when ``REVIEW_DB_PASSWORD`` is not set and nothing can be written.
    """
    if settings.review_db_password is None:
        return None

    return ReviewActions(reader, ReviewRepository(settings), AuditLog(settings.audit_log_path))
