"""First-run setup CLI for the IBKR live data bridge.

Walks the user through:
    1. Bilingual privacy disclaimer + explicit opt-in.
    2. Local Redis reachability + loopback-bind safety check.
    3. (Optional) IB Gateway / TWS reachability probe.
    4. (Optional) one-shot static account identification (writes
       ``~/.ict-engine/ibkr_capabilities.json``).

Subcommands:
    --enable   default; runs the full flow (idempotent).
    --revoke   deletes ``~/.ict-engine/ibkr_consent.json``; optionally
               wipes ``ibkr:*`` Redis keys with --clean-redis.
    --status   prints current consent / capabilities / Redis / Gateway state.

This is the **only** module the user is expected to invoke manually before
using IBKR features. After --enable succeeds, ``bridge.py``,
``fetch_external.py ibkr-historical``, and any other entry point can run
non-interactively.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from dataclasses import asdict
from pathlib import Path

import redis

from .account_prober import (
    DEFAULT_PROBE_TIMEOUT_SEC,
    PROBE_CLIENT_ID,
    probe_account,
)
from .consent import (
    CONSENT_PATH,
    is_opted_in,
    revoke as revoke_consent,
    show_disclaimer_and_prompt,
)
from .rate_limiter import CAPABILITIES_PATH, IbkrCapabilities

DEFAULT_REDIS_URL = "redis://localhost:6379"
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 7497  # paper

# ANSI colours; degraded gracefully on non-tty sinks
_GREEN = "\033[32m" if sys.stdout.isatty() else ""
_RED = "\033[31m" if sys.stdout.isatty() else ""
_YELLOW = "\033[33m" if sys.stdout.isatty() else ""
_DIM = "\033[2m" if sys.stdout.isatty() else ""
_RESET = "\033[0m" if sys.stdout.isatty() else ""

OK = f"{_GREEN}✓{_RESET}"
FAIL = f"{_RED}✗{_RESET}"
WARN = f"{_YELLOW}!{_RESET}"


# ---------------------------------------------------------------------------
# Probes


def _ping_redis(url: str) -> tuple[bool, str]:
    try:
        r = redis.Redis.from_url(url, decode_responses=True,
                                  socket_connect_timeout=2.0)
        if not r.ping():
            return False, "PING returned falsy"
        bind = r.config_get("bind").get("bind", "")
        protected = r.config_get("protected-mode").get("protected-mode", "")
        version = r.info("server").get("redis_version", "?")
        external = any(part not in {"127.0.0.1", "::1"} and part
                       for part in bind.split())
        if external and protected != "yes":
            return False, (f"Redis bound to {bind!r} with protected-mode={protected!r}; "
                            "refusing to enable IBKR (would expose your data feed). "
                            "Edit /opt/homebrew/etc/redis.conf to bind 127.0.0.1 only.")
        return True, f"Redis {version}, bind={bind!r}, protected-mode={protected}"
    except redis.exceptions.RedisError as exc:
        return False, f"Redis unreachable: {exc}"


def _ping_gateway_socket(host: str, port: int, timeout: float = 2.0
                          ) -> tuple[bool, str]:
    """TCP-level reachability check; does not establish IBKR session."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} accepting connections"
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return False, f"TCP {host}:{port} unreachable: {exc}"


# ---------------------------------------------------------------------------
# Subcommands


