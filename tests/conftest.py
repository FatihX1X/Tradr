import time
from datetime import datetime, timedelta, timezone

import pytest

from tradr.config import Config
from tradr.models import Account, Book, D, Market
from tradr.policy import Policies
from tradr.storage import Journal


@pytest.fixture
def markets():
    return [Market(venue, mid, "BTC", "CRYPTO", D("0.1"), D("0.0001"), D("0.0001"),
                   D(5), D(100), D(100), D("0.1"), D("0.05"), D(0),
                   raw={"fundingRate": "0", "current_funding_rate": "0"}, observed=time.monotonic())
            for venue, mid in (("arcus", 1), ("lighter", 0))]


@pytest.fixture
def profile():
    now = datetime.now(timezone.utc)
    economics = {"multiplier": "1", "oracle": "BTC-USD-index", "dividends": "none",
                 "splits": "none", "roll": "none", "margin_mode": "cross"}
    return {"approved": True, "arcus_symbol": "BTC", "lighter_symbol": "BTC", "category": "CRYPTO",
            "underlying": "Bitcoin", "quote_currency": "USD", "settlement": "linear-perpetual",
            "reviewed_at": (now-timedelta(hours=1)).isoformat(), "valid_until": (now+timedelta(days=1)).isoformat(),
            "sources": ["https://docs.arcus.xyz/concepts/perpetuals/overview.md",
                        "https://docs.lighter.xyz/trading/contract-specifications.md"],
            "arcus": dict(economics), "lighter": dict(economics)}


@pytest.fixture
def policies(tmp_path, profile):
    p = Policies(str(tmp_path/"profiles.json"), str(tmp_path/"boosts.json"))
    p.profiles = [profile]
    return p


@pytest.fixture
def config(tmp_path):
    return Config(state_dir=str(tmp_path), daily_loss_limit_usd="1", health_port=0)


@pytest.fixture
def journal(tmp_path):
    j = Journal(str(tmp_path), "paper")
    yield j
    j.close()


def fresh_book(bid="99.9", ask="100.1", size="10", sequence=1):
    return Book([(D(bid), D(size))], [(D(ask), D(size))], time.time(), sequence=sequence)


def account(equity="100", positions=None):
    return Account(D(equity), D(equity), positions or {}, set(), D(0), D(0))
