"""AI 기반 종목 스캐닝 모듈.

KIS API에서 거래량 상위 종목을 조회하고,
AI를 활용하여 유망 종목을 선별하여 감시 목록에 추가한다.
"""

import json
import logging
import time
import traceback

from core.broker import BrokerClient
from core.database import Database

logger = logging.getLogger(__name__)

SCAN_SYSTEM_PROMPT = """당신은 단기 매매(1~3일 보유) 전문가입니다.
거래량 상위 종목 데이터를 분석하여 단기 수익이 기대되는 종목을 엄선해주세요.

핵심 선별 기준 (리스크 관리 최우선):
1. 거래량 급증 + 상승 초기 단계: 거래량이 터지면서 등락률 +1~4% 수준인 종목 선호
2. 이미 급등한 종목 회피: 등락률 +7% 이상인 종목은 고점 매수 위험 → 반드시 제외
3. 급락 종목 주의: 등락률 -5% 이하 종목은 하락 추세 가능성 → 제외
4. 거래대금 충분: 거래대금이 너무 적으면 유동성 부족 → 제외
5. 눌림목 매수 기회: 전일 강세 후 당일 소폭 조정(-1~-3%) 중 거래량 유지 종목 선호

절대 제외 대상:
- 등락률 +7% 이상 (고점 추격 매수 위험)
- 등락률 -5% 이하 (하락 추세)
- 관리종목, 투기성 테마주 의심 종목

반드시 아래 JSON 형식으로만 응답하세요:
{"picks": [{"symbol": "종목코드", "reason": "선별 사유"}], "market_sentiment": "bullish 또는 bearish 또는 neutral", "summary": "시장 분위기 한줄 요약"}

최대 5개 종목만 선별하세요. 확신이 없으면 적게 선별하세요. 유망 종목이 없으면 빈 리스트를 반환하세요."""

ROTATE_SYSTEM_PROMPT = """당신은 단기 매매(1~3일 보유) 종목 로테이션 전문가입니다.
현재 감시/보유 중인 종목을 재평가하고, 거래량 상위에서 더 좋은 기회를 찾아주세요.

손실 종목 교체 원칙 (가장 중요):
- 보유 중이고 수익률 -3% 이하인 종목 → drops에 반드시 포함 (손실 확대 방지)
- 보유 중이고 모멘텀을 잃은 종목 (거래량 감소 + 횡보) → drops에 포함
- 보유 중이고 수익 중인 종목은 유지 (drops에 넣지 마세요)

신규 종목 선별 기준:
- 거래량 급증 + 등락률 +1~4% (상승 초기)
- 등락률 +7% 이상 급등 종목은 제외 (고점 매수 위험)
- 등락률 -5% 이하 급락 종목은 제외 (하락 추세)

반드시 아래 JSON 형식으로만 응답하세요:
{"picks": [{"symbol": "종목코드", "reason": "선별 사유"}], "drops": ["제거할종목코드1"], "market_sentiment": "bullish 또는 bearish 또는 neutral", "summary": "시장 분위기 한줄 요약"}

picks는 최대 5개. drops는 손실 종목이나 모멘텀 상실 종목만 포함. 보유 종목 현황이 있으면 반드시 참고하세요."""


