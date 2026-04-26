"""
TomacKillzoneBreakout — US-session range breakout filtered by 4h trend, derived from Tomac ICT killzone DNA.

Paradigm: breakout
Hypothesis: On 1h crypto, breakouts above the prior 24h high concentrated in the US-active window (UTC 13-21) deliver follow-through when the 4h trend (EMA21>EMA89) agrees, mirroring Tomac's AM/PM killzone exploit on futures (re-expressed for 24/7 spot crypto).
Parent: root
Created: pending-first-commit
Status: active
Uses MTF: yes

Provenance: idea seeded by /Users/thrill3r/Downloads/Tomac/ultimate_ict_strategy.py and 90wr1.5rrr_strategy.py (read-only reference). No Tomac code is imported or executed; only the trading idea (session-confined breakout with trend filter) is re-expressed natively.
"""
from __future__ import annotations

import talib.abstract as ta
from freqtrade.strategy import IStrategy, informative
from pandas import DataFrame


class TomacKillzoneBreakout(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.05

    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    startup_candle_count: int = 250

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=89)
        return dataframe

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["high_24h"] = dataframe["high"].rolling(24).max().shift(1)
        dataframe["low_24h"] = dataframe["low"].rolling(24).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["hour_utc"] = dataframe["date"].dt.hour
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        in_killzone = (dataframe["hour_utc"] >= 13) & (dataframe["hour_utc"] <= 20)
        breakout = dataframe["close"] > dataframe["high_24h"]
        trend_4h = dataframe["ema_fast_4h"] > dataframe["ema_slow_4h"]
        regime_1d = dataframe["close"] > dataframe["ema200_1d"]
        dataframe.loc[
            in_killzone & breakout & trend_4h & regime_1d,
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakdown = dataframe["close"] < dataframe["low_24h"]
        trend_break_4h = dataframe["ema_fast_4h"] < dataframe["ema_slow_4h"]
        dataframe.loc[breakdown | trend_break_4h, "exit_long"] = 1
        return dataframe
