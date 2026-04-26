"""
auto_quant_live_signal_publisher.py — Auto-Quant -> ict-engine Phase 2 producer

Loads a single FreqTrade strategy module from `user_data/strategies_ibkr/`,
subscribes to the IBKR-bridge candle stream on Redis, and on each new bar
calls the strategy's `live_factor_contributions(df, latest_index)` method
to produce ict-engine-compatible `FactorContribution` envelopes. Each
envelope is published to a dedicated Redis stream:

    auto_quant:factor_signals:<lowercased_symbol>

The ict-engine consumer (`ict-engine auto-quant-consume-live-signals`) reads
from that stream, validates against the wire schema, and appends each
contribution to a JSONL log inside its state directory.

The wire format is documented in:
    /Users/thrill3r/projects-ict-engine/ict-engine/docs/
        2026-04-26-auto-quant-live-signals-plan.md

Run with:

    python auto_quant_live_signal_publisher.py \
        --symbol NQ \
        --strategy user_data/strategies_ibkr/MyBreakoutICT.py \
        --bar-size 5min \
        --redis-url redis://localhost:6379

or in self-test mode (no Redis required):

    python auto_quant_live_signal_publisher.py --selftest
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from auto_quant_meta import StrategyMeta, parse_meta_from_file

SCHEMA_VERSION = "1.0"
STREAM_KEY_PREFIX = "auto_quant:factor_signals"
STATUS_HASH_PREFIX = "auto_quant:factor_signals_status"
ENVELOPE_FIELD = "payload"
DEFAULT_REDIS_URL = "redis://localhost:6379"
DEFAULT_BAR_SIZE = "5min"
DEFAULT_STREAM_MAXLEN = 50_000
ALLOWED_DIRECTIONS = ("Bull", "Bear", "Neutral")

REQUIRED_CONTRIBUTION_KEYS = (
    "factor_name",
    "category",
    "direction",
    "value",
    "confidence",
    "weighted_score",
    "uncertainty_contribution",
)


class WireValidationError(ValueError):
    """Strategy returned a contribution that violates the wire schema."""


@dataclass
class PublisherConfig:
    symbol: str
    strategy_path: Path
    bar_size: str = DEFAULT_BAR_SIZE
    redis_url: str = DEFAULT_REDIS_URL
    max_iter: int | None = None
    block_ms: int = 2000
    stream_maxlen: int = DEFAULT_STREAM_MAXLEN


def stream_key_for(symbol: str) -> str:
    return f"{STREAM_KEY_PREFIX}:{symbol.lower()}"


def status_key_for(symbol: str) -> str:
    return f"{STATUS_HASH_PREFIX}:{symbol.lower()}"


def candle_stream_key_for(symbol: str, bar_size: str) -> str:
    # Mirrors ibkr_bridge.bridge: ibkr:bars:<SYM>:<bar_size>
    # bar_size for kup-style bars matches the suffix bridge.py uses
    # (e.g. "5min", "30sec", "1hour"). The plain real-time 5sec stream
    # uses suffix "5sec".
    return f"ibkr:bars:{symbol}:{bar_size}"


# ---------------------------------------------------------------------------
# Strategy loading
# ---------------------------------------------------------------------------


def load_strategy_class(path: Path):
    """Import the strategy module at ``path`` and return its primary class.

    The class is the one whose name matches ``path.stem`` exactly. We do
    not use FreqTrade's resolver because the publisher must work without
    a full FreqTrade environment.
    """
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build a module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    cls = getattr(module, path.stem, None)
    if cls is None:
        raise RuntimeError(
            f"strategy module {path} does not expose a class named '{path.stem}'"
        )
    return cls


def instantiate_strategy(cls):
    """Construct the strategy with no FreqTrade context.

    FreqTrade's ``IStrategy`` accepts an optional ``config`` dict at
    construction. Passing an empty dict is enough for the live-signal
    contract because the publisher only invokes
    ``live_factor_contributions``, which by spec must not depend on
    runtime FreqTrade state.
    """
    try:
        return cls(config={})
    except TypeError:
        return cls()


# ---------------------------------------------------------------------------
# Wire validation
# ---------------------------------------------------------------------------


def validate_contribution(c: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise WireValidationError(
            f"{where}: expected dict, got {type(c).__name__}"
        )
    missing = [k for k in REQUIRED_CONTRIBUTION_KEYS if k not in c]
    if missing:
        raise WireValidationError(
            f"{where}: missing required keys {missing}"
        )
    name = c["factor_name"]
    if not isinstance(name, str) or not name:
        raise WireValidationError(f"{where}: factor_name must be a non-empty str")
    direction = c["direction"]
    if direction not in ALLOWED_DIRECTIONS:
        raise WireValidationError(
            f"{where}: direction must be one of {ALLOWED_DIRECTIONS}, got {direction!r}"
        )
    for fname in (
        "value",
        "confidence",
        "weighted_score",
        "uncertainty_contribution",
    ):
        v = c[fname]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise WireValidationError(
                f"{where}: {fname} must be a finite number, got {v!r}"
            )
        v = float(v)
        if not math.isfinite(v):
            raise WireValidationError(f"{where}: {fname} must be finite, got {v}")
        c[fname] = v
    if "explanation" in c and not isinstance(c["explanation"], str):
        raise WireValidationError(
            f"{where}: explanation, if present, must be a str"
        )
    if "category" in c and not isinstance(c["category"], str):
        raise WireValidationError(
            f"{where}: category must be a str, got {type(c['category']).__name__}"
        )
    return {
        "factor_name": name,
        "category": c["category"],
        "direction": direction,
        "value": float(c["value"]),
        "confidence": float(c["confidence"]),
        "weighted_score": float(c["weighted_score"]),
        "uncertainty_contribution": float(c["uncertainty_contribution"]),
        "explanation": c.get("explanation", ""),
    }


def build_envelope(
    *,
    meta: StrategyMeta,
    symbol: str,
    bar_close_ts_ms: int,
    contributions: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    if not contributions:
        raise WireValidationError(
            "publisher refuses to emit an envelope with zero contributions"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "timestamp_ms": int(time.time() * 1000),
        "auto_quant_run_id": run_id,
        "strategy_name": meta.strategy,
        "strategy_mutation_id": meta.mutation_id,
        "bar_close_ts_ms": int(bar_close_ts_ms),
        "contributions": contributions,
    }


# ---------------------------------------------------------------------------
# Candle assembly from Redis stream entries
# ---------------------------------------------------------------------------


BAR_FLOAT_COLS = ("open", "high", "low", "close", "volume", "wap")
BAR_INT_COLS = ("count",)


def candle_entry_to_row(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Turn a single ibkr-bridge bar entry into a typed row.

    The bridge serialises bars as
        {ts, open, high, low, close, volume, count, wap, ...}
    where every value is a string (Redis Streams). We coerce to
    float / int and drop unknown keys.
    """
    row: dict[str, Any] = {"stream_id": entry_id}
    ts = fields.get("ts")
    if ts is not None:
        try:
            ts_val = float(ts)
        except (TypeError, ValueError):
            ts_val = float("nan")
        row["ts"] = ts_val
        row["date"] = pd.to_datetime(ts_val, unit="s", utc=True)
    for k in BAR_FLOAT_COLS:
        if k in fields:
            try:
                row[k] = float(fields[k])
            except (TypeError, ValueError):
                row[k] = float("nan")
    for k in BAR_INT_COLS:
        if k in fields:
            try:
                row[k] = int(float(fields[k]))
            except (TypeError, ValueError):
                row[k] = 0
    return row


