from __future__ import annotations

import asyncio
import os
import time

import lighter

from ..models import Account, Book, D, Market, Order, Position, dec, epoch_seconds, integer
from .base import TransportError, Venue


class Lighter(Venue):
    name, url, ws_url = "lighter", "https://api.rh.lighter.xyz", "wss://api.rh.lighter.xyz/stream"
    chain_id = 466324

    @staticmethod
    def fee_rates(limits):
        # Official lighter-go types/txtypes/constants.go: FeeTick = 1_000_000.
        # accountLimits returns the actual account's current fee ticks on this instance.
        return (dec(limits["current_maker_fee_tick"]) / 1_000_000,
                dec(limits["current_taker_fee_tick"]) / 1_000_000)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.signer = None
        self._levels = {}
        self.maker_fee, self.taker_fee = D("0.00012"), D("0.00035")  # RH conservative premium bounds
        self._order_snapshot = False
        self._limits_received = 0

    def load_key(self):
        self.signer = lighter.SignerClient(url=self.url, account_index=self.config.lighter_account_index,
            api_private_keys={self.config.lighter_api_key_index: os.environ["LIGHTER_API_PRIVATE_KEY"]},
            chain_id=self.chain_id)

    def auth(self):
        if self.signer is None:
            self.load_key()
        token, err = self.signer.create_auth_token_with_expiry(api_key_index=self.config.lighter_api_key_index)
        if err:
            raise TransportError("Lighter auth signing failed")
        return token

    async def discover(self):
        response = await self.request("GET", "/api/v1/orderBookDetails")
        for row in response["order_book_details"]:
            if row.get("market_type") != "perp":
                continue
            previous = self.markets.get(row["market_id"])
            self.markets[row["market_id"]] = Market("lighter", row["market_id"], row["symbol"], "UNKNOWN",
                D(10) ** -row["price_decimals"], D(10) ** -row["size_decimals"], dec(row["min_base_amount"]),
                dec(row["min_quote_amount"]), previous.mark if previous else D(0),
                previous.oracle if previous else D(0), dec(row["default_initial_margin_fraction"]) / 10000,
                dec(row["maintenance_margin_fraction"]) / 10000,
                previous.funding_hourly if previous else D(0), row["status"] == "active",
                (previous.raw | row) if previous else row,
                previous.observed if previous else 0, previous.multiplier if previous else D(1))

    async def subscribe(self, ws):
        self._levels = {}
        await ws.send_json({"type": "subscribe", "channel": "market_stats/all"})
        for mid in sorted(self.subscriptions):
            await ws.send_json({"type": "subscribe", "channel": f"order_book/{mid}"})
            await asyncio.sleep(.03)
        if self.live:
            await ws.send_json({"type": "subscribe", "channel": f"account_all_orders/{self.config.lighter_account_index}",
                                "auth": self.auth()})

    @staticmethod
    def parse_order(row):
        filled = dec(row.get("filled_base_amount", "0"))
        quote = dec(row.get("filled_quote_amount", "0"))
        return Order(str(row["client_order_index"]), row["status"], filled,
                     quote / filled if filled else D(0), str(row["order_index"]))

    def ingest(self, data):
        channel = data.get("channel", "")
        if channel.startswith("market_stats:"):
            stats = data["market_stats"]
            rows = [stats] if "market_id" in stats else stats.values()
            for row in rows:
                m = self.markets.get(int(row["market_id"]))
                if m:
                    m.mark, m.oracle = dec(row["mark_price"]), dec(row["index_price"])
                    # Lighter market_stats rates are percentages, Arcus rates are fractions.
                    m.funding_hourly = max(abs(dec(row["current_funding_rate"])),
                                           abs(dec(row["funding_rate"]))) / 100
                    m.observed = time.monotonic()
                    m.raw.update(row)
                    if time.time() - epoch_seconds(data["timestamp"]) > 2:
                        m.observed = 0
        elif channel.startswith("order_book:"):
            mid = int(channel.split(":")[1])
            payload = data["order_book"]
            snapshot = data["type"].startswith("subscribed/")
            if snapshot:
                self._levels[mid] = {"bids": {}, "asks": {}}
            old = self.books.get(mid)
            if not snapshot and (old is None or not old.valid or int(payload["begin_nonce"]) != old.sequence):
                if old:
                    old.valid = False
                raise TransportError("Lighter orderbook nonce chain broken")
            levels = self._levels[mid]
            for side in ("bids", "asks"):
                for price, size in self.levels(payload[side]):
                    if price <= 0 or size < 0:
                        raise TransportError("Invalid Lighter book level")
                    if size == 0:
                        levels[side].pop(price, None)
                    else:
                        levels[side][price] = size
                if len(levels[side]) > 50000:
                    raise TransportError("Orderbook memory bound exceeded")
            self.books[mid] = Book(sorted(levels["bids"].items(), reverse=True),
                                   sorted(levels["asks"].items()), epoch_seconds(data["timestamp"]),
                                   sequence=int(payload["nonce"]))
        elif channel.startswith("account_all_orders:"):
            for rows in data["orders"].values():
                for row in rows:
                    self.cache_order(self.parse_order(row))
            if data["type"].startswith("subscribed/"):
                self._order_snapshot = True

    async def account(self):
        token = self.auth()
        if time.monotonic() - self._limits_received > 60:
            limits = await self.request("GET", "/api/v1/accountLimits",
                                       params={"account_index": self.config.lighter_account_index},
                                       headers={"Authorization": token})
            self.maker_fee, self.taker_fee = self.fee_rates(limits)
            self._limits_received = time.monotonic()
        raw = await self.request("GET", "/api/v1/account",
                                params={"by": "index", "value": str(self.config.lighter_account_index)})
        accounts = raw["accounts"]
        if len(accounts) != 1 or int(accounts[0]["index"]) != self.config.lighter_account_index:
            raise TransportError("Lighter account identity mismatch")
        row, positions, pnl = accounts[0], {}, D(0)
        for r in row["positions"]:
            pnl += dec(r["unrealized_pnl"])
            quantity = dec(r["position"]) * int(r["sign"])
            if quantity:
                m = self.markets[int(r["market_id"])]
                positions[m.id] = Position(m.id, quantity, dec(r["avg_entry_price"]), m.mark,
                    dec(r["allocated_margin"]), "isolated" if int(r["margin_mode"]) else "cross",
                    dec(r["liquidation_price"]) if dec(r["liquidation_price"]) > 0 else None)
        # The order snapshot must be received before treating the account as clean.
        if not self._order_snapshot:
            active = await self.request("GET", "/api/v1/accountActiveOrders",
                params={"account_index": self.config.lighter_account_index}, headers={"Authorization": token})
            for r in active["orders"]:
                self.cache_order(self.parse_order(r))
            self._order_snapshot = True
        open_orders = {cid for cid, o in self.orders.items() if not o.terminal}
        # Do not mix the portfolio/spot total_asset_value with perp collateral a second time.
        return Account(dec(row["collateral"]) + pnl, dec(row["available_balance"]), positions,
                       open_orders, self.maker_fee, self.taker_fee)

    async def _signed(self, method, *args, **kwargs):
        async with self._write_lock:
            if self.signer is None:
                self.load_key()
            response = await self.request("GET", "/api/v1/nextNonce",
                params={"account_index": self.config.lighter_account_index,
                        "api_key_index": self.config.lighter_api_key_index})
            nonce = int(response["nonce"])
            tx_type, tx_info, tx_hash, error = getattr(self.signer, method)(
                *args, **kwargs, nonce=nonce, api_key_index=self.config.lighter_api_key_index)
            if error:
                raise TransportError("Lighter transaction signing failed")
            # Persist unsigned public identifiers, never tx_info/auth/key material.
            self.journal.event("lighter_tx", {"nonce": nonce, "hash": tx_hash, "type": tx_type})
            return await self.request("POST", "/api/v1/sendTx",
                                      form={"tx_type": str(tx_type), "tx_info": tx_info})

    async def prepare_market(self, market, profile):
        mode = 1 if profile["margin_mode"] == "isolated" else 0
        # 10000 / 10000 = 100% initial margin, a 1x venue leverage setting.
        await self._signed("sign_update_leverage", market.id, 10000, mode)
        for _ in range(3):
            raw = await self.request("GET", "/api/v1/account", params={"by": "index",
                                            "value": str(self.config.lighter_account_index)})
            matches = [r for r in raw["accounts"][0]["positions"] if int(r["market_id"]) == market.id]
            if matches and int(matches[0]["margin_mode"]) == mode and dec(
                    matches[0]["initial_margin_fraction"]) == 10000:
                return
            await asyncio.sleep(.2)
        raise TransportError("Lighter 1x leverage and margin mode not confirmed")

    async def submit(self, intent):
        m = self.markets[intent.market_id]
        await self._signed("sign_create_order", market_index=m.id, client_order_index=int(intent.id),
            base_amount=integer(intent.quantity, m.step), price=integer(intent.price, m.tick),
            is_ask=not intent.buy, order_type=0, time_in_force=2 if intent.maker else 0,
            reduce_only=intent.reduce_only, order_expiry=int(time.time()*1000) + 7*86400000 if intent.maker else 0,
            self_trade_behavior_mode=2, self_trade_equality_mode=1)
        return Order(intent.id, "submitted")

    async def lookup(self, intent, server_id=None):
        cached = self.orders.get(intent.id)
        if cached and cached.terminal:
            return cached
        token = self.auth()
        for path in ("accountActiveOrders", "accountInactiveOrders"):
            cursor = None
            for _ in range(10):
                params = {"account_index": self.config.lighter_account_index, "market_id": intent.market_id}
                if path == "accountInactiveOrders":
                    params["limit"] = 100
                    if cursor:
                        params["cursor"] = cursor
                raw = await self.request("GET", "/api/v1/" + path, params=params,
                                         headers={"Authorization": token})
                for row in raw["orders"]:
                    if str(row["client_order_index"]) == intent.id:
                        order = self.parse_order(row)
                        self.cache_order(order)
                        return order
                cursor = raw.get("next_cursor")
                if path == "accountActiveOrders" or not cursor:
                    break
        return Order(intent.id, "unknown")

    async def cancel(self, intent):
        await self._signed("sign_cancel_order", intent.market_id, int(intent.id))

    async def arm_cancel(self, market):
        await self._signed("sign_cancel_all_orders", 1, int(time.time()*1000) + 60000,
                           cancel_all_market_index=market.id)

    async def close(self):
        await super().close()
        if self.signer:
            await self.signer.close()
