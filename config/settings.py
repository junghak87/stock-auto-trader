"""애플리케이션 설정 관리 모듈."""

from pydantic_settings import BaseSettings
from pydantic import Field


class KISSettings(BaseSettings):
    """한국투자증권 API 설정."""

    app_key: str = Field(alias="KIS_APP_KEY")
    app_secret: str = Field(alias="KIS_APP_SECRET")
    account_no: str = Field(alias="KIS_ACCOUNT_NO")

    paper_app_key: str = Field(default="", alias="KIS_PAPER_APP_KEY")
    paper_app_secret: str = Field(default="", alias="KIS_PAPER_APP_SECRET")
    paper_account_no: str = Field(default="", alias="KIS_PAPER_ACCOUNT_NO")

    model_config = {"env_file": ".env", "extra": "ignore"}


class TelegramSettings(BaseSettings):
    """텔레그램 봇 설정."""

    bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    chat_id: str = Field(alias="TELEGRAM_CHAT_ID")

    model_config = {"env_file": ".env", "extra": "ignore"}


class TradingSettings(BaseSettings):
    """매매 관련 설정."""

    mode: str = Field(default="paper", alias="TRADING_MODE")
    total_budget: float = Field(default=0, alias="TOTAL_BUDGET")
    max_position_ratio: float = Field(default=0.25, alias="MAX_POSITION_RATIO")
    stop_loss_pct: float = Field(default=5.0, alias="STOP_LOSS_PCT")
    take_profit_pct: float = Field(default=10.0, alias="TAKE_PROFIT_PCT")
    trailing_activation_pct: float = Field(default=3.0, alias="TRAILING_ACTIVATION_PCT")
    trailing_stop_pct: float = Field(default=2.0, alias="TRAILING_STOP_PCT")
    daily_max_loss_pct: float = Field(default=3.0, alias="DAILY_MAX_LOSS_PCT")
    consecutive_loss_limit: int = Field(default=3, alias="CONSECUTIVE_LOSS_LIMIT")
    consecutive_loss_cooldown: int = Field(default=60, alias="CONSECUTIVE_LOSS_COOLDOWN")
    limit_order_enabled: bool = Field(default=True, alias="LIMIT_ORDER_ENABLED")
    limit_buy_offset_pct: float = Field(default=0.3, alias="LIMIT_BUY_OFFSET_PCT")
    limit_tp_offset_pct: float = Field(default=0.3, alias="LIMIT_TP_OFFSET_PCT")
    limit_order_timeout_sec: int = Field(default=300, alias="LIMIT_ORDER_TIMEOUT_SEC")
    split_buy_enabled: bool = Field(default=True, alias="SPLIT_BUY_ENABLED")
    split_buy_first_ratio: float = Field(default=0.5, alias="SPLIT_BUY_FIRST_RATIO")
    split_buy_dip_pct: float = Field(default=2.0, alias="SPLIT_BUY_DIP_PCT")
    split_sell_enabled: bool = Field(default=True, alias="SPLIT_SELL_ENABLED")
    split_sell_first_ratio: float = Field(default=0.5, alias="SPLIT_SELL_FIRST_RATIO")
    max_daily_trades: int = Field(default=20, alias="MAX_DAILY_TRADES")
    usd_krw_rate: float = Field(default=1450, alias="USD_KRW_RATE")

    watch_stocks_kr: str = Field(default="005930", alias="WATCH_STOCKS_KR")
    watch_stocks_us: str = Field(default="AAPL", alias="WATCH_STOCKS_US")

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def kr_stock_list(self) -> list[str]:
        return [s.strip() for s in self.watch_stocks_kr.split(",") if s.strip()]

    @property
    def us_stock_list(self) -> list[str]:
        return [s.strip() for s in self.watch_stocks_us.split(",") if s.strip()]

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def budget_per_stock(self) -> float:
        """종목당 최대 투자 금액을 반환한다."""
        if self.total_budget > 0:
            return self.total_budget * self.max_position_ratio
        return 0


class AISettings(BaseSettings):
    """AI 전략 설정."""

    enabled: bool = Field(default=False, alias="AI_STRATEGY_ENABLED")
    provider: str = Field(default="gemini", alias="AI_PROVIDER")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    claude_api_key: str = Field(default="", alias="CLAUDE_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    model: str = Field(default="", alias="AI_MODEL")

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def active_api_key(self) -> str:
        """현재 provider에 해당하는 API 키를 반환한다."""
        keys = {
            "gemini": self.gemini_api_key,
            "claude": self.claude_api_key,
            "openai": self.openai_api_key,
        }
        return keys.get(self.provider, "")

    @property
    def is_configured(self) -> bool:
        """AI 전략이 사용 가능한 상태인지 확인한다."""
        return self.enabled and bool(self.active_api_key)


class Settings:
    """전체 설정을 하나로 묶는 클래스."""

    def __init__(self):
        self.kis = KISSettings()
        self.telegram = TelegramSettings()
        self.trading = TradingSettings()
        self.ai = AISettings()
