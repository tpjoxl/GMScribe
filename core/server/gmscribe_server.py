"""GMScribe 伺服器的啟動入口。

用這支檔案(而不是直接打 `uvicorn server.app:app`)啟動,是為了讓 process 的指令列帶著這支檔案的
絕對路徑:伺服器開關(native_host/server_control.py)關伺服器時,只找指令列含這個路徑的 process,
不會誤關電腦上其他也叫 `server.app:app` 的程式。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("server.app:app", host="127.0.0.1", port=8765)
