"""스케줄링 작업 정의 모듈.

장 시작 전 준비, 장중 전략 실행, 장 마감 후 정산,
해외 장 시간대 스케줄 등을 관리한다.
"""

import logging
import time

from core.kis_client import KISClient
from core.database import Database
from core.telegram_bot import TelegramNotifier
from strategies.base import BaseStrategy, Signal
from strategies.stock_scanner import StockScanner
from trading.executor import TradingExecutor
from trading.risk_manager import RiskManager

logger = logging.getLogger(__name__)

# API 호출 간격 (초) — 실전 20req/s, 모의 5req/s
RATE_LIMIT_DELAY = 0.15


class TradingJobs:
    """매매 스케줄 작업 모음."""

    def __init__(
        self,
        kis_client: KISClient,
        database: Database,
        notifier: TelegramNotifier,
        executor: TradingExecutor,
        risk_manager: RiskManager,
        strategy: BaseStrategy,
        scanner: StockScanner | None = None,
    ):
        self.kis = kis_client
        self.db = database
        self.notifier = notifier
        self.executor = executor
        self.risk = risk_manager
        self.strategy = strategy
        self.scanner = scanner

    # ── 국내 장 작업 ──────────────────────────────────────

    def _get_kr_stocks(self) -> list[str]:
        """DB에서 현재 활성 국내 감시 종목을 조회한다."""
        return self.db.get_watchlist_symbols("KR")

    def _get_us_stocks(self) -> list[str]:
        """DB에서 현재 활성 해외 감시 종목을 조회한다."""
        return self.db.get_watchlist_symbols("US")

    def job_kr_market_open(self):
        """장 시작 전 준비 (08:50)."""
        logger.info("=== 국내 장 시작 준비 ===")
        self.notifier.notify_system("국내 장 시작 준비 중...")

        kr_stocks = self._get_kr_stocks()
        for symbol in kr_stocks:
            try:
                price = self.kis.get_kr_price(symbol)
                logger.info("[%s] %s: %,.0f원 (%+.1f%%)", symbol, price.name, price.price, price.change_pct)
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error("[%s] 시세 조회 실패: %s", symbol, e)

        # AI 종목 스캔
        if self.scanner:
            try:
                picks = self.scanner.scan_and_select()
                if picks:
                    names = ", ".join(f"{p['symbol']}" for p in picks)
                    self.notifier.notify_system(f"AI 스캔 신규 종목: {names}")
                    kr_stocks = self._get_kr_stocks()  # 갱신
            except Exception as e:
                logger.error("AI 종목 스캔 실패: %s", e)

        self.notifier.notify_system(f"국내 감시 종목 {len(kr_stocks)}개 준비 완료")

    def job_kr_strategy_run(self):
        """국내 장중 전략 실행 (매 5분)."""
        logger.info("--- 국내 전략 실행 ---")

        for symbol in self._get_kr_stocks():
            try:
                # 일봉 데이터 조회
                ohlcv = self.kis.get_kr_daily_prices(symbol, count=60)
                if not ohlcv:
                    continue
                time.sleep(RATE_LIMIT_DELAY)

                # DataFrame 변환 후 전략 분석
                df = self.strategy.ohlcv_to_dataframe(ohlcv)
                result = self.strategy.analyze(df)

                # ATR 기반 동적 손절/익절 업데이트
                self.risk.update_dynamic_thresholds(symbol, df)

                logger.info("[%s] 시그널: %s (강도: %.2f) — %s", symbol, result.signal.value, result.strength, result.detail)

                # 시그널이 있으면 주문 실행
                if result.signal != Signal.HOLD and result.strength >= 0.3:
                    self.executor.execute_signal(symbol, "KR", result)

                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error("[%s] 전략 실행 실패: %s", symbol, e)

        # 손절/익절 체크
        self._check_risk("KR")

    def job_kr_market_close(self):
        """장 마감 후 정산 (15:40)."""
        logger.info("=== 국내 장 마감 정산 ===")

        trades = self.db.get_trades_today()
        positions = self.kis.get_all_positions()

        total_trades = len(trades)
        try:
            cash_info = self.kis.get_cash_balance()
            total_pnl = cash_info.get("total_pnl", 0)
        except Exception:
            total_pnl = 0

        win_count = sum(1 for t in trades if t.get("side") == "sell" and t.get("success"))
        loss_count = total_trades - win_count

        self.db.save_daily_summary(total_trades, total_pnl, win_count, loss_count)
        self.notifier.notify_daily_summary(total_trades, total_pnl, positions)

        logger.info("일일 정산 완료: %d건, 손익: %+,.0f원", total_trades, total_pnl)

    # ── 해외 장 작업 ──────────────────────────────────────

    def job_us_market_open(self):
        """해외 장 시작 준비 (23:20)."""
        logger.info("=== 해외 장 시작 준비 ===")
        self.notifier.notify_system("해외 장 시작 준비 중...")

        us_stocks = self._get_us_stocks()
        for symbol in us_stocks:
            try:
                price = self.kis.get_us_price(symbol)
                logger.info("[%s] %s: $%.2f (%+.1f%%)", symbol, price.name, price.price, price.change_pct)
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error("[%s] 해외 시세 조회 실패: %s", symbol, e)

        self.notifier.notify_system(f"해외 감시 종목 {len(us_stocks)}개 준비 완료")

    def job_us_strategy_run(self):
        """해외 장중 전략 실행 (매 10분)."""
        logger.info("--- 해외 전략 실행 ---")

        for symbol in self._get_us_stocks():
            try:
                ohlcv = self.kis.get_us_daily_prices(symbol, count=60)
                if not ohlcv:
                    continue
                time.sleep(RATE_LIMIT_DELAY)

                df = self.strategy.ohlcv_to_dataframe(ohlcv)
                result = self.strategy.analyze(df)

                # ATR 기반 동적 손절/익절 업데이트
                self.risk.update_dynamic_thresholds(symbol, df)

                logger.info("[%s] 시그널: %s (강도: %.2f) — %s", symbol, result.signal.value, result.strength, result.detail)

                if result.signal != Signal.HOLD and result.strength >= 0.3:
                    self.executor.execute_signal(symbol, "US", result)

                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                logger.error("[%s] 해외 전략 실행 실패: %s", symbol, e)

        self._check_risk("US")

    def job_us_market_close(self):
        """해외 장 마감 정산 (06:10)."""
        logger.info("=== 해외 장 마감 정산 ===")
        self.notifier.notify_system("해외 장 마감 — 일일 정산은 국내 장 마감 시 통합 수행")

    # ── 공통 ──────────────────────────────────────────────

    def _check_risk(self, market: str):
        """손절/익절 대상을 확인하고 실행한다."""
        try:
            if market == "KR":
                positions = self.kis.get_kr_balance()
            else:
                positions = self.kis.get_us_balance()

            checks = self.risk.check_positions(positions)

            for pos in checks["stop_loss"]:
                self.executor.execute_stop_loss(pos.symbol, pos.market, pos.qty)

            for pos in checks["take_profit"]:
                self.executor.execute_take_profit(pos.symbol, pos.market, pos.qty)
        except Exception as e:
            logger.error("리스크 체크 실패 [%s]: %s", market, e)
