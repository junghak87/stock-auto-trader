"""리스크 관리 모듈.

포지션 한도, 손절/익절, 일일 거래 제한 등을 관리한다.
"""

import logging

from core.kis_client import KISClient, Position
from core.database import Database

logger = logging.getLogger(__name__)


class RiskManager:
    """리스크 관리자."""

    def __init__(
        self,
        kis_client: KISClient,
        database: Database,
        max_position_ratio: float = 0.1,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        max_daily_trades: int = 20,
    ):
        self.kis = kis_client
        self.db = database
        self.max_position_ratio = max_position_ratio
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_daily_trades = max_daily_trades

    def can_trade(self) -> tuple[bool, str]:
        """현재 거래가 가능한 상태인지 확인한다."""
        trade_count = self.db.get_trade_count_today()
        if trade_count >= self.max_daily_trades:
            return False, f"일일 최대 거래 횟수 초과 ({trade_count}/{self.max_daily_trades})"
        return True, "거래 가능"

    def calculate_buy_qty(self, symbol: str, price: float, market: str = "KR") -> int:
        """매수 가능 수량을 계산한다 (최대 포지션 비율 기반)."""
        try:
            cash_info = self.kis.get_cash_balance()
            total_eval = cash_info.get("total_eval", 0)
            available_cash = cash_info.get("cash", 0)

            if total_eval <= 0 or price <= 0:
                return 0

            max_invest = total_eval * self.max_position_ratio
            max_by_cash = available_cash * 0.95  # 현금의 95%까지만 사용
            invest_amount = min(max_invest, max_by_cash)
            qty = int(invest_amount / price)

            logger.info(
                "매수 수량 계산: %s | 총평가=%s, 최대투자=%s, 가격=%s → %d주",
                symbol, f"{total_eval:,.0f}", f"{invest_amount:,.0f}", f"{price:,.0f}", qty,
            )
            return max(qty, 0)
        except Exception as e:
            logger.error("매수 수량 계산 실패: %s", e)
            return 0

    def check_stop_loss(self, positions: list[Position]) -> list[Position]:
        """손절선에 도달한 포지션을 반환한다."""
        stop_targets = []
        for pos in positions:
            if pos.pnl_pct <= -self.stop_loss_pct:
                logger.warning(
                    "손절 대상: %s %s (수익률: %.1f%%, 손절선: -%.1f%%)",
                    pos.symbol, pos.name, pos.pnl_pct, self.stop_loss_pct,
                )
                stop_targets.append(pos)
        return stop_targets

    def check_take_profit(self, positions: list[Position]) -> list[Position]:
        """익절선에 도달한 포지션을 반환한다."""
        profit_targets = []
        for pos in positions:
            if pos.pnl_pct >= self.take_profit_pct:
                logger.info(
                    "익절 대상: %s %s (수익률: %.1f%%, 익절선: +%.1f%%)",
                    pos.symbol, pos.name, pos.pnl_pct, self.take_profit_pct,
                )
                profit_targets.append(pos)
        return profit_targets

    def check_positions(self, positions: list[Position]) -> dict[str, list[Position]]:
        """전체 포지션을 점검하여 손절/익절 대상을 반환한다."""
        return {
            "stop_loss": self.check_stop_loss(positions),
            "take_profit": self.check_take_profit(positions),
        }
