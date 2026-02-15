"""꼬리 매매 전략 모듈.

1분봉 데이터를 5분봉으로 변환한 뒤,
긴 아래꼬리(하락 반등) + 거래량 급증 패턴을 감지하여 매수 시그널을 생성한다.
"""

import logging
from datetime import datetime

import pandas as pd

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)


class TailTradingStrategy(BaseStrategy):
    """꼬리 매매 전략: 분봉 기반 반등 포착."""

    name = "TailTrading"

    def __init__(
        self,
        tail_ratio: float = 2.0,
        volume_ratio: float = 1.5,
        recovery_pct: float = 0.6,
        cooldown_minutes: int = 30,
    ):
        """
        Args:
            tail_ratio: 꼬리/몸통 비율 기준 (2.0 = 꼬리가 몸통의 2배 이상)
            volume_ratio: 거래량 급증 기준 (1.5 = 평균의 1.5배 이상)
            recovery_pct: 반등 확인 비율 (0.6 = 캔들 범위 상위 60%)
            cooldown_minutes: 동일 종목 재진입 쿨다운 (분)
        """
        self.tail_ratio = tail_ratio
        self.volume_ratio = volume_ratio
        self.recovery_pct = recovery_pct
        self.cooldown_minutes = cooldown_minutes
        # 쿨다운 추적: {symbol: last_signal_datetime}
        self._cooldowns: dict[str, datetime] = {}

    def is_cooled_down(self, symbol: str) -> bool:
        """쿨다운이 끝났는지 확인한다."""
        if symbol not in self._cooldowns:
            return True
        elapsed = (datetime.now() - self._cooldowns[symbol]).total_seconds() / 60
        return elapsed >= self.cooldown_minutes

    def mark_signal(self, symbol: str):
        """시그널 발생 시 쿨다운을 기록한다."""
        self._cooldowns[symbol] = datetime.now()

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        """5분봉 DataFrame을 분석하여 꼬리 매매 시그널을 반환한다."""
        if len(df) < 3:
            return StrategyResult(
                signal=Signal.HOLD, strength=0,
                strategy_name=self.name,
                detail="데이터 부족 (최소 3봉 필요)",
            )

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 거래량 평균 (최근 10봉, 부족하면 전체)
        vol_window = min(10, len(df))
        vol_avg = df["volume"].tail(vol_window).mean()

        # 매수 꼬리 감지
        buy_detected, buy_strength = self._detect_buy_tail(latest, prev, vol_avg)
        if buy_detected:
            return StrategyResult(
                signal=Signal.BUY,
                strength=buy_strength,
                strategy_name=self.name,
                detail=self._format_detail("매수 꼬리", latest, vol_avg),
            )

        # 매도 꼬리 감지 (shooting star)
        sell_detected, sell_strength = self._detect_sell_tail(latest, vol_avg)
        if sell_detected:
            return StrategyResult(
                signal=Signal.SELL,
                strength=sell_strength,
                strategy_name=self.name,
                detail=self._format_detail("매도 윗꼬리", latest, vol_avg),
            )

        return StrategyResult(
            signal=Signal.HOLD, strength=0,
            strategy_name=self.name,
            detail="꼬리 패턴 없음",
        )

    def _detect_buy_tail(self, candle, prev_candle, vol_avg: float) -> tuple[bool, float]:
        """매수 꼬리 패턴을 감지한다.

        Returns:
            (detected, strength) — strength는 0.3~1.0
        """
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        vol = candle["volume"]

        candle_range = h - l
        if candle_range <= 0:
            return False, 0

        body = abs(c - o)
        lower_wick = min(o, c) - l
        body = max(body, candle_range * 0.01)  # 도지 캔들: 최소 몸통

        # 1) 긴 아래꼬리: 하단 꼬리 >= 몸통 × tail_ratio
        if lower_wick < body * self.tail_ratio:
            return False, 0

        # 2) 반등 확인: 종가가 캔들 범위 상위 recovery_pct 이상
        recovery = (c - l) / candle_range
        if recovery < self.recovery_pct:
            return False, 0

        # 3) 거래량 급증
        if vol_avg > 0 and vol < vol_avg * self.volume_ratio:
            return False, 0

        # 4) 직전 하락 확인: 현재 저가가 직전 종가보다 0.3% 이상 낮음
        prev_close = prev_candle["close"]
        if prev_close > 0 and l >= prev_close * 0.997:
            return False, 0

        # 강도 계산: 꼬리 비율 + 거래량 비율 + 반등 정도
        tail_score = min(lower_wick / body / self.tail_ratio, 2.0) / 2.0  # 0~1
        vol_score = min(vol / vol_avg / self.volume_ratio, 2.0) / 2.0 if vol_avg > 0 else 0.5
        recovery_score = min(recovery / self.recovery_pct, 1.5) / 1.5

        strength = (tail_score * 0.4 + vol_score * 0.35 + recovery_score * 0.25)
        strength = max(0.3, min(1.0, strength))

        return True, strength

    def _detect_sell_tail(self, candle, vol_avg: float) -> tuple[bool, float]:
        """매도 꼬리 패턴(shooting star)을 감지한다."""
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        vol = candle["volume"]

        candle_range = h - l
        if candle_range <= 0:
            return False, 0

        body = abs(c - o)
        upper_wick = h - max(o, c)
        body = max(body, candle_range * 0.01)

        # 1) 긴 윗꼬리
        if upper_wick < body * self.tail_ratio:
            return False, 0

        # 2) 종가가 캔들 범위 하위 40% 이하
        close_position = (c - l) / candle_range
        if close_position > 0.4:
            return False, 0

        # 3) 거래량 평균 이상
        if vol_avg > 0 and vol < vol_avg:
            return False, 0

        # 강도 계산
        tail_score = min(upper_wick / body / self.tail_ratio, 2.0) / 2.0
        vol_score = min(vol / vol_avg, 2.0) / 2.0 if vol_avg > 0 else 0.5

        strength = (tail_score * 0.5 + vol_score * 0.5)
        strength = max(0.3, min(1.0, strength))

        return True, strength

    @staticmethod
    def aggregate_to_5min(df_1min: pd.DataFrame) -> pd.DataFrame:
        """1분봉 DataFrame을 5분봉으로 변환한다."""
        df = df_1min.copy()
        df["datetime"] = pd.to_datetime(df["date"], format="%Y%m%d %H%M%S")
        df = df.set_index("datetime").sort_index()

        df_5min = df.resample("5min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()

        df_5min["date"] = df_5min.index.strftime("%Y%m%d %H%M%S")
        return df_5min.reset_index(drop=True)

    def _format_detail(self, pattern: str, candle, vol_avg: float) -> str:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        vol = candle["volume"]
        body = abs(c - o)
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        vol_ratio = vol / vol_avg if vol_avg > 0 else 0

        return (
            f"{pattern} | 종가:{c:,.0f} 저가:{l:,.0f} "
            f"하꼬리:{lower_wick:,.0f} 윗꼬리:{upper_wick:,.0f} 몸통:{body:,.0f} "
            f"거래량:{vol:,}({vol_ratio:.1f}x)"
        )
