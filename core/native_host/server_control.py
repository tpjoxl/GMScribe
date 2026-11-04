"""Chrome Native Messaging host:讓擴充功能的 popup 可以啟動/關閉本機錄音伺服器。

Chrome 擴充功能本身不能執行本機程式,官方唯一的管道就是 Native Messaging:Chrome 依照
NativeMessagingHosts 底下的 manifest 啟動這支程式,透過 stdin/stdout 交換「4 位元組長度 + JSON」
格式的訊息。stdout 只能輸出協定訊息,伺服器本身的輸出一律導到 log 檔。

指令:{"cmd": "status" | "start" | "stop"} → 回 {"running": bool, ...}
安裝見 install.sh。
"""
import json
import os
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent  # core/
REPO = CORE.parent  # 專案最外層
(CORE / "logs").mkdir(exist_ok=True)
PID_FILE = CORE / "logs" / ".server.pid"
LOG_FILE = CORE / "logs" / "server.log"
PYTHON = REPO / ".venv" / "bin" / "python3"
SERVER_SCRIPT = CORE / "server" / "gmscribe_server.py"
HEALTH_URL = "http://127.0.0.1:8765/health"


def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def is_up():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def find_pids():
    """這個專案的伺服器 process(不只是這支程式啟動的,用 start_server.command 手動開的也算)。
    只比對指令列裡的 gmscribe_server.py 絕對路徑,不會關到其他專案的程式。"""
    out = subprocess.run(["pgrep", "-f", str(SERVER_SCRIPT)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


LOG_MAX_BYTES = 5 * 1024 * 1024


def start():
    if is_up():
        return {"running": True, "message": "伺服器已經在跑"}
    # 紀錄檔每次啟動都接著寫,處理進度條也會寫進來,不管的話會一直變大;超過上限就換一個新的、只留上一份
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
        LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    log = open(LOG_FILE, "ab")
    # Chrome 啟動 native host 時給的 PATH 很陽春(沒有 /opt/homebrew/bin),伺服器轉錄要呼叫的
    # ffmpeg 會找不到(2026-10-13 實測:上傳成功、轉錄第一步就失敗)。
    env = dict(os.environ)
    # /usr/sbin:Chrome 給的 PATH 也沒有,套件查 CPU 核心數(sysctl)會失敗、在紀錄裡留一串錯誤
    env["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin"), "/usr/sbin", "/sbin"])
    proc = subprocess.Popen(
        # nice:降低 CPU 優先順序,處理逐字稿時把 CPU 讓給使用者正在用的程式
        ["/usr/bin/nice", "-n", "10", str(PYTHON), str(SERVER_SCRIPT)],
        cwd=str(REPO),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # 跟 Chrome 脫鉤,這支 host 結束後伺服器繼續跑
    )
    PID_FILE.write_text(str(proc.pid))
    for _ in range(60):  # 載入套件要幾秒,最多等 30 秒
        if is_up():
            return {"running": True, "message": "伺服器已啟動"}
        if proc.poll() is not None:
            return {"running": False, "message": f"伺服器啟動失敗,請看 {LOG_FILE.name}"}
        time.sleep(0.5)
    return {"running": is_up(), "message": "伺服器啟動逾時,請看 server.log"}


def stop():
    for pid in find_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not find_pids():
            break
        time.sleep(0.5)
    PID_FILE.unlink(missing_ok=True)
    return {"running": is_up(), "message": "伺服器已關閉"}


def main():
    msg = read_message() or {}
    cmd = msg.get("cmd")
    try:
        if cmd == "start":
            send_message(start())
        elif cmd == "stop":
            send_message(stop())
        else:
            send_message({"running": is_up()})
    except Exception as e:  # noqa: BLE001
        send_message({"running": is_up(), "error": str(e)})


if __name__ == "__main__":
    main()
