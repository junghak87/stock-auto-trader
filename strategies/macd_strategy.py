"""MACD(Moving Average Convergence Divergence) 전략.

MACD 라인이 시그널 라인을 상향 돌파하면 매수,
하향 돌파하면 매도 시그널을 생성한다.
"""

import pandas as pd
import ta

from .base import BaseStrategy, Signal, StrategyResult


class MACDStrategy(BaseStrategy):
    """MACD 교차 전략."""

    name = "MACD"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_period = signal

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        min_required = self.slow + self.signal_period
        if len(df) < min_required:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"데이터 부족 (필요: {min_required}일, 현재: {len(df)}일)",
            )

        df = df.copy()
        macd_indicator = ta.trend.MACD(
            close=df["close"],
            window_fast=self.fast,
            window_slow=self.slow,
            window_sign=self.signal_period,
        )
        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_hist"] = macd_indicator.macd_diff()

        curr_macd = df["macd"].iloc[-1]
        curr_signal = df["macd_signal"].iloc[-1]
        prev_macd = df["macd"].iloc[-2]
        prev_signal = df["macd_signal"].iloc[-2]
        curr_hist = df["macd_hist"].iloc[-1]
        prev_hist = df["macd_hist"].iloc[-2]

        # MACD가 시그널을 상향 돌파 → 매수
        if prev_macd <= prev_signal and curr_macd > curr_signal:
            strength = min(abs(curr_hist) / (abs(curr_signal) + 1e-10), 1.0)
            return StrategyResult(
                signal=Signal.BUY,
                strength=min(max(strength, 0.3), 1.0),
                strategy_name=self.name,
                detail=f"MACD 골든크로스 (MACD={curr_macd:.2f}, Signal={curr_signal:.2f}, Hist={curr_hist:.2f})",
            )

        # MACD가 시그널을 하향 돌파 → 매도
        if prev_macd >= prev_signal and curr_macd < curr_signal:
            strength = min(abs(curr_hist) / (abs(curr_signal) + 1e-10), 1.0)
            return StrategyResult(
                signal=Signal.SELL,
                strength=min(max(strength, 0.3), 1.0),
                strategy_name=self.name,
                detail=f"MACD 데드크로스 (MACD={curr_macd:.2f}, Signal={curr_signal:.2f}, Hist={curr_hist:.2f})",
            )

        # 히스토그램 증가 추세 (상승 모멘텀 강화)
        if curr_hist > 0 and curr_hist > prev_hist:
            strength = min(abs(curr_hist - prev_hist) / (abs(curr_signal) + 1e-10), 0.5)
            return StrategyResult(
                signal=Signal.BUY,
                strength=max(0.2, strength),
                strategy_name=self.name,
                detail=f"상승 모멘텀 강화 (Hist={curr_hist:.2f}, 변화={curr_hist - prev_hist:+.2f})",
            )

        # 히스토그램 감소 추세 (하락 모멘텀 강화)
        if curr_hist < 0 and curr_hist < prev_hist:
            strength = min(abs(curr_hist - prev_hist) / (abs(curr_signal) + 1e-10), 0.5)
            return StrategyResult(
                signal=Signal.SELL,
                strength=max(0.2, strength),
                strategy_name=self.name,
                detail=f"하락 모멘텀 강화 (Hist={curr_hist:.2f}, 변화={curr_hist - prev_hist:+.2f})",
            )

        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"MACD 중립 (MACD={curr_macd:.2f}, Signal={curr_signal:.2f})",
        )
