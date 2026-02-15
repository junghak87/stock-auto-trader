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
from strategies.tail_trading import TailTradingStrategy
from trading.executor import TradingExecutor
from trading.risk_manager import RiskManager

logger = logging.getLogger(__name__)


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
        tail_strategy: TailTradingStrategy | None = None,
    ):
        self.kis = kis_client
        self.db = database
        self.notifier = notifier
        self.executor = executor
        self.risk = risk_manager
        self.strategy = strategy
        self.scanner = scanner
        self.tail_strategy = tail_strategy
        # 실전 20req/s(0.08s), 모의 5req/s(0.25s)
        self.rate_delay = 0.08 if kis_client.is_live else 0.25

    # ── 국내 장 작업 ──────────────────────────────────────

    def _get_kr_stocks(self) -> list[str]:
        """DB에서 현재 활성 국내 감시 종목을 조회한다."""
        return self.db.get_watchlist_symbols("KR")

    def _get_us_stocks(self) -> list[str]:
        """DB에서 현재 활성 해외 감시 종목을 조회한다."""
        return self.db.get_watchlist_symbols("US")

    def _clear_ai_cache(self):
        """CompositeStrategy 내부의 AI 캐시를 초기화한다."""
        if hasattr(self.strategy, '_ai_strategy') and self.strategy._ai_strategy:
            self.strategy._ai_strategy.clear_cache()

    def job_kr_market_open(self):
        """장 시작 전 준비 (08:50)."""
        logger.info("=== 국내 장 시작 준비 ===")
        self.notifier.notify_system("국내 장 시작 준비 중...")
        self._clear_ai_cache()

        kr_stocks = self._get_kr_stocks()
        for symbol in kr_stocks:
            try:
                price = self.kis.get_kr_price(symbol)
                logger.info("[%s] %s: %,.0f원 (%+.1f%%)", symbol, price.name, price.price, price.change_pct)
                time.sleep(self.rate_delay)
            except Exception as e:
                logger.error("[%s] 시세 조회 실패: %s", symbol, e)

        # AI 종목 스캔 (매일 초기화 후 재스캔)
        if self.scanner:
            try:
                self.db.clear_ai_watchlist("KR")
                picks = self.scanner.scan_and_select()
                if picks:
                    names = ", ".join(f"{p['symbol']}" for p in picks)
                    self.notifier.notify_system(f"AI 스캔 신규 종목: {names}")
                    kr_stocks = self._get_kr_stocks()  # 갱신
            except Exception as e:
                logger.error("AI 종목 스캔 실패: %s", e)

        self.notifier.notify_system(f"국내 감시 종목 {len(kr_stocks)}개 준비 완료")

    def _fetch_kr_market_context(self) -> str:
        """KOSPI/KOSDAQ 지수를 조회하여 시장 컨텍스트 문자열을 생성한다."""
        lines = ["[시장 지수]"]
        for code, name in [("0001", "KOSPI"), ("1001", "KOSDAQ")]:
            try:
                idx = self.kis.get_kr_index(code)
                lines.append(f"{name}: {idx['price']:,.2f} ({idx['change_pct']:+.2f}%)")
                time.sleep(self.rate_delay)
            except Exception as e:
                logger.debug("%s 지수 조회 실패: %s", name, e)
        return "\n".join(lines) if len(lines) > 1 else ""

    def job_kr_strategy_run(self):
        """국내 장중 전략 실행 (매 15분)."""
        logger.info("--- 국내 전략 실행 ---")

        # 시장 컨텍스트 설정 (AI에게 KOSPI/KOSDAQ 지수 정보 전달)
        if hasattr(self.strategy, 'set_market_context'):
            ctx = self._fetch_kr_market_context()
            if ctx:
                self.strategy.set_market_context(ctx)

        for symbol in self._get_kr_stocks():
            try:
                # 일봉 데이터 조회
                ohlcv = self.kis.get_kr_daily_prices(symbol, count=60)
                if not ohlcv:
                    continue
                time.sleep(self.rate_delay)

                # DataFrame 변환 후 전략 분석
                df = self.strategy.ohlcv_to_dataframe(ohlcv)
                result = self.strategy.analyze(df)

                # ATR 기반 동적 손절/익절 업데이트
                self.risk.update_dynamic_thresholds(symbol, df)

                logger.info("[%s] 시그널: %s (강도: %.2f) — %s", symbol, result.signal.value, result.strength, result.detail)

                # 시그널이 있으면 주문 실행
                if result.signal != Signal.HOLD and result.strength >= 0.3:
                    self.executor.execute_signal(symbol, "KR", result)

                time.sleep(self.rate_delay)
            except Exception as e:
                logger.error("[%s] 전략 실행 실패: %s", symbol, e)

    def job_kr_risk_check(self):
        """국내 손절/익절 체크 (매 5분)."""
        self._check_risk("KR")

    def job_kr_tail_trading(self):
        """국내 꼬리 매매 (매 3분) — 분봉 기반 반등 포착."""
        if not self.tail_strategy:
            return

        for symbol in self._get_kr_stocks():
            try:
                # 쿨다운 체크
                if not self.tail_strategy.is_cooled_down(symbol):
                    continue

                # 1분봉 30개 조회
                minute_data = self.kis.get_kr_minute_prices(symbol)
                if not minute_data or len(minute_data) < 10:
                    time.sleep(self.rate_delay)
                    continue

                # DataFrame 변환 → 5분봉 집계
                df_1min = self.strategy.ohlcv_to_dataframe(minute_data)
                df_5min = self.tail_strategy.aggregate_to_5min(df_1min)

                if len(df_5min) < 3:
                    time.sleep(self.rate_delay)
                    continue

                result = self.tail_strategy.analyze(df_5min)

                if result.signal != Signal.HOLD and result.strength >= 0.3:
                    logger.info("[%s] 꼬리 매매 시그널: %s (%.2f) — %s",
                                symbol, result.signal.value, result.strength, result.detail)
                    self.executor.execute_signal(symbol, "KR", result)
                    self.tail_strategy.mark_signal(symbol)

                time.sleep(self.rate_delay)
            except Exception as e:
                logger.error("[%s] 꼬리 매매 실패: %s", symbol, e)

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

        # 전략별 성과 저장
        try:
            from datetime import datetime as dt
            today = dt.now().strftime("%Y-%m-%d")
            strategy_stats = self.db.calculate_strategy_performance(days=1)
            for s in strategy_stats:
                self.db.save_strategy_performance(today, s["strategy"], s)
            if strategy_stats:
                logger.info("전략별 성과 저장: %d개 전략", len(strategy_stats))
        except Exception as e:
            logger.error("전략별 성과 저장 실패: %s", e)

        # 오래된 데이터 정리 (90일 이전)
        self.db.cleanup_old_data(retention_days=90)

        logger.info("일일 정산 완료: %d건, 손익: %+,.0f원", total_trades, total_pnl)

    # ── 해외 장 작업 ──────────────────────────────────────

    def job_kr_midday_scan(self):
        """장중 AI 추가 스캔 (11:00, 13:30) — 기존 watchlist 유지하며 추가만."""
        if not self.scanner:
            return
        logger.info("--- 국내 장중 AI 추가 스캔 ---")
        try:
            picks = self.scanner.scan_and_select()
            if picks:
                names = ", ".join(f"{p['symbol']}" for p in picks)
                self.notifier.notify_system(f"장중 AI 스캔 추가: {names}")
                logger.info("장중 스캔 %d개 종목 추가", len(picks))
        except Exception as e:
            logger.error("장중 AI 스캔 실패: %s", e)

    def job_us_market_open(self):
        """해외 장 시작 준비 (23:20)."""
        logger.info("=== 해외 장 시작 준비 ===")
        self.notifier.notify_system("해외 장 시작 준비 중...")
        self._clear_ai_cache()

        us_stocks = self._get_us_stocks()
        for symbol in us_stocks:
            try:
                price = self.kis.get_us_price(symbol)
                logger.info("[%s] %s: $%.2f (%+.1f%%)", symbol, price.name, price.price, price.change_pct)
                time.sleep(self.rate_delay)
            except Exception as e:
                logger.error("[%s] 해외 시세 조회 실패: %s", symbol, e)

        # AI 해외 종목 스캔
        if self.scanner:
            try:
                self.db.clear_ai_watchlist("US")
                picks = self.scanner.scan_us_and_select()
                if picks:
                    names = ", ".join(f"{p['symbol']}" for p in picks)
                    self.notifier.notify_system(f"AI US 스캔 신규 종목: {names}")
                    us_stocks = self._get_us_stocks()  # 갱신
            except Exception as e:
                logger.error("AI US 종목 스캔 실패: %s", e)

        self.notifier.notify_system(f"해외 감시 종목 {len(us_stocks)}개 준비 완료")

    def job_us_strategy_run(self):
        """해외 장중 전략 실행 (매 15분)."""
        logger.info("--- 해외 전략 실행 ---")

        for symbol in self._get_us_stocks():
            try:
                ohlcv = self.kis.get_us_daily_prices(symbol, count=60)
                if not ohlcv:
                    continue
                time.sleep(self.rate_delay)

                df = self.strategy.ohlcv_to_dataframe(ohlcv)
                result = self.strategy.analyze(df)

                # ATR 기반 동적 손절/익절 업데이트
                self.risk.update_dynamic_thresholds(symbol, df)

                logger.info("[%s] 시그널: %s (강도: %.2f) — %s", symbol, result.signal.value, result.strength, result.detail)

                if result.signal != Signal.HOLD and result.strength >= 0.3:
                    self.executor.execute_signal(symbol, "US", result)

                time.sleep(self.rate_delay)
            except Exception as e:
                logger.error("[%s] 해외 전략 실행 실패: %s", symbol, e)

    def job_us_risk_check(self):
        """해외 손절/익절 체크 (매 5분)."""
        self._check_risk("US")

    def job_us_market_close(self):
        """해외 장 마감 정산 (06:10)."""
        logger.info("=== 해외 장 마감 정산 ===")
        self.notifier.notify_system("해외 장 마감 — 일일 정산은 국내 장 마감 시 통합 수행")

    # ── 공통 ──────────────────────────────────────────────

    def _check_risk(self, market: str):
        """손절/익절 대상을 확인하고, 미체결 주문 및 분할 매수도 체크한다."""
        # 미체결 지정가 주문 타임아웃 체크
        try:
            self.executor.check_pending_orders()
        except Exception as e:
            logger.error("미체결 주문 체크 실패: %s", e)

        # 거래 가능 여부 확인 (연속 손절 쿨다운, 일일 최대 손실)
        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            logger.warning("거래 중단: %s", reason)
            return

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

            # 분할 매수 2단계 체크
            for symbol, stage_info in list(self.executor._position_stages.items()):
                if stage_info.get("market") != market or stage_info.get("stage", 0) >= 2:
                    continue
                try:
                    self.executor.check_split_buy_opportunity(symbol, market)
                    time.sleep(self.rate_delay)
                except Exception as e:
                    logger.error("[%s] 분할 매수 체크 실패: %s", symbol, e)
        except Exception as e:
            logger.error("리스크 체크 실패 [%s]: %s", market, e)
