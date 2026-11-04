#!/bin/bash
# Chrome 啟動 native host 時環境變數很少,直接用專案 venv 的 python 絕對路徑執行。
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/../../.venv/bin/python3" "$DIR/server_control.py"
