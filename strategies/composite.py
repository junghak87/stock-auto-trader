"""복합 전략 모듈.

여러 개별 전략의 시그널을 종합하여 최종 매매 결정을 내린다.
가중 투표 방식: 기술적 지표 전략 + 볼린저/ATR + AI(거부권 보유).
"""

import logging

import pandas as pd

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)

# 전략별 가중치 — 레짐별 동적 조정
REGIME_WEIGHTS = {
    "BULL": {
        "MA_Cross": 0.5,     # 추세 추종 보조
        "RSI": 1.0,          # 과매수 신호 줄임 (상승장에서 잦은 매도 방지)
        "MACD": 2.0,         # 모멘텀 추종 강화
        "BB_ATR": 1.0,       # 밴드 반전 약화
        "AI": 2.5,
    },
    "BEAR": {
        "MA_Cross": 0.3,     # 일봉 크로스 의존 최소화
        "RSI": 2.0,          # 과매도 반등 포착 강화
        "MACD": 1.0,         # 모멘텀 의존 줄임
        "BB_ATR": 2.0,       # 밴드 하단 반등 강화
        "AI": 3.0,           # AI 판단 최대 의존
    },
    "SIDEWAYS": {
        "MA_Cross": 0.5,
        "RSI": 1.5,
        "MACD": 1.5,
        "BB_ATR": 1.5,
        "AI": 2.5,
    },
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
        self._regime = "SIDEWAYS"
        self._minute_df = None

    def set_regime(self, regime: str):
        """시장 레짐을 설정한다 (BULL/BEAR/SIDEWAYS)."""
        if regime in REGIME_WEIGHTS:
            self._regime = regime

    def set_market_context(self, context: str):
        """AI 전략에 시장 컨텍스트를 전달한다."""
        if self._ai_strategy:
            self._ai_strategy.set_market_context(context)

    def set_stock_info(self, symbol: str, name: str = ""):
        """AI 전략에 현재 분석 종목 정보를 전달한다."""
        if self._ai_strategy:
            self._ai_strategy.set_stock_info(symbol, name)

    def set_minute_data(self, df):
        """AI 전략에 장중 분봉 데이터를 전달한다."""
        self._minute_df = df
        if self._ai_strategy:
            self._ai_strategy.set_minute_data(df)

    def _calc_minute_momentum(self) -> float:
        """분봉 데이터에서 장중 모멘텀 보정값을 계산한다.

        Returns:
            -0.15 ~ +0.15 범위의 보정값.
            양수: 장중 상승 흐름 → 매수 시그널 강화 / 매도 시그널 약화
            음수: 장중 하락 흐름 → 매수 시그널 약화 / 매도 시그널 강화
        """
        if self._minute_df is None or len(self._minute_df) < 3:
            return 0.0

        mdf = self._minute_df
        # 최근 분봉의 종가 변화율 (시가 대비)
        first_close = mdf["close"].iloc[0]
        last_close = mdf["close"].iloc[-1]
        if first_close <= 0:
            return 0.0

        intraday_pct = (last_close - first_close) / first_close * 100
        # -0.15 ~ +0.15 범위로 클램프 (1% 등락 = ±0.15 보정)
        momentum = max(-0.15, min(0.15, intraday_pct * 0.15))
        self._minute_df = None  # 사용 후 초기화 (종목 간 오염 방지)
        return momentum

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

        # 레짐별 가중치 적용
        weights = REGIME_WEIGHTS.get(self._regime, REGIME_WEIGHTS["SIDEWAYS"])

        # 가중 점수 계산 — 활성 투표자(BUY/SELL) 기준
        buy_score = 0.0
        sell_score = 0.0
        active_weight = 0.0  # 시그널을 낸 전략의 가중치 합

        for r in results:
            weight = weights.get(r.strategy_name, DEFAULT_WEIGHT)
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

        # AI 단독 매매: AI 확신도 >= 0.6이면 다른 전략 동의 없이 시그널 발생
        if ai_result and ai_result.signal != Signal.HOLD and ai_result.strength >= 0.6:
            logger.info("AI 단독 매매: %s (강도: %.2f)", ai_result.signal.value, ai_result.strength)
            return StrategyResult(
                signal=ai_result.signal,
                strength=ai_result.strength,
                strategy_name=self.name,
                detail=f"AI 단독 {ai_result.signal.value} (강도: {ai_result.strength:.2f}) [{details}]",
            )

        # AI가 동의하면 1개 전략 동의로도 시그널 발생
        ai_agrees_buy = ai_result and ai_result.signal == Signal.BUY and ai_result.strength > 0.1
        ai_agrees_sell = ai_result and ai_result.signal == Signal.SELL and ai_result.strength > 0.1
        min_voters_buy = 1 if ai_agrees_buy else 2
        min_voters_sell = 1 if ai_agrees_sell else 2

        # 분봉 기반 장중 모멘텀 보정
        momentum = self._calc_minute_momentum()
        momentum_tag = f", 장중보정:{momentum:+.2f}" if momentum != 0 else ""

        # 가중 점수 기반 시그널 결정
        if buy_ratio >= self.min_score and buy_voters >= min_voters_buy:
            adjusted = min(1.0, max(0.1, buy_ratio + momentum))
            return StrategyResult(
                signal=Signal.BUY,
                strength=adjusted,
                strategy_name=self.name,
                detail=f"매수 (점수: {buy_ratio:.2f}{momentum_tag}, {buy_voters}개 전략 동의) [{details}]",
            )

        if sell_ratio >= self.min_score and sell_voters >= min_voters_sell:
            adjusted = min(1.0, max(0.1, sell_ratio - momentum))
            return StrategyResult(
                signal=Signal.SELL,
                strength=adjusted,
                strategy_name=self.name,
                detail=f"매도 (점수: {sell_ratio:.2f}{momentum_tag}, {sell_voters}개 전략 동의) [{details}]",
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
