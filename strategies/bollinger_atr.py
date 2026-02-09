"""볼린저밴드 + ATR 기반 매매 전략 모듈.

볼린저밴드로 진입 타이밍을 포착하고,
ATR(Average True Range)로 변동성에 따라 손절/익절을 동적으로 조절한다.
"""

import logging

import pandas as pd
import ta

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)


class BollingerATRStrategy(BaseStrategy):
    """볼린저밴드 + ATR 복합 전략.

    매수 조건:
    - 가격이 하단밴드 이하로 이탈 후 밴드 안으로 복귀
    - 거래량이 평균 대비 증가 (유동성 확인)

    매도 조건:
    - 가격이 상단밴드 이상으로 이탈 후 밴드 안으로 복귀
    - 또는 중심선(MA20) 아래로 하락 시

    ATR 활용:
    - 변동성이 클수록 진입 기준을 보수적으로
    - 변동성 정보를 detail에 포함하여 리스크 관리에 활용
    """

    name = "BB_ATR"

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0, atr_period: int = 14):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < self.bb_period + 5:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"데이터 부족 (최소 {self.bb_period + 5}일 필요)",
            )

        df = df.copy()

        # 볼린저밴드 계산
        bb = ta.volatility.BollingerBands(
            close=df["close"], window=self.bb_period, window_dev=self.bb_std,
        )
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"] * 100

        # ATR 계산
        atr = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=self.atr_period,
        )
        df["atr"] = atr.average_true_range()
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # 거래량 이동평균
        df["vol_ma20"] = df["volume"].rolling(window=20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma20"]

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        price = latest["close"]
        bb_upper = latest["bb_upper"]
        bb_middle = latest["bb_middle"]
        bb_lower = latest["bb_lower"]
        atr_pct = latest["atr_pct"]
        vol_ratio = latest["vol_ratio"]

        # 밴드 내 위치 (0=하단, 0.5=중심, 1=상단)
        bb_position = (price - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

        # 이전 봉의 밴드 위치
        prev_bb_pos = (prev["close"] - prev["bb_lower"]) / (prev["bb_upper"] - prev["bb_lower"]) if (prev["bb_upper"] - prev["bb_lower"]) > 0 else 0.5

        detail_parts = [
            f"BB위치={bb_position:.2f}",
            f"ATR={atr_pct:.2f}%",
            f"거래량비={vol_ratio:.1f}x",
            f"밴드폭={latest['bb_width']:.1f}%",
        ]

        # ── 매수 시그널 판단 ──
        if bb_position < 0.2 and prev_bb_pos <= 0:
            # 하단밴드 이탈 후 복귀 (반등)
            strength = self._calc_buy_strength(bb_position, vol_ratio, atr_pct)
            return StrategyResult(
                signal=Signal.BUY,
                strength=strength,
                strategy_name=self.name,
                detail=f"하단밴드 반등 ({', '.join(detail_parts)})",
            )

        if bb_position < 0.15 and vol_ratio > 1.2:
            # 하단밴드 근처 + 거래량 증가
            strength = self._calc_buy_strength(bb_position, vol_ratio, atr_pct)
            return StrategyResult(
                signal=Signal.BUY,
                strength=strength,
                strategy_name=self.name,
                detail=f"하단밴드 접근 + 거래량 급증 ({', '.join(detail_parts)})",
            )

        # ── 매도 시그널 판단 ──
        if bb_position > 0.8 and prev_bb_pos >= 1.0:
            # 상단밴드 이탈 후 복귀 (하락 반전)
            strength = self._calc_sell_strength(bb_position, vol_ratio, atr_pct)
            return StrategyResult(
                signal=Signal.SELL,
                strength=strength,
                strategy_name=self.name,
                detail=f"상단밴드 이탈 후 하락 ({', '.join(detail_parts)})",
            )

        if bb_position > 0.85 and vol_ratio < 0.7:
            # 상단밴드 근처 + 거래량 감소 (매수세 약화)
            strength = self._calc_sell_strength(bb_position, vol_ratio, atr_pct)
            return StrategyResult(
                signal=Signal.SELL,
                strength=strength,
                strategy_name=self.name,
                detail=f"상단밴드 근처 + 거래량 감소 ({', '.join(detail_parts)})",
            )

        if price < bb_middle and prev["close"] >= prev["bb_middle"]:
            # 중심선 하향 이탈 (약한 매도)
            strength = 0.3
            return StrategyResult(
                signal=Signal.SELL,
                strength=strength,
                strategy_name=self.name,
                detail=f"중심선 하향 이탈 ({', '.join(detail_parts)})",
            )

        # ── HOLD ──
        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"대기 ({', '.join(detail_parts)})",
        )

    def _calc_buy_strength(self, bb_position: float, vol_ratio: float, atr_pct: float) -> float:
        """매수 시그널 강도를 계산한다."""
        # 밴드 하단에 가까울수록 강한 시그널
        position_score = max(0, 1 - bb_position * 5)  # 0.2 이하에서 활성

        # 거래량이 높을수록 강한 시그널
        vol_score = min(1.0, vol_ratio / 2.0) if vol_ratio > 1.0 else vol_ratio * 0.5

        # 변동성이 너무 높으면 강도 감소 (리스크 반영)
        volatility_penalty = max(0, 1 - atr_pct / 5.0)

        strength = (position_score * 0.4 + vol_score * 0.3 + volatility_penalty * 0.3)
        return max(0.1, min(1.0, strength))

    def _calc_sell_strength(self, bb_position: float, vol_ratio: float, atr_pct: float) -> float:
        """매도 시그널 강도를 계산한다."""
        position_score = max(0, (bb_position - 0.8) * 5)
        vol_score = min(1.0, 1.0 / max(vol_ratio, 0.3)) if vol_ratio < 1.0 else 0.3
        volatility_bonus = min(1.0, atr_pct / 3.0)

        strength = (position_score * 0.4 + vol_score * 0.3 + volatility_bonus * 0.3)
        return max(0.1, min(1.0, strength))
