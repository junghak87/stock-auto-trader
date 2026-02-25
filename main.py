"""한국투자증권 자동매매 시스템 — 메인 진입점.

Usage:
    python main.py              # 모의투자 모드 (기본)
    python main.py --live       # 실전투자 모드
    python main.py --once       # 1회 전략 실행 후 종료
"""

import argparse
import io
import logging
import signal
import sys
import time
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# .env 파일 로드 (Settings 임포트 전에 실행)
load_dotenv()

from config import settings
from core.kis_client import KISClient
from core.database import Database
from core.telegram_bot import TelegramNotifier
from strategies.composite import CompositeStrategy
from strategies.stock_scanner import StockScanner
from strategies.tail_trading import TailTradingStrategy
from trading.executor import TradingExecutor
from trading.risk_manager import RiskManager
from scheduler.jobs import TradingJobs

# ── 로깅 설정 ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")),
        logging.FileHandler("trading.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def build_components(is_live: bool) -> dict:
    """시스템 컴포넌트를 생성하고 연결한다."""
    # KIS 클라이언트
    if is_live:
        kis_client = KISClient(
            app_key=settings.kis.app_key,
            app_secret=settings.kis.app_secret,
            account_no=settings.kis.account_no,
            is_live=True,
        )
    else:
        # 모의투자 키가 설정되어 있으면 모의투자 키 사용, 아니면 실전 키로 모의투자
        app_key = settings.kis.paper_app_key or settings.kis.app_key
        app_secret = settings.kis.paper_app_secret or settings.kis.app_secret
        account_no = settings.kis.paper_account_no or settings.kis.account_no
        kis_client = KISClient(
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            is_live=False,
        )

    # 핵심 모듈
    database = Database()
    notifier = TelegramNotifier(
        bot_token=settings.telegram.bot_token,
        chat_id=settings.telegram.chat_id,
    )
    notifier.set_dependencies(kis_client, database)

    # 전략 (AI 전략 조건부 활성화)
    ai_config = None
    if settings.ai.is_configured:
        ai_config = {
            "provider": settings.ai.provider,
            "api_key": settings.ai.active_api_key,
            "model": settings.ai.model,
        }
    strategy = CompositeStrategy(ai_config=ai_config)

    # 리스크 관리
    risk_manager = RiskManager(
        kis_client=kis_client,
        database=database,
        max_position_ratio=settings.trading.max_position_ratio,
        stop_loss_pct=settings.trading.stop_loss_pct,
        take_profit_pct=settings.trading.take_profit_pct,
        trailing_activation_pct=settings.trading.trailing_activation_pct,
        trailing_stop_pct=settings.trading.trailing_stop_pct,
        daily_max_loss_pct=settings.trading.daily_max_loss_pct,
        consecutive_loss_limit=settings.trading.consecutive_loss_limit,
        consecutive_loss_cooldown=settings.trading.consecutive_loss_cooldown,
        max_daily_trades=settings.trading.max_daily_trades,
        total_budget=settings.trading.total_budget,
        usd_krw_rate=settings.trading.usd_krw_rate,
    )

    # 주문 실행기
    executor = TradingExecutor(
        kis_client=kis_client,
        database=database,
        notifier=notifier,
        risk_manager=risk_manager,
    )
    # 지정가 주문 설정
    executor.limit_order_enabled = settings.trading.limit_order_enabled
    executor.limit_buy_offset_pct = settings.trading.limit_buy_offset_pct
    executor.limit_tp_offset_pct = settings.trading.limit_tp_offset_pct
    executor.limit_order_timeout_sec = settings.trading.limit_order_timeout_sec
    # 분할 매수/매도 설정
    executor.split_buy_enabled = settings.trading.split_buy_enabled
    executor.split_buy_first_ratio = settings.trading.split_buy_first_ratio
    executor.split_buy_dip_pct = settings.trading.split_buy_dip_pct
    executor.split_sell_enabled = settings.trading.split_sell_enabled
    executor.split_sell_first_ratio = settings.trading.split_sell_first_ratio

    # 감시 종목 동기화 (.env ↔ DB)
    database.sync_watchlist_from_config(
        settings.trading.kr_stock_list,
        settings.trading.us_stock_list,
    )

    # AI 종목 스캐너 (AI 설정이 있을 때만)
    scanner = None
    if settings.ai.is_configured:
        scanner = StockScanner(
            kis_client=kis_client,
            database=database,
            ai_provider=settings.ai.provider,
            ai_api_key=settings.ai.active_api_key,
            ai_model=settings.ai.model,
            budget_per_stock=settings.trading.budget_per_stock,
        )

    # 꼬리 매매 전략 (분봉 기반, 국내만)
    tail_strategy = TailTradingStrategy()

    # 스케줄 작업
    jobs = TradingJobs(
        kis_client=kis_client,
        database=database,
        notifier=notifier,
        executor=executor,
        risk_manager=risk_manager,
        strategy=strategy,
        scanner=scanner,
        tail_strategy=tail_strategy,
    )

    return {
        "kis_client": kis_client,
        "database": database,
        "notifier": notifier,
        "strategy": strategy,
        "risk_manager": risk_manager,
        "executor": executor,
        "jobs": jobs,
    }


def setup_scheduler(jobs: TradingJobs) -> BackgroundScheduler:
    """APScheduler 스케줄을 설정한다."""
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # ── 국내 장 스케줄 (월~금) ────────────────────────────
    # 장 시작 준비: 08:50
    scheduler.add_job(jobs.job_kr_market_open, CronTrigger(day_of_week="mon-fri", hour=8, minute=50))

    # 장중 전략 실행: 09:00 ~ 15:00 (매 15분)
    scheduler.add_job(
        jobs.job_kr_strategy_run,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="0,15,30,45"),
    )

    # 장중 손절/익절 체크: 09:05 ~ 15:25 (매 5분) — 빠른 리스크 대응
    scheduler.add_job(
        jobs.job_kr_risk_check,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="5,10,20,25,35,40,50,55"),
    )

    # 꼬리 매매 (분봉 기반): 09:03 ~ 15:24 (매 3분)
    scheduler.add_job(
        jobs.job_kr_tail_trading,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/3"),
    )

    # 장중 종목 로테이션: 09:30, 11:30, 13:30 (매 2시간)
    scheduler.add_job(
        jobs.job_kr_watchlist_rotate,
        CronTrigger(day_of_week="mon-fri", hour="9,11,13", minute=30),
    )

    # 장 마감 정산: 15:40
    scheduler.add_job(jobs.job_kr_market_close, CronTrigger(day_of_week="mon-fri", hour=15, minute=40))

    # ── 해외 장 스케줄 (화~토, 한국 시간 기준) ────────────
    # 미국장 시작 준비: 23:20 (월~금)
    scheduler.add_job(jobs.job_us_market_open, CronTrigger(day_of_week="mon-fri", hour=23, minute=20))

    # 미국장 전략 실행: 23:30~05:45 (매 15분)
    scheduler.add_job(
        jobs.job_us_strategy_run,
        CronTrigger(day_of_week="mon-fri", hour=23, minute="30,45"),
    )
    scheduler.add_job(
        jobs.job_us_strategy_run,
        CronTrigger(day_of_week="tue-sat", hour="0-5", minute="0,15,30,45"),
    )

    # 미국장 손절/익절 체크: 23:35~05:55 (매 5분) — 빠른 리스크 대응
    scheduler.add_job(
        jobs.job_us_risk_check,
        CronTrigger(day_of_week="mon-fri", hour=23, minute="35,40,50,55"),
    )
    scheduler.add_job(
        jobs.job_us_risk_check,
        CronTrigger(day_of_week="tue-sat", hour="0-5", minute="5,10,20,25,35,40,50,55"),
    )

    # 미국장 종목 로테이션: 01:30, 03:30 (매 2시간)
    scheduler.add_job(
        jobs.job_us_watchlist_rotate,
        CronTrigger(day_of_week="tue-sat", hour="1,3", minute=30),
    )

    # 미국장 마감: 06:10 (화~토)
    scheduler.add_job(jobs.job_us_market_close, CronTrigger(day_of_week="tue-sat", hour=6, minute=10))

    return scheduler


