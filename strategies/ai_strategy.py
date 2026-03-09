"""생성형 AI 기반 매매 전략 모듈.

Google Gemini(무료), Claude, OpenAI API를 활용하여
기술적 지표와 시세 데이터를 종합 분석하고 매매 시그널을 생성한다.
"""

import json
import logging
import time

import pandas as pd
import ta

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)

# ── Provider별 기본 모델 ──────────────────────────────────
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash-lite",
    "claude": "claude-haiku-4-5-20250514",
    "openai": "gpt-4o-mini",
}

# ── 시스템 프롬프트 ───────────────────────────────────────
SYSTEM_PROMPT = """당신은 단기 매매(1~3일 보유) 전문가입니다. 기술적 지표를 분석하여 빠르고 명확한 매매 판단을 내려주세요.

핵심 원칙 (단기 매매):
- 추세 초기에 진입, 모멘텀 약화 시 즉시 이탈
- 손실은 작게, 수익은 빠르게 확정
- HOLD는 최소화 — 단기 매매에서 관망은 기회 손실

매수(BUY) 조건 (2개 이상 충족 시):
- MA5 > MA20 (상승 배열) + MA5 기울기 상승 중
- RSI 40~65 범위에서 상승 (과매수 전 진입)
- MACD 히스토그램 양전환 또는 상승 중
- 거래량 20일 평균 대비 1.5배 이상 증가
- 볼린저밴드 중심선 위 + 상단 미도달

매도(SELL) 조건 (1개라도 해당 시):
- MA5 < MA20 (하락 배열)로 전환
- RSI 70 이상 과매수 후 꺾임
- MACD 히스토그램 음전환
- 전일 대비 -2% 이상 하락 + 거래량 증가 (투매 신호)
- 볼린저밴드 상단 이탈 후 복귀 (반전 신호)

시장 환경 참고 (시장 지수 정보가 있을 때):
- 시장 전체 하락 시: 일반 종목은 매도 쪽으로 판단, 인버스 ETF는 매수 쪽으로 판단
- 시장 전체 상승 시: 모멘텀 종목은 매수 유지, 인버스 ETF는 매도 쪽으로 판단

반드시 아래 JSON 형식으로만 응답하세요:
{"signal": "BUY 또는 SELL 또는 HOLD", "strength": 0.0~1.0, "reason": "근거 요약"}

strength 기준: 0.3~0.5 약한 시그널, 0.5~0.7 보통, 0.7~1.0 강한 시그널. 판단했으면 최소 0.3 이상."""


