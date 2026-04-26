"""
export_strategy_library.py — Auto-Quant -> ict-engine handoff producer

Scans `user_data/strategies_ibkr/` and a `run_ibkr.log` to produce a
`strategy_library.json` manifest that ict-engine's
`auto-quant-results-import` command consumes.

Each entry in the manifest carries:
  - the strategy's AUTO_QUANT_META block (parsed from the .py source)
  - aggregate validation metrics (sharpe / dd / wr / pf / trade_count …)
  - per-pair metrics
  - a status field: "ok" | "error" | "not_run"

The strategy-level metric block is the SOURCE of empirical-Bayes
pseudo-counts used by ict-engine to seed the `trade_outcome` CPT prior
(Phase 1 of the Auto-Quant -> BBN integration; see ict-engine
`docs/2026-04-26-auto-quant-bbn-prior-init-plan.md`).

The script is intentionally read-only: it never modifies strategies, never
shells out to FreqTrade, and never re-runs backtests. It simply unifies
two on-disk truths (strategies dir + most recent log) into a single JSON
artifact suitable for cross-repo consumption.

Run with:
    python export_strategy_library.py --selftest
or
    python export_strategy_library.py \
        --strategies-dir user_data/strategies_ibkr \
        --log run_ibkr.log \
        --output state/auto_quant_strategy_library.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from auto_quant_meta import (
    StrategyMeta,
    StrategyMetaError,
    discover_meta_in_dir,
    parse_meta_from_file,
)

MANIFEST_VERSION = "1.0"
PROJECT_DIR = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Log parsing

# A single `---` block emitted by run_ibkr.py looks like:
#
#   ---
#   strategy:         TestStrat
#   commit:           abc1234
#   config:           config.ibkr.json
#   timerange:        20240101-20240201
#   pairs:            SPY/USD,QQQ/USD
#   auto_quant_meta:  {"strategy":"TestStrat",...}
#   sharpe:           1.4200
#   sortino:          2.1300
#   ...
#   per_pair:
#     SPY/USD: sharpe=1.5000 trades=50 profit_pct=15.00 dd_pct=-2.50 wr=58.0 pf=2.10
#
# Or, on failure:
#
#   ---
#   strategy:         TestStrat
#   commit:           abc1234
#   config:           config.ibkr.json
#   auto_quant_meta:  {...}
#   status:           ERROR
#   error_type:       ValueError
#   error_msg:        ...
#   traceback:
#   ...

_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*?)\s*$")
_PAIR_LINE_RE = re.compile(
    r"^\s*(?P<pair>\S+):\s+"
    r"sharpe=(?P<sharpe>-?\d+(?:\.\d+)?)\s+"
    r"trades=(?P<trades>\d+)\s+"
    r"profit_pct=(?P<profit_pct>-?\d+(?:\.\d+)?)\s+"
    r"dd_pct=(?P<dd_pct>-?\d+(?:\.\d+)?)\s+"
    r"wr=(?P<wr>-?\d+(?:\.\d+)?)\s+"
    r"pf=(?P<pf>-?\d+(?:\.\d+)?)\s*$"
)

_AGGREGATE_FLOAT_KEYS = {
    "sharpe", "sortino", "calmar",
    "total_profit_pct", "max_drawdown_pct",
    "win_rate_pct", "profit_factor",
}
_AGGREGATE_INT_KEYS = {"trade_count"}
_BLOCK_LEVEL_KEYS = {
    "strategy", "commit", "config", "timerange", "pairs",
    "auto_quant_meta", "status", "error_type", "error_msg",
}


@dataclass
class LogBlock:
    """One `---` block worth of structured data lifted from the log."""
    strategy: str = ""
    commit: str = ""
    config: str = ""
    timerange: str = ""
    pairs: list[str] = field(default_factory=list)
    auto_quant_meta: dict[str, Any] | None = None
    status: str = "ok"
    error_type: str = ""
    error_msg: str = ""
    aggregate: dict[str, Any] = field(default_factory=dict)
    per_pair: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_log(log_path: str | Path) -> list[LogBlock]:
    """Split a run_ibkr.log into a list of structured LogBlocks.

    Unknown lines (preamble, traceback bodies, etc.) are silently ignored;
    only the `---` block boundaries and the documented key/value lines
    matter.
    """
    text = Path(log_path).read_text()
    blocks: list[LogBlock] = []
    current: LogBlock | None = None
    in_per_pair = False

    for raw in text.splitlines():
        if raw.strip() == "---":
            if current is not None:
                blocks.append(current)
            current = LogBlock()
            in_per_pair = False
            continue
        if current is None:
            continue

        if raw.startswith("per_pair:"):
            in_per_pair = True
            continue

        if in_per_pair:
            m = _PAIR_LINE_RE.match(raw)
            if m:
                current.per_pair[m.group("pair")] = {
                    "sharpe":           float(m.group("sharpe")),
                    "trade_count":      int(m.group("trades")),
                    "total_profit_pct": float(m.group("profit_pct")),
                    "max_drawdown_pct": float(m.group("dd_pct")),
                    "win_rate_pct":     float(m.group("wr")),
                    "profit_factor":    float(m.group("pf")),
                }
                continue
            # A non-matching line ends the per_pair section.
            in_per_pair = False

        m = _KV_RE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2)

        if key in _BLOCK_LEVEL_KEYS:
            if key == "pairs":
                current.pairs = [p for p in value.split(",") if p]
            elif key == "auto_quant_meta":
                try:
                    current.auto_quant_meta = json.loads(value)
                except json.JSONDecodeError:
                    current.auto_quant_meta = None
            elif key == "status":
                current.status = "error" if value.upper() == "ERROR" else "ok"
            else:
                setattr(current, key, value)
        elif key in _AGGREGATE_FLOAT_KEYS:
            try:
                current.aggregate[key] = float(value)
            except ValueError:
                continue
        elif key in _AGGREGATE_INT_KEYS:
            try:
                current.aggregate[key] = int(value)
            except ValueError:
                continue

    if current is not None:
        blocks.append(current)
    return blocks


# ---------------------------------------------------------------------------
# Manifest assembly

def _git_rev(cwd: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(cwd),
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _git_remote(cwd: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=str(cwd),
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out
    except Exception:
        return ""


def _config_timeframe(config_path: Path) -> str:
    try:
        return json.loads(config_path.read_text()).get("timeframe", "")
    except Exception:
        return ""


def assemble_manifest(
    strategies_dir: Path,
    log_path: Path | None,
    config_path: Path,
    project_dir: Path,
) -> dict[str, Any]:
    """Build the manifest dict by joining strategies on disk with log entries.

    For each strategy:
      - If the log contains a block with a matching `strategy:` field, use
        the log's metrics + meta (the log reflects what was actually run).
      - Otherwise mark the strategy as `status="not_run"` and emit just
        its source-file meta.

    Strategies whose source meta block is malformed are NOT included in
    the manifest — they are listed in the manifest's `validation_errors`
    field for operator visibility.
    """
    log_blocks: dict[str, LogBlock] = {}
    if log_path and log_path.exists():
        for block in parse_log(log_path):
            if block.strategy:
                log_blocks[block.strategy] = block

    strategies: list[dict[str, Any]] = []
    validation_errors: list[dict[str, str]] = []

    for path, result in discover_meta_in_dir(strategies_dir):
        if isinstance(result, StrategyMetaError):
            validation_errors.append({
                "file": str(path.relative_to(project_dir))
                        if path.is_relative_to(project_dir) else str(path),
                "error": str(result),
            })
            continue
        meta: StrategyMeta = result
        name = path.stem
        block = log_blocks.get(name)

        entry: dict[str, Any] = {
            "name":      name,
            "file_path": str(path.relative_to(project_dir))
                         if path.is_relative_to(project_dir) else str(path),
            "metadata":  meta.to_json_dict(),
        }

        if block is None:
            entry["status"] = "not_run"
            entry["validation_metrics"] = None
            entry["per_pair_metrics"] = {}
        elif block.status == "error":
            entry["status"] = "error"
            entry["error"] = {
                "type":    block.error_type,
                "message": block.error_msg,
            }
            entry["validation_metrics"] = None
            entry["per_pair_metrics"] = {}
        else:
            entry["status"] = "ok"
            entry["validation_metrics"] = block.aggregate
            entry["per_pair_metrics"] = block.per_pair
            entry["pairs"] = block.pairs
            entry["timerange"] = block.timerange
            entry["commit"] = block.commit

        strategies.append(entry)

    manifest = {
        "manifest_version":      MANIFEST_VERSION,
        "exported_at":           _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "auto_quant_repo_url":   _git_remote(project_dir),
        "auto_quant_pinned_ref": _git_rev(project_dir),
        "config_path":           str(config_path.relative_to(project_dir))
                                  if config_path.is_relative_to(project_dir)
                                  else str(config_path),
        "timeframe":             _config_timeframe(config_path),
        "log_path":              str(log_path.relative_to(project_dir))
                                  if log_path and log_path.is_relative_to(project_dir)
                                  else (str(log_path) if log_path else ""),
        "strategies":            strategies,
        "validation_errors":     validation_errors,
    }
    return manifest


# ---------------------------------------------------------------------------
# CLI

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export Auto-Quant strategy library manifest for ict-engine.",
    )
    p.add_argument(
        "--strategies-dir",
        default=str(PROJECT_DIR / "user_data" / "strategies_ibkr"),
        help="Directory containing strategy .py files",
    )
    p.add_argument(
        "--log",
        default=str(PROJECT_DIR / "run_ibkr.log"),
        help="Path to most recent run_ibkr.log (optional; if absent, all "
             "strategies are marked status=not_run)",
    )
    p.add_argument(
        "--config",
        default=str(PROJECT_DIR / "config.ibkr.json"),
        help="Backtest config (used for timeframe extraction)",
    )
    p.add_argument(
        "--output",
        default=str(PROJECT_DIR / "auto_quant_strategy_library.json"),
        help="Output manifest path",
    )
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return p


def _selftest() -> int:
    """In-memory test of parse_log + assemble_manifest invariants."""
    import io
    import tempfile

    sample_log = """preamble line
