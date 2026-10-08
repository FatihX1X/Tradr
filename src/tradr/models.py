from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import gcd
from typing import Literal

D = Decimal
ZERO = D(0)
BPS = D(10000)
VenueName = Literal["arcus", "lighter"]


def dec(value) -> Decimal:
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite financial value")
    return result


def grid(value: Decimal, step: Decimal, up: bool = False) -> Decimal:
    if step <= 0:
        raise ValueError("Invalid step")
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def integer(value: Decimal, step: Decimal) -> int:
    scaled = value / step
    if scaled != scaled.to_integral_value():
        raise ValueError("Value is not aligned to venue precision")
    return int(scaled)


def common_step(left: Decimal, right: Decimal) -> Decimal:
    scale = 10 ** max(0, -left.as_tuple().exponent, -right.as_tuple().exponent)
    a, b = integer(left * scale, D(1)), integer(right * scale, D(1))
    return D(a * b // gcd(a, b)) / scale


def epoch_seconds(value) -> float:
    n = float(value)
    while n > 100_000_000_000:
        n /= 1000
    return n


@dataclass
class Market:
    venue: VenueName
    id: int
    symbol: str
    category: str
    tick: Decimal
    step: Decimal
    min_size: Decimal
    min_notional: Decimal
    mark: Decimal
    oracle: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    funding_hourly: Decimal
    active: bool = True
    raw: dict = field(default_factory=dict)
    observed: float = 0
    multiplier: Decimal = D(1)

    def price_step(self, price: Decimal) -> Decimal:
        for tier in self.raw.get("tickTiers", []):
            if "upToPrice" not in tier or price <= dec(tier["upToPrice"]):
                return dec(tier["tick"])
        return self.tick


@dataclass
class Book:
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    source_time: float
    received: float = field(default_factory=time.monotonic)
    sequence: int = 0
    valid: bool = True

    def fresh(self, max_age: float = 2) -> bool:
        return (self.valid and bool(self.bids) and bool(self.asks)
                and self.bids[0][0] < self.asks[0][0]
                and time.monotonic() - self.received <= max_age
                and -5 <= time.time() - self.source_time <= max_age)

    def quote(self, buy: bool, quantity: Decimal) -> tuple[Decimal, Decimal]:
        remaining, value, worst = quantity, ZERO, ZERO
        for price, size in self.asks if buy else self.bids:
            taken = min(remaining, size)
            value += taken * price
            remaining -= taken
            worst = price
            if remaining == 0:
                return value / quantity, worst
        raise ValueError("Insufficient executable book depth")


@dataclass
class Position:
    market_id: int
    quantity: Decimal  # signed contracts
    entry: Decimal
    mark: Decimal
    margin: Decimal
    mode: str
    liquidation: Decimal | None = None


@dataclass
class Account:
    equity: Decimal
    free: Decimal
    positions: dict[int, Position]
    open_orders: set[str]
    maker_fee: Decimal
    taker_fee: Decimal
    cash_flow: Decimal | None = None
    received: float = field(default_factory=time.monotonic)


@dataclass
class Intent:
    id: str
    venue: VenueName
    market_id: int
    buy: bool
    quantity: Decimal
    price: Decimal
    reduce_only: bool = False
    maker: bool = False
    expiry_us: int = 0


@dataclass
class Order:
    id: str
    state: str
    filled: Decimal = ZERO
    average: Decimal = ZERO
    server_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state.lower() in {"filled", "canceled", "rejected", "expired", "not-sent"} or (
            self.state.lower().startswith("canceled-"))


@dataclass
class Candidate:
    symbol: str
    long: Market
    short: Market
    quantity: Decimal  # underlying exposure
    long_price: Decimal
    short_price: Decimal
    edge_bps: Decimal
    cost_usd: Decimal
    boost: Decimal = D(1)
    maker_venue: str | None = None
    depth: Decimal = ZERO

    def contracts(self, market: Market) -> Decimal:
        return self.quantity / market.multiplier
