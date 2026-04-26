"""
TomacAggressiveBE — fast-trigger trailing stop on 1h EMA cross with 4h trend confirmation, modelled after Tomac's BE_TRIGGER_R=0.3 break-even discipline.

Paradigm: trend-following
Hypothesis: On 1h crypto, an EMA9>EMA21 cross under a 4h bullish regime captures most of the move, and a tight trailing stop activated very early (positive offset 0.5%, trail 0.3%) protects against give-back at the cost of giving up runners — re-expressing Tomac's aggressive break-even-at-0.3R management for FreqTrade.
Parent: root
Created: pending-first-commit
Status: active
Uses MTF: yes

Provenance: idea seeded by /Users/thrill3r/Downloads/Tomac/optimal_be_1.0.py and no_be_strategy.py (read-only reference). No Tomac code is imported or executed; only the early-BE risk-management discipline is re-expressed natively via FreqTrade trailing-stop primitives.
"""
from __future__ import annotations

import talib.abstract as ta
from freqtrade.strategy import IStrategy, informative
from pandas import DataFrame


class TomacAggressiveBE(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.04

    trailing_stop = True
    trailing_stop_positive = 0.003
    trailing_stop_positive_offset = 0.005
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    startup_candle_count: int = 200

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=55)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema9_prev"] = dataframe["ema9"].shift(1)
        dataframe["ema21_prev"] = dataframe["ema21"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_up = (dataframe["ema9_prev"] <= dataframe["ema21_prev"]) & (
            dataframe["ema9"] > dataframe["ema21"]
        )
        rsi_ok = dataframe["rsi"] > 45
        trend_4h_up = dataframe["ema_fast_4h"] > dataframe["ema_slow_4h"]
        dataframe.loc[cross_up & rsi_ok & trend_4h_up, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cross_down = (dataframe["ema9_prev"] >= dataframe["ema21_prev"]) & (
            dataframe["ema9"] < dataframe["ema21"]
        )
        rsi_overbought = dataframe["rsi"] > 75
        dataframe.loc[cross_down | rsi_overbought, "exit_long"] = 1
        return dataframe