---
strategy:         GoodStrat
commit:           abc1234
config:           config.ibkr.json
timerange:        20240101-20240201
pairs:            SPY/USD,QQQ/USD
auto_quant_meta:  {"strategy":"GoodStrat","mutation_id":"mb-001","base_factor":"f","hypothesis":"h","paradigm":"p","expected_regime":"r","factors_used":["bos"],"parent":"root","asset_class":"equities","status":"active","created":""}
sharpe:           1.4200
sortino:          2.1300
calmar:           4.5000
total_profit_pct: 12.3000
max_drawdown_pct: -3.2000
trade_count:      87
win_rate_pct:     54.5000
profit_factor:    1.8500
per_pair:
  SPY/USD: sharpe=1.5000 trades=50 profit_pct=15.00 dd_pct=-2.50 wr=58.0 pf=2.10
  QQQ/USD: sharpe=1.3000 trades=37 profit_pct=8.50 dd_pct=-3.80 wr=49.0 pf=1.55

---
strategy:         BrokenStrat
commit:           abc1234
config:           config.ibkr.json
auto_quant_meta:  {"strategy":"BrokenStrat","mutation_id":"mb-002","base_factor":"f","hypothesis":"h","paradigm":"p","expected_regime":"r","factors_used":["x"],"parent":"root","asset_class":"equities","status":"active","created":""}
status:           ERROR
error_type:       ValueError
error_msg:        boom
traceback:
  File "x", line 1
