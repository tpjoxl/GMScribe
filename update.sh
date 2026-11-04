#!/bin/bash
# GMScribe 一鍵更新:下載 GitHub 上的最新版、更新套件、重啟伺服器。
# 你的錄音(recordings/)、錯字對照表(glossary.txt)不會被動到。
set -e
cd "$(dirname "$0")"

if ! command -v git >/dev/null || [ ! -d .git ]; then
  echo "這個資料夾不是用 git clone 下載的,沒辦法自動更新。請到 GitHub 下載最新版。"; exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠️  你改過專案裡的程式檔,直接更新可能會蓋掉或衝突。改過的檔案:"
  git status --short --untracked-files=no
  echo "要放棄這些修改的話:git checkout -- <檔名>,再重新執行 ./update.sh。"
  exit 1
fi

OLD=$(git rev-parse --short HEAD)
echo "下載最新版..."
git pull --ff-only
NEW=$(git rev-parse --short HEAD)
if [ "$OLD" = "$NEW" ]; then
  echo "✓ 已經是最新版"; exit 0
fi

echo "更新伺服器套件..."
.venv/bin/pip install -q -r core/server/requirements.txt
# silero-vad 一定要分開、用 --no-deps 裝(原因見 core/server/requirements.txt 的註解)
.venv/bin/pip install -q --no-deps silero-vad==6.2.2 onnxruntime==1.30.0

# 沿用原本的擴充功能 ID 重裝 Native Messaging host(路徑或設定有變時才會用到)
if [ -f core/native_host/.extension_id ]; then
  ./core/native_host/install.sh "$(cat core/native_host/.extension_id)" >/dev/null
fi

# 伺服器開著的話重啟,讓新程式生效。正在處理逐字稿時重啟會中斷那場的處理,所以先問。
if (cd core/native_host && ../../.venv/bin/python -c "import server_control as s, sys; sys.exit(0 if s.is_up() else 1)"); then
  read -r -p "伺服器正在執行,要重啟讓新版生效嗎?(如果還有逐字稿在處理,請先等它完成)[Y/n] " ans
  if [[ ! "$ans" =~ ^[Nn] ]]; then
    (cd core/native_host && ../../.venv/bin/python -c "import server_control as s; s.stop(); print(s.start().get('message'))")
  fi
fi

echo
echo "🎉 已更新($OLD → $NEW)"
echo "最後一步:到 Chrome 網址列輸入 chrome://extensions ,按 GMScribe 卡片上的重新整理按鈕(↻)。"
