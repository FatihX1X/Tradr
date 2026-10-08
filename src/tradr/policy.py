"""Explicit reviewed contract profiles; unknown economics never become tradable by ticker alone."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import D, Market, dec

OFFICIAL = {"docs.arcus.xyz", "arcus.xyz", "docs.lighter.xyz", "apidocs.lighter.xyz",
            "apidocs.rh.lighter.xyz", "robinhood.com", "docs.robinhood.com"}


def official(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in OFFICIAL and not parsed.username


def date(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Policy timestamps must have timezone")
    return parsed.timestamp()


class Policies:
    def __init__(self, profiles_path: str, boosts_path: str):
        self.profiles_path, self.boosts_path = profiles_path, boosts_path
        self.profiles: list[dict] = []
        self.boosts: list[dict] = []
        self.reload()

    def reload(self):
        for path, attr in ((self.profiles_path, "profiles"), (self.boosts_path, "boosts")):
            values = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []
            if not isinstance(values, list):
                raise ValueError(f"{attr} must be a JSON array")
            setattr(self, attr, values)

    def match(self, arcus: Market, lighter: Market) -> tuple[dict | None, str]:
        now = time.time()
        profiles = [p for p in self.profiles if p.get("arcus_symbol") == arcus.symbol
                    and p.get("lighter_symbol") == lighter.symbol]
        if len(profiles) != 1:
            return None, "Exactly one reviewed contract profile is required"
        p = profiles[0]
        try:
            if p.get("approved") is not True or not date(p["reviewed_at"]) <= now < date(p["valid_until"]):
                return None, "Contract profile unapproved, future-dated or expired"
            sources = p["sources"]
            if len(sources) < 2 or not all(official(s) for s in sources):
                return None, "Official evidence from both venues is required"
            if not any(urlparse(s).hostname == "docs.arcus.xyz" for s in sources) or not any(
                    "lighter" in (urlparse(s).hostname or "") for s in sources):
                return None, "Missing venue evidence"
            if not p["underlying"] or p["quote_currency"] != "USD" or p["settlement"] != "linear-perpetual":
                return None, "Unsupported or unknown settlement economics"
            for field in ("oracle", "dividends", "splits", "roll"):
                if not p["arcus"][field] or p["arcus"][field] != p["lighter"][field]:
                    return None, f"Incompatible or unknown {field} economics"
            for m in (arcus, lighter):
                m.multiplier = dec(p[m.venue]["multiplier"])
                if m.multiplier <= 0 or p[m.venue]["margin_mode"] not in {"cross", "isolated"}:
                    return None, "Invalid multiplier or margin mode"
            if not arcus.active or not lighter.active:
                return None, "Venue market offline"
            if p.get("category") != arcus.category:
                return None, "Profile category does not match discovered market"
            if arcus.category != "CRYPTO":
                event = p["event_state"]
                if not date(event["checked_at"]) <= now < date(event["valid_until"]):
                    return None, "Session/holiday/corporate-action snapshot expired"
                if event.get("holiday_checked") is not True or event.get("corporate_actions_checked") is not True:
                    return None, "Missing holiday or corporate-action evidence"
                if not event["sources"] or not all(official(s) for s in event["sources"]):
                    return None, "Unverified event snapshot"
                if event.get("event_in_holding_window") is not False:
                    return None, "Corporate action or roll inside holding window"
                if date(event["valid_until"]) - now < p.get("max_hold_seconds", 14400):
                    return None, "Event coverage shorter than maximum holding period"
                if arcus.raw.get("regularTradingHours") is None or event.get("session_known") is not True:
                    return None, "Unknown session calendar"
                if arcus.raw.get("isOutsideRth"):
                    if p.get("offhours_approved") is not True:
                        return None, "Off-hours profile not approved"
                    if arcus.raw.get("upperTradingBound") is None or arcus.raw.get("lowerTradingBound") is None:
                        return None, "Missing Arcus off-hours price bands"
            return p, "eligible"
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None, "Incomplete or invalid contract evidence"

    def boost(self, symbol: str, venue: str, style: str) -> D:
        value = D(1)
        now = time.time()
        for rule in self.boosts:
            try:
                eligible = (rule.get("verified") is True and rule.get("eligibility_confirmed") is True
                            and rule.get("venue") == venue and rule.get("access_path") == "api"
                            and rule.get("symbol") in (symbol, "*")
                            and rule.get("style") in (style, "any") and official(rule["source_url"])
                            and bool(rule["evidence"]) and date(rule["verified_at"]) <= now < date(rule["valid_until"]))
                if eligible:
                    value = max(value, dec(rule["multiplier"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return value

    def templates(self, markets: list[Market]) -> list[dict]:
        timestamp = datetime.now(timezone.utc).isoformat()
        return [{"approved": False, "arcus_symbol": m.symbol, "lighter_symbol": m.symbol,
                 "category": m.category, "underlying": "", "quote_currency": "USD",
                 "settlement": "linear-perpetual", "reviewed_at": timestamp, "valid_until": timestamp,
                 "sources": [], "offhours_approved": False,
                 "arcus": {"multiplier": "1", "oracle": "", "dividends": "", "splits": "",
                           "roll": "", "margin_mode": "cross"},
                 "lighter": {"multiplier": "1", "oracle": "", "dividends": "", "splits": "",
                             "roll": "", "margin_mode": "cross"},
                 "event_state": {"checked_at": timestamp, "valid_until": timestamp, "sources": [],
                                 "holiday_checked": False, "corporate_actions_checked": False,
                                 "session_known": False, "event_in_holding_window": True}}
                for m in markets]
