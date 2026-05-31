"""
EsMomentumEmaCross1m - EMA crossover with momentum confirmation
Paradigm: trend-following
Regime: MomentumContinuation -> EmaCrossMomentum
Asset: ES/USD (E-mini S&P 500 futures)
Timeframe: 1m
"""
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy

class EsMomentumEmaCross1m(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    can_short = True
    minimal_roi = {"0": 0.002}
    stoploss = -0.003
    trailing_stop = True
    trailing_stop_positive = 0.001
    trailing_stop_positive_offset = 0.0015
    trailing_only_offset_is_reached = True
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["macd"], dataframe["macd_signal"], dataframe["macd_hist"] = ta.MACD(dataframe)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"]
        dataframe["cross_up"] = (dataframe["ema9"] > dataframe["ema21"]) & (dataframe["ema9"].shift(1) <= dataframe["ema21"].shift(1))
        dataframe["cross_down"] = (dataframe["ema9"] < dataframe["ema21"]) & (dataframe["ema9"].shift(1) >= dataframe["ema21"].shift(1))
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["cross_up"]) & (dataframe["ema21"] > dataframe["ema50"]) &
            (dataframe["macd_hist"] > 0) & (dataframe["rsi"] > 50) & (dataframe["rsi"] < 70) &
            (dataframe["vol_ratio"] > 1.0),
            "enter_long"] = 1
        dataframe.loc[
            (dataframe["cross_down"]) & (dataframe["ema21"] < dataframe["ema50"]) &
            (dataframe["macd_hist"] < 0) & (dataframe["rsi"] < 50) & (dataframe["rsi"] > 30) &
            (dataframe["vol_ratio"] > 1.0),
            "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["ema9"] < dataframe["ema21"]), "exit_long"] = 1
        dataframe.loc[(dataframe["ema9"] > dataframe["ema21"]), "exit_short"] = 1
        return dataframe
