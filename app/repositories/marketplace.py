"""Marketplace listings, requests, row locks, and transaction-feed queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from .base import ConflictError, Repository, ValidationError, require_row


class MarketplaceRepository(Repository):
    def listing(
        self, listing_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = "FOR UPDATE OF l" if lock else ""
        return require_row(
            self.connection.execute(
                f"""
                SELECT l.*, t.ticket_number, t.owner_participant_id,
                       t.eliminated_round_id,
                       seller.display_name AS seller_name
                FROM listings l
                JOIN tickets t ON t.id = l.ticket_id
                JOIN draw_participants seller
                    ON seller.id = l.seller_participant_id
                WHERE l.id = %s AND l.draw_id = %s
                {suffix}
                """,
                (listing_id, self.draw_id),
            ).fetchone(),
            "Listing not found.",
        )

    def open_listings(self) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT l.*, t.ticket_number,
                       seller.display_name AS seller_name
                FROM listings l
                JOIN tickets t ON t.id = l.ticket_id
                JOIN draw_participants seller
                    ON seller.id = l.seller_participant_id
                WHERE l.draw_id = %s
                  AND l.status IN ('open', 'reserved')
                  AND t.owner_participant_id = l.seller_participant_id
                  AND t.eliminated_round_id IS NULL
                ORDER BY l.price_cents, l.created_at, l.id
                """,
                (self.draw_id,),
            ).fetchall()
        )

    def participant_listings(
        self, participant_id: UUID
    ) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT l.*, t.ticket_number
                FROM listings l
                JOIN tickets t ON t.id = l.ticket_id
                WHERE l.draw_id = %s AND l.seller_participant_id = %s
                ORDER BY l.created_at DESC, l.id
                """,
                (self.draw_id, participant_id),
            ).fetchall()
        )

    def create_listing(
        self,
        *,
        ticket_id: UUID,
        seller_id: UUID,
        price_cents: int,
    ) -> dict[str, Any]:
        if price_cents <= 0:
            raise ValidationError("Listing price must be positive.")
        try:
            return self.connection.execute(
                """
                INSERT INTO listings (
                    id, draw_id, ticket_id,
                    seller_participant_id, price_cents, status
                ) VALUES (%s, %s, %s, %s, %s, 'open')
                RETURNING *
                """,
                (uuid4(), self.draw_id, ticket_id, seller_id, price_cents),
            ).fetchone()
        except Exception as error:
            if getattr(error, "sqlstate", "") == "23505":
                raise ConflictError(
                    "The ticket already has an active listing."
                ) from error
            raise

    def update_listing_price(
        self,
        listing_id: UUID,
        seller_id: UUID,
        price_cents: int,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        listing = self.listing(listing_id, lock=True)
        if listing["seller_participant_id"] != seller_id:
            raise ConflictError("Only the seller can update this listing.")
        if listing["status"] not in {"open", "reserved"}:
            raise ConflictError("The listing is no longer active.")
        if (
            expected_version is not None
            and listing["version"] != expected_version
        ):
            raise ConflictError("The listing changed; refresh and retry.")
        return self.connection.execute(
            """
            UPDATE listings
            SET price_cents = %s, version = version + 1
            WHERE id = %s AND draw_id = %s
            RETURNING *
            """,
            (price_cents, listing_id, self.draw_id),
        ).fetchone()

    def close_listing(
        self,
        listing_id: UUID,
        *,
        status: str,
        closed_at: datetime,
    ) -> None:
        if status not in {"sold", "cancelled", "invalidated"}:
            raise ValidationError("Invalid closed listing status.")
        self.connection.execute(
            """
            UPDATE listings
            SET status = %s, closed_at = %s, version = version + 1
            WHERE id = %s AND draw_id = %s
            """,
            (status, closed_at, listing_id, self.draw_id),
        )

    def invalidate_ticket(self, ticket_id: UUID, decided_at: datetime) -> int:
        return self.invalidate_tickets([ticket_id], decided_at)

    def invalidate_tickets(
        self, ticket_ids: list[UUID], decided_at: datetime
    ) -> int:
        ids = list(ticket_ids)
        if not ids:
            return 0
        rows = self.connection.execute(
            """
            UPDATE listings
            SET status = 'invalidated', closed_at = %s,
                version = version + 1
            WHERE draw_id = %s AND ticket_id = ANY(%s)
              AND status IN ('open', 'reserved')
            RETURNING id
            """,
            (decided_at, self.draw_id, ids),
        ).fetchall()
        listing_ids = [row["id"] for row in rows]
        if listing_ids:
            self.connection.execute(
                """
                UPDATE purchase_requests
                SET status = 'superseded', decided_at = %s
                WHERE draw_id = %s AND listing_id = ANY(%s)
                  AND status = 'pending'
                """,
                (decided_at, self.draw_id, listing_ids),
            )
        return len(listing_ids)

    def request(
        self, request_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = "FOR UPDATE OF pr" if lock else ""
        return require_row(
            self.connection.execute(
                f"""
                SELECT pr.*, l.ticket_id, l.seller_participant_id,
                       l.status AS listing_status, l.price_cents,
                       t.ticket_number, t.owner_participant_id,
                       t.eliminated_round_id,
                       buyer.display_name AS buyer_name,
                       seller.display_name AS seller_name
                FROM purchase_requests pr
                JOIN listings l ON l.id = pr.listing_id
                JOIN tickets t ON t.id = l.ticket_id
                JOIN draw_participants buyer
                    ON buyer.id = pr.buyer_participant_id
                JOIN draw_participants seller
                    ON seller.id = l.seller_participant_id
                WHERE pr.id = %s AND pr.draw_id = %s
                {suffix}
                """,
                (request_id, self.draw_id),
            ).fetchone(),
            "Purchase request not found.",
        )

    def request_by_idempotency(
        self, buyer_id: UUID, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self.connection.execute(
            """
            SELECT * FROM purchase_requests
            WHERE draw_id = %s AND buyer_participant_id = %s
              AND idempotency_key = %s
            """,
            (self.draw_id, buyer_id, idempotency_key),
        ).fetchone()

    def create_request(
        self,
        *,
        listing_id: UUID,
        buyer_id: UUID,
        offered_price_cents: int,
        idempotency_key: str,
        expires_at: datetime | None,
    ) -> dict[str, Any]:
        existing = self.request_by_idempotency(buyer_id, idempotency_key)
        if existing is not None:
            return existing
        return self.connection.execute(
            """
            INSERT INTO purchase_requests (
                id, draw_id, listing_id, buyer_participant_id,
                offered_price_cents, idempotency_key, status, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
            RETURNING *
            """,
            (
                uuid4(),
                self.draw_id,
                listing_id,
                buyer_id,
                offered_price_cents,
                idempotency_key,
                expires_at,
            ),
        ).fetchone()

    def decide_request(
        self,
        request_id: UUID,
        *,
        status: str,
        decided_at: datetime,
    ) -> None:
        if status not in {
            "approved",
            "declined",
            "withdrawn",
            "expired",
            "superseded",
        }:
            raise ValidationError("Invalid terminal purchase-request status.")
        row = self.connection.execute(
            """
            UPDATE purchase_requests
            SET status = %s, decided_at = %s
            WHERE id = %s AND draw_id = %s AND status = 'pending'
            RETURNING id
            """,
            (status, decided_at, request_id, self.draw_id),
        ).fetchone()
        if row is None:
            raise ConflictError("The request is no longer pending.")

    def supersede_competing_requests(
        self,
        listing_id: UUID,
        approved_request_id: UUID,
        decided_at: datetime,
    ) -> int:
        result = self.connection.execute(
            """
            UPDATE purchase_requests
            SET status = 'superseded', decided_at = %s
            WHERE draw_id = %s AND listing_id = %s
              AND id <> %s AND status = 'pending'
            """,
            (decided_at, self.draw_id, listing_id, approved_request_id),
        )
        return result.rowcount

    def participant_requests(
        self, participant_id: UUID
    ) -> dict[str, list[dict[str, Any]]]:
        outgoing = list(
            self.connection.execute(
                """
                SELECT pr.*, t.ticket_number,
                       seller.display_name AS seller_name
                FROM purchase_requests pr
                JOIN listings l ON l.id = pr.listing_id
                JOIN tickets t ON t.id = l.ticket_id
                JOIN draw_participants seller
                    ON seller.id = l.seller_participant_id
                WHERE pr.draw_id = %s AND pr.buyer_participant_id = %s
                ORDER BY pr.created_at DESC
                """,
                (self.draw_id, participant_id),
            ).fetchall()
        )
        incoming = list(
            self.connection.execute(
                """
                SELECT pr.*, t.ticket_number,
                       buyer.display_name AS buyer_name
                FROM purchase_requests pr
                JOIN listings l ON l.id = pr.listing_id
                JOIN tickets t ON t.id = l.ticket_id
                JOIN draw_participants buyer
                    ON buyer.id = pr.buyer_participant_id
                WHERE pr.draw_id = %s
                  AND l.seller_participant_id = %s
                ORDER BY pr.created_at DESC
                """,
                (self.draw_id, participant_id),
            ).fetchall()
        )
        return {"incoming": incoming, "outgoing": outgoing}

    def create_trade(
        self,
        *,
        listing_id: UUID,
        request_id: UUID,
        ticket_id: UUID,
        seller_id: UUID,
        buyer_id: UUID,
        price_cents: int,
        executed_at: datetime,
    ) -> dict[str, Any]:
        return self.connection.execute(
            """
            INSERT INTO trades (
                id, draw_id, listing_id, request_id, ticket_id,
                seller_participant_id, buyer_participant_id,
                price_cents, status, executed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'settled', %s)
            RETURNING *
            """,
            (
                uuid4(),
                self.draw_id,
                listing_id,
                request_id,
                ticket_id,
                seller_id,
                buyer_id,
                price_cents,
                executed_at,
            ),
        ).fetchone()

    def recent_trades(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """
                SELECT tr.*, t.ticket_number,
                       seller.display_name AS seller_name,
                       buyer.display_name AS buyer_name
                FROM trades tr
                JOIN tickets t ON t.id = tr.ticket_id
                JOIN draw_participants seller
                    ON seller.id = tr.seller_participant_id
                JOIN draw_participants buyer
                    ON buyer.id = tr.buyer_participant_id
                WHERE tr.draw_id = %s
                ORDER BY tr.executed_at DESC, tr.id DESC
                LIMIT %s
                """,
                (self.draw_id, limit),
            ).fetchall()
        )

    def market_summary(self) -> dict[str, Any]:
        low_ask = self.connection.execute(
            """
            SELECT min(l.price_cents) AS low_ask
            FROM listings l
            JOIN tickets t ON t.id = l.ticket_id
            WHERE l.draw_id = %s AND l.status = 'open'
              AND t.owner_participant_id = l.seller_participant_id
              AND t.eliminated_round_id IS NULL
            """,
            (self.draw_id,),
        ).fetchone()["low_ask"]
        best_bid = self.connection.execute(
            """
            SELECT max(pr.offered_price_cents) AS best_bid
            FROM purchase_requests pr
            JOIN listings l ON l.id = pr.listing_id
            JOIN tickets t ON t.id = l.ticket_id
            WHERE pr.draw_id = %s AND pr.status = 'pending'
              AND (pr.expires_at IS NULL OR pr.expires_at > now())
              AND l.status IN ('open', 'reserved')
              AND t.owner_participant_id = l.seller_participant_id
              AND t.eliminated_round_id IS NULL
            """,
            (self.draw_id,),
        ).fetchone()["best_bid"]
        trades = self.recent_trades(limit=1)
        return {
            "low_ask_cents": low_ask,
            "best_bid_cents": best_bid,
            "last_trade": trades[0] if trades else None,
        }
