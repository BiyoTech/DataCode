"""Database URL, engine and session utilities.

``init_db`` is deliberately absent: which tables a service creates is the one
service-specific part, and sharing it would give a table two creators. See
``session.py``.
"""

from datacode_common.database.session import Database

__all__ = ["Database"]
