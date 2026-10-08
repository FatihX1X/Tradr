from __future__ import annotations

import asyncio
import json
import os
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..models import Account, Book, D, Intent, Market, Order, Position, dec, epoch_seconds, integer
from .base import TransportError, Venue


def canonical_order(intent: Intent, market: Market, address: str, account_index: int, timestamp: int,
                    cancel=False) -> bytes:
    payload = {"ad": address.lower(), "ai": account_index, "c": intent.id, "ct": timestamp,
               "m": market.id, "op": 2 if cancel else 1, "v": 1}
    if not cancel:
        payload.update(g=intent.expiry_us * 1000, p=integer(intent.price, market.tick),
                       q=integer(intent.quantity, market.step), r=int(intent.reduce_only),
                       s=int(not intent.buy), t=3 if intent.maker else 2)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


class Arcus(Venue):
    name, url, ws_url = "arcus", "https://api.arcus.xyz", "wss://api.arcus.xyz/v1/ws"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.private = None
        self.maker_fee, self.taker_fee = None, None
        self._account_state = None
        self._account_received = 0
        self._order_snapshot = False
        self._open_orders_received = 0
        self._price_epochs = {}
        self.key_expires_at = 0

    def load_key(self):
        value = os.environ["ARCUS_API_PRIVATE_KEY"]
        if value.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(value.encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("Arcus requires an Ed25519 API key")
            self.private = key
        else:
            self.private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value.removeprefix("0x")))
        self.public = self.private.public_key().public_bytes_raw().hex()

    async def open(self):
        await super().open()
        if self.live:
            await self.verify_key()

    async def verify_key(self):
        if self.private is None:
            self.load_key()
        response = await self.request("GET", "/v1/apiKeys", params={"address": self.config.arcus_address})
        matches = [r for r in response["apiKeys"] if r["apiKey"].lower() == self.public
                   and r["address"].lower() == self.config.arcus_address.lower() and r["status"] == "ACTIVE"]
        if len(matches) != 1:
            raise TransportError("Arcus API key is not registered/active for configured address")
        record = matches[0]
        if record.get("allSubaccounts") is not True and record.get("accountIndex") != self.config.arcus_account_index:
            raise TransportError("Arcus API key subaccount scope mismatch")
        self.key_expires_at = int(record["validUntil"])/1000
        if self.key_expires_at and self.key_expires_at <= time.time():
            raise TransportError("Arcus API key expired")

    @staticmethod
    def parse_market(row):
        return Market("arcus", int(row["marketId"]), row["baseAsset"], row["category"],
                      dec(row["tickSize"]), dec(row["stepSize"]), dec(row["minOrderSize"]),
                      dec(row["minOrderNotional"]), dec(row["markPrice"]), dec(row["oraclePrice"]),
                      dec(row["initialMarginFraction"]), dec(row["maintenanceMarginFraction"]),
                      max(abs(dec(row["fundingRate"])), abs(dec(row["nextFundingRate"]))),
                      row["status"] == "ONLINE" and row["type"] == "PERPETUAL", row, time.monotonic())

    async def discover(self):
        response = await self.request("GET", "/v1/markets")
        for row in response["markets"]:
            if row["type"] == "PERPETUAL":
                self.markets[int(row["marketId"])] = self.parse_market(row)
        tiers = (await self.request("GET", "/v1/feetiers"))["tiers"]
        if not tiers:
            raise TransportError("Arcus fee schedule unavailable")
        # Upper bound across all live tiers; rebates are never booked as guaranteed revenue.
        self.maker_fee = max(D(0), max(dec(t["maker_fee_ppm"]) for t in tiers) / D(1_000_000))
        self.taker_fee = max(D(0), max(dec(t["taker_fee_ppm"]) for t in tiers) / D(1_000_000))

    async def subscribe(self, ws):
        await ws.send_json({"type": "subscribe", "channel": "markets"})
        for mid in sorted(self.subscriptions):
            # Production WS currently requires ticker for the id despite some numeric-ID documentation.
            await ws.send_json({"type": "subscribe", "channel": "l2Orderbook",
                                "id": self.markets[mid].raw["marketDisplayName"], "nLevels": 100})
            await ws.send_json({"type": "subscribe", "channel": "oraclePrices",
                                "id": self.markets[mid].raw["marketDisplayName"]})
            await asyncio.sleep(.03)
        if self.live:
            for channel in ("orders", "account"):
                await ws.send_json({"type": "subscribe", "channel": channel,
                                    "id": self.config.arcus_address, "accountIndex": self.config.arcus_account_index})

    @staticmethod
    def parse_order(row):
        state = row.get("status", "unknown")
        if state == "PARTIALLY_FILLED" and row.get("timeInForce") == "IOC":
            state = "canceled"
        filled = row.get("filledSize")
        if filled is None and "originalSize" in row and "remainingSize" in row:
            filled = dec(row["originalSize"]) - dec(row["remainingSize"])
        if filled is None and state.upper() == "FILLED":
            raise TransportError("Filled Arcus order without cumulative fill quantity")
        return Order(str(row.get("clientId") or row.get("orderId", "")), state, dec(filled or 0),
                     dec(row.get("avgFillPrice") or 0), row.get("orderId"))

    def ingest(self, data):
        channel, contents = data.get("channel"), data.get("contents", {})
        if channel == "markets":
            values = contents.get("markets", {})
            for row in values.values() if isinstance(values, dict) else values:
                previous = self.markets.get(int(row["marketId"]))
                if previous:
                    row = previous.raw | row
                m = self.parse_market(row)
                if m.id in self.markets:
                    m.multiplier = self.markets[m.id].multiplier
                    if m.id in self._price_epochs:
                        m.mark, m.oracle, m.observed = previous.mark, previous.oracle, previous.observed
                self.markets[m.id] = m
        elif channel == "oraclePrices":
            if "epoch" not in contents or "prices" not in contents:
                for market in self.markets.values():
                    if market.raw["marketDisplayName"] == data.get("id"):
                        market.observed = 0
                return
            epoch = epoch_seconds(contents["epoch"])
            for row in contents["prices"]:
                m = self.markets.get(int(row["marketId"]))
                if m is None:
                    continue
                if "markEpochNanos" not in row:
                    # Initial oracle subscription snapshots omit the authoritative mark timestamp.
                    # Wait for a stamped update rather than reconnecting or inventing freshness.
                    m.observed = 0
                    continue
                mark_epoch = epoch_seconds(row["markEpochNanos"])
                oracle_epoch = epoch_seconds(row.get("oracleEpoch", contents["epoch"]))
                source_epoch = min(epoch, mark_epoch, oracle_epoch)
                if source_epoch < self._price_epochs.get(m.id, 0):
                    continue
                self._price_epochs[m.id] = source_epoch
                m.oracle, m.mark = dec(row["price"]), dec(row["markPrice"])
                m.observed = time.monotonic() if -5 <= time.time()-source_epoch <= 2 else 0
        elif channel == "l2Orderbook":
            market = next((m for m in self.markets.values()
                           if m.raw["marketDisplayName"] == data.get("id")), None)
            if market is not None:
                if any(key not in contents for key in ("bids", "asks", "lastSequenceId", "timestamp")):
                    # Offline/empty markets may have no stamped snapshot; invalidate only this market.
                    self.books[market.id] = Book([], [], 0, valid=False)
                    return
                seq = int(contents["lastSequenceId"])
                old = self.books.get(market.id)
                if old and old.valid and seq < old.sequence:
                    raise TransportError("Arcus snapshot sequence regressed")
                self.books[market.id] = Book(sorted(self.levels(contents["bids"]), reverse=True),
                                            sorted(self.levels(contents["asks"])),
                                            epoch_seconds(contents["timestamp"]), sequence=seq)
        elif channel == "orders":
            rows = contents.get("orders")
            if rows is None:
                rows = [contents] if "orderId" in contents else []
            if isinstance(rows, dict):
                rows = rows.values()
            for row in rows:
                self.cache_order(self.parse_order(row))
            if data.get("type") == "subscribed":
                self._order_snapshot = True
        elif channel == "account":
            # Account diffs are not complete position snapshots. REST provides the reconciled account.
            self._account_received = time.monotonic()

    def params(self):
        return {"address": self.config.arcus_address, "accountIndex": self.config.arcus_account_index}

    async def account(self):
        raw = await self.request("GET", "/v1/account", params=self.params())
        positions = {}
        for row in raw["positions"].values():
            if dec(row["size"]) != 0:
                positions[int(row["marketId"])] = Position(int(row["marketId"]), dec(row["size"]),
                    dec(row["averageEntryPrice"]), dec(row["markPx"]), dec(row["marginUsed"]),
                    row["marginMode"].lower())
        received = time.monotonic()
        if time.monotonic() - self._open_orders_received > 10:
            open_raw = await self.request("GET", "/v1/openOrders", params=self.params())
            rows = open_raw["orders"]
            if isinstance(rows, dict):
                rows = rows.values()
            for row in rows:
                self.cache_order(self.parse_order(row))
            self._open_orders_received = time.monotonic()
        return Account(dec(raw["equity"]), dec(raw["freeCollateral"]), positions,
                       {cid for cid, order in self.orders.items() if not order.terminal},
                       self.maker_fee, self.taker_fee, dec(raw["netDeposits"]), received)

    def headers(self, payload: bytes, timestamp: int):
        if self.private is None:
            self.load_key()
        return {"X-API-Key": self.public, "X-Timestamp": str(timestamp),
                "X-Signature": self.private.sign(payload).hex()}

    async def legacy(self, action, body):
        timestamp = self.journal.timestamp_ns()
        payload = (str(timestamp) + action + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()
        return await self.request("POST", "/v1/" + action, params=self.params(), body=body,
                                  headers=self.headers(payload, timestamp))

    async def prepare_market(self, market, profile):
        async with self._write_lock:
            await self.legacy("setLeverage", {"address": self.config.arcus_address,
                "accountIndex": self.config.arcus_account_index, "marketId": market.id,
                "leverage": 1, "isolated": profile["margin_mode"] == "isolated"})
        # An ACK is not confirmation. Read back effective configuration.
        raw = await self.request("GET", "/v1/leverages", params=self.params())
        values = raw.get("leverages", raw) if isinstance(raw, dict) else raw
        if isinstance(values, dict):
            values = values.values()
        matches = [r for r in values if int(r["marketId"]) == market.id]
        if len(matches) != 1 or matches[0]["marginMode"].lower() != profile["margin_mode"] or (
                int(matches[0]["leverage"]) != 1):
            raise TransportError("Arcus margin mode not confirmed")

    async def submit(self, intent):
        async with self._write_lock:
            market = self.markets[intent.market_id]
            timestamp = self.journal.timestamp_ns()
            body = {"address": self.config.arcus_address, "accountIndex": self.config.arcus_account_index,
                    "marketId": market.id, "orderSide": "BUY" if intent.buy else "SELL", "orderType": "LIMIT",
                    "quantity": format(intent.quantity, "f"), "price": format(intent.price, "f"),
                    "timeInForce": "ALO" if intent.maker else "IOC", "goodTilTime": str(intent.expiry_us),
                    "clientId": intent.id, "clientTime": str(timestamp), "reduceOnly": intent.reduce_only,
                    "timestamp": str(timestamp)}
            result = await self.request("POST", "/v1/placeOrder", params=self.params(), body=body,
                headers=self.headers(canonical_order(intent, market, self.config.arcus_address,
                                                      self.config.arcus_account_index, timestamp), timestamp))
            return Order(intent.id, "submitted", server_id=result.get("orderId"))

    async def lookup(self, intent, server_id=None):
        cached = self.orders.get(intent.id)
        if cached and cached.terminal:
            return cached
        if server_id:
            raw = await self.request("GET", "/v1/order/" + server_id, params=self.params())
            order = self.parse_order(raw)
            self.cache_order(order)
            return order
        # Covers a lost HTTP response before the server ID was journaled; paginate recent history.
        for path in ("/v1/openOrders", "/v1/orders"):
            cursor = None
            for _ in range(10):
                params = self.params() | {"market": self.markets[intent.market_id].raw["marketDisplayName"]}
                if cursor:
                    params["cursor"] = cursor
                raw = await self.request("GET", path, params=params)
                rows = raw["orders"]
                for row in rows.values() if isinstance(rows, dict) else rows:
                    if str(row.get("clientId")) == intent.id:
                        order = self.parse_order(row)
                        self.cache_order(order)
                        return order
                cursor = raw.get("nextCursor")
                if not cursor:
                    break
        return Order(intent.id, "unknown")

    async def cancel(self, intent):
        async with self._write_lock:
            timestamp = self.journal.timestamp_ns()
            body = self.params() | {"marketId": intent.market_id, "clientId": intent.id,
                                    "clientTime": str(timestamp)}
            await self.request("POST", "/v1/cancelOrder", params=self.params(), body=body,
                headers=self.headers(canonical_order(intent, self.markets[intent.market_id],
                    self.config.arcus_address, self.config.arcus_account_index, timestamp, True), timestamp))

    async def arm_cancel(self, market):
        async with self._write_lock:
            await self.legacy("scheduleCancel", self.params() | {"marketId": market.id,
                              "time": (time.time_ns() // 1000) + 60_000_000})
