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

                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    trade_count INTEGER DEFAULT 0,
                    win_count INTEGER DEFAULT 0,
                    loss_count INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    avg_profit REAL DEFAULT 0,
                    avg_loss REAL DEFAULT 0,
                    UNIQUE(date, strategy)
                );

                CREATE INDEX IF NOT EXISTS idx_strategy_perf ON strategy_performance(date, strategy);
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

    # ── 전략 성과 ───────────────────────────────────────

    def calculate_strategy_performance(self, days: int = 30) -> list[dict]:
        """trades 테이블에서 buy-sell FIFO 매칭으로 전략별 PnL을 계산한다."""
        from collections import defaultdict
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE success=1 AND timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()

        trades = [dict(r) for r in rows]
        buys_by_symbol: dict[str, list[dict]] = defaultdict(list)
        strategy_stats: dict[str, dict] = defaultdict(
            lambda: {"wins": 0, "losses": 0, "profits": [], "losses_list": [], "total_pnl": 0.0}
        )

        for t in trades:
            if t["side"] == "buy":
                buys_by_symbol[t["symbol"]].append(t)
            elif t["side"] == "sell" and buys_by_symbol[t["symbol"]]:
                buy = buys_by_symbol[t["symbol"]].pop(0)
                if buy["price"] <= 0 or t["price"] <= 0:
                    continue
                matched_qty = min(buy["qty"], t["qty"])
                pnl = (t["price"] - buy["price"]) * matched_qty
                strategy = buy["strategy"] or "unknown"
                stats = strategy_stats[strategy]
                stats["total_pnl"] += pnl
                if pnl >= 0:
                    stats["wins"] += 1
                    stats["profits"].append(pnl)
                else:
                    stats["losses"] += 1
                    stats["losses_list"].append(pnl)

        results = []
        for strategy, stats in strategy_stats.items():
            total = stats["wins"] + stats["losses"]
            results.append({
                "strategy": strategy,
                "trade_count": total,
                "win_count": stats["wins"],
                "loss_count": stats["losses"],
                "win_rate": stats["wins"] / total * 100 if total > 0 else 0,
                "total_pnl": stats["total_pnl"],
                "avg_profit": sum(stats["profits"]) / len(stats["profits"]) if stats["profits"] else 0,
                "avg_loss": sum(stats["losses_list"]) / len(stats["losses_list"]) if stats["losses_list"] else 0,
            })
        return sorted(results, key=lambda x: x["total_pnl"], reverse=True)

    def save_strategy_performance(self, date: str, strategy: str, stats: dict):
        """일별 전략 성과를 저장한다."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO strategy_performance
                   (date, strategy, trade_count, win_count, loss_count, total_pnl, avg_profit, avg_loss)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, strategy, stats.get("trade_count", 0), stats.get("win_count", 0),
                 stats.get("loss_count", 0), stats.get("total_pnl", 0),
                 stats.get("avg_profit", 0), stats.get("avg_loss", 0)),
            )

    def get_strategy_summary(self, days: int = 30) -> list[dict]:
        """최근 N일간 전략별 집계 성과를 조회한다."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT strategy,
                          SUM(trade_count) as trade_count,
                          SUM(win_count) as win_count,
                          SUM(loss_count) as loss_count,
                          SUM(total_pnl) as total_pnl
                   FROM strategy_performance
                   WHERE date >= ?
                   GROUP BY strategy
                   ORDER BY total_pnl DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 데이터 정리 ──────────────────────────────────────

    def cleanup_old_data(self, retention_days: int = 90):
        """오래된 데이터를 삭제한다."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            r1 = conn.execute("DELETE FROM market_data WHERE timestamp < ?", (cutoff,))
            r2 = conn.execute("DELETE FROM signals WHERE timestamp < ?", (cutoff,))
            r3 = conn.execute("DELETE FROM trades WHERE timestamp < ? AND success = 0", (cutoff,))
        total = r1.rowcount + r2.rowcount + r3.rowcount
        if total > 0:
            logger.info("DB 정리: %d건 삭제 (%d일 이전 데이터)", total, retention_days)

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

    def clear_ai_watchlist(self, market: str | None = None):
        """AI 스캔으로 추가된 감시 종목을 비활성화한다 (매일 갱신용)."""
        with self._connect() as conn:
            if market:
                cursor = conn.execute(
                    "UPDATE watchlist SET active=0 WHERE source='ai_scan' AND market=?",
                    (market,),
                )
            else:
                cursor = conn.execute(
                    "UPDATE watchlist SET active=0 WHERE source='ai_scan'",
                )
        count = cursor.rowcount
        if count > 0:
            logger.info("AI 스캔 종목 초기화: %d개 비활성화 (market=%s)", count, market or "ALL")

    def get_ai_watchlist(self, market: str) -> list[str]:
        """AI가 추가한 활성 종목만 조회한다."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM watchlist WHERE market=? AND source='ai_scan' AND active=1",
                (market,),
            ).fetchall()
        return [r["symbol"] for r in rows]

    def sync_watchlist_from_config(self, kr_stocks: list[str], us_stocks: list[str]):
        """설정 파일의 종목을 watchlist와 동기화한다.

        - .env에 있는 종목: 활성화 (없으면 추가)
        - .env에서 제거된 config 종목: 비활성화
        """
        config_kr = set(kr_stocks)
        config_us = set(us_stocks)

        # 현재 config 소스 종목 조회
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, market FROM watchlist WHERE source='config'",
            ).fetchall()
        existing_config = {(r["symbol"], r["market"]) for r in rows}

        # .env에 있는 종목 활성화/추가
        added = 0
        for symbol in kr_stocks:
            if (symbol, "KR") not in existing_config:
                self.add_watchlist(symbol, "KR", source="config")
                added += 1
            else:
                # 이미 있으면 활성화만
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE watchlist SET active=1 WHERE symbol=? AND market=? AND source='config'",
                        (symbol, "KR"),
                    )
        for symbol in us_stocks:
            if (symbol, "US") not in existing_config:
                self.add_watchlist(symbol, "US", source="config")
                added += 1
            else:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE watchlist SET active=1 WHERE symbol=? AND market=? AND source='config'",
                        (symbol, "US"),
                    )

        # .env에서 제거된 config 종목 비활성화
        removed = 0
        for symbol, market in existing_config:
            if market == "KR" and symbol not in config_kr:
                self.remove_watchlist(symbol, market)
                removed += 1
            elif market == "US" and symbol not in config_us:
                self.remove_watchlist(symbol, market)
                removed += 1

        if added or removed:
            logger.info("config 종목 동기화: %d개 추가, %d개 제거", added, removed)
