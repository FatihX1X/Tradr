from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod

import aiohttp

from ..models import Book, Intent, Order, dec


class TransportError(RuntimeError):
    """No remote body is included: it may contain authentication material."""


class Venue(ABC):
    name: str
    url: str
    ws_url: str

    def __init__(self, config, journal, live=False):
        self.config, self.journal, self.live = config, journal, live
        self.markets, self.books, self.orders = {}, {}, {}
        self.session = None
        self.task = None
        self.subscriptions = set()
        self.connected = False
        self.metadata_ready = False
        self.last_error = None
        self._http_lock = asyncio.Lock()
        self._next_read = 0
        self._blocked_until = 0
        self._write_lock = asyncio.Lock()

    async def open(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4),
                                             headers={"User-Agent": "Tradr/0.1.0"})
        await self.discover()
        self.metadata_ready = True

    async def request(self, method, path, *, params=None, body=None, headers=None, form=None):
        if method == "GET":
            async with self._http_lock:
                await asyncio.sleep(max(0, self._next_read - time.monotonic()))
                self._next_read = time.monotonic() + 1.25  # <=48 REST reads/minute per venue
        if time.monotonic() < self._blocked_until:
            raise TransportError(f"{self.name}: rate-limit cooldown")
        try:
            async with self.session.request(method, self.url + path, params=params, json=body,
                                            data=form, headers=headers) as response:
                if response.status == 429:
                    delay = max(1, float(response.headers.get("Retry-After", "5")))
                    self._blocked_until = time.monotonic() + delay
                    raise TransportError(f"{self.name}: HTTP 429 (no automatic write retry)")
                if response.status >= 400:
                    raise TransportError(f"{self.name}: HTTP {response.status}")
                data = await response.json()
                if isinstance(data, dict) and data.get("code", 200) not in (0, 200):
                    raise TransportError(f"{self.name}: API request rejected")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise TransportError(f"{self.name}: {type(exc).__name__}; reconcile before retry") from None

    async def stream(self, ids):
        self.subscriptions = set(ids)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = asyncio.create_task(self._stream_loop(), name=f"{self.name}-feed")

    async def _stream_loop(self):
        delay = 1
        while True:
            try:
                self.connected = False
                for book in self.books.values():
                    book.valid = False
                async with self.session.ws_connect(self.ws_url, heartbeat=15, max_msg_size=8*1024*1024) as ws:
                    self.ws = ws
                    await self.subscribe(ws)
                    self.connected, self.last_error, delay = True, None, 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == "ping":
                                await ws.send_json({"type": "pong"})
                                continue
                            if data.get("type") == "error":
                                raise TransportError("WebSocket subscription rejected")
                            self.ingest(data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                if isinstance(exc, KeyError) and str(exc.args[0]).replace("_", "").isalnum():
                    self.last_error += ": missing " + str(exc.args[0])
            finally:
                self.connected = False
                for book in self.books.values():
                    book.valid = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def cache_order(self, order):
        old = self.orders.get(order.id)
        if old and (order.filled < old.filled or old.terminal and not order.terminal):
            return
        self.orders[order.id] = order

    def levels(self, values):
        levels = []
        for value in values:
            if isinstance(value, dict):
                price, size = dec(value["price"]), dec(value.get("size", value.get("quantity", "0")))
            else:
                price, size = dec(value[0]), dec(value[1])
            if price <= 0 or size < 0:
                raise TransportError("Invalid book level")
            levels.append((price, size))
        return levels

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.session:
            await self.session.close()

    @abstractmethod
    async def discover(self): ...

    @abstractmethod
    async def subscribe(self, ws): ...

    @abstractmethod
    def ingest(self, data): ...

    @abstractmethod
    async def account(self): ...

    @abstractmethod
    async def prepare_market(self, market, profile): ...

    @abstractmethod
    async def submit(self, intent: Intent) -> Order: ...

    @abstractmethod
    async def lookup(self, intent: Intent, server_id=None) -> Order: ...

    @abstractmethod
    async def cancel(self, intent: Intent): ...

    async def arm_cancel(self, market):
        raise TransportError("Maker dead-man switch unavailable")

    async def emergency_book(self, market) -> Book:
        book = self.books.get(market.id)
        if book is None or not book.fresh():
            raise TransportError("No fresh executable book for recovery")
        return book

    async def add_market(self, mid):
        if mid in self.subscriptions:
            return
        self.subscriptions.add(mid)
        if self.connected:
            if self.name == "arcus":
                message = {"type": "subscribe", "channel": "l2Orderbook",
                           "id": self.markets[mid].raw["marketDisplayName"], "nLevels": 100}
            else:
                message = {"type": "subscribe", "channel": f"order_book/{mid}"}
            await self.ws.send_json(message)
            if self.name == "arcus":
                await self.ws.send_json({"type": "subscribe", "channel": "oraclePrices",
                                         "id": self.markets[mid].raw["marketDisplayName"]})
