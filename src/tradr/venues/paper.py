from __future__ import annotations

import asyncio
import time

from ..models import Account, D, Order, Position, dec, grid
from .base import TransportError


class Paper:
    """Runs the same execution engine against live books; never delegates a venue write."""

    def __init__(self, feed, config, journal):
        self.feed, self.config, self.journal = feed, config, journal
        self.name = feed.name
        self.pending = {}
        self.modes = {}
        state = journal.get("paper:" + self.name, {})
        self.cash = dec(state.get("cash", config.paper_capital_per_venue))
        self.positions = {}
        for mid, values in state.get("positions", {}).items():
            self.positions[int(mid)] = Position(int(mid), dec(values["quantity"]), dec(values["entry"]),
                                               D(0), dec(values["margin"]), values["mode"])
        self.last_funding_hour = state.get("last_funding_hour", int(time.time() // 3600))
        for intent, order in journal.rows():
            if intent.venue == self.name:
                feed.orders[intent.id] = order
                if not order.terminal:
                    self.pending[intent.id] = intent

    @property
    def markets(self):
        return self.feed.markets

    @property
    def books(self):
        return self.feed.books

    def fees_for(self, market):
        if self.name == "lighter" and self.config.paper_lighter_tier == "standard":
            # Public RH orderBookDetails exposes current Standard rates in percent.
            if "maker_fee" not in market.raw or "taker_fee" not in market.raw:
                raise TransportError("Paper Standard fee metadata unavailable")
            return dec(market.raw["maker_fee"])/100, dec(market.raw["taker_fee"])/100
        return self.feed.maker_fee, self.feed.taker_fee

    @property
    def maker_fee(self):
        return max((self.fees_for(m)[0] for m in self.markets.values() if m.active), default=D(0))

    @property
    def taker_fee(self):
        return max((self.fees_for(m)[1] for m in self.markets.values() if m.active), default=D(0))

    def state(self):
        return {"cash": str(self.cash),
            "last_funding_hour": self.last_funding_hour,
            "positions": {str(mid): {"quantity": str(p.quantity), "entry": str(p.entry),
                                       "margin": str(p.margin), "mode": p.mode}
                          for mid, p in self.positions.items()}}

    def persist(self):
        self.journal.put("paper:" + self.name, self.state())

    def settle_funding(self):
        hour = int(time.time() // 3600)
        if hour > self.last_funding_hour:
            # Missing offline history is uncertain: pause instead of fabricating historical rates.
            if hour - self.last_funding_hour > 1 and self.positions:
                raise TransportError("Paper funding history missing after downtime")
            for mid, position in self.positions.items():
                market = self.markets[mid]
                rate = dec(market.raw["fundingRate"]) if self.name == "arcus" else dec(
                    market.raw["current_funding_rate"]) / 100
                self.cash -= position.quantity * market.multiplier * market.oracle * rate
            self.last_funding_hour = hour
            self.persist()

    def match(self, intent):
        market = self.markets[intent.market_id]
        book = self.books[intent.market_id]
        old = self.feed.orders.get(intent.id, Order(intent.id, "open"))
        if old.terminal:
            return old
        if not book.fresh():
            if intent.maker:
                return old
            return Order(intent.id, "canceled", old.filled, old.average)
        remaining = intent.quantity - old.filled
        position = self.positions.get(market.id)
        if intent.reduce_only:
            if position is None or (intent.buy and position.quantity > 0) or (
                    not intent.buy and position.quantity < 0):
                return Order(intent.id, "rejected")
            remaining = min(remaining, abs(position.quantity))
        filled, value = D(0), D(0)
        for price, size in book.asks if intent.buy else book.bids:
            if (intent.buy and price > intent.price) or (not intent.buy and price < intent.price):
                break
            # Maker queue participation is deliberately conservative and can yield partial fills.
            take = grid(min(remaining - filled, size * (D("0.25") if intent.maker else 1)), market.step)
            filled += take
            value += take * price
            if filled >= remaining:
                break
        if filled:
            average = value / filled
            fees = self.fees_for(market)
            fee = fees[0] if intent.maker else fees[1]
            fee = max(D(0), fee)
            signed = filled if intent.buy else -filled
            self.cash -= signed * market.multiplier * average + value * market.multiplier * fee
            previous = position.quantity if position else D(0)
            after = previous + signed
            if after:
                entry = position.entry if position and previous * signed < 0 else (
                    (abs(previous) * position.entry + filled * average) / abs(after) if position else average)
                self.positions[market.id] = Position(market.id, after, entry, market.mark,
                    abs(after) * market.multiplier * entry, self.modes.get(market.id, "cross"))
            else:
                self.positions.pop(market.id, None)
        total = old.filled + filled
        average = (old.average * old.filled + value) / total if total else D(0)
        state = "filled" if total >= intent.quantity else "open" if intent.maker else "canceled"
        order = Order(intent.id, state, total, average, "paper-" + intent.id)
        self.feed.cache_order(order)
        self.journal.paper_commit(self.name, self.state(), order)
        return order

    async def account(self):
        self.settle_funding()
        for intent in list(self.pending.values()):
            order = self.match(intent)
            if order.terminal:
                self.pending.pop(intent.id, None)
        equity, used = self.cash, D(0)
        for mid, p in self.positions.items():
            m = self.markets[mid]
            p.mark = m.mark
            equity += p.quantity * m.multiplier * m.mark
            used += abs(p.quantity) * m.multiplier * m.mark  # configured 1x margin
        return Account(equity, max(D(0), equity - used), dict(self.positions), set(self.pending),
                       self.maker_fee, self.taker_fee, dec(self.config.paper_capital_per_venue))

    async def prepare_market(self, market, profile):
        self.modes[market.id] = profile["margin_mode"]

    async def submit(self, intent):
        book = self.books[intent.market_id]
        if intent.maker and ((intent.buy and intent.price >= book.asks[0][0]) or (
                not intent.buy and intent.price <= book.bids[0][0])):
            return Order(intent.id, "rejected")
        delay = .02
        if self.name == "lighter":
            delay = (.2 if intent.maker else .3) if self.config.paper_lighter_tier == "standard" else (
                0 if intent.maker else .2)
        await asyncio.sleep(delay)
        self.pending[intent.id] = intent
        order = self.match(intent)
        if order.terminal:
            self.pending.pop(intent.id, None)
        return order

    async def lookup(self, intent, server_id=None):
        if intent.id in self.pending:
            order = self.match(intent)
            if order.terminal:
                self.pending.pop(intent.id, None)
            return order
        return self.feed.orders.get(intent.id, Order(intent.id, "unknown"))

    async def cancel(self, intent):
        # Reconcile possible fills at cancellation time, then cancel the remainder.
        order = self.match(intent)
        if not order.terminal:
            order.state = "canceled"
            self.feed.cache_order(order)
            self.journal.update(order)
        self.pending.pop(intent.id, None)

    async def arm_cancel(self, market):
        pass  # no real resting orders, never call the feed's mutation methods

    async def emergency_book(self, market):
        return await self.feed.emergency_book(market)
