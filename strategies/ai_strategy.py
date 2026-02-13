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
SYSTEM_PROMPT = """당신은 주식 매매 전문가입니다. 기술적 지표를 분석하여 명확한 매매 판단을 내려주세요.

중요 규칙:
- BUY, SELL, HOLD 중 하나를 반드시 선택하세요
- HOLD는 정말로 방향성이 불분명할 때만 사용하세요
- 상승 추세 + 긍정적 지표 → BUY를 적극적으로 선택하세요
- 하락 추세 + 부정적 지표 → SELL을 적극적으로 선택하세요
- "관망" "지켜보자"는 판단 회피입니다. 방향이 보이면 결정하세요

판단 기준:
- MA5 > MA20 + RSI 상승 + 거래량 증가 → BUY
- MA5 < MA20 + RSI 하락 + 거래량 감소 → SELL
- 볼린저밴드 하단 접근 + 반등 조짐 → BUY
- 볼린저밴드 상단 + 과매수 → SELL
- ATR 급등은 추세 전환 가능성 (방향 판단 필요)

반드시 아래 JSON 형식으로만 응답하세요:
{"signal": "BUY 또는 SELL 또는 HOLD", "strength": 0.0~1.0, "reason": "근거 요약"}

strength: 0.3 미만은 사용하지 마세요. 판단했으면 최소 0.3 이상으로 확신을 표현하세요."""


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

        lines = ["[최근 10일 시세 및 기술적 지표]"]
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