async def cmd_enable(args: argparse.Namespace) -> int:
    print(f"{_DIM}── IBKR setup: enable ──{_RESET}\n")

    # 1. Consent
    if is_opted_in():
        print(f"{OK} Consent already on file at {CONSENT_PATH}")
    else:
        opted = show_disclaimer_and_prompt()
        if not opted:
            return 1

    # 2. Redis
    print(f"{_DIM}\n── Local Redis ──{_RESET}")
    ok, msg = _ping_redis(args.redis_url)
    print(f"{OK if ok else FAIL} {msg}")
    if not ok:
        print("\nTo install and start Redis on macOS:")
        print("    brew install redis")
        print("    brew services start redis")
        return 2

    # 3. Gateway TCP probe (optional but recommended)
    print(f"{_DIM}\n── IB Gateway / TWS ──{_RESET}")
    ok, msg = _ping_gateway_socket(args.gateway_host, args.gateway_port)
    if ok:
        print(f"{OK} {msg}")
    else:
        print(f"{WARN} {msg}")
        print("    IBKR features will fail until Gateway/TWS is running on this port.")
        print("    Setup will continue (you can launch Gateway later).")
        if args.require_gateway:
            return 3

    # 4. Account probe (writes capabilities.json) — only if Gateway reachable
    if ok and not args.skip_probe:
        print(f"{_DIM}\n── Account identification probe ──{_RESET}")
        try:
            caps = await probe_account(host=args.gateway_host,
                                        port=args.gateway_port,
                                        client_id=args.client_id,
                                        timeout=args.probe_timeout)
            print(f"{OK} account_type={caps.account_type}  "
                  f"n_subaccounts={caps.n_subaccounts}")
            print(f"  capabilities written to {CAPABILITIES_PATH}")
        except (ConnectionError, RuntimeError) as exc:
            print(f"{WARN} probe skipped: {exc}")
    elif args.skip_probe:
        print(f"{_DIM}── account probe skipped (--skip-probe) ──{_RESET}")

    print(f"\n{OK} IBKR setup complete. You can now run:")
    print(f"    python scripts/ibkr_bridge/bridge.py --config "
          f"scripts/ibkr_bridge/example_config.yaml")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    revoked = revoke_consent()
    if revoked:
        print(f"{OK} consent file removed: {CONSENT_PATH}")
    else:
        print(f"{WARN} no consent file to remove at {CONSENT_PATH}")

    # Capabilities file also goes
    if CAPABILITIES_PATH.exists():
        if args.keep_capabilities:
            print(f"{WARN} kept capabilities at {CAPABILITIES_PATH} "
                  f"(--keep-capabilities)")
        else:
            CAPABILITIES_PATH.unlink()
            print(f"{OK} capabilities file removed: {CAPABILITIES_PATH}")

    if args.clean_redis:
        try:
            r = redis.Redis.from_url(args.redis_url, decode_responses=True)
            keys = list(r.scan_iter(match="ibkr:*"))
            if keys:
                r.delete(*keys)
                print(f"{OK} cleared {len(keys)} ibkr:* keys from Redis")
            else:
                print(f"{OK} no ibkr:* keys in Redis to clean")
        except redis.exceptions.RedisError as exc:
            print(f"{WARN} Redis clean skipped: {exc}")

    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    print(f"{_DIM}── IBKR setup: status ──{_RESET}\n")

    if is_opted_in():
        ts = json.loads(CONSENT_PATH.read_text()).get("timestamp", "?")
        print(f"{OK} Consent: opted-in at {ts}")
    else:
        print(f"{FAIL} Consent: not opted-in (run `setup.py --enable`)")

    if CAPABILITIES_PATH.exists():
        caps = IbkrCapabilities.load_or_default()
        print(f"{OK} Capabilities at {CAPABILITIES_PATH}:")
        print(f"      account_type={caps.account_type}  "
              f"n_subaccounts={caps.n_subaccounts}")
        print(f"      hist_min_interval={caps.hist_min_interval_sec:.2f}s  "
              f"hist_window_cap={caps.hist_window_capacity_for_account()}  "
              f"max_lines={caps.effective_max_lines()}")
    else:
        print(f"{WARN} Capabilities: not yet probed; defaults will apply")

    redis_ok, redis_msg = _ping_redis(args.redis_url)
    print(f"{OK if redis_ok else FAIL} Redis: {redis_msg}")

    gw_ok, gw_msg = _ping_gateway_socket(args.gateway_host, args.gateway_port)
    print(f"{OK if gw_ok else WARN} Gateway: {gw_msg}")

    if redis_ok:
        try:
            r = redis.Redis.from_url(args.redis_url, decode_responses=True)
            ibkr_keys = sum(1 for _ in r.scan_iter(match="ibkr:*", count=200))
            print(f"      ibkr:* keys in Redis: {ibkr_keys}")
        except redis.exceptions.RedisError:
            pass

    return 0 if (is_opted_in() and redis_ok) else 1


# ---------------------------------------------------------------------------
# Entry


def _build_parser() -> argparse.ArgumentParser:
    # Shared connection args — added both to the top-level parser and to
    # every subparser via parents=, so users can write either:
    #     setup --gateway-port 4002 enable
    #     setup enable --gateway-port 4002
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    common.add_argument("--gateway-host", default=DEFAULT_GATEWAY_HOST)
    common.add_argument(
        "--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT,
        help=("TWS: 7497 paper (default) / 7496 live; "
              "Standalone IB Gateway: 4002 paper / 4001 live"),
    )

    p = argparse.ArgumentParser(
        prog="ibkr_bridge.setup",
        parents=[common],
        description=("First-run setup for IBKR live data bridge. "
                     "Bilingual disclaimer + opt-in + environment checks."),
    )

    sub = p.add_subparsers(dest="action")
    # default action when no subcommand: enable
    p.set_defaults(action="enable")

    e = sub.add_parser("enable", parents=[common],
                        help="Run the full setup flow (consent, Redis, Gateway, probe)")
    e.add_argument("--client-id", type=int, default=PROBE_CLIENT_ID,
                    help="Throwaway clientId for the account probe")
    e.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT_SEC)
    e.add_argument("--skip-probe", action="store_true",
                    help="Skip the IBKR account probe (defer to first bridge run)")
    e.add_argument("--require-gateway", action="store_true",
                    help="Fail setup if Gateway/TWS is not reachable")

    r = sub.add_parser("revoke", parents=[common],
                       help="Delete consent + capabilities; optionally clean Redis")
    r.add_argument("--clean-redis", action="store_true",
                    help="Also wipe all ibkr:* keys from Redis")
    r.add_argument("--keep-capabilities", action="store_true",
                    help="Preserve ~/.ict-engine/ibkr_capabilities.json")

    sub.add_parser("status", parents=[common],
                    help="Print current consent / capabilities / Redis / Gateway")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.action == "enable":
        # `enable` defaults — fill missing fields if user invoked the bare CLI
        for attr, default in (("client_id", PROBE_CLIENT_ID),
                                ("probe_timeout", DEFAULT_PROBE_TIMEOUT_SEC),
                                ("skip_probe", False),
                                ("require_gateway", False)):
            if not hasattr(args, attr):
                setattr(args, attr, default)
        return asyncio.run(cmd_enable(args))
    if args.action == "revoke":
        for attr, default in (("clean_redis", False),
                                ("keep_capabilities", False)):
            if not hasattr(args, attr):
                setattr(args, attr, default)
        return cmd_revoke(args)
    if args.action == "status":
        return asyncio.run(cmd_status(args))

    parser.error(f"unknown action {args.action!r}")
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
