"""복합 전략 모듈.

여러 개별 전략의 시그널을 종합하여 최종 매매 결정을 내린다.
과반수 투표 + 가중 평균 강도 방식으로 시그널을 합산한다.
"""

import logging

import pandas as pd

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)


class CompositeStrategy(BaseStrategy):
    """복합 전략: 여러 전략을 결합하여 시그널을 생성한다."""

    name = "Composite"

    def __init__(
        self,
        strategies: list[BaseStrategy] | None = None,
        min_agreement: float = 0.5,
        ai_config: dict | None = None,
    ):
        """
        Args:
            strategies: 개별 전략 리스트
            min_agreement: 최소 합의 비율 (0.5 = 과반수)
            ai_config: AI 전략 설정 {"provider", "api_key", "model"} (None이면 비활성)
        """
        from .ma_cross import MACrossStrategy
        from .rsi_strategy import RSIStrategy
        from .macd_strategy import MACDStrategy

        base_strategies: list[BaseStrategy] = strategies or [
            MACrossStrategy(),
            RSIStrategy(),
            MACDStrategy(),
        ]

        if ai_config:
            from .ai_strategy import AIStrategy
            ai_strategy = AIStrategy(
                provider=ai_config["provider"],
                api_key=ai_config["api_key"],
                model=ai_config.get("model", ""),
            )
            base_strategies.append(ai_strategy)
            logger.info("AI 전략 활성화 (provider: %s, model: %s)", ai_config["provider"], ai_strategy.model)

        self.strategies = base_strategies
        self.min_agreement = min_agreement

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

        # 시그널별 집계 (HOLD는 무시)
        buy_results = [r for r in results if r.signal == Signal.BUY and r.strength > 0.1]
        sell_results = [r for r in results if r.signal == Signal.SELL and r.strength > 0.1]
        total_active = len(buy_results) + len(sell_results)

        if total_active == 0:
            details = " | ".join(f"{r.strategy_name}: {r.detail}" for r in results)
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"활성 시그널 없음 [{details}]",
            )

        # 과반수 합의 확인
        total_strategies = len(results)
        buy_ratio = len(buy_results) / total_strategies
        sell_ratio = len(sell_results) / total_strategies

        details_parts = [f"{r.strategy_name}:{r.signal.value}({r.strength:.1f})" for r in results]
        details = " | ".join(details_parts)

        if buy_ratio >= self.min_agreement:
            avg_strength = sum(r.strength for r in buy_results) / len(buy_results)
            return StrategyResult(
                signal=Signal.BUY,
                strength=avg_strength,
                strategy_name=self.name,
                detail=f"매수 합의 ({len(buy_results)}/{total_strategies}) [{details}]",
            )

        if sell_ratio >= self.min_agreement:
            avg_strength = sum(r.strength for r in sell_results) / len(sell_results)
            return StrategyResult(
                signal=Signal.SELL,
                strength=avg_strength,
                strategy_name=self.name,
                detail=f"매도 합의 ({len(sell_results)}/{total_strategies}) [{details}]",
            )

        return StrategyResult(
            signal=Signal.HOLD,
            strength=0,
            strategy_name=self.name,
            detail=f"합의 부족 (매수:{len(buy_results)} 매도:{len(sell_results)}/{total_strategies}) [{details}]",
        )