def run_once(jobs: TradingJobs):
    """1회 전략 실행 후 종료 (테스트/디버깅용)."""
    logger.info("=== 1회 전략 실행 모드 ===")

    # 스캔 먼저 실행 (watchlist가 비어 있을 수 있으므로)
    if jobs.scanner:
        logger.info("종목 스캔 실행 중...")
        jobs.job_kr_market_open()

    now = datetime.now()
    hour = now.hour

    # 현재 시간에 따라 국내/해외 전략 실행
    if 9 <= hour < 16:
        logger.info("국내 장 시간 — 국내 전략 + 꼬리 매매 실행")
        jobs.job_kr_strategy_run()
        jobs.job_kr_tail_trading()
    elif hour >= 23 or hour < 6:
        logger.info("해외 장 시간 — 해외 전략 실행")
        jobs.job_us_strategy_run()
    else:
        logger.info("장외 시간 — 국내/해외 전략 모두 실행 (테스트)")
        jobs.job_kr_strategy_run()
        jobs.job_us_strategy_run()


def main():
    parser = argparse.ArgumentParser(description="한국투자증권 자동매매 시스템")
    parser.add_argument("--live", action="store_true", help="실전투자 모드 (기본: 모의투자)")
    parser.add_argument("--once", action="store_true", help="1회 전략 실행 후 종료")
    args = parser.parse_args()

    is_live = args.live or settings.trading.is_live
    mode_str = "실전투자" if is_live else "모의투자"
    logger.info("=" * 60)
    logger.info("한국투자증권 자동매매 시스템 시작 [%s 모드]", mode_str)
    if settings.trading.total_budget > 0:
        logger.info("총 투자 한도: %,.0f원 | 종목당: %,.0f원 (%.0f%%)",
                     settings.trading.total_budget,
                     settings.trading.budget_per_stock,
                     settings.trading.max_position_ratio * 100)
    else:
        logger.info("투자 한도: 계좌 총평가액 기준 (종목당 %.0f%%)", settings.trading.max_position_ratio * 100)
    logger.info("국내 감시 종목: %s", settings.trading.kr_stock_list)
    logger.info("해외 감시 종목: %s", settings.trading.us_stock_list)
    if settings.ai.is_configured:
        logger.info("AI 전략: 활성 (provider: %s)", settings.ai.provider)
    else:
        logger.info("AI 전략: 비활성")
    logger.info("=" * 60)

    if is_live:
        logger.warning("⚠ 실전투자 모드입니다. 실제 주문이 체결됩니다!")

    # 컴포넌트 초기화
    components = build_components(is_live)
    jobs = components["jobs"]
    notifier = components["notifier"]

    # 1회 실행 모드
    if args.once:
        run_once(jobs)
        return

    # 텔레그램 봇 폴링 시작 (별도 스레드)
    import asyncio
    bot_loop = asyncio.new_event_loop()

    def _start_bot():
        asyncio.set_event_loop(bot_loop)
        bot_loop.run_until_complete(notifier.start_bot_polling())

    bot_thread = threading.Thread(target=_start_bot, daemon=True)
    bot_thread.start()

    # 스케줄러 시작
    scheduler = setup_scheduler(jobs)
    scheduler.start()
    notifier.notify_system(f"자동매매 시스템 시작 [{mode_str} 모드]")

    # 서비스 시작 시 초기 스캔 (장중 시작 시 watchlist가 비어있는 문제 방지)
    now = datetime.now()
    if jobs.scanner:
        if 9 <= now.hour < 16:
            logger.info("장중 서비스 시작 — 초기 종목 스캔 실행")
            try:
                jobs.job_kr_market_open()
            except Exception as e:
                logger.error("초기 국내 스캔 실패: %s", e)
        elif now.hour >= 23 or now.hour < 6:
            logger.info("해외장 서비스 시작 — 초기 종목 스캔 실행")
            try:
                jobs.job_us_market_open()
            except Exception as e:
                logger.error("초기 해외 스캔 실패: %s", e)

    logger.info("스케줄러 실행 중... Ctrl+C로 종료")

    # 그레이스풀 셧다운
    shutdown_event = threading.Event()

    def handle_shutdown(signum, frame):
        logger.info("종료 시그널 수신, 시스템 종료 중...")
        scheduler.shutdown(wait=False)
        notifier.notify_system("자동매매 시스템 종료")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        # 메인 루프
        while not shutdown_event.is_set():
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("시스템 종료")
        scheduler.shutdown(wait=False)
        notifier.notify_system("자동매매 시스템 종료")


if __name__ == "__main__":
    main()
