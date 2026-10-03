"""Repository collection bound to one connection and one draw."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .base import (
    ConflictError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from .draws import DrawRepository
from .imports import ImportRepository
from .marketplace import MarketplaceRepository
from .notifications import NotificationRepository
from .participants import ParticipantRepository
from .tickets import TicketRepository


class Repositories:
    def __init__(self, connection: Any, draw_id: UUID) -> None:
        self.draws = DrawRepository(connection, draw_id)
        self.tickets = TicketRepository(connection, draw_id)
        self.participants = ParticipantRepository(connection, draw_id)
        self.imports = ImportRepository(connection, draw_id)
        self.notifications = NotificationRepository(connection, draw_id)
        self.marketplace = MarketplaceRepository(connection, draw_id)


__all__ = [
    "ConflictError",
    "NotFoundError",
    "Repositories",
    "RepositoryError",
    "ValidationError",
]
