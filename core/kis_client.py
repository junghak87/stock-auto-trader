"""한국투자증권 API 클라이언트 래퍼 모듈.

REST API 직접 호출로 국내/해외 주식 매매 및 시세 조회를 수행한다.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from core.broker import StockPrice, OrderResult, Position, OHLCVData

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
    "kr_minute_price": "FHKST03010200",
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
    "us_buy_live": "TTTT1002U",
    "us_sell_live": "TTTT1006U",
    "us_buy_paper": "VTTT1002U",
    "us_sell_paper": "VTTT1001U",
    "us_balance_live": "TTTT3012R",
    "us_balance_paper": "VTTT3012R",
}


# 주요 NYSE 상장 종목 (거래소 자동 감지용)
_NYSE_SYMBOLS = {
    "BRK.A", "BRK.B", "JPM", "V", "WMT", "JNJ", "PG", "MA", "UNH",
    "HD", "DIS", "BAC", "KO", "PEP", "ABBV", "MRK", "CVX", "XOM",
    "ABT", "CRM", "TMO", "DHR", "ACN", "LLY", "NKE", "MDT", "PM",
    "NEE", "HON", "UPS", "RTX", "IBM", "GE", "CAT", "BA", "MMM",
    "GS", "AXP", "BLK", "C", "WFC", "MS", "SCHW", "USB", "T", "VZ",
    "LOW", "SPGI", "SYK", "DE", "BDX", "CI", "PLD", "SO", "DUK",
    "CL", "ZTS", "CME", "ICE", "MCO", "SHW", "PNC", "TFC", "AIG",
    "F", "GM", "WBA", "CVS", "HUM", "EL", "FDX", "UNP", "NSC",
}


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
        self.supported_markets = ["KR", "US"]
        self.base_url = BASE_URL_LIVE if is_live else BASE_URL_PAPER
        self.access_token: str | None = None
        self.token_expires_at: float = 0
        self._session = requests.Session()
        self._load_or_issue_token()

    @staticmethod
    def detect_exchange(symbol: str) -> tuple[str, str]:
        """심볼로 거래소를 추정한다. (시세용 3자리, 주문용 4자리)"""
        if symbol.upper() in _NYSE_SYMBOLS:
            return "NYS", "NYSE"
        return "NAS", "NASD"

    # ── 인증 ──────────────────────────────────────────────

    def _cache_key(self) -> str:
        """캐시 키 (live/paper 구분)."""
        return "live" if self.is_live else "paper"

    def _load_or_issue_token(self):
        """캐시된 토큰을 로드하거나 새로 발급받는다."""
        if TOKEN_CACHE_FILE.exists():
            try:
                all_cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
                # 하위호환: 기존 단일 토큰 형식 → 새 멀티 형식 변환
                if "access_token" in all_cache and "is_live" in all_cache:
                    key = "live" if all_cache["is_live"] else "paper"
                    all_cache = {key: all_cache}
                cache = all_cache.get(self._cache_key(), {})
                if cache.get("expires_at", 0) > time.time() + 60:
                    self.access_token = cache["access_token"]
                    self.token_expires_at = cache["expires_at"]
                    logger.info("캐시된 토큰 로드 완료 [%s] (만료: %s)", self._cache_key(), datetime.fromtimestamp(self.token_expires_at))
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

        # 기존 캐시 로드 후 해당 키만 업데이트
        all_cache = {}
        if TOKEN_CACHE_FILE.exists():
            try:
                all_cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
                # 하위호환: 기존 단일 형식 변환
                if "access_token" in all_cache and "is_live" in all_cache:
                    key = "live" if all_cache["is_live"] else "paper"
                    all_cache = {key: {"access_token": all_cache["access_token"], "expires_at": all_cache["expires_at"]}}
            except (json.JSONDecodeError, KeyError):
                all_cache = {}
        all_cache[self._cache_key()] = {
            "access_token": self.access_token,
            "expires_at": self.token_expires_at,
        }
        TOKEN_CACHE_FILE.write_text(json.dumps(all_cache), encoding="utf-8")
        logger.info("새 토큰 발급 완료 [%s] (만료: %s)", self._cache_key(), datetime.fromtimestamp(self.token_expires_at))

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
        """GET 요청 (일시적 오류 시 자동 재시도)."""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, headers=self._headers(tr_id), params=params, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.warning("GET %s 연결 오류 (시도 %d/3): %s", path, attempt + 1, e)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (500, 502, 503, 504):
                    last_exc = e
                    body_text = ""
                    try:
                        body_text = e.response.text[:200]
                    except Exception:
                        pass
                    logger.warning("GET %s 서버 오류 %d (시도 %d/3) %s", path, e.response.status_code, attempt + 1, body_text)
                else:
                    raise
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning("GET %s 타임아웃 (시도 %d/3)", path, attempt + 1)
            time.sleep(1.0 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """POST 요청 (일시적 오류 시 자동 재시도)."""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.post(url, headers=self._headers(tr_id), json=body, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.warning("POST %s 연결 오류 (시도 %d/3): %s", path, attempt + 1, e)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (500, 502, 503, 504):
                    last_exc = e
                    body_text = ""
                    try:
                        body_text = e.response.text[:200]
                    except Exception:
                        pass
                    logger.warning("POST %s 서버 오류 %d (시도 %d/3) %s", path, e.response.status_code, attempt + 1, body_text)
                else:
                    raise
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning("POST %s 타임아웃 (시도 %d/3)", path, attempt + 1)
            time.sleep(1.0 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

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

    def get_kr_minute_prices(self, symbol: str, end_time: str = "") -> list[OHLCVData]:
        """국내 주식 1분봉 데이터를 조회한다 (최대 30개).

        Args:
            symbol: 종목코드
            end_time: 조회 종료 시각 (HHMMSS). 빈 값이면 현재 시각.
        """
        if not end_time:
            end_time = datetime.now().strftime("%H%M%S")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            TR_IDS["kr_minute_price"],
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": end_time,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_ETC_CLS_CODE": "",
            },
        )
        result = []
        for item in data.get("output2", []):
            hour = item.get("stck_cntg_hour", "")
            date_str = item.get("stck_bsop_date", datetime.now().strftime("%Y%m%d"))
            result.append(OHLCVData(
                date=f"{date_str} {hour}",
                open=float(item.get("stck_oprc", 0)),
                high=float(item.get("stck_hgpr", 0)),
                low=float(item.get("stck_lwpr", 0)),
                close=float(item.get("stck_prpr", 0)),
                volume=int(item.get("cntg_vol", 0)),
            ))
        return result

    # ── 해외 주식 시세 ────────────────────────────────────

    def get_us_price(self, symbol: str, exchange: str = "") -> StockPrice:
        """해외 주식 현재가를 조회한다.

        Args:
            symbol: 종목코드 (예: AAPL)
            exchange: 거래소 코드 (NAS=나스닥, NYS=뉴욕, AMS=아멕스). 빈 값이면 자동 감지.
        """
        if not exchange:
            exchange, _ = self.detect_exchange(symbol)
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

    def get_us_daily_prices(self, symbol: str, exchange: str = "", period: str = "0", count: int = 60) -> list[OHLCVData]:
        """해외 주식 일봉 데이터를 조회한다.

        Args:
            period: 0(일), 1(주), 2(월)
        """
        if not exchange:
            exchange, _ = self.detect_exchange(symbol)
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

    # ── 거래량 순위 ────────────────────────────────────

    def get_kr_volume_rank(self, count: int = 20) -> list[dict]:
        """국내 거래량 상위 종목을 조회한다."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
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
        for item in data.get("output", [])[:count]:
            results.append({
                "symbol": item.get("mksc_shrn_iscd", ""),
                "name": item.get("hts_kor_isnm", ""),
                "price": item.get("stck_prpr", "0"),
                "change_pct": item.get("prdy_ctrt", "0"),
                "volume": item.get("acml_vol", "0"),
                "amount": item.get("acml_tr_pbmn", "0"),
            })
        return results

    # ── 국내 지수 조회 ──────────────────────────────────

    def get_kr_index(self, index_code: str = "0001") -> dict:
        """국내 업종 지수를 조회한다.

        Args:
            index_code: 0001=KOSPI, 1001=KOSDAQ
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000",
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code},
        )
        o = data.get("output", {})
        return {
            "index_code": index_code,
            "name": "KOSPI" if index_code == "0001" else "KOSDAQ",
            "price": float(o.get("bstp_nmix_prpr", 0)),
            "change": float(o.get("bstp_nmix_prdy_vrss", 0)),
            "change_pct": float(o.get("bstp_nmix_prdy_ctrt", 0)),
            "volume": int(o.get("acml_vol", 0)),
        }

    # ── 국내 주식 주문 ────────────────────────────────────

    def buy_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        """국내 주식 매수 주문.

        Args:
            price: 0이면 시장가 주문 (모의투자 시 현재가 지정가로 자동 변환)
        """
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"

        # 모의투자는 시장가(06) 미지원 → 현재가 지정가로 변환
        if price <= 0 and not self.is_live:
            price = int(self.get_kr_price(symbol).price)
            logger.info("모의투자 시장가→지정가 변환: %s %d원", symbol, price)
        order_type = "01" if price > 0 else "06"

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

        # 모의투자는 시장가(06) 미지원 → 현재가 지정가로 변환
        if price <= 0 and not self.is_live:
            price = int(self.get_kr_price(symbol).price)
            logger.info("모의투자 시장가→지정가 변환: %s %d원", symbol, price)
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

    def cancel_kr(self, order_no: str, symbol: str, qty: int) -> OrderResult:
        """국내 주식 주문을 취소한다."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        tr_id = self._tr("kr_cancel")
        data = self._post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)
        return self._parse_kr_order(data, symbol, "cancel", qty, 0)

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

    def buy_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        """해외 주식 매수 주문."""
        if not exchange:
            _, exchange = self.detect_exchange(symbol)
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"

        # 모의투자는 시장가 미지원 → 현재가 지정가로 변환
        if price <= 0 and not self.is_live:
            price = self.get_us_price(symbol).price
            logger.info("모의투자 US 시장가→지정가 변환: %s $%.2f", symbol, price)
        order_type = "00" if price > 0 else "01"

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
            "ORD_SVR_DVSN_CD": "0",
        }
        tr_id = self._tr("us_buy")
        data = self._post("/uapi/overseas-stock/v1/trading/order", tr_id, body)
        return self._parse_us_order(data, symbol, "buy", qty, price)

    def sell_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        """해외 주식 매도 주문."""
        if not exchange:
            _, exchange = self.detect_exchange(symbol)
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"

        # 모의투자는 시장가 미지원 → 현재가 지정가로 변환
        if price <= 0 and not self.is_live:
            price = self.get_us_price(symbol).price
            logger.info("모의투자 US 시장가→지정가 변환: %s $%.2f", symbol, price)
        order_type = "00" if price > 0 else "01"

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price),
            "ORD_SVR_DVSN_CD": "0",
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
        """해외 주식 잔고를 조회한다 (NASDAQ + NYSE 모두 조회)."""
        acct_prefix = self.account_no.split("-")[0]
        acct_suffix = self.account_no.split("-")[1] if "-" in self.account_no else "01"
        tr_id = self._tr("us_balance")

        positions = []
        seen = set()
        for exchange in ("NASD", "NYSE", "AMEX"):
            try:
                data = self._get(
                    "/uapi/overseas-stock/v1/trading/inquire-balance",
                    tr_id,
                    params={
                        "CANO": acct_prefix,
                        "ACNT_PRDT_CD": acct_suffix,
                        "OVRS_EXCG_CD": exchange,
                        "TR_CRCY_CD": "USD",
                        "CTX_AREA_FK200": "",
                        "CTX_AREA_NK200": "",
                    },
                )
                for item in data.get("output1", []):
                    qty = int(item.get("ovrs_cblc_qty", 0))
                    symbol = item.get("ovrs_pdno", "")
                    if qty == 0 or symbol in seen:
                        continue
                    seen.add(symbol)
                    avg_price = float(item.get("pchs_avg_pric", 0))
                    current_price = float(item.get("now_pric2", item.get("ovrs_now_pric", 0)))
                    pnl = float(item.get("frcr_evlu_pfls_amt", 0))
                    pnl_pct = float(item.get("evlu_pfls_rt", 0))
                    positions.append(Position(
                        symbol=symbol,
                        name=item.get("ovrs_item_name", ""),
                        qty=qty,
                        avg_price=avg_price,
                        current_price=current_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        market="US",
                    ))
            except Exception as e:
                logger.debug("해외 잔고 조회 [%s]: %s", exchange, e)
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
