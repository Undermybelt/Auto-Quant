"""
EsSessionVwapReclaim1m - Session VWAP reclaim after deviation
Paradigm: mean-reversion
Regime: RangeReversion -> SessionVwapReclaim
Asset: ES/USD (E-mini S&P 500 futures)
Timeframe: 1m
"""
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy

class EsSessionVwapReclaim1m(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    can_short = True
    minimal_roi = {"0": 0.002}
    stoploss = -0.004
    trailing_stop = True
    trailing_stop_positive = 0.001
    trailing_stop_positive_offset = 0.0015
    trailing_only_offset_is_reached = True
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0
        dollar_volume = typical * dataframe["volume"]
        dataframe["vwap"] = dollar_volume.rolling(30).sum() / dataframe["volume"].rolling(30).sum()
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["vwap_dev_atr"] = (dataframe["close"] - dataframe["vwap"]) / dataframe["atr"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"]
        dataframe["above_vwap"] = (dataframe["close"] > dataframe["vwap"]).astype(int)
        dataframe["reclaim_up"] = (dataframe["above_vwap"].shift(1) == 0) & (dataframe["above_vwap"] == 1)
        dataframe["reclaim_down"] = (dataframe["above_vwap"].shift(1) == 1) & (dataframe["above_vwap"] == 0)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["reclaim_up"]) & (dataframe["vwap_dev_atr"].shift(1) < -1.5) &
            (dataframe["vol_ratio"] > 1.2) & (dataframe["rsi"] < 50),
            "enter_long"] = 1
        dataframe.loc[
            (dataframe["reclaim_down"]) & (dataframe["vwap_dev_atr"].shift(1) > 1.5) &
            (dataframe["vol_ratio"] > 1.2) & (dataframe["rsi"] > 50),
            "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] < dataframe["vwap"]), "exit_long"] = 1
        dataframe.loc[(dataframe["close"] > dataframe["vwap"]), "exit_short"] = 1
        return dataframe
