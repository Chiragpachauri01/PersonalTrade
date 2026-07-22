"""Local web dashboard (ROADMAP M16): server-rendered FastAPI + Jinja2, a
thin REST API, and a DB-polling websocket for live updates. See ADR-026.
Read-only over the same domain code the CLI already calls — no order entry,
no config editing (CLAUDE.md Rule 10; ADR-026 decision 5).
"""

from __future__ import annotations
