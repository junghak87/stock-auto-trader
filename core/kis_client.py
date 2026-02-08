"""한국투자증권 API 클라이언트 래퍼 모듈.

python-kis 라이브러리를 활용하여 국내/해외 주식 매매 및 시세 조회를 수행한다.
REST API 직접 호출도 함께 지원하여 라이브러리 미지원 기능을 보완한다.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = Path("token_cache.json")

# ── 기본 URL ──────────────────────────────────────────────
BASE_URL_LIVE = "https://openapi.koreainvestment.com:9443"
BASE_URL_PAPER = "https://openapivts.koreainvestment.com:29443"

# ── TR_ID 매핑 ────────────────────────────────────────────
TR_IDS = {
    # 국내 주식
    "kr_price": "FHKST01010100",
    "kr_daily_price": "FHKST01010400",
    "kr_buy_live": "TTTC0802U",
    "kr_sell_live": "TTTC0801U",
    "kr_buy_paper": "VTTC0802U",
    "kr_sell_paper": "VTTC0801U",
    "kr_cancel_live": "TTTC0803U",
    "kr_cancel_paper": "VTTC0803U",
    "kr_balance_live": "TTTC8434R",
    "kr_balance_paper": "VTTC8434R",
    # 해외 주식
    "us_price": "HHDFS00000300",
    "us_daily_price": "HHDFS76240000",
    "us_buy_live": "JTTT1002U",
    "us_sell_live": "JTTT1006U",
    "us_buy_paper": "VTTT1002U",
    "us_sell_paper": "VTTT1006U",
    "us_balance_live": "JTTT3012R",
    "us_balance_paper": "VTTT3012R",
}


@dataclass
class StockPrice:
    """주식 시세 정보."""

    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    volume: int
    high: float
    low: float
    open: float
    prev_close: float
    market: str  # "KR" or "US"


@dataclass
class OrderResult:
    """주문 결과."""

    success: bool
    order_no: str
    message: str
    symbol: str
    side: str  # "buy" or "sell"
    qty: int
    price: float


@dataclass
class Position:
    """보유 종목."""

    symbol: str
    name: str
    qty: int
    avg_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    market: str


@dataclass
class OHLCVData:
    """일봉 데이터."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class KISClient:
    """한국투자증권 REST API 클라이언트."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        is_live: bool = False,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no
        self.is_live = is_live
        self.base_url = BASE_URL_LIVE if is_live else BASE_URL_PAPER
        self.access_token: str | None = None
        self.token_expires_at: float = 0
        self._session = requests.Session()
        self._load_or_issue_token()

    # ── 인증 ──────────────────────────────────────────────

    def _load_or_issue_token(self):
        """캐시된 토큰을 로드하거나 새로 발급받는다."""
        if TOKEN_CACHE_FILE.exists():
            try:
                cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
                if cache.get("is_live") == self.is_live and cache.get("expires_at", 0) > time.time() + 60:
                    self.access_token = cache["access_token"]
                    self.token_expires_at = cache["expires_at"]
                    logger.info("캐시된 토큰 로드 완료 (만료: %s)", datetime.fromtimestamp(self.token_expires_at))
                    return
            except (json.JSONDecodeError, KeyError):
                pass
        self._issue_token()

    def _issue_token(self):
        """새 접근 토큰을 발급받는다."""
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = self._session.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self.access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self.token_expires_at = time.time() + expires_in

        TOKEN_CACHE_FILE.write_text(
            json.dumps({
                "access_token": self.access_token,
                "expires_at": self.token_expires_at,
                "is_live": self.is_live,
            }),
            encoding="utf-8",
        )
        logger.info("새 토큰 발급 완료 (만료: %s)", datetime.fromtimestamp(self.token_expires_at))

    def _ensure_token(self):
        """토큰 만료 5분 전에 자동 갱신."""
        if time.time() > self.token_expires_at - 300:
            logger.info("토큰 만료 임박, 재발급 시도...")
            self._issue_token()

    def _headers(self, tr_id: str) -> dict:
        """API 요청 공통 헤더."""
        self._ensure_token()
        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }

    def _get(self, path: str, tr_id: str, params: dict | None = None) -> dict:
        """GET 요청."""
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """POST 요청."""
        url = f"{self.base_url}{path}"
        resp = self._session.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _tr(self, key: str) -> str:
        """실전/모의에 맞는 TR_ID를 반환."""
        suffix = "_live" if self.is_live else "_paper"
        return TR_IDS.get(key + suffix, TR_IDS.get(key, ""))

    # ── 국내 주식 시세 ────────────────────────────────────

    def get_kr_price(self, symbol: str) -> StockPrice:
        """국내 주식 현재가를 조회한다."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            TR_IDS["kr_price"],
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        o = data["output"]
        return StockPrice(
            symbol=symbol,
            name=o.get("hts_kor_isnm", ""),
            price=float(o.get("stck_prpr", 0)),
            change=float(o.get("prdy_vrss", 0)),
            change_pct=float(o.get("prdy_ctrt", 0)),
            volume=int(o.get("acml_vol", 0)),
            high=float(o.get("stck_hgpr", 0)),
            low=float(o.get("stck_lwpr", 0)),
            open=float(o.get("stck_oprc", 0)),
            prev_close=float(o.get("stck_sdpr", 0)),
            market="KR",
        )

    def get_kr_daily_prices(self, symbol: str, period: str = "D", count: int = 60) -> list[OHLCVData]:
        """국내 주식 일봉 데이터를 조회한다.

        Args:
            symbol: 종목코드
            period: D(일), W(주), M(월)
            count: 조회 건수 (최대 100)
        """
        today = datetime.now().strftime("%Y%m%d")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            TR_IDS["kr_daily_price"],
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": "",
                "FID_INPUT_DATE_2": today,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        result = []
        for item in data.get("output", [])[:count]:
            result.append(OHLCVData(
                date=item.get("stck_bsop_date", ""),
                open=float(item.get("stck_oprc", 0)),
                high=float(item.get("stck_hgpr", 0)),
                low=float(item.get("stck_lwpr", 0)),
                close=float(item.get("stck_clpr", 0)),
                volume=int(item.get("acml_vol", 0)),
            ))
        return result

    # ── 해외 주식 시세 ────────────────────────────────────

    def get_us_price(self, symbol: str, exchange: str = "NAS") -> StockPrice:
        """해외 주식 현재가를 조회한다.

        Args:
            symbol: 종목코드 (예: AAPL)
            exchange: 거래소 코드 (NAS=나스닥, NYS=뉴욕, AMS=아멕스)
        """
        data = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            TR_IDS["us_price"],
            params={
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
            },
        )
        o = data.get("output", {})
        last = float(o.get("last", 0))
        prev = float(o.get("base", 0))
        change = last - prev if prev else 0
        change_pct = (change / prev * 100) if prev else 0
        return StockPrice(
            symbol=symbol,
            name=o.get("name", symbol),
            price=last,
            change=round(change, 2),
            change_pct=round(change_pct, 2),
            volume=int(o.get("tvol", 0)),
            high=float(o.get("high", 0)),
            low=float(o.get("low", 0)),
            open=float(o.get("open", 0)),
            prev_close=prev,
            market="US",
        )

    def get_us_daily_prices(self, symbol: str, exchange: str = "NAS", period: str = "0", count: int = 60) -> list[OHLCVData]:
        """해외 주식 일봉 데이터를 조회한다.

        Args:
            period: 0(일), 1(주), 2(월)
        """
        data = self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            TR_IDS["us_daily_price"],
            params={
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
                "GUBN": period,
                "BYMD": "",
                "MODP": "1",
            },
        )
        result = []
        for item in data.get("output2", [])[:count]:
            result.append(OHLCVData(
                date=item.get("xymd", ""),
                open=float(item.get("open", 0)),
                high=float(item.get("high", 0)),
                low=float(item.get("low", 0)),
                close=float(item.get("clos", 0)),
                volume=int(item.get("tvol", 0)),
            ))
        return result

    # ── 국내 주식 주문 ────────────────────────────────────

    def buy_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        """국내 주식 매수 주문.

        Args:
            price: 0이면 시장가 주문
        """
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        order_type = "01" if price > 0 else "06"  # 01=지정가, 06=시장가

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        tr_id = self._tr("kr_buy")
        data = self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, body)
        return self._parse_kr_order(data, symbol, "buy", qty, price)

    def sell_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        """국내 주식 매도 주문."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        order_type = "01" if price > 0 else "06"

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        tr_id = self._tr("kr_sell")
        data = self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, body)
        return self._parse_kr_order(data, symbol, "sell", qty, price)

    def _parse_kr_order(self, data: dict, symbol: str, side: str, qty: int, price: float) -> OrderResult:
        output = data.get("output", {})
        rt_cd = data.get("rt_cd", "1")
        return OrderResult(
            success=rt_cd == "0",
            order_no=output.get("ODNO", ""),
            message=data.get("msg1", ""),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
        )

    # ── 해외 주식 주문 ────────────────────────────────────

    def buy_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "NASD") -> OrderResult:
        """해외 주식 매수 주문."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        order_type = "00" if price > 0 else "31"  # 00=지정가, 31=시장가(MOO 아님, LOO)

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
        }
        tr_id = self._tr("us_buy")
        data = self._post("/uapi/overseas-stock/v1/trading/order", tr_id, body)
        return self._parse_us_order(data, symbol, "buy", qty, price)

    def sell_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "NASD") -> OrderResult:
        """해외 주식 매도 주문."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        order_type = "00" if price > 0 else "31"

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
        }
        tr_id = self._tr("us_sell")
        data = self._post("/uapi/overseas-stock/v1/trading/order", tr_id, body)
        return self._parse_us_order(data, symbol, "sell", qty, price)

    def _parse_us_order(self, data: dict, symbol: str, side: str, qty: int, price: float) -> OrderResult:
        output = data.get("output", {})
        rt_cd = data.get("rt_cd", "1")
        return OrderResult(
            success=rt_cd == "0",
            order_no=output.get("ODNO", output.get("odno", "")),
            message=data.get("msg1", ""),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
        )

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_kr_balance(self) -> list[Position]:
        """국내 주식 잔고를 조회한다."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        tr_id = self._tr("kr_balance")

        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            params={
                "CANO": acct_prefix,
                "ACNT_PRDT_CD": acct_suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        positions = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty == 0:
                continue
            avg_price = float(item.get("pchs_avg_pric", 0))
            current_price = float(item.get("prpr", 0))
            pnl = float(item.get("evlu_pfls_amt", 0))
            pnl_pct = float(item.get("evlu_pfls_rt", 0))
            positions.append(Position(
                symbol=item.get("pdno", ""),
                name=item.get("prdt_name", ""),
                qty=qty,
                avg_price=avg_price,
                current_price=current_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                market="KR",
            ))
        return positions

    def get_us_balance(self) -> list[Position]:
        """해외 주식 잔고를 조회한다."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        tr_id = self._tr("us_balance")

        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id,
            params={
                "CANO": acct_prefix,
                "ACNT_PRDT_CD": acct_suffix,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        positions = []
        for item in data.get("output1", []):
            qty = int(item.get("ovrs_cblc_qty", 0))
            if qty == 0:
                continue
            avg_price = float(item.get("pchs_avg_pric", 0))
            current_price = float(item.get("now_pric2", item.get("ovrs_now_pric", 0)))
            pnl = float(item.get("frcr_evlu_pfls_amt", 0))
            pnl_pct = float(item.get("evlu_pfls_rt", 0))
            positions.append(Position(
                symbol=item.get("ovrs_pdno", ""),
                name=item.get("ovrs_item_name", ""),
                qty=qty,
                avg_price=avg_price,
                current_price=current_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                market="US",
            ))
        return positions

    def get_all_positions(self) -> list[Position]:
        """국내 + 해외 전체 보유 종목을 조회한다."""
        positions = []
        try:
            positions.extend(self.get_kr_balance())
        except Exception as e:
            logger.error("국내 잔고 조회 실패: %s", e)
        try:
            positions.extend(self.get_us_balance())
        except Exception as e:
            logger.error("해외 잔고 조회 실패: %s", e)
        return positions

    def get_cash_balance(self) -> dict:
        """예수금(현금) 잔고를 조회한다."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        tr_id = self._tr("kr_balance")

        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            params={
                "CANO": acct_prefix,
                "ACNT_PRDT_CD": acct_suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        output2 = data.get("output2", [{}])
        info = output2[0] if output2 else {}
        return {
            "total_eval": float(info.get("tot_evlu_amt", 0)),
            "cash": float(info.get("dnca_tot_amt", 0)),
            "stock_eval": float(info.get("scts_evlu_amt", 0)),
            "total_pnl": float(info.get("evlu_pfls_smtl_amt", 0)),
        }
