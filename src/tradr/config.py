from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import D, dec


@dataclass
class Config:
    state_dir: str = ".tradr"
    profiles_path: str = "profiles.json"
    boosts_path: str = "boosts.json"
    daily_loss_limit_usd: str | None = None
    paper_capital_per_venue: str = "100"
    paper_lighter_tier: str = "standard"
    max_leg_usd: str = "50"
    equity_fraction: str = "0.5"
    min_edge_bps: str = "10"
    slippage_bps: str = "20"
    emergency_slippage_bps: str = "100"
    max_hold_seconds: int = 14400
    maker_wait_seconds: int = 30
    hedge_timeout_seconds: int = 5
    health_host: str = "127.0.0.1"
    health_port: int = 8787
    arcus_address: str = ""
    arcus_account_index: int = 0
    lighter_account_index: int = -1
    lighter_api_key_index: int = 4
    poll_seconds: int = 2
    discovery_seconds: int = 60

    def validate(self, live: bool = False):
        if self.paper_lighter_tier not in {"standard", "premium"}:
            raise ValueError("paper_lighter_tier must be standard or premium")
        for key in ("paper_capital_per_venue", "max_leg_usd", "equity_fraction", "min_edge_bps",
                    "slippage_bps", "emergency_slippage_bps"):
            if dec(getattr(self, key)) <= 0:
                raise ValueError(f"{key} must be positive")
        if dec(self.equity_fraction) > D("0.5"):
            raise ValueError("equity_fraction cannot exceed 0.5")
        if self.daily_loss_limit_usd is not None and dec(self.daily_loss_limit_usd) <= 0:
            raise ValueError("daily_loss_limit_usd must be positive")
        if self.health_host not in {"127.0.0.1", "::1"}:
            raise ValueError("Health endpoint must bind to loopback")
        if not 0 <= self.health_port <= 65535:
            raise ValueError("Invalid health_port")
        if not 1 <= self.hedge_timeout_seconds <= 5 or not 1 <= self.maker_wait_seconds <= 30:
            raise ValueError("Execution deadlines exceed safety bounds")
        if not 1 <= self.max_hold_seconds <= 14400 or self.poll_seconds != 2:
            raise ValueError("Invalid holding period or account polling interval")
        if self.discovery_seconds < 60:
            raise ValueError("Discovery interval must be at least 60 seconds")
        if live:
            if self.daily_loss_limit_usd is None:
                raise ValueError("Live mode requires an explicit daily_loss_limit_usd")
            if not self.arcus_address.startswith("0x") or len(self.arcus_address) != 42:
                raise ValueError("Configure arcus_address")
            if self.lighter_account_index <= 0 or not 0 <= self.arcus_account_index <= 9:
                raise ValueError("Invalid venue account index")
            if self.lighter_api_key_index not in set(range(4, 157)) | set(range(158, 255)):
                raise ValueError("Reserved Lighter RH API key index")
            if not os.environ.get("ARCUS_API_PRIVATE_KEY") or not os.environ.get("LIGHTER_API_PRIVATE_KEY"):
                raise ValueError("Set ARCUS_API_PRIVATE_KEY and LIGHTER_API_PRIVATE_KEY locally")
        return self

    @classmethod
    def load(cls, path: str):
        values = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
        config = cls(**values)
        base = Path(path).resolve().parent
        for name in ("state_dir", "profiles_path", "boosts_path"):
            setattr(config, name, str(base / getattr(config, name)))
        return config.validate()

    def save(self, path: str):
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