class AIStrategy(BaseStrategy):
    """생성형 AI 기반 매매 전략.

    일봉 데이터 기반이므로 같은 종목+같은 날짜는 캐시된 결과를 반환한다.
    """

    name = "AI"

    def __init__(self, provider: str = "gemini", api_key: str = "", model: str = ""):
        self.provider = provider
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS.get(provider, "")
        # 캐시: {(symbol_or_date_key): StrategyResult}
        self._cache: dict[str, StrategyResult] = {}
        # 시장 컨텍스트 (KOSPI/KOSDAQ 지수 등)
        self._market_context: str = ""
        # 현재 분석 중인 종목 정보
        self._stock_name: str = ""
        self._stock_symbol: str = ""
        self._last_ai_call: float = 0  # rate limit 방어용 타임스탬프

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < 26:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail="데이터 부족 (최소 26일 필요)",
            )

        # 캐시 키: 종목코드 + 최신 일봉 날짜 + 1시간 블록 (장중 매시간 재판단)
        latest = df.iloc[-1]
        from datetime import datetime as _dt
        hour_block = _dt.now().hour  # 매시간 새 블록
        symbol_key = self._stock_symbol or f"{latest['open']}"
        cache_key = f"{symbol_key}_{latest['date']}_{hour_block}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.debug("AI 캐시 히트: %s → %s (%.2f)", cache_key, cached.signal.value, cached.strength)
            return cached

        try:
            prompt = self._build_prompt(df)
            response = self._call_ai(prompt)
            result = self._parse_response(response)
            self._cache[cache_key] = result
            # 캐시가 너무 커지지 않도록 오래된 항목 정리 (최대 100개)
            if len(self._cache) > 100:
                oldest_keys = list(self._cache.keys())[:-50]
                for k in oldest_keys:
                    del self._cache[k]
            return result
        except Exception as e:
            logger.error("AI 전략 분석 실패: %s", e)
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"AI 분석 오류: {e}",
            )

    def clear_cache(self):
        """캐시를 초기화한다 (매일 장 시작 시 호출)."""
        self._cache.clear()
        self._market_context = ""
        logger.info("AI 전략 캐시 초기화")

    def set_market_context(self, context: str):
        """시장 컨텍스트를 설정한다 (전략 실행 전 호출)."""
        self._market_context = context

    def set_stock_info(self, symbol: str, name: str = ""):
        """현재 분석 종목 정보를 설정한다 (전략 실행 전 호출)."""
        self._stock_symbol = symbol
        self._stock_name = name

    # ── 프롬프트 구성 ─────────────────────────────────────

    def _build_prompt(self, df: pd.DataFrame) -> str:
        """시세 데이터와 기술적 지표를 분석 프롬프트로 구성한다."""
        df = df.copy()

        # 기술적 지표 계산
        df["ma5"] = df["close"].rolling(window=5).mean()
        df["ma20"] = df["close"].rolling(window=20).mean()
        df["rsi"] = ta.momentum.RSIIndicator(close=df["close"], window=14).rsi()
        macd = ta.trend.MACD(close=df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # 볼린저밴드
        bb = ta.volatility.BollingerBands(close=df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()

        # ATR (변동성)
        atr = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14,
        )
        df["atr"] = atr.average_true_range()
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # 거래량 이동평균
        df["vol_ma20"] = df["volume"].rolling(window=20).mean()

        # 최근 10일 데이터 추출
        recent = df.tail(10).copy()

        # 종목 정보
        if self._stock_name or self._stock_symbol:
            stock_label = self._stock_name or self._stock_symbol
            if self._stock_name and self._stock_symbol:
                stock_label = f"{self._stock_name} ({self._stock_symbol})"
            lines = [f"[종목: {stock_label}]", ""]
        else:
            lines = []
        lines.append("[최근 10일 시세 및 기술적 지표]")
        lines.append("날짜 | 종가 | 거래량 | MA5 | MA20 | RSI | MACD_Hist | BB상단 | BB하단 | ATR%")
        lines.append("-" * 110)

        for _, row in recent.iterrows():
            lines.append(
                f"{row['date']} | "
                f"{row['close']:,.0f} | {row['volume']:,} | "
                f"{row['ma5']:,.0f} | {row['ma20']:,.0f} | "
                f"{row['rsi']:.1f} | {row['macd_hist']:.2f} | "
                f"{row['bb_upper']:,.0f} | {row['bb_lower']:,.0f} | "
                f"{row['atr_pct']:.2f}%"
            )

        # 추세 요약
        latest = recent.iloc[-1]
        prev = recent.iloc[-2]
        price_change = (latest["close"] - prev["close"]) / prev["close"] * 100
        vol_change = (latest["volume"] - prev["volume"]) / prev["volume"] * 100 if prev["volume"] > 0 else 0
        vol_ratio = latest["volume"] / latest["vol_ma20"] if latest["vol_ma20"] > 0 else 1.0

        # 볼린저밴드 내 위치 (0=하단, 1=상단)
        bb_range = latest["bb_upper"] - latest["bb_lower"]
        bb_position = (latest["close"] - latest["bb_lower"]) / bb_range if bb_range > 0 else 0.5

        lines.append(f"\n[현재 상태 요약]")
        lines.append(f"최신 종가: {latest['close']:,.0f} (전일 대비 {price_change:+.2f}%)")
        lines.append(f"거래량: 20일 평균 대비 {vol_ratio:.1f}배 (변화: {vol_change:+.1f}%)")
        lines.append(f"MA5 vs MA20: {'MA5 > MA20 (상승 배열)' if latest['ma5'] > latest['ma20'] else 'MA5 < MA20 (하락 배열)'}")
        lines.append(f"RSI: {latest['rsi']:.1f} ({'과매수' if latest['rsi'] > 70 else '과매도' if latest['rsi'] < 30 else '중립'})")
        lines.append(f"MACD Histogram: {latest['macd_hist']:.2f} ({'상승' if latest['macd_hist'] > 0 else '하락'} 모멘텀)")
        lines.append(f"볼린저밴드 위치: {bb_position:.2f} (0=하단, 0.5=중심, 1=상단)")
        lines.append(f"ATR 변동성: {latest['atr_pct']:.2f}% ({'고변동' if latest['atr_pct'] > 3 else '저변동' if latest['atr_pct'] < 1 else '보통'})")

        if self._market_context:
            lines.append(f"\n{self._market_context}")

        lines.append("\n이 데이터를 기반으로 매매 판단을 내려주세요.")

        return "\n".join(lines)

    # ── AI API 호출 ───────────────────────────────────────

    def _call_ai(self, prompt: str) -> str:
        """설정된 provider에 따라 AI API를 호출한다 (rate limit 방어 + 429 재시도)."""
        providers = {
            "gemini": self._call_gemini,
            "claude": self._call_claude,
            "openai": self._call_openai,
        }
        call_fn = providers.get(self.provider)
        if not call_fn:
            raise ValueError(f"지원하지 않는 AI provider: {self.provider}")

        # Gemini 무료 tier: 15 RPM → 최소 5초 간격
        if self.provider == "gemini":
            now = time.time()
            elapsed = now - self._last_ai_call
            if elapsed < 5:
                time.sleep(5 - elapsed)

        delays = [10, 30, 60]
        for attempt in range(3):
            try:
                result = call_fn(prompt)
                self._last_ai_call = time.time()
                return result
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    logger.warning("AI API 429 rate limit — %d초 후 재시도 (%d/3)", delays[attempt], attempt + 1)
                    time.sleep(delays[attempt])
                else:
                    raise

    def _call_gemini(self, prompt: str) -> str:
        """Google Gemini API를 호출한다."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=512,
            ),
        )
        return response.text

    def _call_claude(self, prompt: str) -> str:
        """Anthropic Claude API를 호출한다."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        message = client.messages.create(
            model=self.model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    def _call_openai(self, prompt: str) -> str:
        """OpenAI API를 호출한다."""
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=512,
        )
        return response.choices[0].message.content

    # ── 응답 파싱 ─────────────────────────────────────────

    def _parse_response(self, text: str) -> StrategyResult:
        """AI 응답을 StrategyResult로 파싱한다."""
        try:
            # JSON 블록 추출 (```json ... ``` 또는 순수 JSON)
            cleaned = text.strip()
            if "```" in cleaned:
                start = cleaned.find("{")
                end = cleaned.rfind("}") + 1
                cleaned = cleaned[start:end]

            data = json.loads(cleaned)

            signal_str = data.get("signal", "HOLD").upper()
            if signal_str == "BUY":
                signal = Signal.BUY
            elif signal_str == "SELL":
                signal = Signal.SELL
            else:
                signal = Signal.HOLD

            strength = float(data.get("strength", 0))
            strength = max(0.0, min(1.0, strength))

            reason = data.get("reason", "AI 판단")

            logger.info("AI 분석 결과: %s (강도: %.2f) — %s", signal.value, strength, reason)

            return StrategyResult(
                signal=signal,
                strength=strength,
                strategy_name=self.name,
                detail=f"AI: {reason}",
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("AI 응답 파싱 실패: %s | 원본: %s", e, text[:200])
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"AI 응답 파싱 실패: {e}",
            )
