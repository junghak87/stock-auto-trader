# Stock Auto-Trading System

KIS(한국투자증권) + 키움증권 API 기반 자동매매 시스템.
국내(KOSPI/KOSDAQ) + 해외(미국) 주식을 지원하며, AI 기반 종목 스크리닝과 기술적 지표 복합 전략으로 매매한다.

## 주요 기능

- **복합 전략**: MA Cross, RSI, MACD, Bollinger+ATR 가중 투표 + AI 거부권
- **시장 레짐 감지**: KOSPI/KOSDAQ 등락률 기반 BULL/BEAR/SIDEWAYS → 전략 가중치 자동 조정
- **리스크 관리**: ATR 동적 손절/익절, 트레일링 스탑, 연속 손절 쿨다운, 일일 최대 손실
- **변동성 포지션 사이징**: ATR% 역비례로 투자금 자동 조정 (0.3x~1.5x)
- **분할 매수/매도**: 2단계 분할 매수, 분할 익절 + 본전스톱
- **포지션 교체**: 현금 부족 시 약한 보유 종목 매도 → 신규 매수
- **장기 횡보 청산**: 보유 3일 초과 + ±1% 이내 종목 자동 정리
- **AI 종목 스크리닝**: Gemini/Claude/OpenAI로 거래량 상위 종목 선별
- **꼬리 매매**: 분봉 기반 급락 반등 포착
- **텔레그램 봇**: 실시간 알림 + 명령어 (/status, /balance, /positions, /watchlist)

## 설치

```bash
git clone https://github.com/your-repo/stock.git
cd stock
pip install -r requirements.txt
cp .env.example .env
# .env 파일을 편집하여 API 키 설정
```

### 요구사항
- Python 3.11+
- 한국투자증권 API 키 (실전 또는 모의)
- 키움증권 API 키 (hybrid 모드 시)

## 실행

```bash
python main.py              # 모의투자 (기본)
python main.py --live       # 실전투자
python main.py --once       # 1회 전략 실행 후 종료
```

## 증권사 모드

`.env`의 `BROKER` 설정으로 선택:

| 모드 | 국내 주문 | 해외 주문 | 시세 조회 |
|------|-----------|-----------|-----------|
| `kis` | KIS | KIS | KIS |
| `kiwoom` | 키움 | - | 키움 |
| `hybrid` | 키움 | KIS | 각 증권사 |

## 프로젝트 구조

```
stock/
├── main.py                 # 진입점, 스케줄러 등록
├── config/
│   └── settings.py         # pydantic-settings 환경변수 관리
├── core/
│   ├── broker.py           # BrokerClient Protocol (공통 인터페이스)
│   ├── kis_client.py       # 한국투자증권 REST API
│   ├── kiwoom_client.py    # 키움증권 REST API
│   ├── hybrid_client.py    # 하이브리드 라우터 (KR→키움, US→KIS)
│   ├── database.py         # SQLite (trades, signals, market_data, daily_summary)
│   └── telegram_bot.py     # 텔레그램 알림 + 봇 명령어
├── strategies/
│   ├── base.py             # BaseStrategy ABC + Signal/StrategyResult
│   ├── composite.py        # 복합 전략 (가중 투표 + 레짐별 가중치)
│   ├── ai_strategy.py      # AI 매매 판단 (Gemini/Claude/OpenAI)
│   ├── ma_cross.py         # 이동평균 교차
│   ├── rsi_strategy.py     # RSI 과매수/과매도
│   ├── macd_strategy.py    # MACD 히스토그램
│   ├── bollinger_atr.py    # 볼린저밴드 + ATR
│   ├── tail_trading.py     # 분봉 기반 꼬리매매
│   └── stock_scanner.py    # AI 종목 스크리닝
├── trading/
│   ├── executor.py         # 주문 실행 (지정가, 분할매수, 포지션 교체)
│   └── risk_manager.py     # 리스크 관리 (손절/익절, 레짐, 변동성 사이징)
├── scheduler/
│   └── jobs.py             # APScheduler 작업 (장 시작/전략/리스크/마감)
├── .env.example            # 환경변수 템플릿
├── requirements.txt        # Python 패키지
└── CLAUDE.md               # AI 코딩 어시스턴트 규칙
```

## 스케줄 (한국 시간)

### 국내 장 (월~금)
| 시간 | 작업 |
|------|------|
| 08:50 | 장 시작 준비 + AI 종목 스캔 |
| 09:05, 11:30, 13:00, 15:10 | 전략 실행 (일봉 기반, 하루 4회) |
| 매 5분 | 리스크 체크 (손절/익절/분할매수/횡보 청산) |
| 매 3분 | 꼬리 매매 (분봉 기반) |
| 매 1시간 | 종목 로테이션 |
| 15:40 | 장 마감 정산 |

### 해외 장 (월~금, 한국 시간)
| 시간 | 작업 |
|------|------|
| 23:20 | 장 시작 준비 + AI 종목 스캔 |
| 23:35, 01:00, 03:00, 05:30 | 전략 실행 (하루 4회) |
| 매 5분 | 리스크 체크 |
| 매 1시간 | 종목 로테이션 |
| 06:10 | 장 마감 |

## 환경변수

`.env.example` 참고. 주요 설정:

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `BROKER` | 증권사 (kis/kiwoom/hybrid) | kis |
| `TRADING_MODE` | live/paper | paper |
| `TOTAL_BUDGET` | 총 투자 한도 (원) | 0 (계좌 기준) |
| `STOP_LOSS_PCT` | 기본 손절선 (%) | 3.5 |
| `TAKE_PROFIT_PCT` | 기본 익절선 (%) | 7.0 |
| `AI_PROVIDER` | gemini/claude/openai | gemini |
| `WATCH_STOCKS_KR` | 국내 감시 종목 (쉼표 구분) | 005930 |
| `WATCH_STOCKS_US` | 해외 감시 종목 (쉼표 구분) | AAPL |

## 텔레그램 봇 명령어

| 명령어 | 설명 |
|--------|------|
| `/status` | 시스템 상태 |
| `/balance` | 계좌 잔고 |
| `/positions` | 보유 종목 |
| `/watchlist` | 감시 종목 목록 |
| `/add 005930` | 감시 종목 추가 |
| `/remove 005930` | 감시 종목 제거 |
| `/performance` | 전략별 성과 |

## 전략 구조

```
CompositeStrategy (오케스트레이터)
├── MA Cross     — 이동평균 교차 (추세)
├── RSI          — 과매수/과매도 (반전)
├── MACD         — 모멘텀 전환
├── Bollinger+ATR — 밴드 반전 + 변동성
└── AI           — 거부권 + 보조 투표

투표 방식: 레짐별 가중치 → 최소 2개 전략 동의 → 분봉 모멘텀 보정
AI 역할: 종목 선정(스캐너)이 주력, 매매 판단은 보조
```

## 리스크 관리

- **ATR 동적 손절/익절**: 변동성에 따라 손절폭/익절폭 자동 조절
- **트레일링 스탑**: 수익 3% 이상 후 고점 대비 2% 하락 시 익절
- **연속 손절 쿨다운**: 3회 연속 손절 시 60분 매수 중단
- **일일 최대 손실**: 총 투자금 대비 3% 손실 시 당일 매수 중단
- **고변동성 차단**: ATR 5% 이상 종목 매수 차단
- **포지션 사이징**: ATR% 역비례 (고변동 → 소액, 저변동 → 다액)
- **장기 횡보 청산**: 3일 초과 보유 + ±1% 이내 → 자동 매도
