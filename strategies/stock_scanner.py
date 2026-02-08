"""AI 기반 종목 스캐닝 모듈.

KIS API에서 거래량 상위 종목을 조회하고,
AI를 활용하여 유망 종목을 선별하여 감시 목록에 추가한다.
"""

import json
import logging
import time

from core.kis_client import KISClient
from core.database import Database

logger = logging.getLogger(__name__)

# 거래량 상위 종목 조회 TR_ID
TR_VOLUME_RANK = "FHPST01710000"

SCAN_SYSTEM_PROMPT = """당신은 주식 종목 선별 전문가입니다.
거래량 상위 종목 데이터를 분석하여 단기 매매에 유망한 종목을 선별해주세요.

선별 기준:
- 거래량이 평소 대비 급증한 종목 (관심 증가)
- 상승 추세의 초기 단계로 보이는 종목
- 과도하게 급등하여 리스크가 높은 종목은 제외
- 시가총액이 너무 작은 종목(투기성) 제외

반드시 아래 JSON 형식으로만 응답하세요:
{"picks": [{"symbol": "종목코드", "reason": "선별 사유"}], "summary": "시장 분위기 한줄 요약"}

최대 5개 종목만 선별하세요. 유망 종목이 없으면 빈 리스트를 반환하세요."""


class StockScanner:
    """AI 기반 종목 스캐너."""

    def __init__(
        self,
        kis_client: KISClient,
        database: Database,
        ai_provider: str = "gemini",
        ai_api_key: str = "",
        ai_model: str = "",
    ):
        self.kis = kis_client
        self.db = database
        self.ai_provider = ai_provider
        self.ai_api_key = ai_api_key
        self.ai_model = ai_model

    def scan_kr_volume_rank(self) -> list[dict]:
        """국내 거래량 상위 종목을 조회한다."""
        try:
            data = self.kis._get(
                "/uapi/domestic-stock/v1/quotations/volume-rank",
                TR_VOLUME_RANK,
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_COND_SCR_DIV_CODE": "20101",
                    "FID_INPUT_ISCD": "0000",
                    "FID_DIV_CLS_CODE": "0",
                    "FID_BLNG_CLS_CODE": "0",
                    "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "000000",
                    "FID_INPUT_PRICE_1": "0",
                    "FID_INPUT_PRICE_2": "0",
                    "FID_VOL_CNT": "0",
                    "FID_INPUT_DATE_1": "",
                },
            )
            results = []
            for item in data.get("output", [])[:20]:
                results.append({
                    "symbol": item.get("mksc_shrn_iscd", ""),
                    "name": item.get("hts_kor_isnm", ""),
                    "price": item.get("stck_prpr", "0"),
                    "change_pct": item.get("prdy_ctrt", "0"),
                    "volume": item.get("acml_vol", "0"),
                    "amount": item.get("acml_tr_pbmn", "0"),
                })
            return results
        except Exception as e:
            logger.error("거래량 상위 종목 조회 실패: %s", e)
            return []

    def scan_and_select(self) -> list[dict]:
        """거래량 상위 종목을 AI로 분석하여 유망 종목을 선별한다."""
        # 거래량 상위 종목 조회
        volume_rank = self.scan_kr_volume_rank()
        if not volume_rank:
            logger.info("거래량 상위 종목 데이터 없음 — 스캔 스킵")
            return []

        # 현재 감시 중인 종목
        current_watchlist = self.db.get_watchlist_symbols("KR")

        # AI에게 전달할 데이터 구성
        prompt = self._build_scan_prompt(volume_rank, current_watchlist)

        # AI 호출
        try:
            response = self._call_ai(prompt)
            picks = self._parse_scan_response(response)
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

    def _call_ai(self, prompt: str) -> str:
        """AI API를 호출한다."""
        if self.ai_provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=self.ai_api_key)
            model = genai.GenerativeModel(
                model_name=self.ai_model or "gemini-2.0-flash",
                system_instruction=SCAN_SYSTEM_PROMPT,
            )
            response = model.generate_content(prompt)
            return response.text
        elif self.ai_provider == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=self.ai_api_key)
            message = client.messages.create(
                model=self.ai_model or "claude-haiku-4-5-20250514",
                max_tokens=512,
                system=SCAN_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        elif self.ai_provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=self.ai_api_key)
            response = client.chat.completions.create(
                model=self.ai_model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SCAN_SYSTEM_PROMPT},
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
