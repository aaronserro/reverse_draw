"""Authoritative relational marketplace snapshot projection."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.repositories import Repositories
from app import config

from .trader_state import build_trader_payload


_shared_cache: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_shared_cache_lock = threading.Lock()


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _listing_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "ticket": int(row["ticket_number"]),
        "seller_id": str(row["seller_participant_id"]),
        "seller_name": row.get("seller_name", ""),
        "price_cents": int(row["price_cents"]),
        "status": row["status"],
        "version": int(row["version"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "closed_at": _iso(row.get("closed_at")),
    }


def _request_payload(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    status = row["status"]
    expires_at = row.get("expires_at")
    if status == "pending" and expires_at is not None:
        comparable = expires_at
        if comparable.tzinfo is None:
            comparable = comparable.replace(tzinfo=timezone.utc)
        if comparable <= now:
            status = "expired"
    return {
        "id": str(row["id"]),
        "listing_id": str(row["listing_id"]),
        "buyer_id": str(row["buyer_participant_id"]),
        "buyer_name": row.get("buyer_name", ""),
        "seller_name": row.get("seller_name", ""),
        "ticket": int(row["ticket_number"]),
        "offered_price_cents": int(row["offered_price_cents"]),
        "status": status,
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(expires_at),
        "decided_at": _iso(row.get("decided_at")),
    }


def _trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "listing_id": str(row["listing_id"]),
        "request_id": str(row["request_id"]),
        "ticket": int(row["ticket_number"]),
        "seller_id": str(row["seller_participant_id"]),
        "seller_name": row["seller_name"],
        "buyer_id": str(row["buyer_participant_id"]),
        "buyer_name": row["buyer_name"],
        "price_cents": int(row["price_cents"]),
        "status": row["status"],
        "executed_at": _iso(row["executed_at"]),
    }


def _buy_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "buyer_id": str(row["buyer_participant_id"]),
        "buyer_name": row.get("buyer_name", ""),
        "price_cents": int(row["price_cents"]),
        "status": row["status"],
        "version": int(row["version"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "closed_at": _iso(row.get("closed_at")),
    }


def _buy_order_fill_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "buy_order_id": str(row["buy_order_id"]),
        "ticket": int(row["ticket_number"]),
        "seller_id": str(row["seller_participant_id"]),
        "seller_name": row["seller_name"],
        "buyer_id": str(row["buyer_participant_id"]),
        "buyer_name": row["buyer_name"],
        "price_cents": int(row["price_cents"]),
        "status": "settled",
        "executed_at": _iso(row["executed_at"]),
    }


def _shared_market_payload(
    repositories: Repositories,
    draw: dict[str, Any],
) -> dict[str, Any]:
    key = (
        str(draw["id"]),
        int(draw["version"]),
        int(draw["marketplace_version"]),
    )
    now = time.monotonic()
    with _shared_cache_lock:
        cached = _shared_cache.get(key)
        if cached and now - cached[0] <= config.TRADING_MARKET_CACHE_SECONDS:
            return cached[1]

        trades = repositories.marketplace.recent_trades(
            limit=config.TRADING_HISTORY_LIMIT
        )
        buy_orders = repositories.marketplace.open_buy_orders()
        buy_fills = repositories.marketplace.recent_buy_order_fills(
            limit=config.TRADING_HISTORY_LIMIT
        )
        summary = repositories.marketplace.market_summary()
        feed = [_trade_payload(row) for row in trades]
        feed.extend(_buy_order_fill_payload(row) for row in buy_fills)
        feed.sort(key=lambda item: item["executed_at"], reverse=True)
        feed = feed[: config.TRADING_HISTORY_LIMIT]
        payload = {
            "open_listings": [
                _listing_payload(row)
                for row in repositories.marketplace.open_listings()
            ],
            "low_ask_cents": (
                int(summary["low_ask_cents"])
                if summary["low_ask_cents"] is not None
                else None
            ),
            "best_bid_cents": (
                int(summary["best_bid_cents"])
                if summary["best_bid_cents"] is not None
                else None
            ),
            "open_buy_orders": [
                _buy_order_payload(row) for row in buy_orders
            ],
            "last_trade": feed[0] if feed else None,
            "feed": feed,
        }
        _shared_cache.clear()
        _shared_cache[key] = (now, payload)
        return payload


def build_marketplace_snapshot(
    repositories: Repositories,
    participant_id: UUID,
    *,
    server_time: datetime | None = None,
) -> dict[str, Any]:
    now = server_time or datetime.now(timezone.utc)
    draw = repositories.draws.get()
    participant = repositories.participants.by_id(participant_id)
    requests = repositories.marketplace.participant_requests(participant_id)
    participant_buy_orders = (
        repositories.marketplace.participant_buy_orders(participant_id)
    )
    return {
        **build_trader_payload(
            repositories,
            participant_id,
            participant=participant,
            draw=draw,
        ),
        **_shared_market_payload(repositories, draw),
        "participant_id": str(participant["id"]),
        "draw_version": int(draw["version"]),
        "marketplace_version": int(draw["marketplace_version"]),
        "draw_status": draw["status"],
        "own_buy_orders": [
            _buy_order_payload(row) for row in participant_buy_orders
        ],
        "own_listings": [
            _listing_payload(row)
            for row in repositories.marketplace.participant_listings(
                participant_id
            )
        ],
        "incoming_requests": [
            _request_payload(row, now) for row in requests["incoming"]
        ],
        "outgoing_requests": [
            _request_payload(row, now) for row in requests["outgoing"]
        ],
        "server_time": _iso(now),
    }


__all__ = ["build_marketplace_snapshot"]