# ---------------------------------------------------------------------------
# Main loop (Redis path)
# ---------------------------------------------------------------------------


def run_publisher(cfg: PublisherConfig) -> int:
    import redis as redis_module  # imported here so --selftest works without redis

    meta = parse_meta_from_file(cfg.strategy_path)
    cls = load_strategy_class(cfg.strategy_path)
    strategy = instantiate_strategy(cls)
    if not hasattr(strategy, "live_factor_contributions"):
        sys.stderr.write(
            f"[publisher] strategy {meta.strategy} does not implement "
            f"live_factor_contributions; nothing to publish\n"
        )
        return 0

    r = redis_module.Redis.from_url(
        cfg.redis_url, decode_responses=True, socket_connect_timeout=2.0
    )
    try:
        r.ping()
    except redis_module.exceptions.RedisError as exc:
        raise RuntimeError(
            f"publisher requires a reachable Redis at {cfg.redis_url!r} "
            f"(is the bridge running?). Underlying: {exc}"
        ) from exc

    candle_key = candle_stream_key_for(cfg.symbol, cfg.bar_size)
    out_key = stream_key_for(cfg.symbol)
    status_key = status_key_for(cfg.symbol)
    run_id = f"live:{cfg.symbol}:{meta.strategy}:{int(time.time())}"
    last_id = "$"  # only future bars, never replay backlog
    iter_count = 0
    history: list[dict[str, Any]] = []

    _set_status(r, status_key, "running", run_id)
    try:
        while True:
            if cfg.max_iter is not None and iter_count >= cfg.max_iter:
                break
            iter_count += 1
            try:
                resp = r.xread(
                    streams={candle_key: last_id}, block=cfg.block_ms, count=64
                )
            except redis_module.exceptions.RedisError as exc:
                _set_status(r, status_key, f"error: {exc}", run_id)
                raise
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    row = candle_entry_to_row(entry_id, fields)
                    last_id = entry_id
                    history.append(row)
                    if len(history) > 4096:
                        history = history[-2048:]
                    df = pd.DataFrame(history)
                    contributions: list[dict[str, Any]] = []
                    try:
                        raw = strategy.live_factor_contributions(
                            df, len(df) - 1
                        )
                        if not isinstance(raw, list):
                            raise WireValidationError(
                                "live_factor_contributions must return a list"
                            )
                        for idx, c in enumerate(raw):
                            contributions.append(
                                validate_contribution(c, where=f"contributions[{idx}]")
                            )
                        if not contributions:
                            # Strategy produced no signal this bar; skip silently.
                            continue
                        envelope = build_envelope(
                            meta=meta,
                            symbol=cfg.symbol,
                            bar_close_ts_ms=int(row.get("ts", 0) * 1000),
                            contributions=contributions,
                            run_id=run_id,
                        )
                    except WireValidationError as exc:
                        sys.stderr.write(
                            f"[publisher] dropping bar {entry_id}: {exc}\n"
                        )
                        continue
                    payload = json.dumps(envelope, separators=(",", ":"))
                    r.xadd(
                        out_key,
                        {ENVELOPE_FIELD: payload},
                        maxlen=cfg.stream_maxlen,
                        approximate=True,
                    )
                    _bump_status(r, status_key, last_id, run_id)
        _set_status(r, status_key, "stopped", run_id)
    except KeyboardInterrupt:
        _set_status(r, status_key, "stopped", run_id)
    return 0


