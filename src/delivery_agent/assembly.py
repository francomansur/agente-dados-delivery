"""Assembly of the read-only path from the settings to the database.

A pool of connections of the read-only role, and the SQL tools over it, which validate every query
and write it to the audit log. The entry points build this path with ``open_sql_tools``, so that it
is assembled in one place.
"""

from __future__ import annotations

from delivery_agent.core.audit import AuditLog
from delivery_agent.core.settings import Settings
from delivery_agent.repositories.delivery_repository import DeliveryRepository
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
