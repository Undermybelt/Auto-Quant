"""
TomacRRWinRate — RSI-extreme mean reversion gated by 1d bull regime, targeting Tomac's high-win-rate / 1.5RR profile.

Paradigm: mean-reversion
Hypothesis: On 1h crypto, RSI<25 oversold strikes within a 1d bullish regime (close>EMA200_1d) tend to revert to RSI>=55 fast enough to clear a fixed 3%-take-profit / 2%-stop (1.5RR) target a high fraction of the time, mirroring Tomac's "90wr 1.5rrr" win-rate-prioritized profile but on a different asset class.
Parent: root
Created: pending-first-commit
Status: active
Uses MTF: yes

Provenance: idea seeded by /Users/thrill3r/Downloads/Tomac/90wr1.5rrr_final.py, 90wr1.5rrr_balanced.py, and ict_90wr_1.5rrr_strategy.py (read-only reference). No Tomac code is imported or executed; only the win-rate-over-RR philosophy is re-expressed natively via fixed-RR FreqTrade primitives.
"""
from __future__ import annotations

import talib.abstract as ta
from freqtrade.strategy import IStrategy, informative
from pandas import DataFrame


class TomacRRWinRate(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 0.03}
    stoploss = -0.02

    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    startup_candle_count: int = 300

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2, nbdevdn=2)
        dataframe["bb_lower"] = bb["lowerband"]
        dataframe["bb_middle"] = bb["middleband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        oversold_1h = dataframe["rsi"] < 25
        below_bb = dataframe["close"] < dataframe["bb_lower"]
        not_overbought_4h = dataframe["rsi_4h"] < 60
        bull_regime_1d = (dataframe["close"] > dataframe["ema200_1d"]) & (
            dataframe["ema50_1d"] > dataframe["ema200_1d"]
        )
        dataframe.loc[
            oversold_1h & below_bb & not_overbought_4h & bull_regime_1d,
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        recovered = dataframe["rsi"] > 55
        regime_break = dataframe["close"] < dataframe["ema200_1d"]
        dataframe.loc[recovered | regime_break, "exit_long"] = 1
        return dataframe
