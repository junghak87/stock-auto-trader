# Stock Auto-Trading System

KIS + 키움 하이브리드 자동매매. 국내=키움, 해외=KIS 라우팅.

## 명령어

```bash
python main.py              # 모의투자
python main.py --live       # 실전투자
python main.py --once       # 1회 실행 후 종료
```

## 과거 버그 — 반복 금지

**종목코드 'A' 접두어**: KIS 거래량 API는 'A014530', 잔고 API는 '014530' 반환.
KR 종목코드가 외부에서 들어오는 모든 지점에서 `normalize_kr_symbol()` 적용 필수.

**캐시 전체 삭제**: `_balance_cache.clear()`로 다른 마켓 캐시까지 날림.
→ `del self._balance_cache[market]`로 해당 마켓만 무효화.

**AI JSON 파싱**: 응답에 ` ``` `이 있지만 `{}`가 없으면 크래시.
→ `{`와 `}` 존재 확인 후에만 슬라이싱.

**현금 부족 무한 매수**: `calculate_buy_qty()`가 0이어도 흐름이 계속됨.
→ 매수 전 현금 잔고 사전 확인 (KR: 5만원, US: $50 미만 시 즉시 return).

## 절대 하지 말 것

- `_balance_cache.clear()` — 마켓별 독립 무효화만
- KR 종목코드를 `normalize_kr_symbol()` 없이 비교/저장
- 로거에 f-string (`logger.info(f"...")` → `%`-style만)
- `__init__` 밖에서 새 인스턴스 변수 추가
- 본전스톱(`_breakeven_symbols`) 종목의 손절선 덮어쓰기
- 일봉 전략을 15분마다 반복 실행 (데이터가 안 바뀜 — 하루 4회면 충분)

## 반드시 할 것

- 외부 API 호출 후 `time.sleep(rate_delay)` — 실전 0.08s, 모의 0.25s, 시세 0.15s
- 매수/매도 성공 후: `risk._cash_cache_time = None` + `del _balance_cache[market]`
- 새 전략 추가: `BaseStrategy` 상속 → `REGIME_WEIGHTS` 3개 레짐 모두에 가중치 등록
- 새 설정 추가: `config/settings.py` 필드 + `.env.example` 갱신
- 텔레그램 알림에 AI 판단 사유(reason) 포함

## 실전/모의 차이

| 항목 | 실전 | 모의 |
|------|------|------|
| base_url | openapi.koreainvestment.com:9443 | openapivts.koreainvestment.com:29443 |
| TR_ID 접두어 | TTTC / JTTT | VTTC / VTTT |
| Rate limit | 20 req/s | 5 req/s |
| 토큰 만료 | 24시간 (재발급 쿨다운 6시간) | 동일 |
| 지정가 offset | 설정값 적용 | 0 (즉시 체결) |

## 코딩 스타일

- 로거: `%`-style만. 일반 문자열: f-string
- 타입힌트: `str | None` (Optional/Union 금지)
- docstring: Google style 한국어
- import: stdlib → third-party → local
- 에러: `except Exception as e:` (bare except 금지)
