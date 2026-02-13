"""복합 전략 모듈.

여러 개별 전략의 시그널을 종합하여 최종 매매 결정을 내린다.
가중 투표 방식: 기술적 지표 전략 + 볼린저/ATR + AI(거부권 보유).
"""

import logging

import pandas as pd

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)

# 전략별 가중치 — AI가 가장 높은 비중
STRATEGY_WEIGHTS = {
    "MA_Cross": 1.0,
    "RSI": 1.0,
    "MACD": 1.0,
    "BB_ATR": 1.5,      # 볼린저+ATR은 높은 비중
    "AI": 2.0,           # AI는 2배 가중치
}
DEFAULT_WEIGHT = 1.0


class CompositeStrategy(BaseStrategy):
    """복합 전략: 여러 전략을 가중 투표로 결합하고 AI가 거부권을 행사한다."""

    name = "Composite"

    def __init__(
        self,
        strategies: list[BaseStrategy] | None = None,
        min_score: float = 0.4,
        ai_config: dict | None = None,
    ):
        """
        Args:
            strategies: 개별 전략 리스트
            min_score: 최소 가중 점수 비율 (0.4 = 40% 이상 동의)
            ai_config: AI 전략 설정 {"provider", "api_key", "model"} (None이면 비활성)
        """
        from .ma_cross import MACrossStrategy
        from .rsi_strategy import RSIStrategy
        from .macd_strategy import MACDStrategy
        from .bollinger_atr import BollingerATRStrategy

        base_strategies: list[BaseStrategy] = strategies or [
            MACrossStrategy(),
            RSIStrategy(),
            MACDStrategy(),
            BollingerATRStrategy(),
        ]

        self._ai_strategy = None
        if ai_config:
            from .ai_strategy import AIStrategy
            self._ai_strategy = AIStrategy(
                provider=ai_config["provider"],
                api_key=ai_config["api_key"],
                model=ai_config.get("model", ""),
            )
            base_strategies.append(self._ai_strategy)
            logger.info("AI 전략 활성화 (provider: %s, model: %s)", ai_config["provider"], self._ai_strategy.model)

        self.strategies = base_strategies
        self.min_score = min_score

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        results: list[StrategyResult] = []
        for strategy in self.strategies:
            try:
                result = strategy.analyze(df)
                results.append(result)
                logger.debug("[%s] %s (강도: %.2f) — %s", strategy.name, result.signal.value, result.strength, result.detail)
            except Exception as e:
                logger.warning("전략 '%s' 분석 실패: %s", strategy.name, e)

        if not results:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail="모든 전략 분석 실패",
            )

        # AI 결과 분리
        ai_result = next((r for r in results if r.strategy_name == "AI"), None)

        # 가중 점수 계산 — 활성 투표자(BUY/SELL) 기준
        buy_score = 0.0
        sell_score = 0.0
        active_weight = 0.0  # 시그널을 낸 전략의 가중치 합

        for r in results:
            weight = STRATEGY_WEIGHTS.get(r.strategy_name, DEFAULT_WEIGHT)
            if r.signal == Signal.BUY and r.strength > 0.1:
                buy_score += weight * r.strength
                active_weight += weight
            elif r.signal == Signal.SELL and r.strength > 0.1:
                sell_score += weight * r.strength
                active_weight += weight

        # 정규화: 활성 투표자 가중치 기준 (HOLD 전략은 분모에서 제외)
        buy_ratio = buy_score / active_weight if active_weight > 0 else 0
        sell_ratio = sell_score / active_weight if active_weight > 0 else 0

        details_parts = [f"{r.strategy_name}:{r.signal.value}({r.strength:.1f})" for r in results]
        details = " | ".join(details_parts)

        # 활성 투표자 수 (최소 2개 전략이 같은 방향이어야 시그널)
        buy_voters = sum(1 for r in results if r.signal == Signal.BUY and r.strength > 0.1)
        sell_voters = sum(1 for r in results if r.signal == Signal.SELL and r.strength > 0.1)

        # AI 거부권 체크: 기술지표가 매수인데 AI가 매도면 → HOLD
        if ai_result and ai_result.strength >= 0.5:
            if buy_ratio >= self.min_score and ai_result.signal == Signal.SELL:
                logger.info("AI 거부권 발동: 기술지표 매수 vs AI 매도 → HOLD")
                return StrategyResult(
                    signal=Signal.HOLD,
                    strength=0,
                    strategy_name=self.name,
                    detail=f"AI 거부권 (기술지표 매수 but AI 매도) [{details}]",
                )
            if sell_ratio >= self.min_score and ai_result.signal == Signal.BUY:
                logger.info("AI 거부권 발동: 기술지표 매도 vs AI 매수 → HOLD")
                return StrategyResult(
                    signal=Signal.HOLD,
                    strength=0,
                    strategy_name=self.name,
                    detail=f"AI 거부권 (기술지표 매도 but AI 매수) [{details}]",
                )

        # 가중 점수 기반 시그널 결정 (최소 2개 전략 동의 필요)
        if buy_ratio >= self.min_score and buy_voters >= 2:
            return StrategyResult(
                signal=Signal.BUY,
                strength=min(1.0, buy_ratio),
                strategy_name=self.name,
                detail=f"매수 (점수: {buy_ratio:.2f}, {buy_voters}개 전략 동의) [{details}]",
            )

        if sell_ratio >= self.min_score and sell_voters >= 2:
            return StrategyResult(
                signal=Signal.SELL,
                strength=min(1.0, sell_ratio),
                strategy_name=self.name,
                detail=f"매도 (점수: {sell_ratio:.2f}, {sell_voters}개 전략 동의) [{details}]",
            )

        if buy_score == 0 and sell_score == 0:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"활성 시그널 없음 [{details}]",
            )

        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"조건 미달 (매수:{buy_ratio:.2f}/{buy_voters}표 매도:{sell_ratio:.2f}/{sell_voters}표) [{details}]",
        )
