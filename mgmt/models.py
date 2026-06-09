"""Pydantic request/response models shared across more than one mgmt router.

Models used by a single router live in that router module; only genuinely shared shapes are
promoted here (imported during the app.py → routers migration as the need appears).
"""
from __future__ import annotations

from pydantic import BaseModel  # noqa: F401  (re-exported for routers)
