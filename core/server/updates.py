"""檢查 GitHub 上有沒有新版本。

只送一個「最新版本號是多少」的查詢給 GitHub(不送任何會議資料),一天最多問一次。
在專案最外層放一個 `.no-update-check` 檔案就會關掉(伺服器是從 Chrome 啟動的,使用者終端機裡設的環境變數
傳不進來,所以用檔案);環境變數 GMSCRIBE_NO_UPDATE_CHECK=1 或 UPDATE_REPO 是空字串時也不會檢查。
Meet 改版造成講者偵測失效時,使用者能做的就是更新到修好的版本,所以 popup 會在有新版時提示。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

# GitHub 上的「帳號/專案」;fork 出去自己維護的話改成自己的
UPDATE_REPO = "tpjoxl/GMScribe"
_REPO_DIR = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_DIR / "extension" / "manifest.json"
_OPT_OUT_FILE = _REPO_DIR / ".no-update-check"
_CHECK_INTERVAL_SEC = 24 * 3600
_state: dict = {"latest": None, "checked_at": 0.0}
_lock = threading.Lock()


def current_version() -> str:
    try:
        return json.loads(_MANIFEST.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "0.0.0"


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _enabled() -> bool:
    return (
        bool(UPDATE_REPO)
        and os.environ.get("GMSCRIBE_NO_UPDATE_CHECK") != "1"
        and not _OPT_OUT_FILE.exists()
    )


def _fetch_latest() -> str | None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "GMScribe-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("tag_name")
    except Exception:  # noqa: BLE001  網路不通、還沒發佈過 release 都當作沒有新版
        return None


def check_in_background() -> None:
    if not _enabled():
        return
    with _lock:
        if time.time() - _state["checked_at"] < _CHECK_INTERVAL_SEC:
            return
        _state["checked_at"] = time.time()

    def run() -> None:
        latest = _fetch_latest()
        if latest:
            _state["latest"] = latest

    threading.Thread(target=run, daemon=True).start()


def status() -> dict:
    check_in_background()
    current, latest = current_version(), _state["latest"]
    return {
        "current": current,
        "latest": latest,
        "update_available": bool(latest) and _parse(latest) > _parse(current),
    }
