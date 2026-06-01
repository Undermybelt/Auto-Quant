"""
EsOpeningRangeBreakout1m - Opening range breakout with volume
Paradigm: breakout
Regime: TrendExpansion -> OpeningRangeBreakout
Asset: ES/USD (E-mini S&P 500 futures)
Timeframe: 1m
"""
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy

class EsOpeningRangeBreakout1m(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    can_short = True
    minimal_roi = {"0": 0.003}
    stoploss = -0.005
    trailing_stop = True
    trailing_stop_positive = 0.001
    trailing_stop_positive_offset = 0.002
    trailing_only_offset_is_reached = True
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["range_high"] = dataframe["high"].rolling(30).max().shift(1)
        dataframe["range_low"] = dataframe["low"].rolling(30).min().shift(1)
        dataframe["range_width"] = (dataframe["range_high"] - dataframe["range_low"]) / dataframe["close"]
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"]
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["breakout_up"] = (dataframe["close"] > dataframe["range_high"]) & (dataframe["close"].shift(1) <= dataframe["range_high"].shift(1))
        dataframe["breakout_down"] = (dataframe["close"] < dataframe["range_low"]) & (dataframe["close"].shift(1) >= dataframe["range_low"].shift(1))
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["breakout_up"]) & (dataframe["vol_ratio"] > 1.3) &
            (dataframe["range_width"] > 0.001) & (dataframe["range_width"] < 0.01) &
            (dataframe["ema20"] > dataframe["ema50"]),
            "enter_long"] = 1
        dataframe.loc[
            (dataframe["breakout_down"]) & (dataframe["vol_ratio"] > 1.3) &
            (dataframe["range_width"] > 0.001) & (dataframe["range_width"] < 0.01) &
            (dataframe["ema20"] < dataframe["ema50"]),
            "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] < dataframe["ema20"]), "exit_long"] = 1
        dataframe.loc[(dataframe["close"] > dataframe["ema20"]), "exit_short"] = 1
        return dataframe
