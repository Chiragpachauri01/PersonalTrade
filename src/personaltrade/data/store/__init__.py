"""SQLite store: ORM models, engine/session management, repositories.

Everything above this package talks to repositories, never to SQLAlchemy directly,
so the storage backend can be replaced (ADR-003).
"""
