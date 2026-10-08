import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradr.engine import Engine
from tradr.models import Account, D, Order, Position, grid
from tradr.storage import Journal
from tradr.venues.paper import Paper
from conftest import fresh_book


class FakeVenue:
    def __init__(self, market, bid, ask, fraction="1", lose_response=False):
        self.name = market.venue
        self.markets = {market.id: market}
        self.books = {market.id: fresh_book(bid, ask)}
        self.orders = {}
        self.positions = {}
        self.cash = D(100)
        self.fraction = D(fraction)
        self.lose_response = lose_response
        self.submissions = []
        self.maker_fee = self.taker_fee = D(0)
        self.cancel_fill = D(0)

    async def account(self):
        equity = self.cash + sum(p.quantity * self.markets[mid].mark for mid, p in self.positions.items())
        return Account(equity, max(D(0), equity-sum(abs(p.quantity)*p.mark for p in self.positions.values())),
                       copy.deepcopy(self.positions), {cid for cid, o in self.orders.items() if not o.terminal},
                       D(0), D(0))

    def fill(self, intent, quantity):
        existing = self.positions.get(intent.market_id)
        signed = quantity if intent.buy else -quantity
        self.cash -= signed * intent.price
        result = (existing.quantity if existing else D(0)) + signed
        if result:
            self.positions[intent.market_id] = Position(intent.market_id, result, intent.price,
                                                       self.markets[intent.market_id].mark, D(100), "cross")
        else:
            self.positions.pop(intent.market_id, None)

    async def submit(self, intent):
        self.submissions.append(intent)
        quantity = intent.quantity if intent.reduce_only else grid(intent.quantity * self.fraction,
                                                                   self.markets[intent.market_id].step)
        self.fill(intent, quantity)
        order = Order(intent.id, "open" if intent.maker else "filled" if quantity == intent.quantity
                      else "canceled", quantity, intent.price)
        self.orders[intent.id] = order
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError("lost response after matching")
        return order

    async def lookup(self, intent, server_id=None):
        return self.orders.get(intent.id, Order(intent.id, "unknown"))

    async def cancel(self, intent):
        order = self.orders[intent.id]
        extra = min(self.cancel_fill, intent.quantity-order.filled)
        if extra:
            self.fill(intent, extra)
            order.filled += extra
            self.cancel_fill = D(0)
        order.state = "canceled"

    async def prepare_market(self, market, profile):
        pass

    async def arm_cancel(self, market):
        pass

    async def emergency_book(self, market):
        return self.books[market.id]

    def cache_order(self, order):
        self.orders[order.id] = order


def engine_fixture(config, policies, journal, markets, **kwargs):
    a = FakeVenue(markets[0], "99.9", "100.1", **kwargs)
    b = FakeVenue(markets[1], "103", "103.1")
    engine = Engine(config, policies, journal, {"arcus": a, "lighter": b})
    return engine, a, b


@pytest.mark.asyncio
async def test_lost_response_is_reconciled_without_resubmission(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets, lose_response=True)
    await engine.refresh_accounts(True)
    engine.loss()
    candidate = engine.scanner.scan(a.markets, b.markets, {"arcus": a.books, "lighter": b.books}, engine.accounts)[0]
    await engine.open(candidate)
    assert len(a.submissions) == len(b.submissions) == 1
    assert journal.get("hedge")["state"] == "hedged"
    assert await engine.flatten()
    assert not a.positions and not b.positions and journal.get("hedge") is None


@pytest.mark.asyncio
async def test_partial_entry_trims_surplus_using_reduce_only(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets, fraction="0.5")
    await engine.refresh_accounts(True)
    engine.loss()
    candidate = engine.scanner.scan(a.markets, b.markets, {"arcus": a.books, "lighter": b.books}, engine.accounts)[0]
    await engine.open(candidate)
    assert abs(a.positions[1].quantity) == abs(b.positions[0].quantity)
    assert b.submissions[-1].reduce_only


@pytest.mark.asyncio
async def test_single_leg_rejection_unwinds_filled_leg(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets, fraction="0")
    await engine.refresh_accounts(True)
    engine.loss()
    candidate = engine.scanner.scan(a.markets, b.markets, {"arcus": a.books, "lighter": b.books}, engine.accounts)[0]
    await engine.open(candidate)
    assert not a.positions and not b.positions and journal.get("hedge") is None
    assert b.submissions[-1].reduce_only


