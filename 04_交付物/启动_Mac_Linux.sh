#!/usr/bin/env bash
# 瓯单通 一键启动（macOS / Linux）
cd "$(dirname "$0")/ou_dan_tong" || exit 1

echo ""
echo "  ============================================"
echo "    OU DAN TONG / 瓯单通"
echo "  ============================================"
echo ""
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ERROR] Python 3.10+ not found."
  echo "Install from https://www.python.org/downloads/"
  exit 1
fi

echo "  Starting local service ..."
echo "  Then open:  http://127.0.0.1:8000"
echo ""
echo "  Keep this terminal OPEN while using it."
echo "  Press Ctrl+C to STOP."
echo "  ============================================"
echo ""

exec "$PY" web_app.py
