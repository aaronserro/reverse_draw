"""Shared repository contracts and domain-facing persistence errors."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class ConflictError(RepositoryError):
    pass


class ValidationError(RepositoryError):
    pass


def normalize_person_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def clean_display_name(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f�]", "", str(value or "")).strip()[:120]


def require_row(row: dict[str, Any] | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return row


class Repository:
    def __init__(self, connection: Any, draw_id: UUID) -> None:
        self.connection = connection
        self.draw_id = draw_id
