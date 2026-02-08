"""주문 실행기 모듈.

전략 시그널과 리스크 관리 결과를 기반으로 실제 주문을 실행한다.
"""

import logging

from core.kis_client import KISClient, OrderResult
from core.database import Database
from core.telegram_bot import TelegramNotifier
from strategies.base import Signal, StrategyResult
from trading.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class TradingExecutor:
    """매매 주문 실행기."""

    def __init__(
        self,
        kis_client: KISClient,
        database: Database,
        notifier: TelegramNotifier,
        risk_manager: RiskManager,
    ):
        self.kis = kis_client
        self.db = database
        self.notifier = notifier
        self.risk = risk_manager

    def execute_signal(self, symbol: str, market: str, result: StrategyResult) -> OrderResult | None:
        """전략 시그널에 따라 주문을 실행한다."""
        if result.signal == Signal.HOLD:
            return None

        # 거래 가능 여부 확인
        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            logger.warning("거래 불가: %s", reason)
            self.notifier.notify_error(f"거래 불가: {reason}")
            return None

        # 시그널 알림
        self.notifier.notify_signal(symbol, market, result.strategy_name, result.signal.value, result.detail)
        self.db.save_signal(symbol, market, result.strategy_name, result.signal.value, result.strength, result.detail)

        if result.signal == Signal.BUY:
            return self._execute_buy(symbol, market, result)
        elif result.signal == Signal.SELL:
            return self._execute_sell(symbol, market, result)
        return None

    def _execute_buy(self, symbol: str, market: str, result: StrategyResult) -> OrderResult | None:
        """매수 주문을 실행한다."""
        try:
            if market == "KR":
                price_info = self.kis.get_kr_price(symbol)
                price = price_info.price
                qty = self.risk.calculate_buy_qty(symbol, price, market)
                if qty <= 0:
                    logger.info("매수 수량 0 — 주문 스킵: %s", symbol)
                    return None
                order = self.kis.buy_kr(symbol, qty, price=0)  # 시장가 매수
            else:
                price_info = self.kis.get_us_price(symbol)
                price = price_info.price
                qty = self.risk.calculate_buy_qty(symbol, price, market)
                if qty <= 0:
                    logger.info("매수 수량 0 — 주문 스킵: %s", symbol)
                    return None
                order = self.kis.buy_us(symbol, qty, price=0)  # 시장가 매수

            self._log_order(order, market, result.strategy_name)
            return order

        except Exception as e:
            logger.error("매수 주문 실패: %s %s — %s", symbol, market, e)
            self.notifier.notify_error(f"매수 주문 실패: {symbol} — {e}")
            return None

    def _execute_sell(self, symbol: str, market: str, result: StrategyResult) -> OrderResult | None:
        """매도 주문을 실행한다 (보유 중인 경우에만)."""
        try:
            # 보유 수량 확인
            if market == "KR":
                positions = self.kis.get_kr_balance()
            else:
                positions = self.kis.get_us_balance()

            held = next((p for p in positions if p.symbol == symbol), None)
            if not held or held.qty <= 0:
                logger.info("보유하지 않은 종목 — 매도 스킵: %s", symbol)
                return None

            if market == "KR":
                order = self.kis.sell_kr(symbol, held.qty, price=0)  # 시장가 매도
            else:
                order = self.kis.sell_us(symbol, held.qty, price=0)  # 시장가 매도

            self._log_order(order, market, result.strategy_name)
            return order

        except Exception as e:
            logger.error("매도 주문 실패: %s %s — %s", symbol, market, e)
            self.notifier.notify_error(f"매도 주문 실패: {symbol} — {e}")
            return None

    def execute_stop_loss(self, symbol: str, market: str, qty: int) -> OrderResult | None:
        """손절 매도를 실행한다."""
        try:
            logger.warning("손절 매도 실행: %s %d주", symbol, qty)
            if market == "KR":
                order = self.kis.sell_kr(symbol, qty, price=0)
            else:
                order = self.kis.sell_us(symbol, qty, price=0)
            self._log_order(order, market, "stop_loss")
            return order
        except Exception as e:
            logger.error("손절 매도 실패: %s — %s", symbol, e)
            self.notifier.notify_error(f"손절 매도 실패: {symbol} — {e}")
            return None

    def execute_take_profit(self, symbol: str, market: str, qty: int) -> OrderResult | None:
        """익절 매도를 실행한다."""
        try:
            logger.info("익절 매도 실행: %s %d주", symbol, qty)
            if market == "KR":
                order = self.kis.sell_kr(symbol, qty, price=0)
            else:
                order = self.kis.sell_us(symbol, qty, price=0)
            self._log_order(order, market, "take_profit")
            return order
        except Exception as e:
            logger.error("익절 매도 실패: %s — %s", symbol, e)
            self.notifier.notify_error(f"익절 매도 실패: {symbol} — {e}")
            return None

    def _log_order(self, order: OrderResult, market: str, strategy: str):
        """주문 결과를 DB에 기록하고 텔레그램으로 알린다."""
        self.db.save_trade(
            symbol=order.symbol,
            name="",
            market=market,
            side=order.side,
            qty=order.qty,
            price=order.price,
            order_no=order.order_no,
            strategy=strategy,
            success=order.success,
            message=order.message,
        )
        self.notifier.notify_order(
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=order.price,
            success=order.success,
            message=order.message,
        )
        if order.success:
            logger.info("주문 체결: %s %s %d주 @ %s [%s]", order.side, order.symbol, order.qty, order.price, strategy)
        else:
            logger.error("주문 실패: %s %s — %s", order.side, order.symbol, order.message)
