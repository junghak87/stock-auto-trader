# 한국투자증권 API 자동매매 시스템 구현 계획

## 기술 스택
- **언어**: Python 3.11+
- **KIS API**: `python-kis` (v2.1.6, 가장 활발히 유지보수되는 라이브러리)
- **스케줄러**: `APScheduler` (주기적 시세 조회 및 전략 실행)
- **DB**: SQLite (매매 내역, 시세 데이터 저장)
- **알림**: Telegram Bot API (`python-telegram-bot`)
- **설정 관리**: `.env` + `pydantic-settings`
- **데이터 분석**: `pandas`, `ta` (기술적 지표 라이브러리)

## 프로젝트 구조

```
stock/
├── .env.example              # 환경변수 템플릿
├── requirements.txt          # 의존성 목록
├── config/
│   └── settings.py           # Pydantic 기반 설정 관리
├── core/
│   ├── __init__.py
│   ├── kis_client.py         # KIS API 클라이언트 래퍼
│   ├── telegram_bot.py       # 텔레그램 알림 모듈
│   └── database.py           # SQLite DB 관리 (매매 기록, 시세)
├── strategies/
│   ├── __init__.py
│   ├── base.py               # 전략 베이스 클래스 (인터페이스)
│   ├── ma_cross.py           # 이동평균 교차 전략
│   ├── rsi_strategy.py       # RSI 과매수/과매도 전략
│   └── macd_strategy.py      # MACD 전략
├── trading/
│   ├── __init__.py
│   ├── executor.py           # 주문 실행기 (매수/매도/취소)
│   └── risk_manager.py       # 리스크 관리 (손절/익절/포지션 한도)
├── scheduler/
│   ├── __init__.py
│   └── jobs.py               # 스케줄링 작업 정의
└── main.py                   # 메인 진입점
```

## 구현 단계

### Step 1: 프로젝트 초기화 및 설정
- `requirements.txt` 작성
- `.env.example` 템플릿 생성 (APP_KEY, APP_SECRET, 계좌번호, 텔레그램 토큰 등)
- `config/settings.py` — pydantic-settings로 환경변수 로드 및 검증

### Step 2: KIS API 클라이언트 (`core/kis_client.py`)
- python-kis 라이브러리 래핑
- 국내/해외 주식 시세 조회
- 국내/해외 주식 매수/매도/취소
- 잔고 조회, 주문 내역 조회
- 토큰 자동 관리 (24시간 만료, 6시간 재발급 쿨다운)

### Step 3: DB 모듈 (`core/database.py`)
- SQLite 테이블 설계:
  - `trades` — 체결 내역 (종목, 수량, 가격, 시간, 전략명)
  - `market_data` — 시세 스냅샷 (OHLCV)
  - `signals` — 전략 시그널 로그
- 매매 기록 저장/조회 함수

### Step 4: 전략 엔진 (`strategies/`)
- `BaseStrategy` 추상 클래스 정의 (analyze → signal 반환)
- 이동평균 교차 전략 (`ma_cross.py`):
  - 단기(5일) > 장기(20일) 골든크로스 → 매수
  - 단기 < 장기 데드크로스 → 매도
- RSI 전략 (`rsi_strategy.py`):
  - RSI < 30 → 매수, RSI > 70 → 매도
- MACD 전략 (`macd_strategy.py`):
  - MACD 시그널 교차 기반 매매
- 복합 전략: 여러 전략의 시그널을 종합하여 최종 결정

### Step 5: 리스크 관리 (`trading/risk_manager.py`)
- 종목당 최대 투자 비율 제한
- 손절선/익절선 설정
- 일일 최대 거래 횟수 제한
- 총 포트폴리오 손실 한도

### Step 6: 주문 실행기 (`trading/executor.py`)
- 시그널 기반 주문 생성 및 실행
- 주문 실패 시 재시도 로직
- 모의투자/실전투자 모드 전환

### Step 7: 텔레그램 알림 (`core/telegram_bot.py`)
- 매매 시그널 발생 시 알림
- 체결 완료 알림
- 일일 수익률 요약
- 오류/장애 발생 알림
- 간단한 명령어 지원 (/status, /balance, /positions)

### Step 8: 스케줄러 (`scheduler/jobs.py`)
- 장 시작 전 준비 (08:50): 관심종목 시세 로드
- 장중 주기적 분석 (매 5분): 전략 실행 → 시그널 → 주문
- 장 마감 후 정산 (15:40): 일일 수익률 계산 및 텔레그램 리포트
- 해외 장 시간대 별도 스케줄 (미국: 한국 시간 23:30~06:00)

### Step 9: 메인 진입점 (`main.py`)
- 전체 시스템 초기화 및 실행
- CLI 인자로 모드 선택 (live/paper/backtest)
- 그레이스풀 셧다운 처리
