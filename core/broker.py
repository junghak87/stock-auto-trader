"""브로커 클라이언트 인터페이스 및 공통 데이터 클래스.

각 증권사 클라이언트(KIS, Kiwoom 등)가 구현해야 하는 공통 인터페이스를 정의한다.
Protocol 기반으로, 상속 없이도 같은 메서드 시그니처만 맞추면 타입 체크가 통과한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class StockPrice:
    """주식 시세 정보."""

    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    volume: int
    high: float
    low: float
    open: float
    prev_close: float
    market: str  # "KR" or "US"


@dataclass
class OrderResult:
    """주문 결과."""

    success: bool
    order_no: str
    message: str
    symbol: str
    side: str  # "buy" or "sell"
    qty: int
    price: float


@dataclass
class Position:
    """보유 종목."""

    symbol: str
    name: str
    qty: int
    avg_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    market: str


@dataclass
class OHLCVData:
    """일봉/분봉 데이터."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@runtime_checkable
class BrokerClient(Protocol):
    """증권사 클라이언트 공통 인터페이스.

    각 증권사 클라이언트는 이 Protocol의 메서드를 구현해야 한다.
    해외 주식을 지원하지 않는 증권사는 US 관련 메서드에서 NotImplementedError를 발생시킨다.
    """

    is_live: bool
    supported_markets: list[str]  # ["KR"] or ["KR", "US"]

    # ── 국내 주식 시세 ──────────────────────────────────

    def get_kr_price(self, symbol: str) -> StockPrice: ...
    def get_kr_daily_prices(self, symbol: str, period: str = "D", count: int = 60) -> list[OHLCVData]: ...
    def get_kr_minute_prices(self, symbol: str, end_time: str = "") -> list[OHLCVData]: ...
    def get_kr_index(self, index_code: str = "0001") -> dict: ...

    # ── 해외 주식 시세 ──────────────────────────────────

    def get_us_price(self, symbol: str, exchange: str = "") -> StockPrice: ...
    def get_us_daily_prices(self, symbol: str, exchange: str = "", period: str = "0", count: int = 60) -> list[OHLCVData]: ...

    # ── 국내 주식 주문 ──────────────────────────────────

    def buy_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult: ...
    def sell_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult: ...
    def cancel_kr(self, order_no: str, symbol: str, qty: int) -> OrderResult: ...

    # ── 해외 주식 주문 ──────────────────────────────────

    def buy_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult: ...
    def sell_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult: ...

    # ── 거래량 순위 ─────────────────────────────────────

    def get_kr_volume_rank(self, count: int = 20) -> list[dict]: ...

    # ── 잔고 조회 ───────────────────────────────────────

    def get_kr_balance(self) -> list[Position]: ...
    def get_us_balance(self) -> list[Position]: ...
    def get_all_positions(self) -> list[Position]: ...
    def get_cash_balance(self) -> dict: ...
