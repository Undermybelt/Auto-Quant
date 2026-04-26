"""
run_ibkr.py — sibling of run.py / run_tomac.py for IBKR-backed strategies.

Runs every strategy in `user_data/strategies_ibkr/` against the prepared
IBKR feather data, using `config.ibkr.json` (5m timeframe by default,
synthetic pseudo-pairs like SPY/USD, EUR/USD, BTC/USD).

Why a separate runner?

  * `run.py` is the agent's READ-ONLY oracle for the crypto loop and
    must stay untouched (program.md contract).
  * `run_tomac.py` is the equivalent for non-crypto futures and uses a
    custom synthetic-market factory.
  * `run_ibkr.py` is the equivalent for IBKR live-data strategies. It
    reuses run_tomac's synthetic-market trick so FreqTrade accepts
    pseudo-pairs that don't exist on Binance, but is otherwise pure
    backtest — no IBKR network calls happen here.

Workflow:
    # T0: bridge runs in another shell (or already finished a session)
    uv run prepare_ibkr.py --from-redis --bar-size '5 mins'
    uv run run_ibkr.py > run_ibkr.log 2>&1

The agent reads `run_ibkr.log` (one `---` block per strategy, matching
the canonical run.py format) to drive the keep/evolve/fork/kill loop.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from freqtrade.configuration import Configuration
from freqtrade.enums import RunMode
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.resolvers import ExchangeResolver

from auto_quant_meta import (
    StrategyMeta,
    StrategyMetaError,
    discover_meta_in_dir,
)

PROJECT_DIR = Path(__file__).parent.resolve()
USER_DATA = PROJECT_DIR / "user_data"
STRATEGIES_DIR = USER_DATA / "strategies_ibkr"
DATA_DIR = USER_DATA / "data"
CONFIG = PROJECT_DIR / "config.ibkr.json"


# ---------------------------------------------------------------------------
# Synthetic market factory (lifted from run_tomac.py with the same shape).
# IBKR pseudo-pairs do not exist on Binance, so we hand-craft a market
# entry that satisfies FreqTrade's IPairList._whitelist_for_active_markets
# without touching FreqTrade source.

def _synthetic_market(pair: str) -> dict[str, Any]:
    base, quote = pair.split("/", 1)
    return {
        "id": pair.replace("/", ""),
        "symbol": pair,
        "base": base,
        "quote": quote,
        "active": True,
        "type": "spot",
        "spot": True,
        "margin": False,
        "swap": False,
        "future": False,
        "option": False,
        "contract": False,
        "linear": None,
        "inverse": None,
        "settle": None,
        "settleId": None,
        "expiry": None,
        "expiryDatetime": None,
        "strike": None,
        "optionType": None,
        "taker": 0.0,
        "maker": 0.0,
        "percentage": True,
        "tierBased": False,
        "feeSide": "get",
        "precision": {"amount": 8, "price": 8, "base": 8, "quote": 8},
        "limits": {
            "amount": {"min": 0, "max": None},
            "price":  {"min": 0, "max": None},
            "cost":   {"min": 0, "max": None},
            "leverage": {"min": 1, "max": 1},
        },
        "info": {},
    }


def _build_exchange_with_synthetic_pairs(config: dict[str, Any]):
    exchange = ExchangeResolver.load_exchange(config, load_leverage_tiers=False)
    for pair in config["exchange"].get("pair_whitelist", []):
        if pair not in exchange._markets:
            exchange._markets[pair] = _synthetic_market(pair)
    return exchange


# ---------------------------------------------------------------------------
# Metric extraction (mirrors run.py / run_tomac.py)

def _get(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _entry_metrics(entry: dict[str, Any]) -> dict[str, float]:
    return {
        "sharpe":           _get(entry, "sharpe", "sharpe_ratio"),
        "sortino":          _get(entry, "sortino", "sortino_ratio"),
        "calmar":           _get(entry, "calmar", "calmar_ratio"),
        "total_profit_pct": _get(entry, "profit_total_pct"),
        "max_drawdown_pct": -abs(_get(entry, "max_drawdown_account")) * 100,
        "trade_count":      int(_get(entry, "trades", "total_trades")),
        "win_rate_pct":     _get(entry, "winrate") * 100,
        "profit_factor":    _get(entry, "profit_factor"),
    }


def extract_metrics(results: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    strat = results.get("strategy", {}).get(strategy_name, {}) or {}
    per_pair_list = strat.get("results_per_pair", []) or []
    aggregate: dict[str, float] = {}
    per_pair: dict[str, dict[str, float]] = {}
    for entry in per_pair_list:
        key = entry.get("key", "")
        metrics = _entry_metrics(entry)
        if key == "TOTAL":
            aggregate = metrics
        elif key:
            per_pair[key] = metrics
    if not aggregate:
        aggregate = _entry_metrics(strat)
    return {"aggregate": aggregate, "per_pair": per_pair}


# ---------------------------------------------------------------------------
# Backtest entrypoint

def get_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_DIR), text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def discover_strategies() -> list[tuple[str, StrategyMeta]]:
    """Discover strategies and validate their AUTO_QUANT_META blocks.

    Returns a list of (name, meta) tuples. Raises SystemExit with a clean
    aggregated error message if ANY strategy fails meta validation, so the
    operator sees the full failure list before any backtest runs.
    """
    if not STRATEGIES_DIR.exists():
        return []
    found: list[tuple[str, StrategyMeta]] = []
    errors: list[str] = []
    for path, result in discover_meta_in_dir(STRATEGIES_DIR):
        if isinstance(result, StrategyMetaError):
            errors.append(f"  {path.name}: {result}")
            continue
        if result.strategy != path.stem:
            errors.append(
                f"  {path.name}: AUTO_QUANT_META Strategy='{result.strategy}' "
                f"does not match filename stem '{path.stem}'"
            )
            continue
        found.append((path.stem, result))
    if errors:
        raise SystemExit(
            "ERROR: AUTO_QUANT_META validation failed for the following "
            "strategies. Fix the docstrings (see _template.py.example) "
            "before running:\n" + "\n".join(errors)
        )
    return found


def run_backtest(strategy_name: str) -> dict[str, Any]:
    args = {
        "config":         [str(CONFIG)],
        "user_data_dir":  str(USER_DATA),
        "datadir":        str(DATA_DIR),
        "strategy":       strategy_name,
        "strategy_path":  str(STRATEGIES_DIR),
        "export":         "none",
        "exportfilename": None,
        "cache":          "none",
    }
    config = Configuration(args, RunMode.BACKTEST).get_config()
    exchange = _build_exchange_with_synthetic_pairs(config)
    bt = Backtesting(config, exchange=exchange)
    bt.start()
    return bt.results


# ---------------------------------------------------------------------------
# Output (matches run_tomac.py block format for easy agent parsing)

def emit_block(strategy_name: str, meta: StrategyMeta, commit: str,
                 config_pairs: list[str], timerange: str,
                 metrics: dict[str, Any]) -> None:
    agg = metrics["aggregate"]
    print("---")
    print(f"strategy:         {strategy_name}")
    print(f"commit:           {commit}")
    print(f"config:           {CONFIG.name}")
    print(f"timerange:        {timerange}")
    print(f"pairs:            {','.join(config_pairs)}")
    print(f"auto_quant_meta:  {json.dumps(meta.to_json_dict(), separators=(',', ':'))}")
    print(f"sharpe:           {agg['sharpe']:.4f}")
    print(f"sortino:          {agg['sortino']:.4f}")
    print(f"calmar:           {agg['calmar']:.4f}")
    print(f"total_profit_pct: {agg['total_profit_pct']:.4f}")
    print(f"max_drawdown_pct: {agg['max_drawdown_pct']:.4f}")
    print(f"trade_count:      {agg['trade_count']}")
    print(f"win_rate_pct:     {agg['win_rate_pct']:.4f}")
    print(f"profit_factor:    {agg['profit_factor']:.4f}")
    if metrics["per_pair"]:
        print("per_pair:")
        for pair in config_pairs:
            m = metrics["per_pair"].get(pair)
            if m is None:
                print(f"  {pair}: (no data)")
                continue
            print(
                f"  {pair}: sharpe={m['sharpe']:.4f} "
                f"trades={m['trade_count']} "
                f"profit_pct={m['total_profit_pct']:.2f} "
                f"dd_pct={m['max_drawdown_pct']:.2f} "
                f"wr={m['win_rate_pct']:.1f} "
                f"pf={m['profit_factor']:.2f}"
            )
    print()


def emit_error(strategy_name: str, meta: StrategyMeta, commit: str,
                 exc: BaseException) -> None:
    print("---")
    print(f"strategy:         {strategy_name}")
    print(f"commit:           {commit}")
    print(f"config:           {CONFIG.name}")
    print(f"auto_quant_meta:  {json.dumps(meta.to_json_dict(), separators=(',', ':'))}")
    print(f"status:           ERROR")
    print(f"error_type:       {type(exc).__name__}")
    print(f"error_msg:        {exc}")
    print("traceback:")
    traceback.print_exc()
    print()


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    if not CONFIG.exists():
        print(f"ERROR: {CONFIG} not found", file=sys.stderr)
        return 2
    if not STRATEGIES_DIR.exists():
        print(
            f"ERROR: {STRATEGIES_DIR} not found.\n"
            "  Create it and add at least one .py strategy file. "
            "See user_data/strategies_ibkr/_template.py.example for the skeleton.",
            file=sys.stderr,
        )
        return 2

    cfg = json.loads(CONFIG.read_text())
    config_pairs = cfg["exchange"]["pair_whitelist"]
    timerange = cfg.get("timerange", "")

    # Sanity: warn if the pairs in the config don't have feather files yet.
    missing = []
    tf = cfg.get("timeframe", "5m")
    for pair in config_pairs:
        feather = DATA_DIR / f"{pair.replace('/', '_')}-{tf}.feather"
        if not feather.exists():
            missing.append(feather.name)
    if missing:
        print(
            f"WARN: {len(missing)} pair(s) missing prepared feather files: "
            f"{', '.join(missing)}.\n"
            f"  Run: uv run prepare_ibkr.py --from-redis --bar-size "
            f"'{_tf_to_bar_size(tf)}'",
            file=sys.stderr,
        )

    strategies = discover_strategies()
    if not strategies:
        print(
            f"ERROR: no strategies found in {STRATEGIES_DIR}.\n"
            "  Drop at least one .py file in there (see "
            "_template.py.example for the skeleton).",
            file=sys.stderr,
        )
        return 2

    commit = get_commit()
    names = [name for name, _ in strategies]
    print(f"Discovered {len(strategies)} strategies: {', '.join(names)}")
    print(f"Timerange: {timerange}  Pairs: {','.join(config_pairs)}")
    print()

    n_ok = n_err = 0
    for name, meta in strategies:
        try:
            results = run_backtest(name)
            metrics = extract_metrics(results, name)
            emit_block(name, meta, commit, config_pairs, timerange, metrics)
            n_ok += 1
        except BaseException as exc:  # noqa: BLE001  (mirror run.py isolation)
            emit_error(name, meta, commit, exc)
            n_err += 1

    print(f"Done: {n_ok} succeeded, {n_err} failed.")
    return 0 if n_err == 0 else 1


# Inverse of prepare_ibkr's _BAR_SIZE_TO_TF — only used for the friendly
# WARN message when a feather is missing. Keep the most common entries.
def _tf_to_bar_size(tf: str) -> str:
    return {
        "1s": "1 secs", "5s": "5 secs", "10s": "10 secs",
        "1m": "1 min", "5m": "5 mins", "15m": "15 mins", "30m": "30 mins",
        "1h": "1 hour", "4h": "4 hours", "1d": "1 day",
    }.get(tf, tf)


if __name__ == "__main__":
    raise SystemExit(main())
