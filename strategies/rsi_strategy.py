"""RSI(Relative Strength Index) 기반 전략.

RSI가 과매도 구간(30 이하)이면 매수, 과매수 구간(70 이상)이면 매도 시그널을 생성한다.
"""

import pandas as pd
import ta

from .base import BaseStrategy, Signal, StrategyResult


class RSIStrategy(BaseStrategy):
    """RSI 과매수/과매도 전략."""

    name = "RSI"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < self.period + 1:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"데이터 부족 (필요: {self.period + 1}일, 현재: {len(df)}일)",
            )

        df = df.copy()
        rsi_indicator = ta.momentum.RSIIndicator(close=df["close"], window=self.period)
        df["rsi"] = rsi_indicator.rsi()

        current_rsi = df["rsi"].iloc[-1]
        prev_rsi = df["rsi"].iloc[-2]

        # 과매도 탈출 → 매수
        if current_rsi > self.oversold and prev_rsi <= self.oversold:
            strength = (self.oversold - min(prev_rsi, self.oversold)) / self.oversold
            return StrategyResult(
                signal=Signal.BUY,
                strength=min(max(strength, 0.3), 1.0),
                strategy_name=self.name,
                detail=f"RSI 과매도 탈출 ({prev_rsi:.1f} → {current_rsi:.1f})",
            )

        # 과매수 진입 → 매도
        if current_rsi < self.overbought and prev_rsi >= self.overbought:
            strength = (max(prev_rsi, self.overbought) - self.overbought) / (100 - self.overbought)
            return StrategyResult(
                signal=Signal.SELL,
                strength=min(max(strength, 0.3), 1.0),
                strategy_name=self.name,
                detail=f"RSI 과매수 하락 ({prev_rsi:.1f} → {current_rsi:.1f})",
            )

        # 현재 과매도 구간에 있으면 매수 대기 시그널
        if current_rsi <= self.oversold:
            return StrategyResult(
                signal=Signal.BUY,
                strength=0.2,
                strategy_name=self.name,
                detail=f"RSI 과매도 구간 (RSI={current_rsi:.1f})",
            )

        # 현재 과매수 구간에 있으면 매도 대기 시그널
        if current_rsi >= self.overbought:
            return StrategyResult(
                signal=Signal.SELL,
                strength=0.2,
                strategy_name=self.name,
                detail=f"RSI 과매수 구간 (RSI={current_rsi:.1f})",
            )

        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"RSI 중립 구간 (RSI={current_rsi:.1f})",
        )
