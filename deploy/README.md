# Oracle Cloud 배포 가이드

## 사전 준비
- Oracle Cloud 계정 (Always Free)
- SSH 키 페어

## Step 1: OCI 인스턴스 생성

1. [OCI 콘솔](https://cloud.oracle.com) 로그인
2. **Compute → Instances → Create Instance**
3. 설정:
   - **Shape**: `VM.Standard.E2.1.Micro` (1 OCPU, 1GB RAM)
   - **Image**: Rocky Linux 9 (x86_64)
   - **SSH Key**: 공개키 업로드
   - **Public IP**: Ephemeral public IP 할당
4. **Create** 클릭

## Step 2: SSH 접속 및 초기 설정

```bash
ssh -i ~/.ssh/<private_key> rocky@<PUBLIC_IP>

# 프로젝트 클론
cd /home/rocky
git clone https://github.com/junghak87/stock-auto-trader.git
cd stock-auto-trader

# 초기 설정 자동화
bash deploy/setup.sh
```

## Step 3: 환경변수 설정

```bash
nano .env
```

필수 항목:
- `KIS_PAPER_APP_KEY` / `KIS_PAPER_APP_SECRET` / `KIS_PAPER_ACCOUNT_NO`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY` (AI 전략 사용 시)

## Step 4: 테스트 실행

```bash
source .venv/bin/activate
python main.py --once
```

에러 없으면 서비스 시작:

```bash
sudo systemctl start stock-trader
```

## 일상 운영

### 로그 확인
```bash
sudo journalctl -u stock-trader -f        # 실시간
sudo journalctl -u stock-trader -n 100    # 최근 100줄
sudo journalctl -u stock-trader --since today
```

### 서비스 관리
```bash
sudo systemctl status stock-trader   # 상태
sudo systemctl restart stock-trader  # 재시작
sudo systemctl stop stock-trader     # 중지
```

### 코드 업데이트
```bash
bash deploy/deploy.sh
```

### 텔레그램 원격 명령
- `/status` — 시스템 상태
- `/balance` — 잔고 확인
- `/positions` — 보유 종목
- `/performance` — 전략 성과