class StockScanner:
    """AI 기반 종목 스캐너."""

    def __init__(
        self,
        kis_client: BrokerClient,
        database: Database,
        ai_provider: str = "gemini",
        ai_api_key: str = "",
        ai_model: str = "",
        budget_per_stock: float = 0,
        quote_client: BrokerClient | None = None,
    ):
        self.kis = kis_client
        self.quote = quote_client or kis_client  # 시세 조회 전용 클라이언트
        self.db = database
        self.ai_provider = ai_provider
        self.ai_api_key = ai_api_key
        self.ai_model = ai_model
        self.budget_per_stock = budget_per_stock
        self.last_drops: list[str] = []
        self._last_ai_call: float = 0  # rate limit 방어용 타임스탬프

    def scan_kr_volume_rank(self) -> list[dict]:
        """국내 거래량 상위 종목을 조회한다."""
        try:
            return self.quote.get_kr_volume_rank(count=20)
        except Exception as e:
            logger.error("거래량 상위 종목 조회 실패: %s", e)
            return []

    def scan_and_select(self, rotate: bool = False, positions: list = None) -> list[dict]:
        """거래량 상위 종목을 AI로 분석하여 유망 종목을 선별한다.

        rotate=True이면 기존 watchlist 종목도 재평가하여 drops를 반환한다.
        positions: 보유 종목 리스트 (로테이션 시 손익 정보 전달용)
        """
        self.last_drops = []

        # 거래량 상위 종목 조회
        volume_rank = self.scan_kr_volume_rank()
        if not volume_rank:
            logger.info("거래량 상위 종목 데이터 없음 -- 스캔 스킵")
            return []

        # 종목당 예산 내 매수 가능한 종목만 필터링
        if self.budget_per_stock > 0:
            before = len(volume_rank)
            volume_rank = [
                v for v in volume_rank
                if float(v.get("price", "0").replace(",", "")) <= self.budget_per_stock
            ]
            filtered = before - len(volume_rank)
            if filtered > 0:
                logger.info("예산 필터: %d개 종목 제외 (종목당 한도: %s원)", filtered, f"{self.budget_per_stock:,.0f}")

        if not volume_rank:
            logger.info("예산 내 매수 가능 종목 없음 -- 스캔 스킵")
            return []

        # 현재 감시 중인 종목
        current_watchlist = self.db.get_watchlist_symbols("KR")

        # AI에게 전달할 데이터 구성
        if rotate:
            sys_prompt = ROTATE_SYSTEM_PROMPT
            prompt = self._build_rotate_prompt(volume_rank, current_watchlist, positions)
        else:
            sys_prompt = SCAN_SYSTEM_PROMPT
            prompt = self._build_scan_prompt(volume_rank, current_watchlist)

        # AI 호출
        try:
            response = self._call_ai(prompt, sys_prompt=sys_prompt)
            picks = self._parse_scan_response(response)
            if rotate:
                self.last_drops = self._parse_drops(response)
        except Exception as e:
            logger.error("AI 종목 스캔 실패: %s", e)
            return []

        # 선별된 종목을 watchlist에 추가
        added = []
        for pick in picks:
            symbol = pick["symbol"]
            if symbol not in current_watchlist:
                name = next((v["name"] for v in volume_rank if v["symbol"] == symbol), "")
                self.db.add_watchlist(symbol, "KR", name=name, source="ai_scan", reason=pick.get("reason", ""))
                added.append(pick)
                logger.info("AI 스캔 종목 추가: %s %s — %s", symbol, name, pick.get("reason", ""))

        return added

    def scan_us_and_select(self, candidates: list[str] | None = None, rotate: bool = False) -> list[dict]:
        """미국 주요 종목 시세를 조회하고 AI로 유망 종목을 선별한다."""
        # 후보 풀: 전달받은 목록 또는 주요 미국 종목
        if not candidates:
            candidates = [
                "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                "AMD", "NFLX", "AVGO", "CRM", "ORCL", "PLTR", "SOFI",
                "COIN", "SQ", "SHOP", "SNOW", "UBER", "ABNB",
            ]

        us_data = []
        current_watchlist = self.db.get_watchlist_symbols("US")

        for symbol in candidates:
            try:
                price = self.quote.get_us_price(symbol)
                us_data.append({
                    "symbol": symbol,
                    "name": price.name,
                    "price": f"{price.price:.2f}",
                    "change_pct": f"{price.change_pct:.2f}",
                    "volume": str(price.volume),
                })
                time.sleep(0.08 if self.quote.is_live else 0.25)
            except Exception as e:
                logger.debug("US 시세 조회 실패 [%s]: %s", symbol, e)

        if not us_data:
            logger.info("US 시세 데이터 없음 -- 스캔 스킵")
            return []

        # AI 프롬프트 구성
        lines = ["[미국 주요 종목 시세]"]
        lines.append("종목코드 | 종목명 | 현재가($) | 등락률 | 거래량")
        lines.append("-" * 80)
        for item in us_data:
            lines.append(f"{item['symbol']} | {item['name']} | ${item['price']} | {item['change_pct']}% | {item['volume']}")
        if current_watchlist:
            lines.append(f"\n[현재 감시 중인 종목]: {', '.join(current_watchlist)}")
            if rotate:
                lines.append("기존 종목 중 모멘텀을 잃은 종목은 drops에, 신규 유망 종목은 picks에 넣어주세요.")
            else:
                lines.append("이미 감시 중인 종목은 제외하고 새로운 종목만 선별해주세요.")
        lines.append("\n위 데이터를 분석하여 단기 매매에 유망한 미국 종목을 선별해주세요.")
        prompt = "\n".join(lines)

        try:
            sys_prompt = ROTATE_SYSTEM_PROMPT if rotate else SCAN_SYSTEM_PROMPT
            response = self._call_ai(prompt, sys_prompt=sys_prompt)
            picks = self._parse_scan_response(response)
            if rotate:
                self.last_drops = self._parse_drops(response)
        except Exception as e:
            logger.error("AI US 종목 스캔 실패: %s", e)
            return []

        added = []
        for pick in picks:
            symbol = pick["symbol"]
            if symbol not in current_watchlist:
                name = next((v["name"] for v in us_data if v["symbol"] == symbol), "")
                self.db.add_watchlist(symbol, "US", name=name, source="ai_scan", reason=pick.get("reason", ""))
                added.append(pick)
                logger.info("AI US 스캔 종목 추가: %s %s — %s", symbol, name, pick.get("reason", ""))

        return added

    def _build_scan_prompt(self, volume_rank: list[dict], current_watchlist: list[str]) -> str:
        """스캔 프롬프트를 구성한다."""
        lines = ["[거래량 상위 20 종목]"]
        lines.append("종목코드 | 종목명 | 현재가 | 등락률 | 거래량 | 거래대금")
        lines.append("-" * 80)
        for item in volume_rank:
            lines.append(
                f"{item['symbol']} | {item['name']} | "
                f"{item['price']} | {item['change_pct']}% | "
                f"{item['volume']} | {item['amount']}"
            )

        if current_watchlist:
            lines.append(f"\n[현재 감시 중인 종목]: {', '.join(current_watchlist)}")
            lines.append("이미 감시 중인 종목은 제외하고 새로운 종목만 선별해주세요.")

        lines.append("\n위 데이터를 분석하여 단기 매매에 유망한 종목을 선별해주세요.")
        return "\n".join(lines)

    def _build_rotate_prompt(self, volume_rank: list[dict], current_watchlist: list[str], positions: list = None) -> str:
        """로테이션용 프롬프트 — 기존 종목 재평가 + 신규 추천."""
        lines = ["[거래량 상위 20 종목]"]
        lines.append("종목코드 | 종목명 | 현재가 | 등락률 | 거래량 | 거래대금")
        lines.append("-" * 80)
        for item in volume_rank:
            lines.append(
                f"{item['symbol']} | {item['name']} | "
                f"{item['price']} | {item['change_pct']}% | "
                f"{item['volume']} | {item['amount']}"
            )

        # 보유 종목 손익 현황 (핵심 정보)
        if positions:
            lines.append("\n[현재 보유 종목 손익 현황]")
            lines.append("종목코드 | 종목명 | 매수가 | 현재가 | 수익률 | 수량")
            lines.append("-" * 60)
            for pos in positions:
                lines.append(
                    f"{pos.symbol} | {pos.name} | {pos.avg_price:,.0f} | "
                    f"{pos.current_price:,.0f} | {pos.pnl_pct:+.1f}% | {pos.qty}"
                )
            loss_positions = [p for p in positions if p.pnl_pct <= -3]
            if loss_positions:
                loss_names = ", ".join(f"{p.symbol}({p.pnl_pct:+.1f}%)" for p in loss_positions)
                lines.append(f"⚠ 손실 종목: {loss_names} → drops에 포함을 강력 권장")

        if current_watchlist:
            lines.append(f"\n[현재 감시 중인 종목]: {', '.join(current_watchlist)}")
            lines.append("손실 중인 보유 종목은 drops에, 신규 유망 종목은 picks에 넣어주세요.")
        else:
            lines.append("\n감시 종목이 없습니다. 신규 유망 종목을 picks에 넣어주세요.")

        lines.append("\n위 데이터를 분석하여 종목 교체 판단을 내려주세요.")
        return "\n".join(lines)

    def _parse_drops(self, text: str) -> list[str]:
        """AI 응답에서 제거 대상 종목을 파싱한다."""
        try:
            cleaned = text.strip()
            if "```" in cleaned:
                start = cleaned.find("{")
                end = cleaned.rfind("}") + 1
                cleaned = cleaned[start:end]
            data = json.loads(cleaned)
            return data.get("drops", [])
        except (json.JSONDecodeError, KeyError):
            return []

    def _call_ai(self, prompt: str, sys_prompt: str = "") -> str:
        """AI API를 호출한다 (rate limit 방어 + 429 재시도)."""
        if not sys_prompt:
            sys_prompt = SCAN_SYSTEM_PROMPT

        # Gemini 무료 tier: 15 RPM → 최소 5초 간격
        if self.ai_provider == "gemini":
            now = time.time()
            elapsed = now - self._last_ai_call
            if elapsed < 5:
                time.sleep(5 - elapsed)

        delays = [10, 30, 60]
        for attempt in range(3):
            try:
                result = self._call_ai_once(prompt, sys_prompt)
                self._last_ai_call = time.time()
                return result
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    logger.warning("AI API 429 rate limit — %d초 후 재시도 (%d/3)", delays[attempt], attempt + 1)
                    time.sleep(delays[attempt])
                else:
                    raise

    def _call_ai_once(self, prompt: str, sys_prompt: str) -> str:
        """AI API 1회 호출."""
        if self.ai_provider == "gemini":
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.ai_api_key)
            response = client.models.generate_content(
                model=self.ai_model or "gemini-2.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    max_output_tokens=512,
                ),
            )
            return response.text
        elif self.ai_provider == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=self.ai_api_key)
            message = client.messages.create(
                model=self.ai_model or "claude-haiku-4-5-20250514",
                max_tokens=512,
                system=sys_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        elif self.ai_provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=self.ai_api_key)
            response = client.chat.completions.create(
                model=self.ai_model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512,
            )
            return response.choices[0].message.content
        else:
            raise ValueError(f"지원하지 않는 AI provider: {self.ai_provider}")

    def _parse_scan_response(self, text: str) -> list[dict]:
        """AI 응답에서 선별된 종목을 파싱한다."""
        try:
            cleaned = text.strip()
            if "```" in cleaned:
                start = cleaned.find("{")
                end = cleaned.rfind("}") + 1
                cleaned = cleaned[start:end]

            data = json.loads(cleaned)
            picks = data.get("picks", [])
            summary = data.get("summary", "")

            if summary:
                logger.info("AI 시장 분석: %s", summary)

            return picks[:5]
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("AI 스캔 응답 파싱 실패: %s", e)
            return []
