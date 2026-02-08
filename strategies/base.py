"""매매 전략 베이스 클래스.

모든 전략은 이 클래스를 상속받아 analyze 메서드를 구현한다.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(Enum):
    """매매 시그널."""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class StrategyResult:
    """전략 분석 결과."""
    signal: Signal
    strength: float  # 0.0 ~ 1.0 (시그널 강도)
    strategy_name: str
    detail: str = ""


class BaseStrategy(ABC):
    """매매 전략 추상 베이스 클래스."""

    name: str = "base"

    @abstractmethod
    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        """일봉 데이터(DataFrame)를 분석하여 매매 시그널을 반환한다.

        Args:
            df: OHLCV DataFrame (columns: date, open, high, low, close, volume)
                최신 데이터가 마지막 행이어야 한다.

        Returns:
            StrategyResult: 시그널, 강도, 상세 정보
        """

    @staticmethod
    def ohlcv_to_dataframe(ohlcv_list: list) -> pd.DataFrame:
        """OHLCVData 리스트를 DataFrame으로 변환한다."""
        records = [
            {
                "date": item.date,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in ohlcv_list
        ]
        df = pd.DataFrame(records)
        df = df.sort_values("date").reset_index(drop=True)
        return df
