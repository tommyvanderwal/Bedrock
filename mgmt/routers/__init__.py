"""Bedrock mgmt API routers (FastAPI bigger-applications layout).

Each module here exposes a module-level ``router = APIRouter(...)`` covering one resource
domain; ``app.py`` includes them all. Handlers get the shared Daemon state via the
``get_state`` dependency and cross-cutting auth via ``require_operator`` / ``require_peer``
from ``mgmt.dependencies``.
"""
