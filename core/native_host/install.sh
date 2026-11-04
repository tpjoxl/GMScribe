#!/bin/bash
# 安裝 Native Messaging host,讓擴充功能可以啟動/關閉錄音伺服器。只需要做一次
# (擴充功能資料夾搬家、或擴充功能 ID 改變時要重跑)。
# 用法:./core/native_host/install.sh [擴充功能ID]
# 擴充功能 ID 在 chrome://extensions(開發人員模式)上看得到;不給的話用「未封裝擴充功能」
# 依資料夾路徑算出來的 ID。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_PATH="$(cd "$DIR/../../extension" && pwd)"
if [ -n "$1" ]; then
  EXT_ID="$1"
else
  EXT_ID="$(python3 -c "import hashlib,sys;h=hashlib.sha256(sys.argv[1].encode()).hexdigest()[:32];print(''.join(chr(97+int(c,16)) for c in h))" "$EXT_PATH")"
fi
TARGET_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
mkdir -p "$TARGET_DIR"
cat > "$TARGET_DIR/com.gmscribe.server_control.json" <<JSON
{
  "name": "com.gmscribe.server_control",
  "description": "GMScribe 本機伺服器開關",
  "path": "$DIR/server_control.sh",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON
# 伺服器用這個檔案判斷「只接受這個擴充功能」的請求(見 server/app.py 的 only_local_extension)
echo "$EXT_ID" > "$DIR/.extension_id"
echo "已安裝,擴充功能 ID:$EXT_ID"
echo "如果跟 chrome://extensions 上顯示的 ID 不一樣,請用 ./core/native_host/install.sh <正確ID> 重跑。"
