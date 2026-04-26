"""
prepare_ibkr.py — turn IBKR bridge data into FreqTrade feather files.

Two complementary input modes — pick whichever matches your workflow:

  --from-redis        Drain bars that bridge.py has already published into
                       Redis Streams (`ibkr:bars:<SYM>:<bar_size>`). Works
                       offline once the bridge has run, no Gateway needed
                       at prepare time. Best for "incremental top-up after
                       last bridge session".

  --from-csv-dir DIR  Ingest CSVs that `fetch_external.py ibkr-bulk` wrote
                       (default schema: `<SYM>_<bar_suffix>.csv` with cols
                       date,open,high,low,close,volume[,wap,count]). Best
                       for "build a clean cold-start backtest dataset".

Output: writes `<datadir>/<PAIR>-<tf>.feather` files with the FreqTrade
schema (`date` int64-ms, `open/high/low/close/volume` numeric). The same
schema `prepare_external.py` produces, so `run.py` / `run_tomac.py` /
`run_ibkr.py` all read it without modification.

Pair convention (Auto-Quant pseudo-pairs):
  IBKR symbol   →  FreqTrade pair
  ---------------- -----------------
  SPY           →  SPY/USD          (US equities, ETFs)
  AAPL          →  AAPL/USD
  EURUSD        →  EUR/USD          (CASH on IDEALPRO; underscore stripped)
  BTC           →  BTC/USD          (CRYPTO on PAXOS)

Override the mapping with `--pair-map SYM=PAIR,SYM=PAIR,...` if your
universe differs.

Usage:
  # Drain everything bridge.py has buffered for the last session:
  uv run prepare_ibkr.py --from-redis --bar-size '5 mins' \\
      --datadir user_data/data

  # Build cold-start dataset from ibkr-bulk CSV output:
  uv run prepare_ibkr.py --from-csv-dir user_data/data/ibkr_bulk \\
      --datadir user_data/data

This script never connects to IBKR. It only reads from Redis (which the
running bridge fills) or local CSVs. It therefore never needs consent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd


# ---------------------------------------------------------------------------
# Conventions

DEFAULT_REDIS_URL = "redis://localhost:6379"
DEFAULT_BAR_SIZE = "5 mins"
DEFAULT_DATADIR = Path("user_data/data")

# IBKR bar-size string -> FreqTrade timeframe label.
# We only emit timeframes FreqTrade understands.
_BAR_SIZE_TO_TF = {
    "1 secs":  "1s",   # FreqTrade ≥ 2025 supports 1s; older versions skip silently
    "5 secs":  "5s",
    "10 secs": "10s",
    "15 secs": "15s",
    "30 secs": "30s",
    "1 min":   "1m",
    "2 mins":  "2m",
    "3 mins":  "3m",
    "5 mins":  "5m",
    "10 mins": "10m",
    "15 mins": "15m",
    "20 mins": "20m",
    "30 mins": "30m",
    "1 hour":  "1h",
    "2 hours": "2h",
    "4 hours": "4h",
    "1 day":   "1d",
    "1 week":  "1w",
}

# Bulk-CSV file suffix -> IBKR bar-size. Strict inverse of the canonical
# table in fetch_external.py (`_BAR_SIZE_FILE_SUFFIX`) and
# bridge.py (`_BAR_SIZE_SUFFIX`). Used to recover the bar size from a
# filename `<SYM>_<suffix>.csv` produced by `ibkr-bulk`.
_BAR_SUFFIX_TO_BAR_SIZE = {
    "1sec":  "1 secs",  "5sec":  "5 secs", "10sec": "10 secs",
    "15sec": "15 secs", "30sec": "30 secs",
    "1min":  "1 min",   "2min":  "2 mins", "3min":  "3 mins",
    "5min":  "5 mins",  "10min": "10 mins", "15min": "15 mins",
    "20min": "20 mins", "30min": "30 mins",
    # Hour suffixes use the FreqTrade-style short form to stay round-trip
    # compatible with bridge.py / fetch_external.py.
    "1h":    "1 hour",  "2h":    "2 hours", "3h":    "3 hours",
    "4h":    "4 hours", "8h":    "8 hours",
    "1d":    "1 day",   "1w":    "1W",      "1mo":   "1M",
}

# Default IBKR symbol → FreqTrade pseudo-pair.
DEFAULT_PAIR_MAP: dict[str, str] = {
    # US equities + ETFs
    "SPY":  "SPY/USD",
    "QQQ":  "QQQ/USD",
    "IWM":  "IWM/USD",
    "AAPL": "AAPL/USD",
    "MSFT": "MSFT/USD",
    "NVDA": "NVDA/USD",
    "GOOGL":"GOOGL/USD",
    "META": "META/USD",
    "AMZN": "AMZN/USD",
    "TSLA": "TSLA/USD",
    # Forex (IDEALPRO) — strip the embedded second currency since FreqTrade
    # already encodes it as the quote in PAIR notation.
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "GBPUSD": "GBP/USD",
    "AUDUSD": "AUD/USD",
    "USDCHF": "USD/CHF",
    "USDCAD": "USD/CAD",
    "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY",
    # Crypto (PAXOS)
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "LTC": "LTC/USD",
    "BCH": "BCH/USD",
    # Indices (CME / CBOE)
    "ES":  "ES/USD",
    "NQ":  "NQ/USD",
    "YM":  "YM/USD",
    "RTY": "RTY/USD",
}

REQUIRED_COLS = ["date", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Pair-map handling

def parse_pair_map(spec: str | None) -> dict[str, str]:
    """Parse `SYM=PAIR,SYM=PAIR` into a dict, layered on top of defaults.

    User overrides win — pass `--pair-map SPY=SPY/USDT` to remap a default.
    """
    out = dict(DEFAULT_PAIR_MAP)
    if not spec:
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"bad --pair-map entry {chunk!r}, expected SYM=PAIR")
        sym, pair = chunk.split("=", 1)
        out[sym.strip().upper()] = pair.strip()
    return out


def symbol_to_pair(symbol: str, pair_map: dict[str, str]) -> str:
    """Return the FreqTrade pseudo-pair for an IBKR symbol.

    Falls back to ``<SYMBOL>/USD`` when the symbol isn't in the map, since
    that's the safe default for US-quoted instruments.
    """
    sym = symbol.upper()
    if sym in pair_map:
        return pair_map[sym]
    return f"{sym}/USD"


# ---------------------------------------------------------------------------
# Redis-side reader

def _read_from_redis(redis_url: str, bar_size: str,
                       symbols_filter: list[str] | None,
                       pair_map: dict[str, str]
                       ) -> dict[str, pd.DataFrame]:
    """Drain `ibkr:bars:<SYM>:<bar_size>` streams into per-symbol DataFrames.

    Uses ``XRANGE - +`` so every entry the bridge has accumulated lands in
    the result (Redis Streams are append-only and capped only via MAXLEN
    set by the bridge — see scripts/ibkr_bridge/bridge.py).
    """
    try:
        import redis  # noqa: WPS433  (lazy import; avoids redis dep for CSV path)
    except ImportError as exc:
        raise SystemExit(
            "--from-redis requires the `redis` package. "
            "Install via: uv add redis. "
            f"Underlying error: {exc}"
        )

    suffix = _bar_size_to_redis_suffix(bar_size)
    r = redis.Redis.from_url(redis_url, decode_responses=True,
                              socket_connect_timeout=2.0)
    try:
        r.ping()
    except redis.exceptions.RedisError as exc:
        raise SystemExit(
            f"Redis at {redis_url!r} is not reachable. Is the bridge "
            f"running? Or pass --from-csv-dir instead. Underlying: {exc}"
        )

    pattern = f"ibkr:bars:*:{suffix}"
    out: dict[str, pd.DataFrame] = {}
    for key in r.scan_iter(match=pattern, count=200):
        # ibkr:bars:<SYM>:<suffix>
        parts = key.split(":")
        if len(parts) != 4:
            continue
        sym = parts[2]
        if symbols_filter and sym.upper() not in {s.upper() for s in symbols_filter}:
            continue
        entries = r.xrange(key)
        if not entries:
            continue
        rows = []
        for _stream_id, fields in entries:
            rows.append(fields)
        df = pd.DataFrame(rows)
        df = _coerce_redis_bars(df)
        if df.empty:
            continue
        out[sym] = df
    return out


# Canonical bar-size -> Redis-key suffix table. MUST match the same-named
# table in scripts/ibkr_bridge/bridge.py (`_BAR_SIZE_SUFFIX`). They are
# duplicated rather than imported so this script can run without ever
# loading ib_async (which the bridge module pulls in).
_BAR_SIZE_TO_KEY_SUFFIX = {
    "1 secs":  "1sec",  "5 secs":  "5sec",  "10 secs": "10sec",
    "15 secs": "15sec", "30 secs": "30sec",
    "1 min":   "1min",  "2 mins":  "2min",  "3 mins":  "3min",
    "5 mins":  "5min",  "10 mins": "10min", "15 mins": "15min",
    "20 mins": "20min", "30 mins": "30min",
    "1 hour":  "1h",    "2 hours": "2h",    "3 hours": "3h",
    "4 hours": "4h",    "8 hours": "8h",
    "1 day":   "1d",    "1W":      "1w",    "1M":      "1mo",
}


def _bar_size_to_redis_suffix(bar_size: str) -> str:
    """Translate an IBKR bar-size string to the Redis-key suffix bridge.py uses.

    Falls back to ``bar_size.lower().replace(" ", "")`` for sizes outside
    the canonical table — same fallback bridge.py applies.
    """
    suffix = _BAR_SIZE_TO_KEY_SUFFIX.get(bar_size)
    if suffix:
        return suffix
    return bar_size.lower().replace(" ", "")


def _coerce_redis_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Convert raw Redis-stream rows to the FreqTrade-feather schema.

    The bridge writes `ts` (epoch seconds, float). We convert to the
    int64-ms ``date`` column FreqTrade expects.
    """
    if "ts" not in df.columns:
        return pd.DataFrame(columns=REQUIRED_COLS)
    df = df.copy()
    df["date"] = pd.to_datetime(pd.to_numeric(df["ts"], errors="coerce"),
                                  unit="s", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df.get(col, pd.Series(dtype=float)),
                                  errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return df[REQUIRED_COLS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# CSV-dir reader (output of `fetch_external.py ibkr-bulk`)

def _read_from_csv_dir(csv_dir: Path,
                         symbols_filter: list[str] | None,
                         pair_map: dict[str, str],
                         only_bar_size: str | None,
                         ) -> dict[tuple[str, str], pd.DataFrame]:
    """Walk a directory of CSVs produced by ibkr-bulk; return per-(sym,bar) frames.

    The default ibkr-bulk filename template is `{symbol}_{bar_suffix}.csv`,
    e.g. `SPY_5min.csv` or `BTC_1day.csv`. We parse that to recover the
    target bar size.
    """
    if not csv_dir.exists():
        raise SystemExit(f"--from-csv-dir not found: {csv_dir}")

    out: dict[tuple[str, str], pd.DataFrame] = {}
    for path in sorted(csv_dir.glob("*.csv")):
        stem = path.stem  # "SPY_5min"
        if "_" not in stem:
            print(f"  skip (cannot parse symbol_suffix): {path.name}",
                  file=sys.stderr)
            continue
        sym, suffix = stem.rsplit("_", 1)
        bar_size = _BAR_SUFFIX_TO_BAR_SIZE.get(suffix.lower())
        if bar_size is None:
            print(f"  skip (unknown bar-suffix '{suffix}'): {path.name}",
                  file=sys.stderr)
            continue
        if only_bar_size and bar_size != only_bar_size:
            continue
        if symbols_filter and sym.upper() not in {s.upper() for s in symbols_filter}:
            continue

        try:
            df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip (read error): {path.name}: {exc}", file=sys.stderr)
            continue
        coerced = _coerce_csv_bars(df)
        if coerced.empty:
            print(f"  skip (empty after coerce): {path.name}", file=sys.stderr)
            continue
        out[(sym, bar_size)] = coerced
    return out


def _coerce_csv_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce ibkr-bulk CSVs to the canonical schema.

    The CSVs already have FreqTrade-style columns (date, open, high, low,
    close, volume), but `date` is an ISO-8601 string written by Pandas.
    We re-parse to UTC datetime, then `write_feather` will convert to
    int64-ms before the on-disk write.
    """
    rename = {"timestamp": "date", "ts": "date"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "date" not in df.columns:
        return pd.DataFrame(columns=REQUIRED_COLS)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return df[REQUIRED_COLS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Writer (mirrors prepare_external.write_feather to keep schema identical)

def write_feather(df: pd.DataFrame, datadir: Path, pair: str,
                    timeframe: str) -> Path:
    """Write a per-(pair, tf) feather file. Returns absolute output path.

    The on-disk schema matches FreqTrade exactly:
      - cols: date(int64 ms), open, high, low, close, volume
      - filename: <PAIR_underscored>-<tf>.feather
    """
    datadir.mkdir(parents=True, exist_ok=True)
    pair_filename = pair.replace("/", "_").replace(":", "_")
    out_path = datadir / f"{pair_filename}-{timeframe}.feather"
    out = df.copy()
    # FreqTrade reads `date` as int64 ms. pandas Timestamp -> ns int -> ms int.
    out["date"] = (out["date"].astype("int64") // 1_000_000).astype("int64")
    out.to_feather(out_path)
    return out_path


def _emit_summary(label: str, written: list[tuple[str, str, int, Path]]) -> None:
    """Pretty-print a per-(pair, tf) summary block."""
    print(f"--- {label} ---")
    if not written:
        print("  (no files written)")
        return
    for pair, tf, n_rows, path in written:
        print(f"  {pair:<10s} {tf:>5s}  {n_rows:>7,} bars -> {path.name}")


# ---------------------------------------------------------------------------
# Glue

def _resolve_timeframe(bar_size: str) -> str:
    tf = _BAR_SIZE_TO_TF.get(bar_size)
    if tf is None:
        raise ValueError(
            f"unsupported bar_size {bar_size!r}; supported: "
            f"{sorted(_BAR_SIZE_TO_TF)}"
        )
    return tf


def _split_csv_filter(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# CLI

def main() -> int:
    p = argparse.ArgumentParser(
        description="Convert IBKR bridge data (Redis or ibkr-bulk CSVs) into "
                    "FreqTrade feather files.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-redis", action="store_true",
                       help="Drain bars from Redis Streams (`ibkr:bars:*`)")
    src.add_argument("--from-csv-dir", type=Path,
                       help="Read CSVs produced by `fetch_external.py ibkr-bulk`")

    p.add_argument("--redis-url", default=DEFAULT_REDIS_URL,
                     help=f"Redis URL (default {DEFAULT_REDIS_URL})")
    p.add_argument("--bar-size", default=DEFAULT_BAR_SIZE,
                     help=("IBKR bar-size string. With --from-redis this "
                           "selects which stream suffix to drain. With "
                           "--from-csv-dir, only files matching this bar "
                           "size are emitted (others ignored)."))
    p.add_argument("--symbols", default=None,
                     help=("Optional comma-separated IBKR-symbol whitelist; "
                           "default is to convert every symbol found."))
    p.add_argument("--pair-map", default=None,
                     help=("Override default symbol→pair map: "
                           "`SYM=PAIR,SYM=PAIR,...`."))
    p.add_argument("--datadir", type=Path, default=DEFAULT_DATADIR,
                     help=f"Destination feather dir (default {DEFAULT_DATADIR})")
    p.add_argument("--all-bar-sizes", action="store_true",
                     help=("CSV mode only: emit feathers for ALL bar sizes "
                           "found in the CSV directory (default: only the "
                           "one matching --bar-size)."))

    args = p.parse_args()

    pair_map = parse_pair_map(args.pair_map)
    symbols_filter = _split_csv_filter(args.symbols)

    written: list[tuple[str, str, int, Path]] = []

    if args.from_redis:
        timeframe = _resolve_timeframe(args.bar_size)
        per_sym = _read_from_redis(args.redis_url, args.bar_size,
                                      symbols_filter, pair_map)
        if not per_sym:
            print("WARN: no data found in Redis for the given bar-size. "
                  "Has the bridge published any bars yet?", file=sys.stderr)
            return 3
        for sym, df in sorted(per_sym.items()):
            pair = symbol_to_pair(sym, pair_map)
            out_path = write_feather(df, args.datadir, pair, timeframe)
            written.append((pair, timeframe, len(df), out_path))
        _emit_summary(f"prepare_ibkr (redis @ {args.bar_size})", written)
        return 0

    # --from-csv-dir
    only_bs = None if args.all_bar_sizes else args.bar_size
    per_pair = _read_from_csv_dir(args.from_csv_dir, symbols_filter,
                                     pair_map, only_bs)
    if not per_pair:
        print("WARN: no usable CSVs found.", file=sys.stderr)
        return 3
    for (sym, bar_size), df in sorted(per_pair.items()):
        pair = symbol_to_pair(sym, pair_map)
        try:
            tf = _resolve_timeframe(bar_size)
        except ValueError as exc:
            print(f"  skip {sym} ({bar_size}): {exc}", file=sys.stderr)
            continue
        out_path = write_feather(df, args.datadir, pair, tf)
        written.append((pair, tf, len(df), out_path))
    _emit_summary(f"prepare_ibkr (csv-dir {args.from_csv_dir})", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
