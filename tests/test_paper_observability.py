import asyncio
import time

import pytest

from conftest import fresh_book
from test_execution_recovery import FakeVenue, engine_fixture
from tradr.models import D, Intent
from tradr.telemetry import market_snapshots
from tradr.venues.base import TransportError
from tradr.venues.paper import Paper


def test_snapshot_keeps_actual_quote_and_signed_funding(policies, markets):
    a, b = FakeVenue(markets[0], "101.1", "101.2"), FakeVenue(markets[1], "100.9", "101.0")
    markets[0].raw["nextFundingRate"] = "-0.00001"
    markets[1].raw["current_funding_rate"] = "-0.001"
    data = market_snapshots({"arcus": a, "lighter": b}, policies)["BTC"]
    assert data["arcus"]["bid"] == "101.1"
    assert data["lighter"]["ask"] == "101.0"
    assert data["arcus"]["funding_hourly_estimate_fraction"] == "-0.00001"
    assert data["lighter"]["funding_hourly_estimate_fraction"] == "-0.00001"
    assert data["arcus"]["book_source_utc"] and data["arcus"]["book_fresh"]
    a.books[1].source_time = time.time()-10
    assert not market_snapshots({"arcus": a, "lighter": b}, policies)["BTC"]["arcus"]["book_fresh"]


@pytest.mark.asyncio
async def test_paper_standard_charges_public_market_fee_not_premium_bound(config, journal, markets):
    market = markets[1]
    market.raw.update(maker_fee="0.0000", taker_fee="0.0000")
    feed = FakeVenue(market, "99.9", "100.1")
    feed.taker_fee = D("0.00035")
    paper = Paper(feed, config, journal)
    intent = Intent(journal.next_id(), "lighter", market.id, True, D("0.1"), D("100.2"))
    journal.prepare(intent)
    order = await paper.submit(intent)
    assert order.filled == D("0.1") and paper.cash == D("89.99")
    assert (await paper.account()).taker_fee == 0
    market.raw.pop("taker_fee")
    with pytest.raises(TransportError, match="fee metadata"):
        paper.fees_for(market)


@pytest.mark.asyncio
async def test_flat_metadata_fault_waits_then_resumes_without_orders(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets)
    a.metadata_ready = False
    # No economic edge even once metadata recovers; no test orders should be produced.
    b.books[0] = fresh_book()
    observations = []
    original_publish = engine.publish

    def observe(force=False):
        observations.append(engine.phase)
        original_publish(force)

    engine.publish = observe

    async def repair():
        await asyncio.sleep(.4)
        a.metadata_ready = True

    task = asyncio.create_task(repair())
    await engine.run(seconds=1.2)
    await task
    assert "paused" in observations and "scanning" in observations
    assert not engine.halted and not a.submissions and not b.submissions


def test_health_separates_simulated_balances_and_actual_market_quotes(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets)
    engine.accounts = {}
    health = engine.health()
    assert health["execution_mode"] == "paper"
    assert health["market_data"]["BTC"]["arcus"]["ask"] == "100.1"
    assert health["market_data"]["BTC"]["lighter"]["bid"] == "103"
