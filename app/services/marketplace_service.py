"""Transactional commands for exact-ticket marketplace behavior."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from uuid import UUID

from psycopg.errors import DeadlockDetected, SerializationFailure

from app.projections.marketplace_state import build_marketplace_snapshot
from app.repositories import ConflictError, Repositories, ValidationError


def retry_transaction(method):
    """Replay a transaction only after PostgreSQL confirms its rollback."""

    @wraps(method)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return method(*args, **kwargs)
            except (DeadlockDetected, SerializationFailure):
                if attempt == 2:
                    raise
                time.sleep(random.uniform(0.02, 0.1) * (attempt + 1))
        raise RuntimeError("Unreachable marketplace retry state.")

    return wrapped


class MarketplaceService:
    def __init__(
        self,
        database: Any,
        *,
        minimum_price_cents: int,
        maximum_price_cents: int,
        request_ttl_seconds: int,
    ) -> None:
        if minimum_price_cents <= 0:
            raise ValueError("The minimum marketplace price must be positive.")
        if maximum_price_cents < minimum_price_cents:
            raise ValueError(
                "The maximum price must not be below the minimum."
            )
        if request_ttl_seconds <= 0:
            raise ValueError("The purchase-request lifetime must be positive.")
        self.database = database
        self.minimum_price_cents = minimum_price_cents
        self.maximum_price_cents = maximum_price_cents
        self.request_ttl = timedelta(seconds=request_ttl_seconds)

    @retry_transaction
    def upsert_listing(
        self,
        participant_id: UUID,
        ticket_number: int,
        price_cents: int,
        *,
        expected_draw_version: int | None = None,
        expected_listing_version: int | None = None,
    ) -> dict[str, Any]:
        price = self._validate_price(price_cents)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock()
            self._require_market_open(draw)
            participant = repositories.participants.by_id(participant_id)
            if not participant["active"]:
                raise ConflictError("The participant is not active.")
            ticket = repositories.tickets.by_number(ticket_number, lock=True)
            if ticket["owner_participant_id"] != participant_id:
                raise ConflictError(
                    "Only the current owner can list a ticket."
                )
            if ticket["eliminated_round_id"] is not None:
                raise ConflictError("An eliminated ticket cannot be listed.")

            existing = self._active_listing_for_ticket(
                connection, ticket["id"], lock=True
            )
            if existing is None:
                if expected_listing_version is not None:
                    raise ConflictError("The listing no longer exists.")
                listing = repositories.marketplace.create_listing(
                    ticket_id=ticket["id"],
                    seller_id=participant_id,
                    price_cents=price,
                )
            else:
                listing = repositories.marketplace.update_listing_price(
                    existing["id"],
                    participant_id,
                    price,
                    expected_version=expected_listing_version,
                )
            repositories.draws.increment_marketplace_version()
            return dict(listing)

    @retry_transaction
    def cancel_listing(
        self,
        participant_id: UUID,
        listing_id: UUID,
        *,
        expected_draw_version: int | None = None,
        expected_listing_version: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock()
            listing = repositories.marketplace.listing(listing_id, lock=True)
            if listing["seller_participant_id"] != participant_id:
                raise ConflictError("Only the seller can cancel this listing.")
            if listing["status"] not in {"open", "reserved"}:
                raise ConflictError("The listing is no longer active.")
            if (
                expected_listing_version is not None
                and listing["version"] != expected_listing_version
            ):
                raise ConflictError("The listing changed; refresh and retry.")
            repositories.marketplace.close_listing(
                listing_id, status="cancelled", closed_at=now
            )
            self._supersede_all_requests(connection, listing_id, now)
            repositories.draws.increment_marketplace_version()
            return build_marketplace_snapshot(
                repositories, participant_id, server_time=now
            )

    @retry_transaction
    def request_purchase(
        self,
        buyer_id: UUID,
        listing_id: UUID,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValidationError(
                "An idempotency key of at most 200 characters is required."
            )
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock()
            self._require_market_open(draw)
            existing = repositories.marketplace.request_by_idempotency(
                buyer_id, key
            )
            if existing is not None:
                if existing["listing_id"] != listing_id:
                    raise ConflictError(
                        "The idempotency key was already used for another "
                        "request."
                    )
                return dict(existing)
            buyer = repositories.participants.by_id(buyer_id)
            if not buyer["active"]:
                raise ConflictError("The participant is not active.")
            listing = repositories.marketplace.listing(listing_id, lock=True)
            self._expire_listing_requests(connection, listing_id, now)
            if listing["status"] != "open":
                raise ConflictError("The listing is not open for requests.")
            if listing["seller_participant_id"] == buyer_id:
                raise ValidationError("A seller cannot buy their own ticket.")
            if (
                listing["owner_participant_id"]
                != listing["seller_participant_id"]
            ):
                raise ConflictError("The seller no longer owns the ticket.")
            if listing["eliminated_round_id"] is not None:
                raise ConflictError("The listed ticket is no longer active.")
            request = repositories.marketplace.create_request(
                listing_id=listing_id,
                buyer_id=buyer_id,
                offered_price_cents=listing["price_cents"],
                idempotency_key=key,
                expires_at=now + self.request_ttl,
            )
            repositories.draws.increment_marketplace_version()
            return dict(request)

    @retry_transaction
    def approve_request(
        self,
        seller_id: UUID,
        request_id: UUID,
        *,
        expected_draw_version: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expired = False
        snapshot = None
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock()
            request = repositories.marketplace.request(request_id, lock=True)
            if request["seller_participant_id"] != seller_id:
                raise ConflictError(
                    "Only the seller can approve this request."
                )
            if request["status"] == "approved":
                trade = repositories.marketplace.trade_by_request(request_id)
                if trade is not None:
                    return build_marketplace_snapshot(
                        repositories, seller_id, server_time=now
                    )
            self._require_market_open(draw)
            listing = repositories.marketplace.listing(
                request["listing_id"], lock=True
            )
            ticket = repositories.tickets.by_number(
                request["ticket_number"], lock=True
            )
            seller = repositories.participants.by_id(seller_id)
            buyer = repositories.participants.by_id(
                request["buyer_participant_id"]
            )
            if not seller["active"] or not buyer["active"]:
                raise ConflictError(
                    "Both marketplace participants must remain active."
                )
            if request["status"] != "pending":
                raise ConflictError("The request is no longer pending.")
            if request["expires_at"] and request["expires_at"] <= now:
                repositories.marketplace.decide_request(
                    request_id, status="expired", decided_at=now
                )
                repositories.draws.increment_marketplace_version()
                expired = True
            else:
                self._validate_settlement(
                    draw, request, listing, ticket, seller_id
                )
                trade = repositories.marketplace.create_trade(
                    listing_id=listing["id"],
                    request_id=request_id,
                    ticket_id=ticket["id"],
                    seller_id=seller_id,
                    buyer_id=request["buyer_participant_id"],
                    price_cents=request["offered_price_cents"],
                    executed_at=now,
                )
                repositories.tickets.set_owner(
                    ticket["ticket_number"],
                    request["buyer_participant_id"],
                    reason="trade",
                    actor_type="trader",
                    actor_identifier=str(seller_id),
                    trade_id=trade["id"],
                )
                repositories.marketplace.decide_request(
                    request_id, status="approved", decided_at=now
                )
                repositories.marketplace.close_listing(
                    listing["id"], status="sold", closed_at=now
                )
                repositories.marketplace.supersede_competing_requests(
                    listing["id"], request_id, now
                )
                repositories.draws.increment_version()
                repositories.draws.increment_marketplace_version()
                snapshot = build_marketplace_snapshot(
                    repositories, seller_id, server_time=now
                )
        if expired:
            raise ConflictError("The purchase request has expired.")
        return snapshot

    @retry_transaction
    def decline_request(
        self,
        seller_id: UUID,
        request_id: UUID,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock()
            request = repositories.marketplace.request(request_id, lock=True)
            if request["seller_participant_id"] != seller_id:
                raise ConflictError(
                    "Only the seller can decline this request."
                )
            repositories.marketplace.decide_request(
                request_id, status="declined", decided_at=now
            )
            repositories.draws.increment_marketplace_version()
            return dict(repositories.marketplace.request(request_id))

    @retry_transaction
    def withdraw_request(
        self,
        buyer_id: UUID,
        request_id: UUID,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock()
            request = repositories.marketplace.request(request_id, lock=True)
            if request["buyer_participant_id"] != buyer_id:
                raise ConflictError(
                    "Only the buyer can withdraw this request."
                )
            repositories.marketplace.decide_request(
                request_id, status="withdrawn", decided_at=now
            )
            repositories.draws.increment_marketplace_version()
            return dict(repositories.marketplace.request(request_id))

    @retry_transaction
    def upsert_buy_order(
        self,
        buyer_id: UUID,
        price_cents: int,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        price = self._validate_price(price_cents)
        key = self._validate_idempotency_key(idempotency_key)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock()
            self._require_market_open(draw)
            buyer = repositories.participants.by_id(buyer_id)
            if not buyer["active"]:
                raise ConflictError("The participant is not active.")
            replay = repositories.marketplace.buy_order_by_idempotency(
                buyer_id, key
            )
            if replay is not None:
                if replay["price_cents"] != price:
                    raise ConflictError(
                        "The idempotency key was already used for another bid."
                    )
                return dict(replay)
            existing = repositories.marketplace.open_buy_order_for_buyer(
                buyer_id, lock=True
            )
            if existing is None:
                if expected_version is not None:
                    raise ConflictError("The buy bid no longer exists.")
                order = repositories.marketplace.create_buy_order(
                    buyer_id=buyer_id,
                    price_cents=price,
                    idempotency_key=key,
                )
            else:
                order = repositories.marketplace.update_buy_order(
                    existing["id"],
                    price_cents=price,
                    idempotency_key=key,
                    expected_version=expected_version,
                )
            repositories.draws.increment_marketplace_version()
            return dict(order)

    @retry_transaction
    def cancel_buy_order(
        self,
        buyer_id: UUID,
        buy_order_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            repositories.draws.lock()
            order = repositories.marketplace.buy_order(
                buy_order_id, lock=True
            )
            if order["buyer_participant_id"] != buyer_id:
                raise ConflictError("Only the buyer can cancel this bid.")
            if (
                expected_version is not None
                and order["version"] != expected_version
            ):
                raise ConflictError("The buy bid changed; refresh and retry.")
            repositories.marketplace.close_buy_order(
                buy_order_id, status="cancelled", closed_at=now
            )
            repositories.draws.increment_marketplace_version()
            return build_marketplace_snapshot(
                repositories, buyer_id, server_time=now
            )

    @retry_transaction
    def accept_buy_order(
        self,
        seller_id: UUID,
        buy_order_id: UUID,
        ticket_number: int,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = self._validate_idempotency_key(idempotency_key)
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            draw = repositories.draws.lock()
            order = repositories.marketplace.buy_order(
                buy_order_id, lock=True
            )
            if order["status"] == "filled":
                fill = repositories.marketplace.buy_order_fill(buy_order_id)
                if (
                    fill is not None
                    and fill["seller_participant_id"] == seller_id
                    and fill["idempotency_key"] == key
                ):
                    return build_marketplace_snapshot(
                        repositories, seller_id, server_time=now
                    )
                raise ConflictError("The buy bid has already been filled.")
            self._require_market_open(draw)
            if order["status"] != "open":
                raise ConflictError("The buy bid is no longer open.")
            if order["buyer_participant_id"] == seller_id:
                raise ConflictError("A buyer cannot fill their own bid.")
            ticket = repositories.tickets.by_number(
                ticket_number, lock=True
            )
            active_listing = self._active_listing_for_ticket(
                connection, ticket["id"], lock=True
            )
            seller = repositories.participants.by_id(seller_id)
            buyer = repositories.participants.by_id(
                order["buyer_participant_id"]
            )
            if not seller["active"] or not buyer["active"]:
                raise ConflictError(
                    "Both marketplace participants must remain active."
                )
            if ticket["owner_participant_id"] != seller_id:
                raise ConflictError(
                    "Only the current owner can sell this ticket."
                )
            if ticket["eliminated_round_id"] is not None:
                raise ConflictError("An eliminated ticket cannot be sold.")

            event_id = repositories.tickets.transfer_for_buy_order(
                ticket_number,
                order["buyer_participant_id"],
                actor_identifier=str(seller_id),
            )
            repositories.marketplace.create_buy_order_fill(
                buy_order_id=buy_order_id,
                ticket_id=ticket["id"],
                seller_id=seller_id,
                buyer_id=order["buyer_participant_id"],
                ownership_event_id=event_id,
                price_cents=order["price_cents"],
                idempotency_key=key,
                executed_at=now,
            )
            repositories.marketplace.close_buy_order(
                buy_order_id, status="filled", closed_at=now
            )
            if active_listing is not None:
                repositories.marketplace.invalidate_ticket(ticket["id"], now)
            repositories.draws.increment_version()
            repositories.draws.increment_marketplace_version()
            return build_marketplace_snapshot(
                repositories, seller_id, server_time=now
            )

    def snapshot(
        self, participant_id: UUID, *, connection: Any | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            repositories = Repositories(
                connection, self.database.active_draw_id
            )
            return build_marketplace_snapshot(repositories, participant_id)
        with self.database.connection() as owned_connection:
            repositories = Repositories(
                owned_connection, self.database.active_draw_id
            )
            return build_marketplace_snapshot(repositories, participant_id)

    def _validate_price(self, value: int) -> int:
        if isinstance(value, bool):
            raise ValidationError("The listing price must be an integer.")
        try:
            price = int(value)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "The listing price must be an integer."
            ) from error
        if not self.minimum_price_cents <= price <= self.maximum_price_cents:
            raise ValidationError(
                "The listing price is outside the configured limits."
            )
        return price

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        key = value.strip()
        if not key or len(key) > 200:
            raise ValidationError(
                "An idempotency key of at most 200 characters is required."
            )
        return key

    @staticmethod
    def _require_market_open(draw: dict[str, Any]) -> None:
        if draw["status"] != "active":
            raise ConflictError(
                "Trading is available only during an active draw."
            )

    def _active_listing_for_ticket(
        self, connection: Any, ticket_id: UUID, *, lock: bool
    ) -> dict[str, Any] | None:
        suffix = "FOR UPDATE" if lock else ""
        return connection.execute(
            f"""
            SELECT * FROM listings
            WHERE draw_id = %s AND ticket_id = %s
              AND status IN ('open', 'reserved')
            {suffix}
            """,
            (self.database.active_draw_id, ticket_id),
        ).fetchone()

    def _expire_listing_requests(
        self, connection: Any, listing_id: UUID, now: datetime
    ) -> int:
        result = connection.execute(
            """
            UPDATE purchase_requests
            SET status = 'expired', decided_at = %s
            WHERE draw_id = %s AND listing_id = %s
              AND status = 'pending' AND expires_at <= %s
            """,
            (now, self.database.active_draw_id, listing_id, now),
        )
        return result.rowcount

    def _supersede_all_requests(
        self, connection: Any, listing_id: UUID, now: datetime
    ) -> int:
        result = connection.execute(
            """
            UPDATE purchase_requests
            SET status = 'superseded', decided_at = %s
            WHERE draw_id = %s AND listing_id = %s AND status = 'pending'
            """,
            (now, self.database.active_draw_id, listing_id),
        )
        return result.rowcount

    @staticmethod
    def _validate_settlement(
        draw: dict[str, Any],
        request: dict[str, Any],
        listing: dict[str, Any],
        ticket: dict[str, Any],
        seller_id: UUID,
    ) -> None:
        if draw["status"] != "active":
            raise ConflictError("The draw is not open for trading.")
        if listing["status"] not in {"open", "reserved"}:
            raise ConflictError("The listing is no longer active.")
        if listing["seller_participant_id"] != seller_id:
            raise ConflictError("Only the seller can approve this request.")
        if ticket["owner_participant_id"] != seller_id:
            raise ConflictError("The seller no longer owns the ticket.")
        if ticket["eliminated_round_id"] is not None:
            raise ConflictError("The listed ticket is no longer active.")
        if request["buyer_participant_id"] == seller_id:
            raise ConflictError("A seller cannot buy their own ticket.")
