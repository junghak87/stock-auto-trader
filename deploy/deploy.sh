#!/bin/bash
# 코드 업데이트 및 서비스 재시작 스크립트
# 사용법: bash deploy.sh

set -e

cd /home/rocky/stock-auto-trader

echo "=== Git Pull ==="
git pull origin main

echo "=== 의존성 업데이트 ==="
source .venv/bin/activate
pip install -r requirements.txt

echo "=== 서비스 재시작 ==="
sudo systemctl restart stock-trader

echo "=== 상태 확인 ==="
sleep 3
sudo systemctl status stock-trader --no-pager

echo ""
echo "배포 완료: $(date '+%Y-%m-%d %H:%M:%S')"
