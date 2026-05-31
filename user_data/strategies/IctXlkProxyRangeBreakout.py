"""
IctXlkProxyRangeBreakout - compression breakout with higher-timeframe permission

Paradigm: breakout
Hypothesis: The XLK reclaim branch maps to crypto when quiet 1h compression
            resolves above a rolling range while 4h trend and participation
            confirm expansion instead of a range fakeout.
Parent: ict-engine IBKR_XLK_VWAP_RECLAIM_GATE_TAIL
Created: 34ba6b6ee6aa69813a50a72158d4c089d97afb96
Status: active
Uses MTF: yes
"""

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy, informative


class IctXlkProxyRangeBreakout(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.16
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 260

    pair_basket = ["ETH/USDT", "SOL/USDT", "AVAX/USDT"]
    test_timeranges = [
        ("full_5y", "20210101-20251231"),
        ("test_25", "20250101-20251231"),
    ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["atr_pct_sma"] = dataframe["atr_pct"].rolling(72).mean()
        dataframe["range_high"] = dataframe["high"].rolling(24).max().shift(1)
        dataframe["range_low"] = dataframe["low"].rolling(24).min().shift(1)
        dataframe["range_width_pct"] = (
            (dataframe["range_high"] - dataframe["range_low"]) / dataframe["close"]
        )
        dataframe["volume_sma"] = dataframe["volume"].rolling(24).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe["range_high"])
            & (dataframe["range_width_pct"] < dataframe["range_width_pct"].rolling(120).median())
            & (dataframe["atr_pct"] > dataframe["atr_pct_sma"] * 0.85)
            & (dataframe["volume"] > dataframe["volume_sma"] * 1.20)
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["ema20_4h"] > dataframe["ema50_4h"])
            & (dataframe["close"] > dataframe["ema50_1d"]),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] < dataframe["ema20"])
            | (dataframe["close"] < dataframe["range_high"].shift(1)),
            "exit_long",
        ] = 1
        return dataframe
