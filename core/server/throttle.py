"""潤飾時的效能節流:讓 LLM 只用一部分顯示卡時間,把其餘留給使用者正在用的程式。

做法是「做一下、歇一下」:每產生約 0.2 秒的字,就停 busy × (1 − d) / d 秒,d 是允許使用的比例
(duty)。顯示卡同時要畫畫面,LLM 一直佔著它整台電腦就會頓;中間留空檔,畫面就排得進去。

d 的範圍由使用者在 popup 調整,但上下限由伺服器依當下狀況決定,每 10 秒重算:
- 上限:看 CPU 負載。電腦已經很忙時不讓使用者把潤飾開到全速。
- 記憶體吃緊(macOS `kern.memorystatus_level`,越低越吃緊)時反過來把**下限**拉高:模型約 6GB,
  歇著的空檔系統會把它擠到 swap,下一段要用時再從硬碟搬回來,越慢越常搬,整台電腦反而更卡。
  2026-11-02 實測:level 18、swap 用到 19.7/20GB,15% 時十分鐘只潤飾了 2 段;拉到 45% 三分鐘跑了 11 段。
  這時候盡快跑完、把模型釋放掉,對電腦的影響比較小。
- 下限:看時間。依目前每行實際花的運算時間,估算剩下的行要多久;照這個比例跑會超過時間預算
  (會議長度的 1.5 倍,至少 20 分鐘、最多 60 分鐘)就把下限往上拉,不讓它慢到跑不完。
- 兩者衝突(電腦很忙、會議又長)時以上限為準:寧可跑久一點,也不要把電腦拖垮。
預設跟著下限走(最不影響電腦);使用者選過之後用他選的值,但仍會被夾在上下限之間。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

ABS_MIN = 0.15
ABS_MAX = 1.0
_BURST_SEC = 0.2
_RECALC_SEC = 10.0
_DEFAULT_LINE_SEC = 2.0  # 還沒量到實際數字前,假設每行 LLM 要算 2 秒


def _memory_level() -> int | None:
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True, timeout=2
        ).stdout
        return int(out.strip())
    except Exception:  # noqa: BLE001
        return None


def memory_floor() -> float:
    level = _memory_level()
    if level is None or level >= 35:
        return ABS_MIN
    return 0.4 if level >= 20 else 0.6


def machine_cap() -> float:
    cap = 1.0
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        load = 0.0
    if load > 0.8:
        cap *= 0.75
    return max(ABS_MIN, cap)


def _round5(x: float) -> float:
    return round(x * 20) / 20


class PolishThrottle:
    def __init__(self, audio_seconds: float):
        self.budget_sec = min(max(audio_seconds * 1.5, 20 * 60), 60 * 60)
        self.total_lines = 0
        self.done_lines = 0
        self.busy_total = 0.0
        self.started_at = time.monotonic()
        self.user_duty: float | None = None
        self._lock = threading.Lock()
        self._range = (ABS_MIN, ABS_MAX)
        self._range_at = -1e9

    def start(self, total_lines: int) -> None:
        self.total_lines = total_lines
        self.started_at = time.monotonic()

    def line_done(self) -> None:
        self.done_lines += 1

    def set_user_duty(self, duty: float) -> None:
        with self._lock:
            self.user_duty = max(ABS_MIN, min(ABS_MAX, float(duty)))

    def _avg_line_sec(self) -> float:
        return self.busy_total / self.done_lines if self.done_lines >= 3 else _DEFAULT_LINE_SEC

    def range(self) -> tuple[float, float]:
        now = time.monotonic()
        if now - self._range_at >= _RECALC_SEC:
            floor = memory_floor()
            hi = max(machine_cap(), floor)
            remaining_work = max(0, self.total_lines - self.done_lines) * self._avg_line_sec()
            remaining_time = self.budget_sec - (now - self.started_at)
            need = ABS_MAX if remaining_time <= 60 else remaining_work / remaining_time
            lo = min(max(floor, need), hi)
            hi = _round5(hi)
            self._range = (min(max(ABS_MIN, _round5(lo)), hi), hi)
            self._range_at = now
        return self._range

    def duty(self) -> float:
        lo, hi = self.range()
        with self._lock:
            chosen = self.user_duty
        return lo if chosen is None else max(lo, min(hi, chosen))

    def pace(self, busy_sec: float) -> None:
        """剛用了 busy_sec 秒的運算,依目前比例歇一下。"""
        self.busy_total += busy_sec
        d = self.duty()
        if d < 1.0:
            time.sleep(busy_sec * (1 - d) / d)

    def snapshot(self) -> dict:
        lo, hi = self.range()
        d = self.duty()
        remaining = max(0, self.total_lines - self.done_lines) * self._avg_line_sec() / d
        return {
            "min": lo,
            "max": hi,
            "current": d,
            "user_set": self.user_duty is not None,
            "memory_tight": memory_floor() > ABS_MIN,
            "eta_seconds": round(remaining),
        }


def throttled_generate(stream, throttle: PolishThrottle | None) -> str:
    """把 mlx_lm.stream_generate 的輸出接起來;有 throttle 就每約 0.2 秒運算歇一下。"""
    parts: list[str] = []
    t0 = time.monotonic()
    for resp in stream:
        parts.append(resp.text)
        if throttle is not None:
            elapsed = time.monotonic() - t0
            if elapsed >= _BURST_SEC:
                throttle.pace(elapsed)
                t0 = time.monotonic()
    if throttle is not None:
        throttle.pace(time.monotonic() - t0)
    return "".join(parts)
