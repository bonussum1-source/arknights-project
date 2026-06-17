#!/bin/bash
set -e

cd "$(dirname "$0")"

# Install dependencies if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  pip install -r requirements.txt
fi

# Check if DB has data
STORY_COUNT=$(python3 -c "
import sqlite3, pathlib
db = pathlib.Path('data/arknights.db')
if db.exists():
    c = sqlite3.connect(db)
    print(c.execute('SELECT COUNT(*) FROM stories').fetchone()[0])
else:
    print(0)
" 2>/dev/null || echo 0)

if [ "$STORY_COUNT" -eq "0" ]; then
  echo ""
  echo "============================================"
  echo " 데이터베이스가 비어 있습니다."
  echo " 스토리를 가져오려면 별도 터미널에서 실행:"
  echo ""
  echo "   python3 ingest.py"
  echo "   # GitHub 토큰 있으면 (속도 향상):"
  echo "   python3 ingest.py --token YOUR_TOKEN"
  echo "============================================"
  echo ""
fi

HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8000}

echo "서버 시작: http://localhost:$PORT"
echo "모바일: http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo 'YOUR_IP'):$PORT"
echo ""

python3 -m uvicorn main:app --host "$HOST" --port "$PORT" --reload
