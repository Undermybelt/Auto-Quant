"""
IctXlkProxyTrendVwap - MTF trend continuation with VWAP-style participation filter

Paradigm: trend-following
Hypothesis: When the crypto proxy basket is above 1h trend and 4h/1d context,
            VWAP/volume participation marks continuation legs similar to the
            ict-engine XLK technology-leadership reclaim branch.
Parent: ict-engine IBKR_XLK_VWAP_RECLAIM_GATE_TAIL
Created: 34ba6b6ee6aa69813a50a72158d4c089d97afb96
Status: active
Uses MTF: yes
"""

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy, informative


class IctXlkProxyTrendVwap(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.18
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 260

    pair_basket = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    test_timeranges = [
        ("full_5y", "20210101-20251231"),
        ("test_25", "20250101-20251231"),
    ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0
        dollar_volume = typical * dataframe["volume"]
        dataframe["rolling_vwap"] = (
            dollar_volume.rolling(24).sum() / dataframe["volume"].rolling(24).sum()
        )
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["volume_sma"] = dataframe["volume"].rolling(24).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe["rolling_vwap"])
            & (dataframe["close"] > dataframe["ema50"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
            & (dataframe["ema20_4h"] > dataframe["ema50_4h"])
            & (dataframe["close"] > dataframe["ema50_1d"])
            & (dataframe["rsi"] > 48)
            & (dataframe["rsi"] < 72)
            & (dataframe["volume"] > dataframe["volume_sma"] * 1.05),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] < dataframe["ema20"])
            | (dataframe["close"] < dataframe["rolling_vwap"])
            | (dataframe["rsi"] > 78),
            "exit_long",
        ] = 1
        return dataframe
