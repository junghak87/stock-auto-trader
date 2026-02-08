"""한국투자증권 자동매매 시스템 — 메인 진입점.

Usage:
    python main.py              # 모의투자 모드 (기본)
    python main.py --live       # 실전투자 모드
    python main.py --once       # 1회 전략 실행 후 종료
"""

import argparse
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
from trading.executor import TradingExecutor
from trading.risk_manager import RiskManager
from scheduler.jobs import TradingJobs

# ── 로깅 설정 ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
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
        max_daily_trades=settings.trading.max_daily_trades,
    )

    # 주문 실행기
    executor = TradingExecutor(
        kis_client=kis_client,
        database=database,
        notifier=notifier,
        risk_manager=risk_manager,
    )

    # 감시 종목 초기화 (.env → DB 시드)
    database.init_watchlist_from_config(
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
        )

    # 스케줄 작업
    jobs = TradingJobs(
        kis_client=kis_client,
        database=database,
        notifier=notifier,
        executor=executor,
        risk_manager=risk_manager,
        strategy=strategy,
        scanner=scanner,
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

    # 장중 전략 실행: 09:05 ~ 15:20 (매 5분)
    scheduler.add_job(
        jobs.job_kr_strategy_run,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5"),
    )

    # 장 마감 정산: 15:40
    scheduler.add_job(jobs.job_kr_market_close, CronTrigger(day_of_week="mon-fri", hour=15, minute=40))

    # ── 해외 장 스케줄 (화~토, 한국 시간 기준) ────────────
    # 미국장 시작 준비: 23:20 (월~금)
    scheduler.add_job(jobs.job_us_market_open, CronTrigger(day_of_week="mon-fri", hour=23, minute=20))

    # 미국장 전략 실행: 23:30~05:50 (매 10분)
    # 야간 파트 (당일): 23:30 ~ 23:59
    scheduler.add_job(
        jobs.job_us_strategy_run,
        CronTrigger(day_of_week="mon-fri", hour=23, minute="30,40,50"),
    )
    # 야간 파트 (익일): 00:00 ~ 05:50
    scheduler.add_job(
        jobs.job_us_strategy_run,
        CronTrigger(day_of_week="tue-sat", hour="0-5", minute="*/10"),
    )

    # 미국장 마감: 06:10 (화~토)
    scheduler.add_job(jobs.job_us_market_close, CronTrigger(day_of_week="tue-sat", hour=6, minute=10))

    return scheduler


def run_once(jobs: TradingJobs):
    """1회 전략 실행 후 종료 (테스트/디버깅용)."""
    logger.info("=== 1회 전략 실행 모드 ===")

    now = datetime.now()
    hour = now.hour

    # 현재 시간에 따라 국내/해외 전략 실행
    if 9 <= hour < 16:
        logger.info("국내 장 시간 — 국내 전략 실행")
        jobs.job_kr_strategy_run()
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

    # 스케줄러 시작
    scheduler = setup_scheduler(jobs)
    scheduler.start()
    notifier.notify_system(f"자동매매 시스템 시작 [{mode_str} 모드]")

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