def _set_status(r, status_key: str, state: str, run_id: str) -> None:
    r.hset(
        status_key,
        mapping={
            "publisher_state": state,
            "run_id": run_id,
            "last_publish_ts_ms": str(int(time.time() * 1000)),
        },
    )


def _bump_status(r, status_key: str, last_id: str, run_id: str) -> None:
    r.hset(
        status_key,
        mapping={
            "publisher_state": "running",
            "run_id": run_id,
            "last_publish_id": last_id,
            "last_publish_ts_ms": str(int(time.time() * 1000)),
        },
    )


# ---------------------------------------------------------------------------
# Self-test (no Redis)
# ---------------------------------------------------------------------------


def _selftest() -> int:
    """In-memory test of validate_contribution + build_envelope."""

    good = {
        "factor_name": "ict_breakout_5m",
        "category": "breakout",
        "direction": "Bull",
        "value": 0.42,
        "confidence": 0.71,
        "weighted_score": 0.30,
        "uncertainty_contribution": 0.08,
        "explanation": "BOS confirmed",
    }
    cleaned = validate_contribution(dict(good), where="c[0]")
    assert cleaned["direction"] == "Bull", cleaned

    # NaN must be rejected.
    bad = dict(good, confidence=float("nan"))
    try:
        validate_contribution(bad, where="c[0]")
    except WireValidationError as e:
        assert "finite" in str(e), e
    else:
        raise AssertionError("nan should have been rejected")

    # Bad direction must be rejected.
    bad = dict(good, direction="sideways")
    try:
        validate_contribution(bad, where="c[0]")
    except WireValidationError as e:
        assert "direction" in str(e), e
    else:
        raise AssertionError("bad direction should have been rejected")

    # build_envelope refuses zero-contribution input.
    fake_meta = StrategyMeta(
        strategy="S",
        mutation_id="m",
        base_factor="f",
        hypothesis="h",
        paradigm="p",
        expected_regime="r",
        factors_used=["x"],
        parent="root",
        asset_class="equities",
        status="active",
        created="",
        raw={},
    )
    try:
        build_envelope(
            meta=fake_meta,
            symbol="NQ",
            bar_close_ts_ms=1234,
            contributions=[],
            run_id="run-1",
        )
    except WireValidationError as e:
        assert "zero contributions" in str(e), e
    else:
        raise AssertionError("zero-contribution build should have failed")

    # End-to-end happy path: build envelope and round-trip JSON.
    env = build_envelope(
        meta=fake_meta,
        symbol="NQ",
        bar_close_ts_ms=1745678900000,
        contributions=[cleaned],
        run_id="run-1",
    )
    payload = json.dumps(env, separators=(",", ":"))
    parsed = json.loads(payload)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["symbol"] == "NQ"
    assert parsed["contributions"][0]["factor_name"] == "ict_breakout_5m"

    print("PASS")
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", help="IBKR symbol, e.g. NQ, ES, GC")
    p.add_argument(
        "--strategy",
        type=Path,
        help="Path to a strategy_ibkr/<Name>.py module exposing "
        "live_factor_contributions",
    )
    p.add_argument(
        "--bar-size",
        default=DEFAULT_BAR_SIZE,
        help=f"Candle stream suffix on Redis (default: {DEFAULT_BAR_SIZE})",
    )
    p.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"Redis URL (default: {DEFAULT_REDIS_URL})",
    )
    p.add_argument(
        "--max-iter",
        type=int,
        default=None,
        help="Optional cap on XREAD iterations; useful for tests + first-runs",
    )
    p.add_argument(
        "--block-ms",
        type=int,
        default=2000,
        help="XREAD BLOCK timeout in milliseconds (default: 2000)",
    )
    p.add_argument(
        "--stream-maxlen",
        type=int,
        default=DEFAULT_STREAM_MAXLEN,
        help=f"Output stream MAXLEN ~ value (default: {DEFAULT_STREAM_MAXLEN})",
    )
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.symbol or not args.strategy:
        sys.stderr.write("--symbol and --strategy are required when not --selftest\n")
        return 2
    cfg = PublisherConfig(
        symbol=args.symbol,
        strategy_path=args.strategy,
        bar_size=args.bar_size,
        redis_url=args.redis_url,
        max_iter=args.max_iter,
        block_ms=args.block_ms,
        stream_maxlen=args.stream_maxlen,
    )
    return run_publisher(cfg)


if __name__ == "__main__":
    sys.exit(main())
