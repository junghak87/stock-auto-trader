"""생성형 AI 기반 매매 전략 모듈.

Google Gemini(무료), Claude, OpenAI API를 활용하여
기술적 지표와 시세 데이터를 종합 분석하고 매매 시그널을 생성한다.
"""

import json
import logging

import pandas as pd
import ta

from .base import BaseStrategy, Signal, StrategyResult

logger = logging.getLogger(__name__)

# ── Provider별 기본 모델 ──────────────────────────────────
DEFAULT_MODELS = {
    "gemini": "gemini-2.0-flash",
    "claude": "claude-haiku-4-5-20250514",
    "openai": "gpt-4o-mini",
}

# ── 시스템 프롬프트 ───────────────────────────────────────
SYSTEM_PROMPT = """당신은 전문 주식 기술적 분석가입니다.
주어진 시세 데이터와 기술적 지표를 분석하여 매매 판단을 내려주세요.

분석 시 고려사항:
- 이동평균(MA5, MA20) 배열과 교차 여부
- RSI 과매수/과매도 구간
- MACD 히스토그램 방향과 교차
- 거래량 변화 추세
- 최근 가격 패턴 (지지/저항, 추세)

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
{"signal": "BUY 또는 SELL 또는 HOLD", "strength": 0.0에서 1.0 사이 숫자, "reason": "판단 근거 요약"}

strength 기준:
- 0.0~0.3: 약한 시그널 (확신 낮음)
- 0.3~0.6: 중간 시그널
- 0.6~1.0: 강한 시그널 (확신 높음)"""


class AIStrategy(BaseStrategy):
    """생성형 AI 기반 매매 전략."""

    name = "AI"

    def __init__(self, provider: str = "gemini", api_key: str = "", model: str = ""):
        self.provider = provider
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS.get(provider, "")

    def analyze(self, df: pd.DataFrame) -> StrategyResult:
        if len(df) < 26:
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail="데이터 부족 (최소 26일 필요)",
            )

        try:
            prompt = self._build_prompt(df)
            response = self._call_ai(prompt)
            return self._parse_response(response)
        except Exception as e:
            logger.error("AI 전략 분석 실패: %s", e)
            return StrategyResult(
                signal=Signal.HOLD,
                strength=0,
                strategy_name=self.name,
                detail=f"AI 분석 오류: {e}",
            )

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

        # 최근 10일 데이터 추출
        recent = df.tail(10).copy()

        lines = ["[최근 10일 시세 및 기술적 지표]"]
        lines.append("날짜 | 시가 | 고가 | 저가 | 종가 | 거래량 | MA5 | MA20 | RSI | MACD | Signal | Hist")
        lines.append("-" * 100)

        for _, row in recent.iterrows():
            lines.append(
                f"{row['date']} | "
                f"{row['open']:,.0f} | {row['high']:,.0f} | {row['low']:,.0f} | {row['close']:,.0f} | "
                f"{row['volume']:,} | "
                f"{row['ma5']:,.0f} | {row['ma20']:,.0f} | "
                f"{row['rsi']:.1f} | "
                f"{row['macd']:.2f} | {row['macd_signal']:.2f} | {row['macd_hist']:.2f}"
            )

        # 추세 요약
        latest = recent.iloc[-1]
        prev = recent.iloc[-2]
        price_change = (latest["close"] - prev["close"]) / prev["close"] * 100
        vol_change = (latest["volume"] - prev["volume"]) / prev["volume"] * 100 if prev["volume"] > 0 else 0

        lines.append(f"\n[현재 상태 요약]")
        lines.append(f"최신 종가: {latest['close']:,.0f} (전일 대비 {price_change:+.2f}%)")
        lines.append(f"거래량 변화: {vol_change:+.1f}%")
        lines.append(f"MA5 vs MA20: {'MA5 > MA20 (상승 배열)' if latest['ma5'] > latest['ma20'] else 'MA5 < MA20 (하락 배열)'}")
        lines.append(f"RSI: {latest['rsi']:.1f} ({'과매수' if latest['rsi'] > 70 else '과매도' if latest['rsi'] < 30 else '중립'})")
        lines.append(f"MACD Histogram: {latest['macd_hist']:.2f} ({'상승' if latest['macd_hist'] > 0 else '하락'} 모멘텀)")

        lines.append("\n이 데이터를 기반으로 매매 판단을 내려주세요.")

        return "\n".join(lines)

    # ── AI API 호출 ───────────────────────────────────────

    def _call_ai(self, prompt: str) -> str:
        """설정된 provider에 따라 AI API를 호출한다."""
        if self.provider == "gemini":
            return self._call_gemini(prompt)
        elif self.provider == "claude":
            return self._call_claude(prompt)
        elif self.provider == "openai":
            return self._call_openai(prompt)
        else:
            raise ValueError(f"지원하지 않는 AI provider: {self.provider}")

    def _call_gemini(self, prompt: str) -> str:
        """Google Gemini API를 호출한다."""
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(
            model_name=self.model,
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(prompt)
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
