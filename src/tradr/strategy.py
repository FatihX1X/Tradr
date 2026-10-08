from __future__ import annotations

import time
from decimal import Decimal

from .config import Config
from .models import Account, BPS, Book, Candidate, D, Market, Position, common_step, dec, grid
from .policy import Policies


def liquidation_distance(account: Account, position: Position, market: Market) -> Decimal:
    mark = market.mark
    if mark <= 0 or not position.quantity:
        raise ValueError("Unknown liquidation reference")
    if position.liquidation is not None and position.liquidation > 0:
        distance = mark - position.liquidation if position.quantity > 0 else position.liquidation - mark
        return max(D(0), distance / mark)
    # Dedicated account with one cross position only. Calculate its adverse-price boundary,
    # allowing maintenance requirement to change with price; short and long differ.
    if position.mode == "isolated":
        capital = position.margin + position.quantity * market.multiplier * (mark - position.entry)
    elif len(account.positions) <= 1:
        capital = account.equity
    else:
        raise ValueError("Cannot derive multi-position liquidation boundary")
    notional = abs(position.quantity) * market.multiplier * mark
    buffer = capital - notional * market.maintenance_margin
    denominator = notional * (1 - market.maintenance_margin if position.quantity > 0
                              else 1 + market.maintenance_margin)
    return max(D(0), buffer / denominator)


def account_risk(account: Account, markets: dict[int, Market]) -> str | None:
    if time.monotonic() - account.received > 2:
        return "Account state stale"
    for mid, p in account.positions.items():
        if mid not in markets or markets[mid].mark <= 0:
            return "Position market risk data unavailable"
        if time.monotonic() - markets[mid].observed > 2:
            return "Position mark/oracle data stale"
        if liquidation_distance(account, p, markets[mid]) < D("0.15"):
            return "Liquidation distance below 15%"
    return None


def entry_risk(account: Account, market: Market, notional: Decimal, isolated: bool) -> bool:
    if time.monotonic() - account.received > 2 or account.equity <= 0:
        return False
    imr = max(market.initial_margin, dec(market.raw.get("offHoursInitialMarginFraction") or 0))
    if not D(0) < market.maintenance_margin < imr <= D(1):
        return False
    # Allocate full leg notional for isolated; cross also reserves at least 50% of equity.
    needed = notional if isolated else notional * imr
    if needed > account.free or notional > account.equity * D("0.5"):
        return False
    synthetic = Position(market.id, notional / market.mark / market.multiplier,
                         market.mark, market.mark, notional, "isolated" if isolated else "cross")
    return liquidation_distance(account, synthetic, market) >= D("0.2")


