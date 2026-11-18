"""Stable UUIDs for seeded rows so re-running the seed is idempotent."""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("6f1c2d4e-9a7b-4c3d-8e2f-1a2b3c4d5e6f")


def seed_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{key}"))