@pytest.mark.asyncio
async def test_maker_late_fill_after_cancel_is_trimmed(config, policies, journal, markets):
    from test_financial_policy import boost_rule
    config.maker_wait_seconds = 1
    policies.boosts = [boost_rule(venue="arcus", style="maker")]
    engine, a, b = engine_fixture(config, policies, journal, markets, fraction="0.5")
    a.cancel_fill = D("0.1")
    await engine.refresh_accounts(True)
    engine.loss()
    candidate = engine.scanner.scan(a.markets, b.markets, {"arcus": a.books, "lighter": b.books}, engine.accounts)[0]
    assert candidate.maker_venue == "arcus"
    await engine.open(candidate)
    assert abs(a.positions[1].quantity) == abs(b.positions[0].quantity)
    assert any(i.reduce_only for i in a.submissions)


@pytest.mark.asyncio
async def test_unknown_order_never_becomes_closed_or_retried(config, policies, journal, markets):
    engine, a, _ = engine_fixture(config, policies, journal, markets)
    config.hedge_timeout_seconds = 1
    intent = engine.intent(markets[0], True, D("0.1"))
    journal.prepare(intent)
    journal.update(Order(intent.id, "unknown"))
    assert not await engine.cancel_entries()
    assert not a.submissions
    assert journal.rows()[0][1].state == "unknown"


@pytest.mark.asyncio
async def test_external_positions_prevent_entry_and_are_not_closed(config, policies, journal, markets):
    engine, a, _ = engine_fixture(config, policies, journal, markets)
    a.positions[1] = Position(1, D("0.1"), D(100), D(100), D(100), "cross")
    await engine.refresh_accounts(True)
    assert "external position" in engine.guard()
    await engine.flatten()
    assert a.positions[1].quantity == D("0.1") and not a.submissions


@pytest.mark.asyncio
async def test_paper_fill_survives_restart_without_double_charge(config, journal, markets):
    feed = FakeVenue(markets[0], "99.9", "100.1")
    paper = Paper(feed, config, journal)
    engine = Engine(config, None, journal, {"arcus": paper})
    intent = engine.intent(markets[0], True, D("0.1"))
    journal.prepare(intent)
    order = await paper.submit(intent)
    original = paper.cash
    feed2 = FakeVenue(markets[0], "99.9", "100.1")
    restored = Paper(feed2, config, journal)
    assert restored.cash == original
    assert (await restored.lookup(intent)).filled == order.filled
    assert not restored.pending


def test_daily_loss_istanbul_persists_and_excludes_known_cash_flows(tmp_path):
    when = datetime(2026, 10, 7, 23, 59, tzinfo=ZoneInfo("Europe/Istanbul"))
    journal = Journal(str(tmp_path), "paper")
    assert journal.daily_loss(D(200), D(200), when) == 0
    assert journal.daily_loss(D(199), D(200), when) == 1
    journal.close()
    journal = Journal(str(tmp_path), "paper")
    assert journal.daily_loss(D(209), D(210), when) == 1
    assert journal.daily_loss(D(199), D(200), when.replace(day=8, hour=0)) == 0
    journal.close()


def test_persisted_ids_and_fill_monotonicity(journal, markets, config, policies):
    engine, *_ = engine_fixture(config, policies, journal, markets)
    intent = engine.intent(markets[0], True, D("0.1"))
    journal.prepare(intent)
    journal.update(Order(intent.id, "filled", D("0.1"), D(100)))
    with pytest.raises(RuntimeError):
        journal.update(Order(intent.id, "unknown", D(0)))
    assert int(journal.next_id()) > int(intent.id)


def test_loss_budget_required_and_reserved_rh_key_indices(config, monkeypatch):
    config.arcus_address = "0x" + "1"*40
    config.lighter_account_index = 1
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", "fixture")
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "fixture")
    config.daily_loss_limit_usd = None
    with pytest.raises(ValueError, match="daily_loss"):
        config.validate(True)
    config.daily_loss_limit_usd = "1"
    for key in (0, 3, 157, 255):
        config.lighter_api_key_index = key
        with pytest.raises(ValueError, match="Reserved"):
            config.validate(True)


@pytest.mark.asyncio
async def test_foreign_increase_in_owned_market_requires_manual_reconciliation(config, policies, journal, markets):
    engine, a, b = engine_fixture(config, policies, journal, markets)
    await engine.refresh_accounts(True)
    engine.loss()
    candidate = engine.scanner.scan(a.markets, b.markets, {"arcus": a.books, "lighter": b.books}, engine.accounts)[0]
    await engine.open(candidate)
    a.positions[1].quantity += D("0.1")
    await engine.refresh_accounts(True)
    assert "diverged" in engine.guard()
    assert not await engine.flatten()
    assert engine.phase == "recovery"
    assert not any(i.reduce_only for i in a.submissions)
