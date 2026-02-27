"""키움증권 REST API 클라이언트 모듈.

REST API 직접 호출로 국내 주식 매매 및 시세 조회를 수행한다.
해외 주식은 지원하지 않는다 (US 메서드 호출 시 NotImplementedError).
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from core.broker import StockPrice, OrderResult, Position, OHLCVData

logger = logging.getLogger(__name__)

KIWOOM_TOKEN_CACHE = Path("kiwoom_token_cache.json")

# ── 기본 URL ──────────────────────────────────────────────
BASE_URL_LIVE = "https://api.kiwoom.com"
BASE_URL_PAPER = "https://mockapi.kiwoom.com"


class KiwoomClient:
    """키움증권 REST API 클라이언트.

    BrokerClient Protocol을 구현하며, 국내(KR) 주식만 지원한다.
    """

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
        self.supported_markets = ["KR"]
        self.base_url = BASE_URL_LIVE if is_live else BASE_URL_PAPER
        self.access_token: str | None = None
        self.token_expires_at: float = 0
        self._session = requests.Session()
        self._last_request_time: float = 0
        self._min_interval: float = 0.25  # 초당 4회 제한
        self._name_cache: dict[str, str] = {}  # 종목코드 → 종목명 캐시
        self._load_or_issue_token()

    # ── 인증 ──────────────────────────────────────────────

    def _load_or_issue_token(self):
        """캐시된 토큰을 로드하거나 새로 발급받는다."""
        if KIWOOM_TOKEN_CACHE.exists():
            try:
                cache = json.loads(KIWOOM_TOKEN_CACHE.read_text(encoding="utf-8"))
                if cache.get("is_live") == self.is_live and cache.get("expires_at", 0) > time.time() + 60:
                    self.access_token = cache["access_token"]
                    self.token_expires_at = cache["expires_at"]
                    logger.info("키움 캐시 토큰 로드 완료 (만료: %s)", datetime.fromtimestamp(self.token_expires_at))
                    return
            except (json.JSONDecodeError, KeyError):
                pass
        self._issue_token()

    def _issue_token(self):
        """새 접근 토큰을 발급받는다."""
        url = f"{self.base_url}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
        resp = self._session.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # 키움 응답: "token" 필드 (KIS는 "access_token")
        self.access_token = data["token"]

        # 만료 시간 파싱: expires_dt(YYYYMMDDHHmmss) 또는 expires_in(초)
        if "expires_dt" in data:
            self.token_expires_at = datetime.strptime(data["expires_dt"], "%Y%m%d%H%M%S").timestamp()
        else:
            expires_in = int(data.get("expires_in", 86400))
            self.token_expires_at = time.time() + expires_in

        KIWOOM_TOKEN_CACHE.write_text(
            json.dumps({
                "access_token": self.access_token,
                "expires_at": self.token_expires_at,
                "is_live": self.is_live,
            }),
            encoding="utf-8",
        )
        logger.info("키움 새 토큰 발급 완료 (만료: %s)", datetime.fromtimestamp(self.token_expires_at))

    def _ensure_token(self):
        """토큰 만료 5분 전에 자동 갱신."""
        if time.time() > self.token_expires_at - 300:
            logger.info("키움 토큰 만료 임박, 재발급 시도...")
            self._issue_token()

    def _headers(self, api_id: str) -> dict:
        """API 요청 공통 헤더."""
        self._ensure_token()
        return {
            "content-type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "api-id": api_id,
            "cont-yn": "N",
            "next-key": "",
        }

    def _throttle(self):
        """요청 간격을 조절한다 (429 방지)."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _post(self, resource_url: str, api_id: str, body: dict) -> dict:
        """POST 요청 (429/5xx 시 자동 재시도)."""
        url = f"{self.base_url}{resource_url}"
        last_exc: Exception | None = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._session.post(url, headers=self._headers(api_id), json=body, timeout=10)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.warning("POST %s 연결 오류 (시도 %d/3): %s", resource_url, attempt + 1, e)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (429, 500, 502, 503, 504):
                    last_exc = e
                    logger.warning("POST %s 오류 %d (시도 %d/3)", resource_url, e.response.status_code, attempt + 1)
                else:
                    raise
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning("POST %s 타임아웃 (시도 %d/3)", resource_url, attempt + 1)
            time.sleep(1.0 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    # ── 유틸리티 ─────────────────────────────────────────

    @staticmethod
    def _p(val) -> float:
        """키움 가격 문자열에서 부호(+/-)를 제거하고 절대값을 반환한다.

        키움 API는 전일 대비 등락 방향을 가격 앞에 +/- 부호로 표시한다.
        예: "-216500" → 실제 가격은 216500 (하락 표시)
        """
        return abs(float(val or 0))

    def _get_name(self, symbol: str) -> str:
        """종목명을 캐시에서 조회하거나, 없으면 API로 1회 조회한다."""
        if symbol in self._name_cache:
            return self._name_cache[symbol]
        try:
            data = self._post("/api/dostk/stkinfo", "ka10002", body={"stk_cd": symbol})
            name = data.get("stk_nm", "")
            if name:
                self._name_cache[symbol] = name
            return name
        except Exception:
            return ""

    # ── 국내 주식 시세 ────────────────────────────────────

    def get_kr_price(self, symbol: str) -> StockPrice:
        """국내 주식 현재가를 조회한다 (ka10005 일주월시분 최신 1건)."""
        data = self._post(
            "/api/dostk/mrkcond", "ka10005",
            body={"stk_cd": symbol},
        )
        items = data.get("stk_ddwkmm", [])
        if not items:
            return StockPrice(
                symbol=symbol, name="", price=0, change=0, change_pct=0,
                volume=0, high=0, low=0, open=0, prev_close=0, market="KR",
            )
        o = items[0]
        close = self._p(o.get("close_pric", 0))
        prev = close
        if len(items) > 1:
            prev = self._p(items[1].get("close_pric", 0))
        change = close - prev if prev else 0
        change_pct = float(o.get("flu_rt", 0))
        return StockPrice(
            symbol=symbol,
            name=self._get_name(symbol),
            price=close,
            change=change,
            change_pct=change_pct,
            volume=int(o.get("trde_qty", 0)),
            high=self._p(o.get("high_pric", 0)),
            low=self._p(o.get("low_pric", 0)),
            open=self._p(o.get("open_pric", 0)),
            prev_close=prev,
            market="KR",
        )

    def get_kr_daily_prices(self, symbol: str, period: str = "D", count: int = 60) -> list[OHLCVData]:
        """국내 주식 일봉 데이터를 조회한다 (ka10081 일봉차트).

        Args:
            symbol: 종목코드
            period: 무시됨 (키움 일봉차트는 일봉만)
            count: 조회 건수
        """
        today = datetime.now().strftime("%Y%m%d")
        data = self._post(
            "/api/dostk/chart", "ka10081",
            body={"stk_cd": symbol, "base_dt": today, "upd_stkpc_tp": "0"},
        )
        result = []
        for item in data.get("stk_dt_pole_chart_qry", [])[:count]:
            result.append(OHLCVData(
                date=item.get("dt", ""),
                open=self._p(item.get("open_pric", 0)),
                high=self._p(item.get("high_pric", 0)),
                low=self._p(item.get("low_pric", 0)),
                close=self._p(item.get("cur_prc", 0)),
                volume=int(item.get("trde_qty", 0)),
            ))
        return result

    def get_kr_minute_prices(self, symbol: str, end_time: str = "") -> list[OHLCVData]:
        """국내 주식 1분봉 데이터를 조회한다 (ka10080).

        Args:
            symbol: 종목코드
            end_time: 무시됨 (키움 API는 자동으로 최신 분봉 반환)
        """
        data = self._post(
            "/api/dostk/chart", "ka10080",
            body={"stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "0"},
        )
        result = []
        for item in data.get("stk_min_pole_chart_qry", []):
            result.append(OHLCVData(
                date=item.get("cntr_tm", ""),
                open=self._p(item.get("open_pric", 0)),
                high=self._p(item.get("high_pric", 0)),
                low=self._p(item.get("low_pric", 0)),
                close=self._p(item.get("cur_prc", 0)),
                volume=int(item.get("trde_qty", 0)),
            ))
        return result

    def get_kr_index(self, index_code: str = "0001") -> dict:
        """국내 업종 지수를 조회한다.

        키움 REST API에는 직접적인 업종 지수 API가 없으므로,
        KOSPI(069500)/KOSDAQ(229200) ETF 시세로 대체한다.
        """
        # KODEX 200 (KOSPI 추종), KODEX KOSDAQ150
        etf_map = {"0001": "069500", "1001": "229200"}
        etf_symbol = etf_map.get(index_code, "069500")

        try:
            price = self.get_kr_price(etf_symbol)
            return {
                "index_code": index_code,
                "name": "KOSPI" if index_code == "0001" else "KOSDAQ",
                "price": price.price,
                "change": price.change,
                "change_pct": price.change_pct,
                "volume": price.volume,
            }
        except Exception:
            return {
                "index_code": index_code,
                "name": "KOSPI" if index_code == "0001" else "KOSDAQ",
                "price": 0, "change": 0, "change_pct": 0, "volume": 0,
            }

    # ── 거래량 순위 ────────────────────────────────────

    def get_kr_volume_rank(self, count: int = 20) -> list[dict]:
        """국내 거래량 상위 종목을 조회한다 (ka10020 호가잔량상위).

        키움 REST API에는 KIS의 volume-rank 직접 대응 API가 없으므로,
        호가잔량상위(순매수잔량순)로 대체한다.
        """
        try:
            data = self._post(
                "/api/dostk/rkinfo", "ka10020",
                body={
                    "mrkt_tp": "001",       # 코스피
                    "sort_tp": "1",         # 순매수잔량순
                    "trde_qty_tp": "0000",  # 전체
                    "stk_cnd": "1",         # 관리종목 제외
                    "crd_cnd": "0",         # 전체
                    "stex_tp": "1",         # KRX
                },
            )
            results = []
            for item in data.get("bid_req_upper", [])[:count]:
                sym = item.get("stk_cd", "")
                name = item.get("stk_nm", "")
                if sym and name:
                    self._name_cache[sym] = name
                price = self._p(item.get("cur_prc", "0"))
                change_pct = float(item.get("flu_rt", 0))
                volume = int(item.get("trde_qty", 0))
                results.append({
                    "symbol": sym,
                    "name": name,
                    "price": str(int(price)),
                    "change_pct": f"{change_pct:.2f}",
                    "volume": str(volume),
                    "amount": str(int(price * volume)) if volume else "0",
                })
            return results
        except Exception as e:
            logger.error("키움 거래량 상위 종목 조회 실패: %s", e)
            return []

    # ── 해외 주식 시세 (미지원) ────────────────────────────

    def get_us_price(self, symbol: str, exchange: str = "") -> StockPrice:
        raise NotImplementedError("키움증권은 해외 주식 시세를 지원하지 않습니다")

    def get_us_daily_prices(self, symbol: str, exchange: str = "", period: str = "0", count: int = 60) -> list[OHLCVData]:
        raise NotImplementedError("키움증권은 해외 주식 시세를 지원하지 않습니다")

    # ── 국내 주식 주문 ────────────────────────────────────

    def buy_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        """국내 주식 매수 주문.

        Args:
            price: 0이면 시장가 주문 (모의투자 시 현재가 지정가로 변환)
        """
        # 모의투자는 시장가 미지원 가능성 → 현재가 지정가로 변환
        if price <= 0 and not self.is_live:
            price = int(self.get_kr_price(symbol).price)
            logger.info("키움 모의투자 시장가→지정가 변환: %s %d원", symbol, price)

        trade_type = "0" if price > 0 else "3"  # 0=지정가, 3=시장가

        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": symbol,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price > 0 else "",
            "trde_tp": trade_type,
            "cond_uv": "",
        }
        data = self._post("/api/dostk/ordr", "kt10000", body)
        return self._parse_order(data, symbol, "buy", qty, price)

    def sell_kr(self, symbol: str, qty: int, price: int = 0) -> OrderResult:
        """국내 주식 매도 주문."""
        if price <= 0 and not self.is_live:
            price = int(self.get_kr_price(symbol).price)
            logger.info("키움 모의투자 시장가→지정가 변환: %s %d원", symbol, price)

        trade_type = "0" if price > 0 else "3"

        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": symbol,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price > 0 else "",
            "trde_tp": trade_type,
            "cond_uv": "",
        }
        data = self._post("/api/dostk/ordr", "kt10001", body)
        return self._parse_order(data, symbol, "sell", qty, price)

    def cancel_kr(self, order_no: str, symbol: str, qty: int) -> OrderResult:
        """국내 주식 주문을 취소한다."""
        body = {
            "dmst_stex_tp": "KRX",
            "orig_ord_no": order_no,
            "stk_cd": symbol,
            "cncl_qty": str(qty) if qty > 0 else "0",  # "0" = 전량 취소
        }
        data = self._post("/api/dostk/ordr", "kt10003", body)
        return self._parse_order(data, symbol, "cancel", qty, 0)

    def _parse_order(self, data: dict, symbol: str, side: str, qty: int, price: float) -> OrderResult:
        """키움 주문 응답을 파싱한다."""
        return_code = data.get("return_code", -1)
        return OrderResult(
            success=return_code == 0,
            order_no=str(data.get("ord_no", "")),
            message=data.get("return_msg", ""),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
        )

    # ── 해외 주식 주문 (미지원) ────────────────────────────

    def buy_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        raise NotImplementedError("키움증권은 해외 주식 주문을 지원하지 않습니다")

    def sell_us(self, symbol: str, qty: int, price: float = 0, exchange: str = "") -> OrderResult:
        raise NotImplementedError("키움증권은 해외 주식 주문을 지원하지 않습니다")

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_kr_balance(self) -> list[Position]:
        """국내 주식 잔고를 조회한다 (kt00018)."""
        data = self._post(
            "/api/dostk/acnt", "kt00018",
            body={"qry_tp": "1", "dmst_stex_tp": "KRX"},
        )
        positions = []
        for item in data.get("acnt_evlt_remn_indv_tot", []):
            qty = int(item.get("rmnd_qty", 0))
            if qty == 0:
                continue
            avg_price = self._p(item.get("pur_pric", 0))
            current_price = self._p(item.get("cur_prc", 0))
            pnl = float(item.get("evltv_prft", 0))
            pnl_pct = float(item.get("prft_rt", 0))
            positions.append(Position(
                symbol=item.get("stk_cd", ""),
                name=item.get("stk_nm", ""),
                qty=qty,
                avg_price=avg_price,
                current_price=current_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                market="KR",
            ))
        return positions

    def get_us_balance(self) -> list[Position]:
        """해외 주식 잔고 — 키움은 해외 미지원이므로 빈 리스트 반환."""
        return []

    def get_all_positions(self) -> list[Position]:
        """전체 보유 종목 (국내만)."""
        return self.get_kr_balance()

    def get_cash_balance(self) -> dict:
        """예수금(현금) 잔고를 조회한다 (kt00001 + kt00018)."""
        # 예수금 상세
        deposit_data = self._post(
            "/api/dostk/acnt", "kt00001",
            body={"qry_tp": "3"},
        )
        cash = float(deposit_data.get("ord_alow_amt", 0))

        # 계좌 평가 (총 평가액, 평가 손익)
        eval_data = self._post(
            "/api/dostk/acnt", "kt00018",
            body={"qry_tp": "1", "dmst_stex_tp": "KRX"},
        )
        total_eval = float(eval_data.get("tot_evlt_amt", 0))
        stock_eval = total_eval  # 주식 평가액 = 총 평가액 (키움은 주식만)
        total_pnl = float(eval_data.get("tot_evlt_pl", 0))

        return {
            "total_eval": cash + total_eval,
            "cash": cash,
            "stock_eval": stock_eval,
            "total_pnl": total_pnl,
        }
