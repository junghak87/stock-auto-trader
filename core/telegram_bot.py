"""텔레그램 봇 알림 모듈.

매매 시그널, 체결 결과, 일일 요약 등을 텔레그램으로 전송한다.
간단한 명령어(/status, /balance, /positions)도 지원한다.
"""

import asyncio
import logging
import threading
from datetime import datetime

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 알림 전송기."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.allowed_chat_ids: set[int] = {int(chat_id)}
        self.bot = Bot(token=bot_token)
        self._app: Application | None = None
        self._kis_client = None
        self._database = None
        # 전용 event loop (동기 전송용)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    def set_dependencies(self, kis_client, database):
        """런타임 의존성 주입 (순환 참조 방지)."""
        self._kis_client = kis_client
        self._database = database

    def _is_authorized(self, update: Update) -> bool:
        """화이트리스트에 등록된 사용자인지 확인한다."""
        chat_id = update.effective_chat.id
        if chat_id not in self.allowed_chat_ids:
            logger.warning("허가되지 않은 접근 차단: chat_id=%s", chat_id)
            return False
        return True

    # ── 메시지 전송 ───────────────────────────────────────

    async def _send(self, text: str, parse_mode: str = "HTML"):
        """텔레그램 메시지를 전송한다."""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error("텔레그램 전송 실패: %s", e)

    def send(self, text: str):
        """동기 방식 메시지 전송 (스케줄러에서 호출용)."""
        future = asyncio.run_coroutine_threadsafe(self._send(text), self._loop)
        try:
            future.result(timeout=10)
        except Exception as e:
            logger.error("텔레그램 전송 타임아웃/에러: %s", e)

    # ── 알림 유형별 전송 ──────────────────────────────────

    def notify_signal(self, symbol: str, market: str, strategy: str, signal: str, detail: str = ""):
        """전략 시그널 발생 알림."""
        emoji = "🔴" if signal == "sell" else "🟢" if signal == "buy" else "⚪"
        side_kr = "매수" if signal == "buy" else "매도" if signal == "sell" else "관망"
        msg = (
            f"{emoji} <b>시그널 발생</b>\n"
            f"종목: {symbol} ({market})\n"
            f"전략: {strategy}\n"
            f"방향: {side_kr}\n"
        )
        if detail:
            msg += f"상세: {detail}\n"
        msg += f"시각: {datetime.now().strftime('%H:%M:%S')}"
        self.send(msg)

    def notify_order(self, symbol: str, side: str, qty: int, price: float, success: bool, message: str = ""):
        """주문 체결 알림."""
        emoji = "✅" if success else "❌"
        side_kr = "매수" if side == "buy" else "매도"
        msg = (
            f"{emoji} <b>주문 {'체결' if success else '실패'}</b>\n"
            f"종목: {symbol}\n"
            f"구분: {side_kr}\n"
            f"수량: {qty}주\n"
            f"가격: {price:,.0f}\n"
        )
        if message:
            msg += f"메시지: {message}\n"
        msg += f"시각: {datetime.now().strftime('%H:%M:%S')}"
        self.send(msg)

    def notify_daily_summary(
        self, total_trades: int, total_pnl: float, positions: list,
        cash_info: dict | None = None,
    ):
        """일일 수익률 요약 알림 (계좌 잔고 포함)."""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        msg = (
            f"{pnl_emoji} <b>일일 매매 요약</b>\n"
            f"날짜: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"총 거래: {total_trades}건\n"
            f"일일 손익: {total_pnl:+,.0f}원\n"
        )
        # 계좌 잔고
        if cash_info:
            msg += (
                f"\n💰 <b>계좌 잔고</b>\n"
                f"  총 평가: {cash_info.get('total_eval', 0):,.0f}원\n"
                f"  현금: {cash_info.get('cash', 0):,.0f}원\n"
                f"  주식 평가: {cash_info.get('stock_eval', 0):,.0f}원\n"
            )
        # 보유 종목
        msg += f"\n📋 <b>보유 종목: {len(positions)}개</b>\n"
        if positions:
            total_stock_pnl = sum(p.pnl for p in positions)
            for p in positions[:10]:
                pnl_sign = "+" if p.pnl_pct >= 0 else ""
                msg += f"  {p.symbol} {p.name}: {p.qty}주 ({pnl_sign}{p.pnl_pct:.1f}%) {p.pnl:+,.0f}원\n"
            if len(positions) > 10:
                msg += f"  ... 외 {len(positions) - 10}개\n"
            msg += f"  <b>보유 합계: {total_stock_pnl:+,.0f}원</b>\n"
        else:
            msg += "  없음\n"
        self.send(msg)

    def notify_error(self, error_msg: str):
        """오류 발생 알림."""
        msg = (
            f"🚨 <b>오류 발생</b>\n"
            f"내용: {error_msg}\n"
            f"시각: {datetime.now().strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def notify_system(self, message: str):
        """시스템 메시지 알림."""
        msg = f"ℹ️ <b>시스템</b>\n{message}"
        self.send(msg)

    # ── 텔레그램 봇 명령어 핸들러 ─────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """현재 시스템 상태를 조회한다."""
        if not self._is_authorized(update):
            return
        if not self._database:
            await update.message.reply_text("시스템이 초기화되지 않았습니다.")
            return
        trades_today = self._database.get_trade_count_today()
        msg = (
            f"📊 <b>시스템 상태</b>\n"
            f"상태: 실행 중\n"
            f"오늘 매매 횟수: {trades_today}건\n"
            f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await update.message.reply_html(msg)

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """계좌 잔고를 조회한다."""
        if not self._is_authorized(update):
            return
        if not self._kis_client:
            await update.message.reply_text("KIS 클라이언트가 초기화되지 않았습니다.")
            return
        try:
            cash = self._kis_client.get_cash_balance()
            msg = (
                f"💰 <b>계좌 잔고</b>\n"
                f"총 평가: {cash['total_eval']:,.0f}원\n"
                f"현금: {cash['cash']:,.0f}원\n"
                f"주식 평가: {cash['stock_eval']:,.0f}원\n"
                f"총 손익: {cash['total_pnl']:+,.0f}원"
            )
            await update.message.reply_html(msg)
        except Exception as e:
            await update.message.reply_text(f"잔고 조회 실패: {e}")

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """보유 종목을 조회한다."""
        if not self._is_authorized(update):
            return
        if not self._kis_client:
            await update.message.reply_text("KIS 클라이언트가 초기화되지 않았습니다.")
            return
        try:
            positions = self._kis_client.get_all_positions()
            if not positions:
                await update.message.reply_text("보유 종목이 없습니다.")
                return
            msg = "📋 <b>보유 종목</b>\n\n"
            for p in positions:
                pnl_sign = "+" if p.pnl >= 0 else ""
                msg += (
                    f"<b>{p.symbol}</b> {p.name} [{p.market}]\n"
                    f"  수량: {p.qty}주 | 평단: {p.avg_price:,.0f}\n"
                    f"  현재가: {p.current_price:,.0f} | 수익: {pnl_sign}{p.pnl_pct:.1f}%\n\n"
                )
            await update.message.reply_html(msg)
        except Exception as e:
            await update.message.reply_text(f"보유 종목 조회 실패: {e}")

    async def _cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """감시 종목을 추가한다. 사용법: /add 005930 또는 /add AAPL US"""
        if not self._is_authorized(update):
            return
        if not self._database:
            await update.message.reply_text("시스템이 초기화되지 않았습니다.")
            return

        args = context.args
        if not args:
            await update.message.reply_text("사용법: /add 종목코드 [시장]\n예: /add 005930\n예: /add AAPL US")
            return

        symbol = args[0].upper()
        market = args[1].upper() if len(args) > 1 else ("US" if symbol.isalpha() else "KR")

        if self._database.add_watchlist(symbol, market, source="telegram"):
            await update.message.reply_html(f"✅ <b>{symbol}</b> [{market}] 감시 종목에 추가되었습니다.")
        else:
            await update.message.reply_text(f"감시 종목 추가 실패: {symbol}")

    async def _cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """감시 종목을 제거한다. 사용법: /remove 005930 또는 /remove AAPL US"""
        if not self._is_authorized(update):
            return
        if not self._database:
            await update.message.reply_text("시스템이 초기화되지 않았습니다.")
            return

        args = context.args
        if not args:
            await update.message.reply_text("사용법: /remove 종목코드 [시장]\n예: /remove 005930\n예: /remove AAPL US")
            return

        symbol = args[0].upper()
        market = args[1].upper() if len(args) > 1 else ("US" if symbol.isalpha() else "KR")

        if self._database.remove_watchlist(symbol, market):
            await update.message.reply_html(f"🗑 <b>{symbol}</b> [{market}] 감시 종목에서 제거되었습니다.")
        else:
            await update.message.reply_text(f"감시 종목에 없는 종목입니다: {symbol}")

    async def _cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """현재 감시 종목 목록을 조회한다."""
        if not self._is_authorized(update):
            return
        if not self._database:
            await update.message.reply_text("시스템이 초기화되지 않았습니다.")
            return

        items = self._database.get_watchlist()
        if not items:
            await update.message.reply_text("감시 종목이 없습니다.\n/add 종목코드 로 추가하세요.")
            return

        msg = "📋 <b>감시 종목 목록</b>\n\n"
        kr_items = [i for i in items if i["market"] == "KR"]
        us_items = [i for i in items if i["market"] == "US"]

        if kr_items:
            msg += "<b>[국내]</b>\n"
            for item in kr_items:
                source_tag = f" ({item['source']})" if item["source"] != "manual" else ""
                msg += f"  {item['symbol']} {item['name']}{source_tag}\n"

        if us_items:
            msg += "\n<b>[해외]</b>\n"
            for item in us_items:
                source_tag = f" ({item['source']})" if item["source"] != "manual" else ""
                msg += f"  {item['symbol']} {item['name']}{source_tag}\n"

        msg += f"\n총 {len(items)}개 종목 감시 중"
        await update.message.reply_html(msg)

    async def _cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """전략별 성과를 조회한다."""
        if not self._is_authorized(update):
            return
        if not self._database:
            await update.message.reply_text("시스템이 초기화되지 않았습니다.")
            return

        stats = self._database.calculate_strategy_performance()
        if not stats:
            await update.message.reply_text("아직 매매 기록이 없습니다.")
            return

        msg = "📊 <b>전략별 성과 (30일)</b>\n\n"
        for s in stats:
            pnl_sign = "+" if s["total_pnl"] >= 0 else ""
            pnl_emoji = "🟢" if s["total_pnl"] >= 0 else "🔴"
            win_rate = f"{s['win_rate']:.0f}%" if s["trade_count"] > 0 else "-"
            msg += (
                f"{pnl_emoji} <b>{s['strategy']}</b>\n"
                f"  거래: {s['trade_count']}건 | 승률: {win_rate}\n"
                f"  손익: {pnl_sign}{s['total_pnl']:,.0f}\n"
                f"  평균수익: +{s['avg_profit']:,.0f} | 평균손실: {s['avg_loss']:,.0f}\n\n"
            )
        await update.message.reply_html(msg)

    def setup_bot_commands(self, app: Application):
        """텔레그램 봇 명령어를 등록한다."""
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("balance", self._cmd_balance))
        app.add_handler(CommandHandler("positions", self._cmd_positions))
        app.add_handler(CommandHandler("add", self._cmd_add))
        app.add_handler(CommandHandler("remove", self._cmd_remove))
        app.add_handler(CommandHandler("watchlist", self._cmd_watchlist))
        app.add_handler(CommandHandler("performance", self._cmd_performance))
        self._app = app

    async def start_bot_polling(self):
        """텔레그램 봇 폴링을 시작한다 (별도 태스크로 실행)."""
        app = Application.builder().token(self.bot_token).build()
        self.setup_bot_commands(app)
        self._app = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("텔레그램 봇 폴링 시작")

    async def stop_bot(self):
        """텔레그램 봇을 정지한다."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("텔레그램 봇 종료")
