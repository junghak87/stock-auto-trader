"""하이브리드 브로커 클라이언트 모듈.

국내(KR)는 키움증권, 해외(US)는 한국투자증권으로 라우팅한다.
BrokerClient Protocol을 구현하므로 기존 코드 변경 없이 투명하게 동작한다.
"""

import logging

from core.broker import StockPrice, OrderResult, Position, OHLCVData

logger = logging.getLogger(__name__)


class HybridBrokerClient:
    """하이브리드 브로커 — KR은 키움, US는 KIS로 위임.

    BrokerClient Protocol을 구현하며, 내부적으로 두 브로커 클라이언트를 보유한다.
    """

    def __init__(self, kr_client, us_client):
        """
        Args:
            kr_client: 국내 주식용 클라이언트 (KiwoomClient)
            us_client: 해외 주식용 클라이언트 (KISClient)
        """
        self.kr = kr_client
        self.us = us_client
        self.is_live = kr_client.is_live  # 동일해야 함
        self.supported_markets = ["KR", "US"]

    # ── 국내 주식 시세 → 키움 ─────────────────────────────

    def get_kr_price(self, symbol: str) -> StockPrice:
        return self.kr.get_kr_price(symbol)

    def get_kr_daily_prices(self, symbol: str, period: str = "D", count: int = 60) -> list[OHLCVData]:
        return self.kr.get_kr_daily_prices(symbol, period, count)

    def get_kr_minute_prices(self, symbol: str, end_time: str = "") -> list[OHLCVData]:
        return self.kr.get_kr_minute_prices(symbol, end_time)

    def get_kr_index(self, index_code: str = "0001") -> dict:
        return self.kr.get_kr_index(index_code)

    # ── 해외 주식 시세 → KIS ─────────────────────────────

    def get_us_price(self, symbol: str, exchange: str = "") -> StockPrice:
        return self.us.get_us_price(symbol, exchange)

    def get_us_daily_prices(self, symbol: str, exchange: str = "", period: str = "0", count: int = 60) -> list[OHLCVData]:
        return self.us.get_us_daily_prices(symbol, exchange, period, count)

    # ── 국내 주식 주문 → 키움 ─────────────────────────────

    def buy_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        return self.kr.buy_kr(symbol, qty, price)

    def sell_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        return self.kr.sell_kr(symbol, qty, price)

    def cancel_kr(self, order_no: str, symbol: str, qty: int) -> OrderResult:
        return self.kr.cancel_kr(order_no, symbol, qty)

    # ── 해외 주식 주문 → KIS ─────────────────────────────

    def buy_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        return self.us.buy_us(symbol, qty, price, exchange)

    def sell_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        return self.us.sell_us(symbol, qty, price, exchange)

    # ── 거래량 순위 → 키움 ───────────────────────────────

    def get_kr_volume_rank(self, count: int = 20) -> list[dict]:
        return self.kr.get_kr_volume_rank(count)

    # ── 잔고 조회 ────────────────────────────────────────

    def get_kr_balance(self) -> list[Position]:
        return self.kr.get_kr_balance()

    def get_us_balance(self) -> list[Position]:
        return self.us.get_us_balance()

    def get_all_positions(self) -> list[Position]:
        positions = []
        try:
            positions.extend(self.kr.get_kr_balance())
        except Exception as e:
            logger.error("국내 잔고 조회 실패 (키움): %s", e)
        try:
            positions.extend(self.us.get_us_balance())
        except Exception as e:
            logger.error("해외 잔고 조회 실패 (KIS): %s", e)
        return positions

    def get_cash_balance(self) -> dict:
        """예수금 조회 — 국내(키움) + 해외(KIS) 합산."""
        kr_cash = {"total_eval": 0, "cash": 0, "stock_eval": 0, "total_pnl": 0}
        us_cash = {"total_eval": 0, "cash": 0, "stock_eval": 0, "total_pnl": 0}
        try:
            kr_cash = self.kr.get_cash_balance()
        except Exception as e:
            logger.error("국내 예수금 조회 실패 (키움): %s", e)
        try:
            us_cash = self.us.get_cash_balance()
        except Exception as e:
            logger.error("해외 예수금 조회 실패 (KIS): %s", e)
        return {
            "total_eval": kr_cash["total_eval"] + us_cash["total_eval"],
            "cash": kr_cash["cash"] + us_cash["cash"],
            "stock_eval": kr_cash["stock_eval"] + us_cash["stock_eval"],
            "total_pnl": kr_cash["total_pnl"] + us_cash["total_pnl"],
            "kr": kr_cash,
            "us": us_cash,
        }
