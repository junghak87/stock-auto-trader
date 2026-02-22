#!/bin/bash
# Oracle Cloud Rocky Linux 서버 초기 설정 스크립트
# 사용법: bash setup.sh

set -e

echo "=== 시스템 업데이트 ==="
sudo dnf update -y

echo "=== Python 3.11 + git 설치 ==="
sudo dnf install -y python3.11 python3.11-pip git

echo "=== 타임존 설정 (Asia/Seoul) ==="
sudo timedatectl set-timezone Asia/Seoul

echo "=== Swap 2GB 생성 ==="
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "Swap 생성 완료"
else
    echo "Swap 이미 존재"
fi

echo "=== 프로젝트 설정 ==="
PROJECT_DIR="/home/rocky/stock-auto-trader"

if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR"
    python3.11 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt

    if [ ! -f .env ]; then
        cp .env.example .env
        echo ""
        echo "=== .env 파일 생성됨 ==="
        echo "nano .env 로 API 키를 입력하세요"
    fi

    echo "=== systemd 서비스 등록 ==="
    sudo cp deploy/stock-trader.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable stock-trader
else
    echo "프로젝트 디렉토리 없음: $PROJECT_DIR"
    echo "먼저 git clone 하세요"
fi

echo ""
echo "=== 설정 완료 ==="
echo "다음 단계:"
echo "  1. nano .env  # API 키, 텔레그램 토큰 입력"
echo "  2. source .venv/bin/activate && python main.py --once  # 1회 테스트"
echo "  3. sudo systemctl start stock-trader  # 서비스 시작"
echo "  4. sudo journalctl -u stock-trader -f  # 로그 확인"
