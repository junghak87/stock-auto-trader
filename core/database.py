"""SQLite 데이터베이스 관리 모듈.

매매 기록, 시세 데이터, 전략 시그널을 저장하고 조회한다.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("trading.db")


class Database:
    """SQLite 기반 매매 데이터 저장소."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        """필요한 테이블을 생성한다."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    market TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price REAL NOT NULL,
                    order_no TEXT DEFAULT '',
                    strategy TEXT DEFAULT '',
                    success INTEGER DEFAULT 1,
                    message TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS market_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    date TEXT
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    strength REAL DEFAULT 0,
                    detail TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS daily_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    total_trades INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    win_count INTEGER DEFAULT 0,
                    loss_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    added_at TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    reason TEXT DEFAULT '',
                    UNIQUE(symbol, market)
                );

                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_market_data_symbol ON market_data(symbol, date);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, timestamp);
                CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist(active, market);
            """)

    # ── 매매 기록 ─────────────────────────────────────────

    def save_trade(
        self,
        symbol: str,
        name: str,
        market: str,
        side: str,
        qty: int,
        price: float,
        order_no: str = "",
        strategy: str = "",
        success: bool = True,
        message: str = "",
    ):
        """매매 기록을 저장한다."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO trades (timestamp, symbol, name, market, side, qty, price, order_no, strategy, success, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), symbol, name, market, side, qty, price, order_no, strategy, int(success), message),
            )
        logger.info("매매 기록 저장: %s %s %s %d주 @ %s", side, symbol, market, qty, price)

    def get_trades_today(self) -> list[dict]:
        """오늘의 매매 기록을 조회한다."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp DESC",
                (f"{today}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_count_today(self) -> int:
        """오늘의 매매 횟수를 반환한다."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE timestamp LIKE ? AND success = 1",
                (f"{today}%",),
            ).fetchone()
        return row["cnt"] if row else 0

    # ── 시세 데이터 ───────────────────────────────────────

    def save_market_data(
        self,
        symbol: str,
        market: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        date: str = "",
    ):
        """시세 데이터를 저장한다."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO market_data (timestamp, symbol, market, open, high, low, close, volume, date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), symbol, market, open_, high, low, close, volume, date),
            )

    def get_market_data(self, symbol: str, days: int = 60) -> list[dict]:
        """최근 N일간의 시세 데이터를 조회한다."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM market_data
                   WHERE symbol = ?
                   ORDER BY date DESC
                   LIMIT ?""",
                (symbol, days),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 시그널 ────────────────────────────────────────────

    def save_signal(
        self,
        symbol: str,
        market: str,
        strategy: str,
        signal: str,
        strength: float = 0,
        detail: str = "",
    ):
        """전략 시그널을 저장한다."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO signals (timestamp, symbol, market, strategy, signal, strength, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), symbol, market, strategy, signal, strength, detail),
            )

    def get_signals_today(self, symbol: str | None = None) -> list[dict]:
        """오늘의 시그널을 조회한다."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE timestamp LIKE ? AND symbol = ? ORDER BY timestamp DESC",
                    (f"{today}%", symbol),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE timestamp LIKE ? ORDER BY timestamp DESC",
                    (f"{today}%",),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── 일일 요약 ─────────────────────────────────────────

    def save_daily_summary(self, total_trades: int, total_pnl: float, win_count: int, loss_count: int):
        """일일 매매 요약을 저장한다."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO daily_summary (date, total_trades, total_pnl, win_count, loss_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (today, total_trades, total_pnl, win_count, loss_count),
            )

    def get_recent_summaries(self, days: int = 30) -> list[dict]:
        """최근 N일간의 일일 요약을 조회한다."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 감시 종목 (Watchlist) ─────────────────────────────

    def add_watchlist(self, symbol: str, market: str, name: str = "", source: str = "manual", reason: str = "") -> bool:
        """감시 종목을 추가한다. 이미 존재하면 활성화한다."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO watchlist (symbol, market, name, source, added_at, active, reason)
                       VALUES (?, ?, ?, ?, ?, 1, ?)
                       ON CONFLICT(symbol, market) DO UPDATE SET active=1, source=?, reason=?, added_at=?""",
                    (symbol, market, name, source, datetime.now().isoformat(), reason,
                     source, reason, datetime.now().isoformat()),
                )
            logger.info("감시 종목 추가: %s [%s] (%s)", symbol, market, source)
            return True
        except Exception as e:
            logger.error("감시 종목 추가 실패: %s — %s", symbol, e)
            return False

    def remove_watchlist(self, symbol: str, market: str) -> bool:
        """감시 종목을 비활성화한다."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE watchlist SET active=0 WHERE symbol=? AND market=?",
                (symbol, market),
            )
        if cursor.rowcount > 0:
            logger.info("감시 종목 제거: %s [%s]", symbol, market)
            return True
        return False

    def get_watchlist(self, market: str | None = None) -> list[dict]:
        """활성 감시 종목을 조회한다."""
        with self._connect() as conn:
            if market:
                rows = conn.execute(
                    "SELECT * FROM watchlist WHERE active=1 AND market=? ORDER BY added_at DESC",
                    (market,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watchlist WHERE active=1 ORDER BY market, added_at DESC",
                ).fetchall()
        return [dict(r) for r in rows]

    def get_watchlist_symbols(self, market: str) -> list[str]:
        """활성 감시 종목의 심볼 리스트를 반환한다."""
        items = self.get_watchlist(market)
        return [item["symbol"] for item in items]

    def init_watchlist_from_config(self, kr_stocks: list[str], us_stocks: list[str]):
        """설정 파일의 종목을 watchlist에 시드로 추가한다 (최초 실행 시)."""
        existing = self.get_watchlist()
        if existing:
            return  # 이미 종목이 있으면 시드 추가 안 함
        for symbol in kr_stocks:
            self.add_watchlist(symbol, "KR", source="config")
        for symbol in us_stocks:
            self.add_watchlist(symbol, "US", source="config")
        logger.info("설정 파일에서 감시 종목 시드 완료: KR=%d, US=%d", len(kr_stocks), len(us_stocks))
