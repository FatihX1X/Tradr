"""Read-only diagnostics: venue prices remain real even when account execution is simulated."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .models import dec


def venue_snapshot(venue, market):
    book = venue.books.get(market.id)
    feed = getattr(venue, "feed", venue)
    rate = market.raw.get("nextFundingRate", market.raw.get("fundingRate")) if market.venue == "arcus" else (
        market.raw.get("current_funding_rate"))
    fraction = dec(rate) if rate is not None else None
    if fraction is not None and market.venue == "lighter":
        fraction /= 100  # RH market_stats rates are percentages
    return {"market_id": market.id, "active": market.active,
            "bid": str(book.bids[0][0]) if book and book.bids else None,
            "ask": str(book.asks[0][0]) if book and book.asks else None,
            "mark": str(market.mark) if market.mark > 0 else None,
            "oracle": str(market.oracle) if market.oracle > 0 else None,
            "funding_hourly_estimate_fraction": str(fraction) if fraction is not None else None,
            "price_age_seconds": round(time.monotonic()-market.observed, 3) if market.observed else None,
            "book_received_age_seconds": round(time.monotonic()-book.received, 3) if book else None,
            "book_source_age_seconds": round(time.time()-book.source_time, 3) if book and book.source_time else None,
            "book_source_utc": datetime.fromtimestamp(book.source_time, timezone.utc).isoformat()
                if book and book.source_time else None,
            "book_fresh": book.fresh() if book else False,
            "price_fresh": market.observed > 0 and time.monotonic()-market.observed <= 2,
            "metadata_ready": getattr(feed, "metadata_ready", True),
            "source_url": getattr(feed, "url", "test-fixture")}


def market_snapshots(venues, policies):
    if "arcus" not in venues or "lighter" not in venues or policies is None:
        return {}
    aliases = {p.get("arcus_symbol"): p.get("lighter_symbol") for p in policies.profiles}
    peers = {m.symbol: m for m in venues["lighter"].markets.values()}
    result = {}
    for arcus in venues["arcus"].markets.values():
        lighter = peers.get(aliases.get(arcus.symbol, arcus.symbol))
        if lighter is not None:
            result[arcus.symbol] = {"category": arcus.category,
                "arcus": venue_snapshot(venues["arcus"], arcus),
                "lighter": venue_snapshot(venues["lighter"], lighter)}
    return result
