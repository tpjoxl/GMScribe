#!/bin/bash
# 雙擊這個檔案就會啟動本機逐字稿伺服器。
# 關掉這個終端機視窗就等於停止伺服器,不會一直佔用背景資源。
cd "$(dirname "$0")"
echo "啟動 GMScribe 伺服器..."
# nice:降低 CPU 優先順序,處理逐字稿時電腦比較不會卡
nice -n 10 .venv/bin/python3 "$(pwd)/core/server/gmscribe_server.py"
