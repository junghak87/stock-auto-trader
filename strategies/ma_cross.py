"""이동평균 교차(Golden/Dead Cross) 전략.

단기 이동평균이 장기 이동평균을 상향 돌파하면 매수(골든크로스),
하향 돌파하면 매도(데드크로스) 시그널을 생성한다.
"""

import pandas as pd

from .base import BaseStrategy, Signal, StrategyResult


class MACrossStrategy(BaseStrategy):
    """이동평균 교차 전략."""

    name = "MA_Cross"

    def __init__(self, short_window: int = 5, long_window: int = 20):
        self.short_window = short_window
        self.long_window = long_window

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < self.long_window + 1:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"데이터 부족 (필요: {self.long_window + 1}일, 현재: {len(df)}일)",
            )

        df = df.copy()
        df["ma_short"] = df["close"].rolling(window=self.short_window).mean()
        df["ma_long"] = df["close"].rolling(window=self.long_window).mean()

        curr_short = df["ma_short"].iloc[-1]
        curr_long = df["ma_long"].iloc[-1]
        prev_short = df["ma_short"].iloc[-2]
        prev_long = df["ma_long"].iloc[-2]

        # 골든크로스: 단기 MA가 장기 MA를 상향 돌파
        if prev_short <= prev_long and curr_short > curr_long:
            gap_pct = (curr_short - curr_long) / curr_long * 100
            return StrategyResult(
                signal=Signal.BUY,
                strength=min(gap_pct / 2, 1.0),
                strategy_name=self.name,
                detail=f"골든크로스 (MA{self.short_window}={curr_short:.0f} > MA{self.long_window}={curr_long:.0f}, 괴리: {gap_pct:.2f}%)",
            )

        # 데드크로스: 단기 MA가 장기 MA를 하향 돌파
        if prev_short >= prev_long and curr_short < curr_long:
            gap_pct = (curr_long - curr_short) / curr_long * 100
            return StrategyResult(
                signal=Signal.SELL,
                strength=min(gap_pct / 2, 1.0),
                strategy_name=self.name,
                detail=f"데드크로스 (MA{self.short_window}={curr_short:.0f} < MA{self.long_window}={curr_long:.0f}, 괴리: {gap_pct:.2f}%)",
            )

        # 추세 유지
        if curr_short > curr_long:
            trend = "상승 추세 유지"
        else:
            trend = "하락 추세 유지"

        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"{trend} (MA{self.short_window}={curr_short:.0f}, MA{self.long_window}={curr_long:.0f})",
        )
