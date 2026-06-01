"""
EsRangeFadeBbRsi1m - Bollinger Band fade with RSI confirmation
Paradigm: mean-reversion
Regime: RangeConsolidation -> BollingerFade
Asset: ES/USD (E-mini S&P 500 futures)
Timeframe: 1m
"""
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy

class EsRangeFadeBbRsi1m(IStrategy):
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
        dataframe["bb_upper"], dataframe["bb_middle"], dataframe["bb_lower"] = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2, nbdevdn=2)
        dataframe["bb_width"] = (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_middle"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"]
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["touch_upper"] = dataframe["high"] >= dataframe["bb_upper"]
        dataframe["touch_lower"] = dataframe["low"] <= dataframe["bb_lower"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["touch_lower"]) & (dataframe["rsi"] < 30) &
            (dataframe["bb_width"] > 0.005) & (dataframe["bb_width"] < 0.03) &
            (dataframe["close"] > dataframe["ema50"]) & (dataframe["vol_ratio"] > 0.8),
            "enter_long"] = 1
        dataframe.loc[
            (dataframe["touch_upper"]) & (dataframe["rsi"] > 70) &
            (dataframe["bb_width"] > 0.005) & (dataframe["bb_width"] < 0.03) &
            (dataframe["close"] < dataframe["ema50"]) & (dataframe["vol_ratio"] > 0.8),
            "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["bb_middle"]), "exit_long"] = 1
        dataframe.loc[(dataframe["close"] < dataframe["bb_middle"]), "exit_short"] = 1
        return dataframe
