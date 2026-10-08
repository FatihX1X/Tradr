from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web
from filelock import FileLock, Timeout

from .config import Config
from .engine import Engine
from .models import dec
from .policy import Policies
from .storage import Journal
from .venues.arcus import Arcus
from .venues.lighter import Lighter
from .venues.paper import Paper


async def feeds(config, journal, live):
    venues = {"arcus": Arcus(config, journal, live), "lighter": Lighter(config, journal, live)}
    try:
        await asyncio.gather(*(v.open() for v in venues.values()))
        return venues
    except BaseException:
        await asyncio.gather(*(v.close() for v in venues.values()), return_exceptions=True)
        raise


async def execute(args, config):
    journal = Journal(config.state_dir, args.mode)
    lock = FileLock(str(Path(config.state_dir) / f"{args.mode}.lock"), timeout=0)
    acquired, venues, runner, engine, discovery = False, {}, None, None, None
    try:
        lock.acquire()
        acquired = True
        config.validate(live=args.mode == "live")
        identity = {"mode": args.mode, "arcus_address": config.arcus_address.lower(),
                    "arcus_account_index": config.arcus_account_index,
                    "lighter_account_index": config.lighter_account_index}
        stored_identity = journal.get("identity")
        if stored_identity is not None and stored_identity != identity:
            raise ValueError("State directory belongs to different venue accounts; use a separate directory")
        journal.put("identity", identity)
        policies = Policies(config.profiles_path, config.boosts_path)
        venues = await feeds(config, journal, args.mode == "live")
        a_symbols = {m.symbol for m in venues["arcus"].markets.values()}
        b_symbols = {m.symbol for m in venues["lighter"].markets.values()}
        aliases = {p.get("arcus_symbol"): p.get("lighter_symbol") for p in policies.profiles}
        selected_a = [m.id for m in venues["arcus"].markets.values()
                      if aliases.get(m.symbol, m.symbol) in b_symbols]
        selected_b = [m.id for m in venues["lighter"].markets.values()
                      if m.symbol in a_symbols or m.symbol in aliases.values()]
        await asyncio.gather(venues["arcus"].stream(selected_a), venues["lighter"].stream(selected_b))
        execution = venues if args.mode == "live" else {n: Paper(v, config, journal) for n, v in venues.items()}
        engine = Engine(config, policies, journal, execution)

        async def refresh_discovery():
            while True:
                await asyncio.sleep(config.discovery_seconds)
                try:
                    await asyncio.gather(*(v.discover() for v in venues.values()))
                    for v in venues.values():
                        v.metadata_ready = True
                    policies.reload()
                    aliases = {p.get("arcus_symbol"): p.get("lighter_symbol") for p in policies.profiles}
                    b_symbols = {m.symbol for m in venues["lighter"].markets.values()}
                    selected = [m for m in venues["arcus"].markets.values()
                                if aliases.get(m.symbol, m.symbol) in b_symbols]
                    b_lookup = {m.symbol: m for m in venues["lighter"].markets.values()}
                    for m in selected:
                        await venues["arcus"].add_market(m.id)
                        await venues["lighter"].add_market(b_lookup[aliases.get(m.symbol, m.symbol)].id)
                except Exception as exc:
                    # Fee/metadata uncertainty invalidates entry readiness until a successful refresh.
                    for v in venues.values():
                        v.metadata_ready = False
                        for m in v.markets.values():
                            m.active = False
                    journal.event("discovery_failed", {"error": type(exc).__name__})

        discovery = asyncio.create_task(refresh_discovery())
        if config.health_port:
            app = web.Application()

            async def health(_):
                status = engine.health()
                ready = engine.phase not in {"starting", "recovery", "paused"} and all(
                    v.connected for v in venues.values())
                return web.json_response(status, status=200 if ready else 503)

            app.router.add_get("/health", health)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, config.health_host, config.health_port).start()
        # Allow fresh public snapshots before scanning; this is not an assertion of readiness.
        await asyncio.sleep(2)
        journal.put("control", None)
        loop = asyncio.get_running_loop()

        def stop_handler(*_):
            loop.call_soon_threadsafe(journal.put, "control", "stop")

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop_handler)
        print(f"Tradr {args.mode}; state={config.state_dir}; health=localhost:{config.health_port}", flush=True)
        await engine.run(seconds=args.seconds, flatten_only=args.command == "flatten")
        print(json.dumps(engine.health(), ensure_ascii=False, indent=2), flush=True)
        return 2 if engine.phase == "recovery" else 0
    finally:
        if discovery:
            discovery.cancel()
            await asyncio.gather(discovery, return_exceptions=True)
        if engine and journal.get("hedge"):
            try:
                await asyncio.shield(engine.flatten())
                engine.publish()
            except Exception:
                journal.event("shutdown_unresolved", {"action": "run flatten after connectivity restoration"})
        if runner:
            await runner.cleanup()
        await asyncio.gather(*(v.close() for v in venues.values()), return_exceptions=True)
        if acquired:
            lock.release()
        journal.close()


