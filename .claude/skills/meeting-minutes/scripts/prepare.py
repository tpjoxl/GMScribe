#!/usr/bin/env python3
"""meeting-minutes skill 的前置檢查:列出最近的會議資料夾,並掃描逐字稿/與會者名單裡可疑的內容。

逐字稿的文字來自會議裡任何人講的話,與會者名單來自每個人自己取的 Meet 顯示名稱——兩者都是外部可控的
輸入,可能被拿來做 prompt injection(對讀逐字稿的 AI 下指令)。這支程式用固定規則(不靠 AI 判斷)
找出看起來像指令、網址、檔案路徑、系統指令的片段,讓 Claude 跟使用者在整理前先看到。
規則只能抓到常見寫法,抓不到不代表安全;抓到也不一定是攻擊(會議本來就可能討論網址或指令)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

RECENT = 5
MAX_HITS_PER_FILE = 20

PATTERNS = [
    ("像是對 AI 下指令", re.compile(
        r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|prior|above|earlier)|"
        r"(system|developer)\s*prompt|you\s+are\s+now|new\s+instructions?|"
        r"(忽略|無視|忘記|忘掉).{0,8}(指示|指令|規則|設定|提示)|系統提示|你現在是|新的指示|"
        r"(請|幫我|立刻|馬上).{0,6}(執行|下載|上傳|傳送|寄出|刪除).{0,12}(檔案|指令|資料夾|金鑰|密碼|token)",
        re.IGNORECASE)),
    ("網址", re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)),
    ("檔案路徑或機密檔", re.compile(
        r"~/|/Users/|/etc/|/home/|\.ssh\b|id_rsa|\.env\b|\.aws\b|credentials?\b|api[_ -]?key", re.IGNORECASE)),
    ("系統指令", re.compile(
        r"\b(curl|wget|rm\s+-rf|chmod|sudo|base64|powershell|bash\s+-c|python3?\s+-c|eval)\b", re.IGNORECASE)),
]


def scan_text(label: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, pat in PATTERNS:
            m = pat.search(line)
            if m:
                start = max(0, m.start() - 30)
                snippet = line[start : m.end() + 30].replace("`", "'")
                hits.append(f"  - {label} 第 {lineno} 行【{kind}】…{snippet}…")
                break
        if len(hits) >= MAX_HITS_PER_FILE:
            hits.append(f"  - {label}:可疑內容太多,只列前 {MAX_HITS_PER_FILE} 筆")
            break
    return hits


def main() -> None:
    root = Path.cwd() / "recordings"
    if not root.is_dir():
        print("找不到 recordings/ 資料夾(要在專案根目錄執行)。")
        return
    folders = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    if not folders:
        print("recordings/ 裡還沒有任何會議。")
        return

    print(f"最近的 {min(RECENT, len(folders))} 場會議(新到舊):\n")
    for d in folders[:RECENT]:
        transcript = d / "逐字稿.txt"
        if not transcript.exists():
            transcript = d / "處理過程" / "逐字稿_潤飾前.txt"
        minutes = sorted(p.name for p in d.glob("會議紀錄_*.md"))
        print(f"### {d.name}")
        print(f"- 逐字稿:{transcript.name if transcript.exists() else '(沒有,可能還在處理或處理失敗)'}")
        print(f"- 已有會議紀錄:{', '.join(minutes) if minutes else '沒有'}")

        hits: list[str] = []
        roster = d / "處理過程" / "roster_input.json"
        if roster.exists():
            try:
                data = json.loads(roster.read_text(encoding="utf-8"))
                names = [data.get("mic_speaker_name") or ""] + list(data.get("other_names") or [])
                print(f"- 與會者:{'、'.join(n for n in names if n)}")
                hits += scan_text("與會者名單", "\n".join(str(n) for n in names))
            except (ValueError, OSError):
                print("- 與會者:roster_input.json 讀取失敗")
        if transcript.exists():
            hits += scan_text(transcript.name, transcript.read_text(encoding="utf-8", errors="replace"))
        if hits:
            print("- ⚠️ 可疑內容(規則比對,請讓使用者確認):")
            print("\n".join(hits))
        else:
            print("- 可疑內容:沒有比對到")
        print()


if __name__ == "__main__":
    sys.exit(main())
