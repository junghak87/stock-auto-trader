"""주문 실행기 모듈.

전략 시그널과 리스크 관리 결과를 기반으로 실제 주문을 실행한다.
지정가 주문, 분할 매수/매도를 지원한다.
"""

import logging
from datetime import datetime

from core.kis_client import KISClient, OrderResult
from core.database import Database
from core.telegram_bot import TelegramNotifier
from strategies.base import Signal, StrategyResult
from trading.risk_manager import RiskManager

logger = logging.getLogger(__name__)

# KRX 호가 단위 테이블
_KR_TICK_TABLE = [
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500), (float("inf"), 1_000),
]


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
        # 지정가 주문 설정 (main.py에서 설정)
        self.limit_order_enabled: bool = True
        self.limit_buy_offset_pct: float = 0.3
        self.limit_tp_offset_pct: float = 0.3
        self.limit_order_timeout_sec: int = 300
        # 분할 매수/매도 설정
        self.split_buy_enabled: bool = True
        self.split_buy_first_ratio: float = 0.5
        self.split_buy_dip_pct: float = 2.0
        self.split_sell_enabled: bool = True
        self.split_sell_first_ratio: float = 0.5
        # 미체결 지정가 주문 추적 {symbol: {order_no, qty, price, placed_at, ...}}
        self._pending_orders: dict[str, dict] = {}
        # 분할 매수 단계 추적 {symbol: {stage, first_buy_price, first_buy_qty, market, ...}}
        self._position_stages: dict[str, dict] = {}
        # 잔고 캐시 (매 전략 실행 사이클에서 반복 API 호출 방지)
        self._balance_cache: dict[str, list] = {}
        self._balance_cache_time: datetime | None = None

    def _get_cached_positions(self, market: str) -> list:
        """캐시된 잔고를 반환한다 (30초 TTL)."""
        now = datetime.now()
        if not self._balance_cache_time or (now - self._balance_cache_time).total_seconds() >= 30:
            self._balance_cache.clear()
            self._balance_cache_time = now

        if market not in self._balance_cache:
            try:
                if market == "KR":
                    positions = self.kis.get_kr_balance()
                else:
                    positions = self.kis.get_us_balance()
                self._balance_cache[market] = positions
            except Exception:
                return []

        return self._balance_cache.get(market, [])

    def _is_holding(self, symbol: str, market: str) -> bool:
        """해당 종목의 보유 여부를 확인한다 (30초 캐시)."""
        positions = self._get_cached_positions(market)
        return any(p.symbol == symbol and p.qty > 0 for p in positions)

    def execute_signal(self, symbol: str, market: str, result: StrategyResult) -> OrderResult | None:
        """전략 시그널에 따라 주문을 실행한다."""
        if result.signal == Signal.HOLD:
            return None

        # 보유 여부 사전 확인 (불필요한 알림/API 호출 방지)
        is_held = self._is_holding(symbol, market)
        if result.signal == Signal.SELL and not is_held:
            logger.debug("미보유 종목 매도 시그널 무시: %s", symbol)
            return None
        if result.signal == Signal.BUY and is_held:
            logger.debug("보유 중 종목 매수 시그널 무시: %s", symbol)
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
        """매수 주문을 실행한다 (이미 보유 중이면 스킵)."""
        try:
            if market == "KR":
                price_info = self.kis.get_kr_price(symbol)
                price = price_info.price
                qty = self.risk.calculate_buy_qty(symbol, price, market)
                if qty <= 0:
                    logger.info("매수 수량 0 -- 주문 스킵: %s", symbol)
                    return None

                # 분할 매수: 첫 매수 시 비율만큼만
                if self.split_buy_enabled:
                    buy_qty = max(1, int(qty * self.split_buy_first_ratio))
                else:
                    buy_qty = qty

                # 지정가 매수: 현재가 대비 offset만큼 낮은 가격 (모의투자: offset=0 즉시 체결)
                if self.limit_order_enabled:
                    offset = 0 if not self.kis.is_live else self.limit_buy_offset_pct
                    limit_price = self._round_kr_tick(int(price * (1 - offset / 100)))
                    order = self.kis.buy_kr(symbol, buy_qty, price=limit_price)
                    if order.success:
                        self._pending_orders[symbol] = {
                            "order_no": order.order_no, "symbol": symbol,
                            "market": market, "side": "buy", "qty": buy_qty,
                            "price": limit_price, "placed_at": datetime.now(),
                            "strategy": result.strategy_name,
                        }
                else:
                    order = self.kis.buy_kr(symbol, buy_qty, price=0)

                # 분할 매수 단계 기록
                if order.success and self.split_buy_enabled:
                    self._position_stages[symbol] = {
                        "stage": 1, "first_buy_price": price,
                        "first_buy_qty": buy_qty, "market": market,
                    }
            else:
                # US
                price_info = self.kis.get_us_price(symbol)
                price = price_info.price
                qty = self.risk.calculate_buy_qty(symbol, price, market)
                if qty <= 0:
                    logger.info("매수 수량 0 -- 주문 스킵: %s", symbol)
                    return None
                buy_qty = max(1, int(qty * self.split_buy_first_ratio)) if self.split_buy_enabled else qty
                order = self.kis.buy_us(symbol, buy_qty, price=0)
                if order.success and self.split_buy_enabled:
                    self._position_stages[symbol] = {
                        "stage": 1, "first_buy_price": price,
                        "first_buy_qty": buy_qty, "market": market,
                    }

            self._log_order(order, market, result.strategy_name)
            return order

        except Exception as e:
            logger.error("매수 주문 실패: %s %s -- %s", symbol, market, e)
            self.notifier.notify_error(f"매수 주문 실패: {symbol} -- {e}")
            return None

    def _get_us_positions_single(self, exchange: str) -> list:
        """단일 거래소의 US 잔고만 조회한다 (API 호출 절약)."""
        try:
            acct_prefix = self.kis.account_no.split("-")[0]
            acct_suffix = self.kis.account_no.split("-")[1] if "-" in self.kis.account_no else "01"
            tr_id = self.kis._tr("us_balance")
            data = self.kis._get(
                "/uapi/overseas-stock/v1/trading/inquire-balance",
                tr_id,
                params={
                    "CANO": acct_prefix,
                    "ACNT_PRDT_CD": acct_suffix,
                    "OVRS_EXCG_CD": exchange,
                    "TR_CRCY_CD": "USD",
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": "",
                },
            )
            from core.kis_client import Position
            positions = []
            for item in data.get("output1", []):
                qty = int(item.get("ovrs_cblc_qty", 0))
                if qty == 0:
                    continue
                positions.append(Position(
                    symbol=item.get("ovrs_pdno", ""),
                    name=item.get("ovrs_item_name", ""),
                    qty=qty,
                    avg_price=float(item.get("pchs_avg_pric", 0)),
                    current_price=float(item.get("now_pric2", item.get("ovrs_now_pric", 0))),
                    pnl=float(item.get("frcr_evlu_pfls_amt", 0)),
                    pnl_pct=float(item.get("evlu_pfls_rt", 0)),
                    market="US",
                ))
            return positions
        except Exception:
            return []

    def _execute_sell(self, symbol: str, market: str, result: StrategyResult) -> OrderResult | None:
        """매도 주문을 실행한다 (보유 중인 경우에만)."""
        try:
            # 보유 수량 확인 (캐시 활용)
            positions = self._get_cached_positions(market)
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
        """손절 매도를 실행한다 (항상 시장가, 전량 매도)."""
        try:
            logger.warning("손절 매도 실행: %s %d주", symbol, qty)
            if market == "KR":
                order = self.kis.sell_kr(symbol, qty, price=0)
            else:
                order = self.kis.sell_us(symbol, qty, price=0)
            self._log_order(order, market, "stop_loss")
            if order.success:
                self.risk.record_stop_loss()
                self._position_stages.pop(symbol, None)
            return order
        except Exception as e:
            logger.error("손절 매도 실패: %s — %s", symbol, e)
            self.notifier.notify_error(f"손절 매도 실패: {symbol} — {e}")
            return None

    def execute_take_profit(self, symbol: str, market: str, qty: int) -> OrderResult | None:
        """익절 매도를 실행한다 (분할 매도 + 지정가 지원)."""
        try:
            stage_info = self._position_stages.get(symbol)

            # 분할 매도: 첫 익절 시 일부만 매도
            if self.split_sell_enabled and stage_info and not stage_info.get("partial_sold"):
                sell_qty = max(1, int(qty * self.split_sell_first_ratio))
                stage_info["partial_sold"] = True
                logger.info("분할 익절 (1차): %s %d/%d주", symbol, sell_qty, qty)
            else:
                sell_qty = qty
                self._position_stages.pop(symbol, None)

            # 지정가 익절 (KR만, 현재가 대비 +offset%, 모의투자: offset=0 즉시 체결)
            if market == "KR" and self.limit_order_enabled:
                price_info = self.kis.get_kr_price(symbol)
                tp_offset = 0 if not self.kis.is_live else self.limit_tp_offset_pct
                limit_price = self._round_kr_tick(int(price_info.price * (1 + tp_offset / 100)))
                order = self.kis.sell_kr(symbol, sell_qty, price=limit_price)
                if order.success:
                    self._pending_orders[f"{symbol}_tp"] = {
                        "order_no": order.order_no, "symbol": symbol,
                        "market": market, "side": "sell", "qty": sell_qty,
                        "price": limit_price, "placed_at": datetime.now(),
                        "strategy": "take_profit",
                    }
            else:
                if market == "KR":
                    order = self.kis.sell_kr(symbol, sell_qty, price=0)
                else:
                    order = self.kis.sell_us(symbol, sell_qty, price=0)

            self._log_order(order, market, "take_profit")
            if order.success:
                self.risk.record_profit()
            return order
        except Exception as e:
            logger.error("익절 매도 실패: %s — %s", symbol, e)
            self.notifier.notify_error(f"익절 매도 실패: {symbol} — {e}")
            return None

    def check_split_buy_opportunity(self, symbol: str, market: str) -> OrderResult | None:
        """분할 매수 2단계 기회를 체크한다 (가격 하락 시 추가 매수)."""
        stage_info = self._position_stages.get(symbol)
        if not stage_info or stage_info.get("stage", 0) >= 2:
            return None

        try:
            if market == "KR":
                price_info = self.kis.get_kr_price(symbol)
            else:
                price_info = self.kis.get_us_price(symbol)
            current_price = price_info.price

            dip_threshold = stage_info["first_buy_price"] * (1 - self.split_buy_dip_pct / 100)
            if current_price > dip_threshold:
                return None

            remaining_ratio = 1.0 - self.split_buy_first_ratio
            full_qty = self.risk.calculate_buy_qty(symbol, current_price, market)
            add_qty = max(1, int(full_qty * remaining_ratio))

            logger.info("분할 매수 2단계: %s | 1차가: %,.0f → 현재: %,.0f (-%,.1f%%), 추가 %d주",
                         symbol, stage_info["first_buy_price"], current_price,
                         (1 - current_price / stage_info["first_buy_price"]) * 100, add_qty)

            if market == "KR":
                if self.limit_order_enabled:
                    offset = 0 if not self.kis.is_live else self.limit_buy_offset_pct
                    limit_price = self._round_kr_tick(int(current_price * (1 - offset / 100)))
                    order = self.kis.buy_kr(symbol, add_qty, price=limit_price)
                else:
                    order = self.kis.buy_kr(symbol, add_qty, price=0)
            else:
                order = self.kis.buy_us(symbol, add_qty, price=0)

            if order.success:
                stage_info["stage"] = 2

            self._log_order(order, market, "split_buy_stage2")
            return order
        except Exception as e:
            logger.error("분할 매수 2단계 실패: %s — %s", symbol, e)
            return None

    def check_pending_orders(self):
        """미체결 지정가 주문을 체크하고 타임아웃 시 취소한다."""
        now = datetime.now()
        for key, info in list(self._pending_orders.items()):
            elapsed = (now - info["placed_at"]).total_seconds()
            if elapsed >= self.limit_order_timeout_sec:
                try:
                    if info["market"] == "KR":
                        cancel_result = self.kis.cancel_kr(
                            order_no=info["order_no"],
                            symbol=info["symbol"],
                            qty=info["qty"],
                        )
                        if cancel_result.success:
                            logger.info("지정가 주문 취소 (타임아웃): %s %s %d주 @ %,d",
                                        info["side"], info["symbol"], info["qty"], info["price"])
                            self.notifier.notify_system(
                                f"지정가 주문 취소: {info['symbol']} {info['qty']}주 "
                                f"@ {info['price']:,} ({elapsed:.0f}초 미체결)"
                            )
                except Exception as e:
                    logger.error("주문 취소 실패: %s — %s", info["symbol"], e)
                del self._pending_orders[key]

    @staticmethod
    def _round_kr_tick(price: int) -> int:
        """KRX 호가 단위에 맞게 내림 처리한다."""
        for threshold, tick in _KR_TICK_TABLE:
            if price < threshold:
                return price - (price % tick)
        return price

    def _log_order(self, order: OrderResult, market: str, strategy: str):
        """주문 결과를 DB에 기록하고 텔레그램으로 알린다."""
        # 시장가 주문(price=0)일 때 실제 체결가를 조회하여 기록
        actual_price = order.price
        if order.success and order.price == 0:
            try:
                if market == "KR":
                    p = self.kis.get_kr_price(order.symbol)
                else:
                    p = self.kis.get_us_price(order.symbol)
                actual_price = p.price
            except Exception:
                pass

        self.db.save_trade(
            symbol=order.symbol,
            name="",
            market=market,
            side=order.side,
            qty=order.qty,
            price=actual_price,
            order_no=order.order_no,
            strategy=strategy,
            success=order.success,
            message=order.message,
        )
        self.notifier.notify_order(
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=actual_price,
            success=order.success,
            message=order.message,
        )
        if order.success:
            logger.info("주문 체결: %s %s %d주 @ %s [%s]", order.side, order.symbol, order.qty, actual_price, strategy)
        else:
            logger.error("주문 실패: %s %s — %s", order.side, order.symbol, order.message)