class Scanner:
    def __init__(self, config: Config, policies: Policies):
        self.config, self.policies = config, policies
        self.reasons: dict[str, str] = {}

    def scan(self, arcus: dict[int, Market], lighter: dict[int, Market], books: dict[str, dict[int, Book]],
             accounts: dict[str, Account]) -> list[Candidate]:
        self.reasons = {}
        result = []
        # Reviewed aliases can link different symbols; never infer XAU == a gold ETF, for example.
        aliases = {p.get("arcus_symbol"): p.get("lighter_symbol") for p in self.policies.profiles}
        by_symbol = {m.symbol: m for m in lighter.values()}
        for a in arcus.values():
            b = by_symbol.get(aliases.get(a.symbol, a.symbol))
            if b is None:
                continue
            profile, reason = self.policies.match(a, b)
            if profile is None:
                self.reasons[a.symbol] = reason
                continue
            try:
                ab, bb = books["arcus"][a.id], books["lighter"][b.id]
                for market, book in ((a, ab), (b, bb)):
                    if not book.fresh():
                        raise ValueError(market.venue + " orderbook stale or invalid")
                    if time.monotonic() - market.observed > 2:
                        raise ValueError(market.venue + " mark/oracle data stale")
                for market in (a, b):
                    if market.mark <= 0 or market.oracle <= 0:
                        raise ValueError("Missing mark/oracle")
                    if abs(market.mark - market.oracle) / market.oracle > D("0.02"):
                        raise ValueError("Mark/oracle divergence above 2%")
                if abs(a.oracle - b.oracle) / min(a.oracle, b.oracle) > D("0.005"):
                    raise ValueError("Venue oracle divergence above 50bps")
                limit = min(dec(self.config.max_leg_usd),
                            min(v.equity for v in accounts.values()) * dec(self.config.equity_fraction))
                worst_reference = max(a.mark, b.mark, a.oracle, b.oracle, ab.asks[0][0], bb.asks[0][0]) * (
                    1 + dec(self.config.slippage_bps)/BPS)
                exposure = grid(limit / worst_reference, common_step(a.step * a.multiplier,
                                                                         b.step * b.multiplier))
                if exposure <= 0:
                    raise ValueError("Zero quantized exposure")
                for m in (a, b):
                    if exposure / m.multiplier < m.min_size or exposure * m.mark < m.min_notional:
                        raise ValueError("Minimum order size/notional exceeds capital budget")
                    if not entry_risk(accounts[m.venue], m, exposure * m.mark,
                                      profile[m.venue]["margin_mode"] == "isolated"):
                        raise ValueError("Insufficient independent collateral or liquidation buffer")
                for long, short, lb, sb in ((a, b, ab, bb), (b, a, bb, ab)):
                    for maker in (None, "arcus", "lighter"):
                        boost = max(self.policies.boost(long.symbol, long.venue,
                                                       "maker" if maker == long.venue else "ioc"),
                                    self.policies.boost(short.symbol, short.venue,
                                                       "maker" if maker == short.venue else "ioc"))
                        if maker and boost <= 1:
                            continue
                        candidate = self.evaluate(long, short, lb, sb, exposure, accounts, boost, maker)
                        if candidate.edge_bps >= dec(self.config.min_edge_bps):
                            result.append(candidate)
                if not any(c.symbol == a.symbol or c.symbol == b.symbol for c in result):
                    self.reasons[a.symbol] = "No net economic edge after four-order costs and funding reserve"
            except KeyError:
                self.reasons[a.symbol] = "Fresh market/orderbook snapshot not available"
            except (ValueError, ArithmeticError) as exc:
                self.reasons[a.symbol] = str(exc)
        return sorted(result, key=lambda c: (c.boost, c.edge_bps, c.depth), reverse=True)

    def evaluate(self, long, short, lb, sb, quantity, accounts, boost, maker):
        lq, sq = quantity / long.multiplier, quantity / short.multiplier
        lp, _ = lb.quote(True, lq)
        sp, _ = sb.quote(False, sq)
        if maker == long.venue:
            lp = lb.bids[0][0]
        if maker == short.venue:
            sp = sb.asks[0][0]
        notional = quantity * max(lp, sp)
        exit_spread = quantity * ((lb.asks[0][0] - lb.bids[0][0]) + (sb.asks[0][0] - sb.bids[0][0])) / 2
        lfee = accounts[long.venue].maker_fee if maker == long.venue else accounts[long.venue].taker_fee
        sfee = accounts[short.venue].maker_fee if maker == short.venue else accounts[short.venue].taker_fee
        fees = quantity * (lp * (max(D(0), lfee) + max(D(0), accounts[long.venue].taker_fee))
                           + sp * (max(D(0), sfee) + max(D(0), accounts[short.venue].taker_fee)))
        funding = quantity * (lp * abs(long.funding_hourly) + sp * abs(short.funding_hourly)) * (
            D(self.config.max_hold_seconds) / 3600)
        slippage = notional * dec(self.config.slippage_bps) / BPS * 4
        costs = fees + funding + exit_spread + slippage
        return Candidate(long.symbol, long, short, quantity, lp, sp,
                         (quantity * (sp - lp) - costs) / notional * BPS, costs, boost, maker,
                         min(sum(s for _, s in lb.asks) * long.multiplier,
                             sum(s for _, s in sb.bids) * short.multiplier))
