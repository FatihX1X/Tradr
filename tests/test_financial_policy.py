import copy
import time
from datetime import datetime, timedelta, timezone

import pytest

from tradr.models import D, common_step, dec, grid, integer
from tradr.strategy import Scanner, entry_risk, liquidation_distance
from tradr.models import Position
from conftest import account, fresh_book


def test_exact_integer_precision_and_common_multiplier_grid():
    assert integer(D("0.0002"), D("0.00001")) == 20
    assert common_step(D("0.003"), D("0.002")) == D("0.006")
    assert grid(D("0.017"), D("0.006")) == D("0.012")
    with pytest.raises(ValueError):
        integer(D("0.00011"), D("0.0001"))
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            dec(bad)


def test_unknown_or_incompatible_contracts_never_trade(policies, markets):
    assert policies.match(*markets)[1] == "eligible"
    policies.profiles[0]["lighter"]["dividends"] = "unknown"
    assert "dividends" in policies.match(*markets)[1]
    policies.profiles.clear()
    assert policies.match(*markets)[0] is None


@pytest.mark.parametrize("field", ["oracle", "dividends", "splits", "roll"])
def test_all_economic_differences_block(policies, markets, field):
    policies.profiles[0]["lighter"][field] = "different"
    assert policies.match(*markets)[0] is None


def test_rwa_requires_events_and_offhours_bands(policies, markets):
    a, b = markets
    a.category = "EQUITIES"
    p = policies.profiles[0]
    p["category"] = "EQUITIES"
    assert policies.match(a, b)[0] is None
    now = datetime.now(timezone.utc)
    p["event_state"] = {"checked_at": (now-timedelta(minutes=1)).isoformat(),
        "valid_until": (now+timedelta(hours=5)).isoformat(), "sources": p["sources"],
        "holiday_checked": True, "corporate_actions_checked": True, "session_known": True,
        "event_in_holding_window": False}
    a.raw.update(regularTradingHours={"timezone": "America/New_York"}, isOutsideRth=True)
    p["offhours_approved"] = True
    assert policies.match(a, b)[0] is None
    a.raw.update(upperTradingBound="110", lowerTradingBound="90")
    assert policies.match(a, b)[1] == "eligible"
    p["event_state"]["event_in_holding_window"] = True
    assert policies.match(a, b)[0] is None


def boost_rule(**kwargs):
    now = datetime.now(timezone.utc)
    return {"verified": True, "eligibility_confirmed": True, "venue": "lighter", "symbol": "BTC",
            "access_path": "api", "style": "ioc", "multiplier": "2",
            "source_url": "https://docs.lighter.xyz/points-program.md", "evidence": "fixture only",
            "verified_at": (now-timedelta(hours=1)).isoformat(),
            "valid_until": (now+timedelta(hours=1)).isoformat(), **kwargs}


@pytest.mark.parametrize("override", [{"access_path": "robinhood_wallet"}, {"verified": False},
    {"eligibility_confirmed": False}, {"source_url": "https://docs.lighter.xyz.evil.test/rules"},
    {"valid_until": "2020-01-01T00:00:00Z"}, {"style": "maker"}, {"symbol": "ETH"}])
def test_boost_unknown_wrong_path_or_expired_is_not_applied(policies, override):
    policies.boosts = [boost_rule(**override)]
    assert policies.boost("BTC", "lighter", "ioc") == 1


def test_boost_does_not_override_economic_or_stale_data_gate(config, policies, markets):
    a, b = markets
    policies.boosts = [boost_rule()]
    scanner = Scanner(config, policies)
    books = {"arcus": {1: fresh_book()}, "lighter": {0: fresh_book()}}
    accounts = {"arcus": account(), "lighter": account()}
    assert scanner.scan({1: a}, {0: b}, books, accounts) == []
    books["lighter"][0] = fresh_book("103", "103.1")
    candidates = scanner.scan({1: a}, {0: b}, books, accounts)
    assert candidates[0].boost == 2
    books["arcus"][1].source_time = time.time()-3
    assert scanner.scan({1: a}, {0: b}, books, accounts) == []


def test_four_order_costs_include_funding_and_fee_bounds(config, policies, markets):
    a, b = markets
    scanner = Scanner(config, policies)
    accounts = {"arcus": account(), "lighter": account()}
    books = {"arcus": {1: fresh_book()}, "lighter": {0: fresh_book("103", "103.1")}}
    first = scanner.scan({1: a}, {0: b}, books, accounts)[0]
    accounts["arcus"].taker_fee = D("0.01")
    b.funding_hourly = D("0.01")
    assert scanner.scan({1: a}, {0: b}, books, accounts) == []
    assert first.quantity * max(a.mark, b.mark) <= 50


def test_independent_margin_and_short_liquidation_asymmetry(markets):
    a = markets[0]
    assert entry_risk(account(), a, D(50), False)
    assert not entry_risk(account("20"), a, D(50), False)
    long = Position(1, D(1), D(100), D(100), D(100), "cross")
    short = copy.copy(long)
    short.quantity = D(-1)
    assert liquidation_distance(account("30", {1: long}), long, a) > liquidation_distance(
        account("30", {1: short}), short, a)


def test_liquidation_reference_on_wrong_side_is_zero_distance(markets):
    a = markets[0]
    long = Position(1, D(1), D(100), D(100), D(100), "cross", D(120))
    short = Position(1, D(-1), D(100), D(100), D(100), "cross", D(80))
    assert liquidation_distance(account("100", {1: long}), long, a) == 0
    assert liquidation_distance(account("100", {1: short}), short, a) == 0


def test_isolated_margin_counts_unrealized_loss(markets):
    a = markets[0]
    long = Position(1, D(1), D(150), D(100), D(100), "isolated")
    assert liquidation_distance(account("100", {1: long}), long, a) < D("0.5")
