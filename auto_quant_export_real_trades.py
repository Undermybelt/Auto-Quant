"""
auto_quant_export_real_trades.py — Auto-Quant -> ict-engine Phase 3 exporter

Reads a FreqTrade backtest results JSON (the file emitted by
`freqtrade backtesting --export trades`, conventionally landing under
`user_data/backtest_results/*.json`) and produces a JSONL artifact
that ict-engine's `auto-quant-ingest-real-trades` command can consume.

The strategy module is parsed for `AUTO_QUANT_META` so each row
carries `strategy_name` + `strategy_mutation_id` provenance. If the
meta block is missing or invalid, the export refuses (no silent
provenance loss).

Wire schema is documented in:
    /Users/thrill3r/projects-ict-engine/ict-engine/docs/
        2026-04-26-auto-quant-real-trades-plan.md

Run with:

    python auto_quant_export_real_trades.py \
        --backtest-result user_data/backtest_results/foo.json \
        --strategy user_data/strategies_ibkr/MyBreakoutICT.py \
        --symbol NQ \
        --output state/NQ/realized_trades_<run_id>.jsonl

or in self-test mode (no FreqTrade results file required):

    python auto_quant_export_real_trades.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from auto_quant_meta import StrategyMeta, parse_meta_from_file

SCHEMA_VERSION = "1.0"
DEFAULT_SOURCE = "auto_quant_real_trades"
ALLOWED_DIRECTIONS = ("Bull", "Bear", "Neutral")


class ExportValidationError(ValueError):
    """A trade row failed schema validation and cannot be exported."""


@dataclass
class ExportConfig:
    backtest_result: Path
    strategy_path: Path
    symbol: str
    output: Path
    auto_quant_run_id: str
    pair_filter: str | None = None  # if set, only trades on this pair are emitted


# ---------------------------------------------------------------------------
# FreqTrade backtest JSON parsing
# ---------------------------------------------------------------------------


def _parse_iso8601_to_ms(value: str) -> int:
    """Parse FreqTrade's ISO 8601-ish timestamps to epoch milliseconds.

    FreqTrade emits trades as e.g. "2026-04-23 13:45:00+00:00". We
    accept both space and T separators and require an explicit
    timezone (we treat naive timestamps as UTC, with a warning).
    """
    if not isinstance(value, str) or not value:
        raise ExportValidationError(f"timestamp must be a non-empty str, got {value!r}")
    cleaned = value.strip().replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ExportValidationError(f"unparseable timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _direction_from_freqtrade_row(row: dict[str, Any]) -> str:
    """Map a FreqTrade trade row to our Bull|Bear|Neutral wire token.

    FreqTrade tracks direction via `is_short` (boolean) for short
    trades, defaulting to long when the field is missing. Anything
    that isn't a boolean is treated as Neutral so we don't silently
    coerce malformed input.
    """
    is_short = row.get("is_short")
    if is_short is True:
        return "Bear"
    if is_short is False or is_short is None:
        return "Bull"
    return "Neutral"


def _coerce_float(value: Any, *, where: str, default: float | None = None) -> float:
    if value is None:
        if default is not None:
            return float(default)
        raise ExportValidationError(f"{where}: missing required float value")
    if isinstance(value, bool):
        raise ExportValidationError(f"{where}: bool is not a valid float")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ExportValidationError(f"{where}: not a float, got {value!r}") from exc
    if not math.isfinite(f):
        raise ExportValidationError(f"{where}: must be finite, got {f}")
    return f


def _outcome_label(profit_abs: float) -> str:
    if profit_abs > 1e-12:
        return "win"
    if profit_abs < -1e-12:
        return "loss"
    return "breakeven"


def freqtrade_trade_to_record(
    row: dict[str, Any],
    *,
    meta: StrategyMeta,
    symbol: str,
    auto_quant_run_id: str,
    fallback_index: int,
) -> dict[str, Any]:
    """Convert one FreqTrade trades-export row into a wire-schema dict."""

    open_ts_ms = _parse_iso8601_to_ms(row.get("open_date", ""))
    close_ts_ms = _parse_iso8601_to_ms(row.get("close_date", ""))
    profit_abs = _coerce_float(row.get("profit_abs"), where="profit_abs")
    direction = _direction_from_freqtrade_row(row)

    pair = row.get("pair", "")
    trade_id = row.get("trade_id")
    if not isinstance(trade_id, (int, str)) or trade_id in ("", None):
        trade_id = (
            f"{meta.strategy}:{symbol}:{pair}:{row.get('open_date', f'idx{fallback_index}')}"
        )
    else:
        trade_id = str(trade_id)

    record = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "trade_id": trade_id,
        "strategy_name": meta.strategy,
        "strategy_mutation_id": meta.mutation_id,
        "auto_quant_run_id": auto_quant_run_id,
        "open_ts_ms": open_ts_ms,
        "close_ts_ms": close_ts_ms,
        "direction": direction,
        "pnl": profit_abs,
        "realized_outcome": _outcome_label(profit_abs),
        "factors_used": [],
    }
    exit_reason = row.get("exit_reason")
    if isinstance(exit_reason, str) and exit_reason:
        # Surface the FreqTrade exit_reason as a coarse regime hint so
        # ict-engine's regime CPT row can still gain something from
        # records that don't carry a full `regime_at_entry` tag.
        record["regime_at_entry"] = "manipulation_expansion"
    return record


def iter_freqtrade_trades(
    backtest_json: dict[str, Any],
    *,
    strategy_name: str,
    pair_filter: str | None,
) -> Iterable[dict[str, Any]]:
    """Yield raw FreqTrade trade rows for one strategy.

    FreqTrade's results JSON is shaped as
        {"strategy": {"<StratName>": {"trades": [...], ...}}, ...}
    when a single strategy was backtested. We accept the older
    flat-list form (`{"trades": [...]}`) too for forward compat.
    """
    strategy_block = (
        backtest_json.get("strategy", {}).get(strategy_name)
        if isinstance(backtest_json.get("strategy"), dict)
        else None
    )
    if strategy_block is not None:
        trades = strategy_block.get("trades", [])
    else:
        trades = backtest_json.get("trades", [])
    if not isinstance(trades, list):
        raise ExportValidationError(
            f"backtest result has no list `trades` for strategy {strategy_name}"
        )
    for row in trades:
        if not isinstance(row, dict):
            continue
        if pair_filter and row.get("pair") != pair_filter:
            continue
        yield row


# ---------------------------------------------------------------------------
# Top-level export routine
# ---------------------------------------------------------------------------


def run_export(cfg: ExportConfig) -> dict[str, Any]:
    """End-to-end export: returns a summary dict suitable for stdout."""

    meta = parse_meta_from_file(cfg.strategy_path)
    raw = json.loads(cfg.backtest_result.read_text())

    cfg.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    invalid = 0

    with cfg.output.open("w", encoding="utf-8") as out:
        for idx, row in enumerate(
            iter_freqtrade_trades(
                raw, strategy_name=meta.strategy, pair_filter=cfg.pair_filter
            )
        ):
            try:
                record = freqtrade_trade_to_record(
                    row,
                    meta=meta,
                    symbol=cfg.symbol,
                    auto_quant_run_id=cfg.auto_quant_run_id,
                    fallback_index=idx,
                )
            except ExportValidationError as exc:
                sys.stderr.write(
                    f"[exporter] skipping trade {idx} (pair={row.get('pair')}): {exc}\n"
                )
                invalid += 1
                continue
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
            written += 1

    return {
        "command": "auto-quant-export-real-trades",
        "symbol": cfg.symbol,
        "strategy_name": meta.strategy,
        "strategy_mutation_id": meta.mutation_id,
        "auto_quant_run_id": cfg.auto_quant_run_id,
        "backtest_result": str(cfg.backtest_result),
        "output": str(cfg.output),
        "trades_written": written,
        "trades_invalid": invalid,
    }


# ---------------------------------------------------------------------------
# Self-test (no FreqTrade results file required)
# ---------------------------------------------------------------------------


def _selftest() -> int:
    """Run the conversion path against in-memory fixtures."""

    fake_meta = StrategyMeta(
        strategy="MyBreakoutICT",
        mutation_id="mb-001",
        base_factor="ict_breakout_5m",
        hypothesis="h",
        paradigm="breakout",
        expected_regime="expansion",
        factors_used=["ict_breakout_5m"],
        parent="root",
        asset_class="futures",
        status="active",
        created="abc1234",
        raw={},
    )

    # Happy path.
    row = {
        "pair": "NQ/USD",
        "open_date": "2026-04-23 13:45:00+00:00",
        "close_date": "2026-04-23 15:05:00+00:00",
        "profit_abs": 12.34,
        "is_short": False,
        "exit_reason": "exit_signal",
    }
    record = freqtrade_trade_to_record(
        row,
        meta=fake_meta,
        symbol="NQ",
        auto_quant_run_id="run-1",
        fallback_index=0,
    )
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["symbol"] == "NQ"
    assert record["strategy_name"] == "MyBreakoutICT"
    assert record["direction"] == "Bull"
    assert record["pnl"] == 12.34
    assert record["realized_outcome"] == "win"
    assert record["open_ts_ms"] == _parse_iso8601_to_ms(row["open_date"])
    assert record["close_ts_ms"] == _parse_iso8601_to_ms(row["close_date"])
    # JSON round-trip is loss-less.
    assert json.loads(json.dumps(record)) == record

    # Short trade.
    row_short = dict(row, is_short=True, profit_abs=-5.0)
    record_short = freqtrade_trade_to_record(
        row_short,
        meta=fake_meta,
        symbol="NQ",
        auto_quant_run_id="run-1",
        fallback_index=1,
    )
    assert record_short["direction"] == "Bear"
    assert record_short["realized_outcome"] == "loss"

    # NaN pnl is rejected.
    bad = dict(row, profit_abs=float("nan"))
    try:
        freqtrade_trade_to_record(
            bad,
            meta=fake_meta,
            symbol="NQ",
            auto_quant_run_id="run-1",
            fallback_index=2,
        )
    except ExportValidationError as exc:
        assert "finite" in str(exc), exc
    else:
        raise AssertionError("NaN profit_abs should have been rejected")

    # Iterator handles both nested and flat shapes.
    nested = {"strategy": {"MyBreakoutICT": {"trades": [row]}}}
    flat = {"trades": [row]}
    assert (
        list(
            iter_freqtrade_trades(nested, strategy_name="MyBreakoutICT", pair_filter=None)
        )
        == list(
            iter_freqtrade_trades(flat, strategy_name="MyBreakoutICT", pair_filter=None)
        )
    )

    # Pair filter.
    multi = {"trades": [row, dict(row, pair="ES/USD")]}
    filtered = list(
        iter_freqtrade_trades(multi, strategy_name="MyBreakoutICT", pair_filter="NQ/USD")
    )
    assert len(filtered) == 1, filtered

    print("PASS")
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--backtest-result",
        type=Path,
        help="Path to the FreqTrade backtest results JSON",
    )
    p.add_argument(
        "--strategy",
        type=Path,
        help="Path to the strategy module (so AUTO_QUANT_META can be parsed)",
    )
    p.add_argument(
        "--symbol",
        help="ict-engine symbol, e.g. NQ, ES, GC. NOT the FreqTrade pair.",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Path to the output JSONL artifact",
    )
    p.add_argument(
        "--auto-quant-run-id",
        default=None,
        help="Optional explicit run id; defaults to derived-from-filename + utcnow",
    )
    p.add_argument(
        "--pair-filter",
        default=None,
        help="Optional FreqTrade pair filter (e.g. 'NQ/USD'); useful when the "
        "backtest covered multiple pairs and you want one symbol",
    )
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return p


def _derived_run_id(backtest_path: Path, symbol: str, strategy_name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", backtest_path.stem)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"backtest:{symbol}:{strategy_name}:{stem}:{now}"


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.selftest:
        return _selftest()
    missing = [
        flag
        for flag, value in (
            ("--backtest-result", args.backtest_result),
            ("--strategy", args.strategy),
            ("--symbol", args.symbol),
            ("--output", args.output),
        )
        if value is None
    ]
    if missing:
        sys.stderr.write(
            f"missing required flags: {', '.join(missing)}\n"
        )
        return 2

    meta = parse_meta_from_file(args.strategy)
    run_id = args.auto_quant_run_id or _derived_run_id(
        args.backtest_result, args.symbol, meta.strategy
    )
    cfg = ExportConfig(
        backtest_result=args.backtest_result,
        strategy_path=args.strategy,
        symbol=args.symbol,
        output=args.output,
        auto_quant_run_id=run_id,
        pair_filter=args.pair_filter,
    )
    summary = run_export(cfg)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
