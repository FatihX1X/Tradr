import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradr.models import D, Intent
from tradr.venues.arcus import Arcus, canonical_order
from tradr.venues.lighter import Lighter
from tradr.venues.base import TransportError


def test_arcus_canonical_native_units_and_keycase(markets):
    intent = Intent("MixedCase_42", "arcus", 1, True, D("0.0002"), D("100.1"), expiry_us=2000000000000000)
    signed = canonical_order(intent, markets[0], "0xABC", 2, 1900000000000000000)
    raw = json.loads(signed)
    assert raw == {"ad": "0xabc", "ai": 2, "c": "MixedCase_42", "ct": 1900000000000000000,
                   "g": 2000000000000000000, "m": 1, "op": 1, "p": 1001, "q": 2,
                   "r": 0, "s": 0, "t": 2, "v": 1}
    key = Ed25519PrivateKey.generate()
    key.public_key().verify(key.sign(signed), signed)
    canceled = json.loads(canonical_order(intent, markets[0], "0xABC", 2, 1, True))
    assert canceled["c"] == "MixedCase_42" and "id" not in canceled and "g" not in canceled


def lighter_frame(nonce, begin=None, snapshot=False):
    payload = {"bids": [{"price": "100", "size": "1"}], "asks": [{"price": "101", "size": "2"}],
               "nonce": nonce}
    if begin is not None:
        payload["begin_nonce"] = begin
    return {"channel": "order_book:0", "type": "subscribed/order_book" if snapshot else "update/order_book",
            "timestamp": int(time.time()*1000), "order_book": payload}


def test_lighter_snapshot_delta_nonce_gap_and_resnapshot(config, journal):
    venue = Lighter(config, journal)
    venue.ingest(lighter_frame(10, snapshot=True))
    venue.ingest(lighter_frame(15, begin=10))  # offsets/nonces need not increment by one
    assert venue.books[0].sequence == 15 and venue.books[0].fresh()
    with pytest.raises(TransportError):
        venue.ingest(lighter_frame(19, begin=16))
    assert not venue.books[0].valid
    venue.ingest(lighter_frame(20, snapshot=True))
    assert venue.books[0].valid


def test_stale_lighter_timestamp_and_funding_percentage(config, journal, markets):
    venue = Lighter(config, journal)
    venue.markets[0] = markets[1]
    venue.ingest({"channel": "market_stats:all", "timestamp": int((time.time()-5)*1000),
        "market_stats": {"0": {"market_id": 0, "mark_price": "100", "index_price": "100",
                               "current_funding_rate": "0.01", "funding_rate": "0.005"}}})
    assert venue.markets[0].funding_hourly == D("0.0001")
    assert venue.markets[0].observed == 0


def test_arcus_snapshot_regression_and_partial_ioc_terminal(config, journal, markets):
    venue = Arcus(config, journal)
    m = markets[0]
    m.raw["marketDisplayName"] = "BTC-USD"
    venue.markets[1] = m
    frame = {"channel": "l2Orderbook", "id": "BTC-USD", "contents": {
        "bids": [["100", "1"]], "asks": [["101", "1"]],
        "timestamp": int(time.time()*1e6), "lastSequenceId": 10}}
    venue.ingest(frame)
    frame["contents"]["lastSequenceId"] = 9
    with pytest.raises(TransportError):
        venue.ingest(frame)
    assert Arcus.parse_order({"clientId": "1", "status": "PARTIALLY_FILLED", "timeInForce": "IOC",
                             "filledSize": "0.1", "avgFillPrice": "100"}).terminal


def test_arcus_initial_oracle_and_offline_frames_do_not_poison_other_markets(config, journal, markets):
    venue = Arcus(config, journal)
    m = markets[0]
    m.raw["marketDisplayName"] = "BTC-USD"
    venue.markets[1] = m
    venue.ingest({"channel": "oraclePrices", "id": "BTC-USD", "contents": {
        "epoch": time.time_ns(), "prices": [{"marketId": 1, "price": "100", "markPrice": "100"}]}})
    assert m.observed == 0  # missing authoritative mark timestamp is not fresh data
    stamp = time.time_ns()
    venue.ingest({"channel": "oraclePrices", "id": "BTC-USD", "contents": {"epoch": stamp,
        "prices": [{"marketId": 1, "price": "100", "markPrice": "100", "markEpochNanos": stamp}]}})
    assert time.monotonic()-m.observed < 1
    venue.ingest({"channel": "oraclePrices", "id": "OFFLINE-USD", "contents": {}})
    assert time.monotonic()-m.observed < 1
    venue.ingest({"channel": "l2Orderbook", "id": "BTC-USD", "contents": {"bids": [], "asks": []}})
    assert not venue.books[1].valid


def test_arcus_fill_without_explicit_filled_size_uses_remaining_size():
    order = Arcus.parse_order({"clientId": "1", "status": "FILLED", "originalSize": "0.5",
                              "remainingSize": "0", "avgFillPrice": "100"})
    assert order.filled == D("0.5")


def test_lighter_fee_ticks_use_official_denominator():
    assert Lighter.fee_rates({"current_maker_fee_tick": 120, "current_taker_fee_tick": 350}) == (
        D("0.00012"), D("0.00035"))
    with pytest.raises(KeyError):
        Lighter.fee_rates({"user_tier_name": "standard"})  # missing actual fee data is not zero fees


@pytest.mark.asyncio
async def test_arcus_read_only_key_scope_verification(config, journal, monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", key.private_bytes_raw().hex())
    config.arcus_address = "0x" + "1"*40
    venue = Arcus(config, journal, live=True)
    record = {"apiKey": key.public_key().public_bytes_raw().hex(), "address": config.arcus_address,
              "status": "ACTIVE", "validUntil": 0, "allSubaccounts": False, "accountIndex": 0}

    async def read(method, path, **kwargs):
        assert method == "GET" and path == "/v1/apiKeys"
        return {"apiKeys": [record]}

    venue.request = read
    await venue.verify_key()
    record["accountIndex"] = 2
    with pytest.raises(TransportError, match="scope"):
        await venue.verify_key()


@pytest.mark.asyncio
async def test_real_native_lighter_signer_offline():
    import lighter
    private, _, error = lighter.create_api_key()
    assert error is None
    signer = lighter.SignerClient(url=Lighter.url, account_index=1, api_private_keys={4: private}, chain_id=466324)
    try:
        _, info, digest, error = signer.sign_create_order(1, 10, 20, 1000, False, 0, 0, order_expiry=0,
                                                        nonce=1, api_key_index=4)
        assert error is None and info and digest
    finally:
        await signer.close()