async def doctor(args, config):
    journal = Journal(config.state_dir, args.mode)
    venues = {}
    try:
        config.validate(live=args.mode == "live")
        venues = await feeds(config, journal, args.mode == "live")
        policies = Policies(config.profiles_path, config.boosts_path)
        b = {m.symbol: m for m in venues["lighter"].markets.values()}
        aliases = {p.get("arcus_symbol"): p.get("lighter_symbol") for p in policies.profiles}
        common = [m for m in venues["arcus"].markets.values() if aliases.get(m.symbol, m.symbol) in b]
        result = []
        for m in common:
            _, reason = policies.match(m, b[aliases.get(m.symbol, m.symbol)])
            result.append({"symbol": m.symbol, "category": m.category, "contract_policy": reason,
                           "arcus_id": m.id, "lighter_id": b[aliases.get(m.symbol, m.symbol)].id})
        if args.export_profiles:
            path = Path(args.export_profiles)
            if path.exists():
                raise ValueError("Refusing to overwrite an existing profile file")
            path.write_text(json.dumps(policies.templates(common), indent=2)+"\n", encoding="utf-8")
        if args.signer_check:
            # Native signer feasibility check uses a throwaway key and sends no network transaction.
            import lighter
            private, _, err = lighter.create_api_key()
            if err:
                raise RuntimeError("Native signer key generation failed")
            signer = lighter.SignerClient(url=Lighter.url, account_index=1, api_private_keys={4: private},
                                          chain_id=Lighter.chain_id)
            try:
                _, _, _, err = signer.sign_create_order(1, 1, 1, 1, False, 0, 0, order_expiry=0,
                                                       nonce=1, api_key_index=4)
                if err:
                    raise RuntimeError("Native signer dry signing failed")
            finally:
                await signer.close()
        account_summary = None
        if args.mode == "live":
            await asyncio.gather(venues["arcus"].stream([]), venues["lighter"].stream([]))
            await asyncio.sleep(2)
            snapshots = await asyncio.gather(*(v.account() for v in venues.values()))
            account_summary = {name: {"equity": str(a.equity), "positions": len(a.positions),
                                      "open_orders": len(a.open_orders), "maker_fee": str(a.maker_fee),
                                      "taker_fee": str(a.taker_fee)} for name, a in zip(venues, snapshots)}
        print(json.dumps({"mode": "read-only", "shared_markets": result,
                          "fees_arcus_upper_bound": {"maker": str(venues["arcus"].maker_fee),
                                                     "taker": str(venues["arcus"].taker_fee)},
                          "signer_checked": args.signer_check, "accounts": account_summary},
                         indent=2, ensure_ascii=False))
        return 0
    finally:
        await asyncio.gather(*(v.close() for v in venues.values()), return_exceptions=True)
        journal.close()


def setup(args, config):
    if Path(args.config).exists():
        raise ValueError("Configuration exists; edit it locally instead of overwriting")
    value = args.daily_loss_limit
    if value is None:
        if not sys.stdin.isatty():
            raise ValueError("Noninteractive setup requires --daily-loss-limit")
        value = input("Günlük toplam kayıp durdurma sınırı (USD): ").strip()
    if dec(value) <= 0:
        raise ValueError("Daily loss limit must be positive")
    config.daily_loss_limit_usd = str(dec(value))
    config.save(args.config)
    print(f"Yapılandırma: {Path(args.config).resolve()}\nVarsayılan paper modudur. Anahtarları yerel ortamda ayarlayın.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tradr Arcus–Lighter RH hedge CLI")
    parser.add_argument("--config", default="config.local.json")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--daily-loss-limit")
    for command in ("doctor", "run", "status", "stop", "flatten", "report"):
        p = sub.add_parser(command)
        p.add_argument("--mode", choices=("paper", "live"), default="paper")
        if command in ("run", "flatten"):
            p.add_argument("--seconds", type=float)
            p.add_argument("--daily-loss-limit")
        if command == "doctor":
            p.add_argument("--export-profiles")
            p.add_argument("--signer-check", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(name)s")
    # Third-party transport debug logging can include auth tokens: never enable it through this CLI.
    for name in ("lighter", "aiohttp", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = Config.load(args.config)
        if args.command == "setup":
            return setup(args, config)
        if args.command in ("status", "report", "stop"):
            journal = Journal(config.state_dir, args.mode)
            try:
                if args.command == "stop":
                    journal.put("control", "stop")
                    print("Stop kaydedildi; çalışan bot emirleri iptal edip pozisyonları kapatmayı deneyecek.")
                else:
                    print(json.dumps(journal.report() if args.command == "report" else journal.get("health"),
                                     indent=2, ensure_ascii=False))
            finally:
                journal.close()
            return 0
        if args.command == "doctor":
            return asyncio.run(doctor(args, config))
        if args.daily_loss_limit:
            config.daily_loss_limit_usd = str(dec(args.daily_loss_limit))
        if args.mode == "live" and config.daily_loss_limit_usd is None and sys.stdin.isatty():
            config.daily_loss_limit_usd = str(dec(input("Günlük toplam kayıp durdurma sınırı (USD): ")))
        return asyncio.run(execute(args, config))
    except Timeout:
        print("Aynı mod için başka bir Tradr süreci çalışıyor.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Configuration exceptions contain field names, not credentials; other errors use type only.
        text = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Tradr durdu: {text}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
