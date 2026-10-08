from __future__ import annotations

import asyncio
import time

from .models import BPS, D, Intent, Order, dec, grid, common_step
from .strategy import Scanner, account_risk


class Engine:
    def __init__(self, config, policies, journal, venues):
        self.config, self.policies, self.journal, self.venues = config, policies, journal, venues
        self.scanner = Scanner(config, policies)
        self.accounts = {}
        self.phase, self.reason = "starting", ""
        self.halted = False
        self._last_accounts = 0
        self._cooldown = journal.get("cooldown_until", 0)

    async def refresh_accounts(self, force=False):
        if force or not self.accounts or any(time.monotonic() - a.received >= 1.5
                                            for a in self.accounts.values()):
            # Independent venue reads; both must succeed before the account pair is trusted.
            results = await asyncio.gather(*(v.account() for v in self.venues.values()), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise RuntimeError("Account reconciliation failed: " + type(failures[0]).__name__)
            self.accounts = dict(zip(self.venues, results))
            self._last_accounts = time.monotonic()
        return self.accounts

    def loss(self):
        equity = sum(a.equity for a in self.accounts.values())
        flows = [a.cash_flow for a in self.accounts.values()]
        cash_flow = sum(flows) if all(f is not None for f in flows) else None
        return self.journal.daily_loss(equity, cash_flow)

    def guard(self):
        if self.config.daily_loss_limit_usd is not None and self.loss() >= dec(self.config.daily_loss_limit_usd):
            return "Daily loss limit reached"
        for name, account in self.accounts.items():
            if not getattr(getattr(self.venues[name], "feed", self.venues[name]), "metadata_ready", True):
                return name + ": venue metadata/fee refresh unavailable"
            reason = account_risk(account, self.venues[name].markets)
            if reason:
                return name + ": " + reason
        # Unknown/outside orders or positions always stop entries and never get attributed to the bot.
        owned = {intent.id for intent, _ in self.journal.rows()}
        for name, account in self.accounts.items():
            if account.open_orders - owned:
                return name + ": external order detected"
            hedge = self.journal.get("hedge")
            expected = {leg["market_id"] for leg in hedge["legs"] if leg["venue"] == name} if hedge else set()
            if set(account.positions) - expected:
                return name + ": external position detected"
            if hedge and hedge.get("state") == "hedged":
                recorded = hedge.get("quantities", {}).get(name, {})
                for mid in expected:
                    position = account.positions.get(mid)
                    actual = position.quantity if position else D(0)
                    if actual != dec(recorded.get(str(mid), "0")):
                        return name + ": position size diverged from hedge journal"
        return None

    def health(self):
        hedge = self.journal.get("hedge")
        exposures = {}
        for name, account in self.accounts.items():
            exposures[name] = sum(p.quantity * self.venues[name].markets[mid].multiplier
                                  for mid, p in account.positions.items())
        return {"phase": self.phase, "reason": self.reason, "timestamp": time.time(), "hedge": hedge,
                "accounts": {name: {"equity": str(a.equity), "free_collateral": str(a.free),
                                     "positions": {str(mid): str(p.quantity) for mid, p in a.positions.items()}}
                             for name, a in self.accounts.items()},
                "net_exposure": str(sum(exposures.values())),
                "daily_loss_usd": str(self.loss()) if self.accounts else None,
                "excluded": self.scanner.reasons,
                "feeds": {name: {"connected": getattr(getattr(v, "feed", v), "connected", False),
                                 "error": getattr(getattr(v, "feed", v), "last_error", None)}
                          for name, v in self.venues.items()}}

    def publish(self):
        self.journal.put("health", self.health())

    def intent(self, market, buy, quantity, *, reduce=False, maker=False, emergency=False):
        book = self.venues[market.venue].books[market.id]
        if not book.fresh():
            raise RuntimeError("Cannot construct an order from stale market data")
        if maker:
            price = book.bids[0][0] if buy else book.asks[0][0]
        else:
            _, worst = book.quote(buy, quantity)
            best = book.asks[0][0] if buy else book.bids[0][0]
            bps = dec(self.config.emergency_slippage_bps if emergency else self.config.slippage_bps)
            # Bound relative to best executable quote; don't add a buffer after walking beyond the cap.
            price = best * (1 + bps/BPS if buy else 1 - bps/BPS)
            if buy and worst > price or not buy and worst < price:
                raise RuntimeError("Insufficient depth inside slippage cap")
        price = grid(price, market.price_step(price), up=not buy)  # buy floor, sell ceiling
        quantity = grid(quantity, market.step)
        if quantity <= 0:
            raise RuntimeError("Order quantity below precision")
        if not reduce and (quantity < market.min_size or quantity*market.multiplier*price < market.min_notional):
            raise RuntimeError("Order below minimum size or notional")
        if market.raw.get("upperTradingBound") is not None:
            if price > dec(market.raw["upperTradingBound"]) or price < dec(market.raw["lowerTradingBound"]):
                raise RuntimeError("Order would cross an off-hours trading band")
        return Intent(self.journal.next_id(), market.venue, market.id, buy, quantity, price, reduce, maker,
                      time.time_ns() // 1000 + 40*86400*1_000_000)

    async def send(self, intent, deadline=None):
        self.journal.prepare(intent)
        self.journal.update(Order(intent.id, "sending"))
        try:
            budget = self.config.hedge_timeout_seconds if deadline is None else max(.01, deadline-time.monotonic())
            async with asyncio.timeout(budget):
                order = await self.venues[intent.venue].submit(intent)
        except (Exception, asyncio.CancelledError) as exc:
            # Includes local cancellation during transmission: delivery is uncertain, never resend.
            order = Order(intent.id, "unknown")
            self.journal.event("uncertain_send", {"id": intent.id, "error": type(exc).__name__})
            self.journal.update(order)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return order
        self.journal.update(order)
        return order

    async def settle(self, intent, order=None, timeout=None):
        order = order or Order(intent.id, "unknown")
        if order.terminal:
            return order
        venue = self.venues[intent.venue]
        end = time.monotonic() + (timeout or self.config.hedge_timeout_seconds)
        while time.monotonic() < end:
            cached = getattr(getattr(venue, "feed", venue), "orders", {}).get(intent.id)
            if cached:
                order = cached
                self.journal.update(order)
                if order.terminal or intent.maker:
                    return order
            try:
                async with asyncio.timeout(max(.01, end-time.monotonic())):
                    order = await venue.lookup(intent, order.server_id)
                self.journal.update(order)
                if order.state.upper() == "PARTIALLY_FILLED" and not intent.maker:
                    order.state = "canceled"
                    self.journal.update(order)
                if order.terminal or intent.maker and order.state != "unknown":
                    return order
            except Exception:
                pass
            await asyncio.sleep(.1)
        return order

    async def cancel_entries(self):
        uncertain = False
        for intent, order in self.journal.rows():
            if order.terminal:
                continue
            # A crash before send is indistinguishable from a crash after delivery. Resolve, never resend.
            try:
                order = await self.settle(intent, order)
                if not order.terminal:
                    await self.venues[intent.venue].cancel(intent)
                    order = await self.settle(intent, order)
                if not order.terminal:
                    uncertain = True
            except Exception as exc:
                uncertain = True
                self.journal.event("cancel_unresolved", {"id": intent.id, "error": type(exc).__name__})
        return not uncertain

    async def open(self, candidate):
        self.phase = "opening"
        hedge = {"symbol": candidate.symbol, "opened_at": time.time(), "state": "opening",
                 "target_exposure": str(candidate.quantity), "boost": str(candidate.boost),
                 "legs": [{"venue": m.venue, "market_id": m.id, "buy": buy}
                          for m, buy in ((candidate.long, True), (candidate.short, False))],
                 "entry_equity": str(sum(a.equity for a in self.accounts.values()))}
        self.journal.put("hedge", hedge)
        profile, reason = self.policies.match(self.venues["arcus"].markets[
            candidate.long.id if candidate.long.venue == "arcus" else candidate.short.id],
            self.venues["lighter"].markets[candidate.long.id if candidate.long.venue == "lighter" else candidate.short.id])
        if profile is None:
            raise RuntimeError(reason)
        try:
            for market in (candidate.long, candidate.short):
                expiry = getattr(self.venues[market.venue], "key_expires_at", 0)
                if expiry and expiry - time.time() < self.config.max_hold_seconds+60:
                    raise RuntimeError("API key lifetime does not cover holding and recovery window")
                await self.venues[market.venue].prepare_market(market, profile[market.venue])
            # Margin setup can take seconds; re-evaluate the real opportunity immediately before sending.
            await self.refresh_accounts(True)
            candidates = self.scanner.scan(self.venues["arcus"].markets, self.venues["lighter"].markets,
                {n: v.books for n, v in self.venues.items()}, self.accounts)
            fresh = next((c for c in candidates if c.long.venue == candidate.long.venue
                          and c.long.id == candidate.long.id and c.short.id == candidate.short.id
                          and c.quantity == candidate.quantity and c.maker_venue == candidate.maker_venue), None)
            if self.guard() or fresh is None:
                self.journal.put("hedge", None)
                return
            candidate = fresh
            if candidate.maker_venue:
                await self.maker_entry(candidate)
            else:
                intents = [self.intent(m, buy, candidate.contracts(m))
                           for m, buy in ((candidate.long, True), (candidate.short, False))]
                deadline = time.monotonic()+self.config.hedge_timeout_seconds
                orders = await asyncio.gather(*(self.send(i, deadline) for i in intents))
                orders = await asyncio.gather(*(self.settle(i, o, max(.01, deadline-time.monotonic()))
                                               for i, o in zip(intents, orders)))
                if not all(o.terminal for o in orders):
                    raise RuntimeError("Entry execution remains uncertain")
            if not await self.balance():
                raise RuntimeError("Unable to establish matching exposure")
            await self.refresh_accounts(True)
            if not any(a.positions for a in self.accounts.values()):
                self.journal.put("hedge", None)
                self._cooldown = time.time() + 60
                return
            hedge["state"] = "hedged"
            hedge["quantities"] = {name: {str(mid): str(p.quantity) for mid, p in a.positions.items()}
                                   for name, a in self.accounts.items()}
            self.journal.put("hedge", hedge)
            self.phase = "holding"
            self.journal.event("hedged", {"symbol": candidate.symbol, "boost": str(candidate.boost)})
        except BaseException:
            self.phase, self.reason = "recovery", "Entry or hedge confirmation failed"
            await self.flatten()
            self.halted = True
            raise

    async def maker_entry(self, candidate):
        maker = candidate.long if candidate.maker_venue == candidate.long.venue else candidate.short
        hedge = candidate.short if maker is candidate.long else candidate.long
        maker_buy = maker is candidate.long
        venue = self.venues[maker.venue]
        await venue.arm_cancel(maker)
        intent = self.intent(maker, maker_buy, candidate.contracts(maker), maker=True)
        order = await self.send(intent)
        deadline, refresh_at, hedged = time.monotonic() + self.config.maker_wait_seconds, time.monotonic()+20, D(0)
        first_unhedged = None
        try:
            while time.monotonic() < deadline:
                order = await self.settle(intent, order)
                difference = order.filled * maker.multiplier - hedged
                if difference > 0:
                    first_unhedged = first_unhedged or time.monotonic()
                    quantity = grid(difference / hedge.multiplier, hedge.step)
                    if quantity < hedge.min_size or quantity*hedge.mark*hedge.multiplier < hedge.min_notional:
                        raise RuntimeError("Maker partial fill cannot meet hedge venue minimum")
                    hedge_intent = self.intent(hedge, not maker_buy, quantity)
                    hedge_deadline = first_unhedged+self.config.hedge_timeout_seconds
                    sent = await self.send(hedge_intent, hedge_deadline)
                    hedge_order = await self.settle(hedge_intent, sent, max(.01, hedge_deadline-time.monotonic()))
                    hedged += hedge_order.filled * hedge.multiplier
                    if not hedge_order.terminal or hedged != order.filled * maker.multiplier:
                        raise RuntimeError("Maker hedge did not fully execute")
                    if time.monotonic() - first_unhedged > self.config.hedge_timeout_seconds:
                        raise RuntimeError("Maker exposure deadline exceeded")
                    first_unhedged = None
                if order.terminal:
                    break
                if self.journal.get("control") in ("stop", "flatten"):
                    raise RuntimeError("User requested stop during maker wait")
                if time.monotonic() >= refresh_at:
                    await venue.arm_cancel(maker)
                    refresh_at = time.monotonic()+20
                await self.refresh_accounts()
                guard_reason = self.guard()
                if guard_reason:
                    raise RuntimeError("Risk guard during maker wait: " + guard_reason)
                self.publish()
                await asyncio.sleep(.1)
        finally:
            if not order.terminal:
                await venue.cancel(intent)
                order = await self.settle(intent, order)
                if not order.terminal:
                    raise RuntimeError("Maker cancel not confirmed; possible late fill")
        # Cancellation may have raced another fill. balance() reads actual positions and trims surplus.

    async def balance(self):
        await self.refresh_accounts(True)
        hedge = self.journal.get("hedge")
        legs = []
        for leg in hedge["legs"]:
            venue = self.venues[leg["venue"]]
            m = venue.markets[leg["market_id"]]
            p = self.accounts[leg["venue"]].positions.get(m.id)
            signed = p.quantity if p else D(0)
            if signed and (signed > 0) != leg["buy"]:
                raise RuntimeError("Position side disagrees with bot journal")
            legs.append((m, signed))
        step = common_step(legs[0][0].step*legs[0][0].multiplier, legs[1][0].step*legs[1][0].multiplier)
        target = grid(min(abs(q)*m.multiplier for m, q in legs), step)
        for market, signed in legs:
            excess = abs(signed) - target/market.multiplier
            if excess > 0:
                intent = self.intent(market, signed < 0, excess, reduce=True, emergency=True)
                order = await self.settle(intent, await self.send(intent))
                if not order.terminal or order.filled != intent.quantity:
                    return False
        await self.refresh_accounts(True)
        quantities = [abs(self.accounts[m.venue].positions[m.id].quantity)*m.multiplier
                      if m.id in self.accounts[m.venue].positions else D(0) for m, _ in legs]
        return quantities[0] == quantities[1] == target

    async def flatten(self):
        self.phase = "closing"
        hedge = self.journal.get("hedge")
        if hedge is None:
            confirmed = await self.cancel_entries()
            self.phase = "flat" if confirmed else "recovery"
            return confirmed
        confirmed_cancels = await self.cancel_entries()
        for _ in range(3):
            try:
                await self.refresh_accounts(True)
                remaining = []
                for leg in hedge["legs"]:
                    venue = self.venues[leg["venue"]]
                    market = venue.markets[leg["market_id"]]
                    position = self.accounts[leg["venue"]].positions.get(market.id)
                    if position and position.quantity:
                        recorded = hedge.get("quantities", {}).get(leg["venue"], {}).get(str(market.id))
                        if recorded is not None:
                            owned = dec(recorded)
                            if position.quantity * owned <= 0 or abs(position.quantity) > abs(owned):
                                raise RuntimeError("Position exceeds journal-owned exposure; manual reconciliation required")
                        # Only journal-owned markets are closed; foreign positions remain untouched.
                        await venue.emergency_book(market)
                        intent = self.intent(market, position.quantity < 0, abs(position.quantity),
                                             reduce=True, emergency=True)
                        remaining.append(intent)
                if not remaining:
                    if not confirmed_cancels:
                        break  # late entry fills still possible; cannot declare closed
                    self.journal.put("hedge", None)
                    self._cooldown = time.time()+60
                    self.journal.put("cooldown_until", self._cooldown)
                    self.journal.event("closed", {"symbol": hedge["symbol"]})
                    self.phase = "flat"
                    return True
                orders = await asyncio.gather(*(self.send(i) for i in remaining))
                settled = await asyncio.gather(*(self.settle(i, o) for i, o in zip(remaining, orders)))
                if not all(o.terminal for o in settled):
                    break  # a pending reduce-only order must be reconciled before issuing another
            except Exception as exc:
                self.journal.event("close_failed", {"error": type(exc).__name__})
                break
        self.phase, self.reason, self.halted = "recovery", "Unresolved order/position; operator action required", True
        self.journal.event("recovery_required", {"symbol": hedge["symbol"]})
        return False

    def exit_due(self):
        hedge = self.journal.get("hedge")
        if not hedge:
            return False
        if self.guard():
            return True
        arcus_leg = next(leg for leg in hedge["legs"] if leg["venue"] == "arcus")
        lighter_leg = next(leg for leg in hedge["legs"] if leg["venue"] == "lighter")
        if self.policies.match(self.venues["arcus"].markets[arcus_leg["market_id"]],
                               self.venues["lighter"].markets[lighter_leg["market_id"]])[0] is None:
            return True
        if time.time() - hedge["opened_at"] >= self.config.max_hold_seconds:
            return True
        # Current combined equity already includes entry fees and settled funding.
        equity = sum(a.equity for a in self.accounts.values())
        reserve = D(0)
        for leg in hedge["legs"]:
            market = self.venues[leg["venue"]].markets[leg["market_id"]]
            profile = next((p for p in self.policies.profiles if p.get("arcus_symbol") == hedge["symbol"]
                            or p.get("lighter_symbol") == hedge["symbol"]), None)
            if profile is None:
                return True
            position = self.accounts[leg["venue"]].positions.get(market.id)
            if not position:
                return True
            book = self.venues[leg["venue"]].books.get(market.id)
            if not book or not book.fresh():
                return False  # freeze entries; stale books cannot price a discretionary exit
            price, _ = book.quote(position.quantity < 0, abs(position.quantity))
            value = abs(position.quantity)*market.multiplier*price
            reserve += value*(max(D(0), self.accounts[leg["venue"]].taker_fee)
                             + dec(self.config.slippage_bps)/BPS)
            reserve += abs(position.quantity)*market.multiplier*abs(price-market.mark)
        target = dec(hedge["target_exposure"])*max(
            self.venues[leg["venue"]].markets[leg["market_id"]].mark for leg in hedge["legs"])*dec(
                self.config.min_edge_bps)/BPS
        return equity - dec(hedge["entry_equity"]) - reserve >= target

    async def run(self, seconds=None, flatten_only=False):
        await self.refresh_accounts(True)
        self.loss()  # establish/preserve the Istanbul daily baseline before any entry
        if self.journal.get("hedge") or any(not order.terminal for _, order in self.journal.rows()):
            self.reason = "Restart recovery; no automatic new entries"
            await self.flatten()
            self.publish()
            return
        if flatten_only:
            self.phase = "flat"
            self.publish()
            return
        start = time.monotonic()
        self.phase = "scanning"
        while not self.halted:
            if seconds is not None and time.monotonic()-start >= seconds:
                self.reason = "Requested runtime complete"
                await self.flatten()
                break
            control = self.journal.get("control")
            if control in ("stop", "flatten"):
                self.reason = "User requested stop/flatten"
                await self.flatten()
                self.journal.put("control", None)
                break
            try:
                await self.refresh_accounts()
                risk = self.guard()
                if risk:
                    self.reason, self.halted = risk, True
                    await self.flatten()
                    break
                if self.journal.get("hedge"):
                    if self.exit_due():
                        await self.flatten()
                    else:
                        self.phase = "holding"
                elif time.time() >= self._cooldown:
                    self.policies.reload()
                    candidates = self.scanner.scan(self.venues["arcus"].markets, self.venues["lighter"].markets,
                        {n: v.books for n, v in self.venues.items()}, self.accounts)
                    self.phase = "scanning"
                    if candidates:
                        await self.open(candidates[0])
                self.publish()
            except Exception as exc:
                self.reason = type(exc).__name__
                if self.journal.get("hedge"):
                    await self.flatten()
                    self.halted = True
                else:
                    self.phase = "paused"
                self.journal.event("guarded_error", {"error": type(exc).__name__})
                self.publish()
            await asyncio.sleep(.25)
        self.publish()
