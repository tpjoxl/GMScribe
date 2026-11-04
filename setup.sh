#!/bin/bash
# GMScribe 一鍵安裝。可以重複執行:已經裝好的步驟會自動跳過。
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ask() { # ask "問題" 預設(y/n) → 回傳 0 = yes
  local hint="[y/N]"; [ "$2" = y ] && hint="[Y/n]"
  read -r -p "$1 $hint " ans
  ans="${ans:-$2}"
  [[ "$ans" =~ ^[Yy] ]]
}

bold "1/6 檢查系統"
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "GMScribe 只支援 Apple Silicon(M 系列晶片)的 Mac。"; exit 1
fi
if ! command -v brew >/dev/null; then
  echo "需要先安裝 Homebrew:https://brew.sh ,裝好後再執行一次這個腳本。"; exit 1
fi
if [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "⚠️  找不到 Google Chrome。GMScribe 只支援 Google Chrome,請先安裝。"
fi
echo "✓ macOS / Apple Silicon"

bold "2/6 安裝 Python 3.13 與 ffmpeg"
command -v python3.13 >/dev/null || brew install python@3.13
command -v ffmpeg >/dev/null || brew install ffmpeg
echo "✓ python3.13、ffmpeg"

bold "3/6 安裝伺服器套件(第一次會比較久)"
[ -d .venv ] || python3.13 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r core/server/requirements.txt
# silero-vad 一定要分開、用 --no-deps 裝(原因見 core/server/requirements.txt 的註解)
.venv/bin/pip install -q --no-deps silero-vad==6.2.2 onnxruntime==1.30.0
[ -f glossary.txt ] || cp glossary.example.txt glossary.txt
echo "✓ 套件安裝完成"

bold "4/6 要不要分辨「其他與會者」各自是誰在講話?"
echo "  要:3 人以上的會議,每一句都會標出是哪位與會者說的。需要一個免費的 Hugging Face 帳號(一次性設定)。"
echo "  不要:你自己說的話一樣會標你的名字;只有一位其他與會者時也會標對方名字;"
echo "        但 3 人以上的會議,其他與會者的發言沒辦法可靠地分出是誰。之後想開再重跑這個腳本即可。"
DIARIZE=n
if ask "要分辨不同的與會者嗎?" y; then
  DIARIZE=y
  if .venv/bin/hf auth whoami >/dev/null 2>&1; then
    echo "✓ 已經登入 Hugging Face"
  else
    echo
    echo "  請依序完成(會幫你打開網頁):"
    echo "  ① 註冊或登入 Hugging Face"
    echo "  ② 在模型頁面按「Agree and access repository」同意使用條款"
    echo "  ③ 建立一個 Read 權限的 token,複製起來"
    open "https://huggingface.co/pyannote/speaker-diarization-community-1"
    open "https://huggingface.co/settings/tokens"
    read -r -p "  完成 ①②③ 後按 Enter,接著貼上 token(畫面不會顯示,是正常的)..."
    .venv/bin/hf auth login
  fi
fi

bold "5/6 載入 Chrome 擴充功能"
echo "  ① 在 Chrome 網址列輸入 chrome://extensions ,打開右上角「開發人員模式」"
echo "  ② 按「載入未封裝項目」,選擇這個資料夾:"
echo "       $ROOT/extension"
echo "  ③ 載入後會自動打開麥克風授權頁,在跳出的視窗按「造訪這個網站時允許」(不要選「這次允許」)"
read -r -p "  完成後按 Enter 繼續..."
./core/native_host/install.sh
echo "  (如果上面顯示的 ID 跟 chrome://extensions 上的不一樣,請執行 ./core/native_host/install.sh <正確的 ID>)"

bold "6/6 預先下載模型(約 6GB)"
echo "  不下載的話,第一場會議處理時會自動下載,那次會等比較久。"
if ask "現在先下載嗎?" y; then
  DIARIZE="$DIARIZE" .venv/bin/python3 - <<'PY'
import os, sys
sys.path.insert(0, "core")
from huggingface_hub import snapshot_download
from server import transcriber as t
for repo in (t.MODEL_REPO, t.POLISH_MODEL):
    print("下載", repo); snapshot_download(repo)
if os.environ.get("DIARIZE") == "y":
    print("下載", t.DIARIZATION_MODEL)
    try:
        snapshot_download(t.DIARIZATION_MODEL, revision=t.DIARIZATION_REVISION)
    except Exception as e:  # noqa: BLE001
        print("⚠️  語者分離模型下載失敗(多半是還沒在模型頁面按同意,或 token 不對):", e)
PY
fi

bold "🎉 安裝完成"
echo "  開會時:點 Chrome 右上角的 GMScribe 圖示 →「啟動錄音伺服器」→「開始錄音」。"
echo "  使用前請先看 README 的「使用聲明與法律責任」。"