"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        log_path = tdp / "run_ibkr.log"
        log_path.write_text(sample_log)

        blocks = parse_log(log_path)
        assert len(blocks) == 2, f"expected 2 blocks, got {len(blocks)}"

        good, broken = blocks
        assert good.strategy == "GoodStrat"
        assert good.status == "ok"
        assert good.aggregate["sharpe"] == 1.42
        assert good.aggregate["trade_count"] == 87
        assert good.per_pair["SPY/USD"]["trade_count"] == 50
        assert good.auto_quant_meta is not None
        assert good.auto_quant_meta["mutation_id"] == "mb-001"
        print(f"[parse_log] good: sharpe={good.aggregate['sharpe']} "
              f"per_pair_keys={list(good.per_pair)}")

        assert broken.strategy == "BrokenStrat"
        assert broken.status == "error"
        assert broken.error_type == "ValueError"
        print(f"[parse_log] broken: error_type={broken.error_type}")

        # assemble_manifest with only the BrokenStrat strategy file present.
        strategies_dir = tdp / "strategies"
        strategies_dir.mkdir()
        (strategies_dir / "BrokenStrat.py").write_text(
            '"""\n# AUTO_QUANT_META v1\n'
            'Strategy:        BrokenStrat\n'
            'Mutation_id:     mb-002\n'
            'Base_factor:     f\n'
            'Hypothesis:      h\n'
            'Paradigm:        p\n'
            'Expected_regime: r\n'
            'Factors_used:    x\n'
            'Parent:          root\n'
            'Asset_class:     equities\n'
            'Status:          active\n'
            '# END_AUTO_QUANT_META\n"""\n'
        )
        config_path = tdp / "config.ibkr.json"
        config_path.write_text('{"timeframe":"5m"}')

        manifest = assemble_manifest(
            strategies_dir=strategies_dir,
            log_path=log_path,
            config_path=config_path,
            project_dir=tdp,
        )
        assert manifest["manifest_version"] == MANIFEST_VERSION
        assert manifest["timeframe"] == "5m"
        names = [s["name"] for s in manifest["strategies"]]
        assert names == ["BrokenStrat"], names
        entry = manifest["strategies"][0]
        assert entry["status"] == "error"
        assert entry["error"]["type"] == "ValueError"
        assert entry["validation_metrics"] is None
        print(f"[assemble] entry: name={entry['name']} status={entry['status']}")

    print("PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.selftest:
        return _selftest()

    strategies_dir = Path(args.strategies_dir).resolve()
    if not strategies_dir.exists():
        print(f"ERROR: strategies dir not found: {strategies_dir}", file=sys.stderr)
        return 2

    log_path = Path(args.log).resolve() if args.log else None
    config_path = Path(args.config).resolve()
    output_path = Path(args.output).resolve()

    manifest = assemble_manifest(
        strategies_dir=strategies_dir,
        log_path=log_path,
        config_path=config_path,
        project_dir=PROJECT_DIR,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2))

    n = len(manifest["strategies"])
    n_ok = sum(1 for s in manifest["strategies"] if s["status"] == "ok")
    n_err = sum(1 for s in manifest["strategies"] if s["status"] == "error")
    n_not_run = sum(1 for s in manifest["strategies"] if s["status"] == "not_run")
    n_invalid = len(manifest["validation_errors"])
    print(
        f"Wrote {output_path} "
        f"({n} strategies: ok={n_ok} error={n_err} not_run={n_not_run}; "
        f"meta_invalid={n_invalid})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
