"""
IctXlkProxyPullbackReclaim - trend pullback reclaim after local exhaustion

Paradigm: mean-reversion
Hypothesis: In the ict-engine XLK branch analogue, crypto majors that remain
            above higher-timeframe trend can buy shallow 1h RSI pullbacks when
            price reclaims the short trend and volume confirms participation.
Parent: ict-engine IBKR_XLK_VWAP_RECLAIM_GATE_TAIL
Created: 34ba6b6ee6aa69813a50a72158d4c089d97afb96
Status: active
Uses MTF: yes
"""

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy, informative


class IctXlkProxyPullbackReclaim(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.14
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 260

    pair_basket = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
    test_timeranges = [
        ("full_5y", "20210101-20251231"),
        ("test_25", "20250101-20251231"),
    ]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
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
        dataframe["ema10"] = ta.EMA(dataframe, timeperiod=10)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["volume_sma"] = dataframe["volume"].rolling(24).mean()
        dataframe["recent_pullback"] = dataframe["rsi"].rolling(8).min()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["recent_pullback"] < 38)
            & (dataframe["rsi"] > dataframe["rsi"].shift(1))
            & (dataframe["close"] > dataframe["ema10"])
            & (dataframe["close"] > dataframe["rolling_vwap"])
            & (dataframe["close"] > dataframe["ema100"])
            & (dataframe["ema50_4h"] > dataframe["ema100_4h"])
            & (dataframe["close"] > dataframe["ema50_1d"])
            & (dataframe["volume"] > dataframe["volume_sma"] * 0.90),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > 68)
            | (dataframe["close"] < dataframe["ema21"])
            | (dataframe["close"] < dataframe["rolling_vwap"]),
            "exit_long",
        ] = 1
        return dataframe
