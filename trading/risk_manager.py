"""리스크 관리 모듈.

포지션 한도, 손절/익절, 일일 거래 제한 등을 관리한다.
ATR(변동성) 기반으로 손절/익절을 동적으로 조절한다.
트레일링 스탑: 수익 활성화 후 고점 대비 하락 시 익절.
포트폴리오 리스크: 일일 최대 손실, 연속 손절 쿨다운.
"""

import logging
from datetime import datetime

import pandas as pd
import ta

from core.broker import BrokerClient, Position
from core.database import Database

logger = logging.getLogger(__name__)

# ATR 기반 동적 손절/익절 배수
ATR_STOP_LOSS_MULTIPLIER = 2.0    # ATR x 2 = 손절폭
ATR_TAKE_PROFIT_MULTIPLIER = 3.0  # ATR x 3 = 익절폭


class RiskManager:
    """리스크 관리자."""

    def __init__(
        self,
        kis_client: BrokerClient,
        database: Database,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        trailing_activation_pct: float = 3.0,
        trailing_stop_pct: float = 2.0,
        daily_max_loss_pct: float = 3.0,
        consecutive_loss_limit: int = 3,
        consecutive_loss_cooldown: int = 60,
        max_daily_trades: int = 20,
        total_budget: float = 0,
        usd_krw_rate: float = 1450,
    ):
        self.kis = kis_client
        self.db = database
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.daily_max_loss_pct = daily_max_loss_pct
        self.consecutive_loss_limit = consecutive_loss_limit
        self.consecutive_loss_cooldown = consecutive_loss_cooldown
        self.max_daily_trades = max_daily_trades
        self.total_budget = total_budget
        self.usd_krw_rate = usd_krw_rate
        # 종목별 동적 손절/익절 캐시 {symbol: (stop_loss_pct, take_profit_pct)}
        self._dynamic_thresholds: dict[str, tuple[float, float]] = {}
        # 트레일링 스탑: 종목별 고점 가격 추적 {symbol: highest_price}
        self._high_watermarks: dict[str, float] = {}
        # 포트폴리오 리스크: 연속 손절 추적
        self._consecutive_losses: int = 0
        self._last_loss_time: datetime | None = None
        self._trading_halted: bool = False
        self._halt_reason: str = ""
        # 일일 최대 손실 체크 캐시 (60초)
        self._daily_loss_cache_time: datetime | None = None
        self._daily_loss_cache_result: tuple[bool, str] = (True, "")
        # 현금 잔고 캐시 (30초) — 같은 사이클 내 반복 API 호출 방지
        self._cash_cache: dict | None = None
        self._cash_cache_time: datetime | None = None

    def can_trade(self) -> tuple[bool, str]:
        """현재 거래가 가능한 상태인지 확인한다."""
        # 1. 일일 거래 횟수 제한
        trade_count = self.db.get_trade_count_today()
        if trade_count >= self.max_daily_trades:
            return False, f"일일 최대 거래 횟수 초과 ({trade_count}/{self.max_daily_trades})"

        # 2. 연속 손절 쿨다운
        if self._trading_halted:
            if self._last_loss_time:
                elapsed = (datetime.now() - self._last_loss_time).total_seconds() / 60
                if elapsed >= self.consecutive_loss_cooldown:
                    self._trading_halted = False
                    self._consecutive_losses = 0
                    self._halt_reason = ""
                    logger.info("쿨다운 해제: 거래 재개")
                else:
                    remaining = self.consecutive_loss_cooldown - elapsed
                    return False, f"{self._halt_reason} (잔여: {remaining:.0f}분)"
            else:
                return False, self._halt_reason

        # 3. 일일 최대 손실 체크 (60초 캐시)
        ok, reason = self._check_daily_loss_cached()
        if not ok:
            return False, reason

        return True, "거래 가능"

    def _check_daily_loss_cached(self) -> tuple[bool, str]:
        """일일 최대 손실을 체크한다 (60초 캐시)."""
        now = datetime.now()
        if self._daily_loss_cache_time and (now - self._daily_loss_cache_time).total_seconds() < 60:
            return self._daily_loss_cache_result

        result = (True, "")
        if self.total_budget > 0:
            try:
                cash_info = self._get_cash_info()
                realized_pnl = cash_info.get("total_pnl", 0)
                max_loss = self.total_budget * (self.daily_max_loss_pct / 100)

                if realized_pnl <= -max_loss:
                    self._trading_halted = True
                    self._halt_reason = f"일일 최대 손실 초과: {realized_pnl:,.0f}원 (한도: -{max_loss:,.0f}원)"
                    result = (False, self._halt_reason)
                    logger.warning(self._halt_reason)
            except Exception as e:
                logger.warning("일일 손실 체크 실패: %s", e)

        self._daily_loss_cache_time = now
        self._daily_loss_cache_result = result
        return result

    def record_stop_loss(self):
        """손절 발생을 기록한다 (연속 손절 추적)."""
        self._consecutive_losses += 1
        self._last_loss_time = datetime.now()
        logger.info("연속 손절 카운트: %d/%d", self._consecutive_losses, self.consecutive_loss_limit)
        if self._consecutive_losses >= self.consecutive_loss_limit:
            self._trading_halted = True
            self._halt_reason = f"연속 {self._consecutive_losses}회 손절 — {self.consecutive_loss_cooldown}분 쿨다운"
            logger.warning(self._halt_reason)

    def record_profit(self):
        """익절 발생을 기록한다 (연속 손절 카운터 리셋)."""
        if self._consecutive_losses > 0:
            logger.info("연속 손절 카운트 리셋 (익절 발생)")
        self._consecutive_losses = 0

    def _get_cash_info(self) -> dict:
        """현금 잔고를 캐시하여 반환한다 (30초 TTL)."""
        now = datetime.now()
        if self._cash_cache and self._cash_cache_time and (now - self._cash_cache_time).total_seconds() < 30:
            return self._cash_cache
        self._cash_cache = self.kis.get_cash_balance()
        self._cash_cache_time = now
        return self._cash_cache

    @staticmethod
    def _calc_max_stocks(budget: float) -> int:
        """투자금 규모에 따른 최대 보유 종목 수."""
        if budget < 10_000_000:
            return 2
        if budget < 30_000_000:
            return 3
        if budget < 50_000_000:
            return 5
        if budget < 100_000_000:
            return 7
        return 10

    def calculate_buy_qty(self, symbol: str, price: float, market: str = "KR") -> int:
        """매수 가능 수량을 계산한다 (총 예산 또는 계좌 평가액 기반)."""
        try:
            cash_info = self._get_cash_info()
            available_cash = cash_info.get("cash", 0)

            if price <= 0:
                return 0

            # 총 예산이 설정된 경우 예산 기준, 아니면 계좌 총평가 기준
            if self.total_budget > 0:
                base_amount = self.total_budget
            else:
                base_amount = cash_info.get("total_eval", 0)

            if base_amount <= 0:
                return 0

            # 투자금 규모별 자동 종목수 산정 → 종목당 투자금 계산
            max_stocks = self._calc_max_stocks(base_amount)
            max_invest = base_amount / max_stocks
            max_by_cash = available_cash * 0.95  # 현금의 95%까지만 사용
            invest_amount = min(max_invest, max_by_cash)

            # US 주식: 원화 예산을 달러로 변환하여 수량 계산
            if market == "US":
                invest_usd = invest_amount / self.usd_krw_rate
                qty = int(invest_usd / price)
                logger.info(
                    "매수 수량 계산: %s | 기준액=%s원, 종목한도=$%.0f (환율:%s), 가격=$%.2f -> %d주",
                    symbol, f"{base_amount:,.0f}", invest_usd, f"{self.usd_krw_rate:,.0f}", price, qty,
                )
            else:
                qty = int(invest_amount / price)
                logger.info(
                    "매수 수량 계산: %s | 기준액=%s, 종목한도=%s, 가격=%s -> %d주",
                    symbol, f"{base_amount:,.0f}", f"{invest_amount:,.0f}", f"{price:,.0f}", qty,
                )

            return max(qty, 0)
        except Exception as e:
            logger.error("매수 수량 계산 실패: %s", e)
            return 0

    def update_dynamic_thresholds(self, symbol: str, df: pd.DataFrame):
        """ATR 기반으로 종목별 동적 손절/익절 비율을 계산한다.

        변동성이 큰 종목: 넓은 손절/익절 (빈번한 손절 방지)
        변동성이 작은 종목: 좁은 손절/익절 (수익 확보)
        """
        if len(df) < 20:
            return

        atr_indicator = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14,
        )
        atr = atr_indicator.average_true_range().iloc[-1]
        price = df["close"].iloc[-1]

        if price <= 0:
            return

        atr_pct = atr / price * 100

        # ATR 기반 동적 계산 (최소/최대 범위 제한)
        dynamic_stop = min(max(atr_pct * ATR_STOP_LOSS_MULTIPLIER, 2.0), self.stop_loss_pct)
        dynamic_profit = min(max(atr_pct * ATR_TAKE_PROFIT_MULTIPLIER, 3.0), self.take_profit_pct)

        self._dynamic_thresholds[symbol] = (dynamic_stop, dynamic_profit)
        logger.debug(
            "동적 손절/익절: %s | ATR=%.2f%% → 손절=-%.1f%%, 익절=+%.1f%%",
            symbol, atr_pct, dynamic_stop, dynamic_profit,
        )

    def _get_thresholds(self, symbol: str) -> tuple[float, float]:
        """종목의 손절/익절 비율을 반환한다 (동적 > 기본값)."""
        return self._dynamic_thresholds.get(symbol, (self.stop_loss_pct, self.take_profit_pct))

    def check_stop_loss(self, positions: list[Position]) -> list[Position]:
        """손절선에 도달한 포지션을 반환한다."""
        stop_targets = []
        for pos in positions:
            stop_pct, _ = self._get_thresholds(pos.symbol)
            if pos.pnl_pct <= -stop_pct:
                logger.warning(
                    "손절 대상: %s %s (수익률: %.1f%%, 손절선: -%.1f%%)",
                    pos.symbol, pos.name, pos.pnl_pct, stop_pct,
                )
                stop_targets.append(pos)
        return stop_targets

    def update_high_watermarks(self, positions: list[Position]):
        """보유 포지션의 고점(high watermark)을 갱신한다.

        매 리스크 체크마다 호출하여 현재가가 기존 고점보다 높으면 갱신.
        포지션이 없는 종목의 고점 기록은 제거한다.
        """
        active_symbols = {pos.symbol for pos in positions}

        # 청산된 종목의 고점 기록 제거
        for sym in list(self._high_watermarks.keys()):
            if sym not in active_symbols:
                del self._high_watermarks[sym]

        # 고점 갱신
        for pos in positions:
            prev_high = self._high_watermarks.get(pos.symbol, 0)
            if pos.current_price > prev_high:
                self._high_watermarks[pos.symbol] = pos.current_price
                if prev_high > 0:
                    logger.debug(
                        "고점 갱신: %s | %s → %s",
                        pos.symbol, f"{prev_high:,.2f}", f"{pos.current_price:,.2f}",
                    )

    def check_take_profit(self, positions: list[Position]) -> list[Position]:
        """익절 대상을 반환한다.

        트레일링 스탑 로직:
        1) 수익률 >= trailing_activation_pct → 트레일링 활성화
        2) 고점 대비 trailing_stop_pct 이상 하락 → 익절 실행
        3) 수익률 >= take_profit_pct → 무조건 익절 (안전장치)
        """
        profit_targets = []
        for pos in positions:
            _, profit_pct = self._get_thresholds(pos.symbol)

            # 안전장치: 고정 익절선 도달 시 즉시 익절
            if pos.pnl_pct >= profit_pct:
                logger.info(
                    "고정 익절 대상: %s %s (수익률: %.1f%%, 익절선: +%.1f%%)",
                    pos.symbol, pos.name, pos.pnl_pct, profit_pct,
                )
                profit_targets.append(pos)
                continue

            # 트레일링 스탑: 활성화 조건 확인
            if pos.pnl_pct >= self.trailing_activation_pct:
                high = self._high_watermarks.get(pos.symbol, pos.current_price)
                if high <= 0:
                    continue

                drop_from_high = (high - pos.current_price) / high * 100
                if drop_from_high >= self.trailing_stop_pct:
                    logger.info(
                        "트레일링 스탑: %s %s | 수익률: %.1f%%, 고점: %s, "
                        "현재: %s, 고점대비: -%.1f%% (기준: -%.1f%%)",
                        pos.symbol, pos.name, pos.pnl_pct, f"{high:,.2f}",
                        f"{pos.current_price:,.2f}", drop_from_high, self.trailing_stop_pct,
                    )
                    profit_targets.append(pos)
                else:
                    logger.debug(
                        "트레일링 활성: %s | 수익률: %.1f%%, 고점: %s, "
                        "현재: %s, 고점대비: -%.1f%%",
                        pos.symbol, pos.pnl_pct, f"{high:,.2f}",
                        f"{pos.current_price:,.2f}", drop_from_high,
                    )
        return profit_targets

    def check_positions(self, positions: list[Position]) -> dict[str, list[Position]]:
        """전체 포지션을 점검하여 손절/익절 대상을 반환한다."""
        # 고점 갱신 후 체크
        self.update_high_watermarks(positions)
        return {
            "stop_loss": self.check_stop_loss(positions),
            "take_profit": self.check_take_profit(positions),
        }
