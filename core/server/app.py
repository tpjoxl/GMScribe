"""
本機逐字稿伺服器。只在你自己的 Mac 上跑,不對外開放。
Chrome 擴充功能錄完會議音訊後,把「麥克風」跟「Meet 分頁」兩軌分開 POST 到這裡,
伺服器用 Whisper 轉錄 + pyannote 分離出每個人的聲音,合併成一份標好講者的逐字稿。
啟動:.venv/bin/uvicorn server.app:app --port 8765
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import transcriber, updates
from .throttle import ABS_MAX, ABS_MIN, PolishThrottle

REPO_DIR = Path(__file__).resolve().parents[2]
RECORDINGS_DIR = REPO_DIR / "recordings"

# 每場會議資料夾:最外層只放使用者要看的結果,處理過程(原始錄音、中間結果、講者偵測資料)放進子資料夾。
# 原始錄音要留著(重跑的唯一來源),講者資料要留著(講者標錯時查原因用),所以不刪,只是收起來。
TRANSCRIPT_FILE = "逐字稿.txt"
WORK_DIR = "處理過程"
MIC_AUDIO = "錄音_我的麥克風.webm"
TAB_AUDIO = "錄音_其他與會者.webm"
RECORDINGS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="GMScribe")

# 只允許本機、而且只允許擴充功能(或沒有 Origin 的本機程式,例如 curl)呼叫。
# 光靠 CORS 擋不住:multipart 表單是「簡單請求」,瀏覽器會照樣送出,只是不讓網頁讀回應——
# 任何你開著的網頁都能偷偷 POST /upload 塞檔案、觸發耗運算的工作。所以在伺服器端直接拒絕:
# - Origin 不是 chrome-extension:// 的請求(一般網頁)
# - Host 不是 localhost/127.0.0.1 的請求(防 DNS rebinding:惡意網域解析到 127.0.0.1)
# - 跑過 native_host/install.sh 的話,只接受那一個擴充功能 ID(其他擴充功能也打不進來)
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
_EXTENSION_ID_FILE = Path(__file__).resolve().parent.parent / "native_host" / ".extension_id"


def _origin_allowed(origin: str | None) -> bool:
    if origin is None:  # curl、本機腳本
        return True
    try:
        ext_id = _EXTENSION_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        ext_id = ""
    return origin == f"chrome-extension://{ext_id}" if ext_id else origin.startswith("chrome-extension://")


@app.middleware("http")
async def only_local_extension(request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    origin = request.headers.get("origin")
    if host not in _ALLOWED_HOSTS or not _origin_allowed(origin):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# 記憶體內的任務狀態,伺服器重啟就會消失(這個工具不需要長期任務佇列)
jobs: dict[str, dict] = {}
# 正在潤飾的工作的節流器(見 throttle.py),popup 用 /polish-speed 調整、/status 回報目前範圍
polish_throttles: dict[str, PolishThrottle] = {}
# 還沒進到潤飾階段時,popup 先選好的速度(0.15~1.0),開始潤飾時套用
pending_polish_duty: dict[str, float] = {}
# 一次只處理一場:連續兩場會議時,第二場要等第一場處理完。兩場同時跑會同時載入兩份模型
# (16GB 的機器會開始大量 swap),而且先跑完的那場會呼叫 release_models() 把另一場正在用的模型放掉。
_job_lock = threading.Lock()


def _run_transcription(job_id: str, *args) -> None:
    if _job_lock.locked():
        jobs[job_id]["stage"] = "排隊中:等上一場會議處理完才會開始"
    with _job_lock:
        jobs[job_id]["started_at"] = time.time()
        try:
            _run_transcription_now(job_id, *args)
        finally:
            pending_polish_duty.pop(job_id, None)


def _run_transcription_now(
    job_id: str,
    mic_path: Path,
    tab_path: Path,
    out_dir: Path,
    speaker_timeline: list[dict],
    mic_speaker_name: str,
    other_names: list[str],
) -> None:
    work = out_dir / WORK_DIR

    def set_stage(stage: str, percent: int) -> None:
        # 用 update 而不是整個蓋掉,才不會弄丟 started_at——/status 要靠它算「已經處理多久」。
        jobs[job_id]["stage"] = stage
        jobs[job_id]["percent"] = percent

    try:
        set_stage("轉錄你的麥克風音軌", 5)
        mic_result = transcriber.transcribe(mic_path)
        (work / "轉錄_我的麥克風.txt").write_text(mic_result.text, encoding="utf-8")
        mic_low_conf = mic_result.low_confidence
        # 只留下 Meet 自己也偵測到使用者在講話的時段(見 filter_mic_by_self_activity 的說明),
        # 被丟掉的內容另外存一份,事後可以檢查有沒有誤丟。
        mic_result, mic_dropped = transcriber.filter_mic_by_self_activity(mic_result, speaker_timeline)
        if mic_dropped:
            (work / "被濾掉的麥克風內容.txt").write_text(
                "\n".join(f"[{s.start:.1f}-{s.end:.1f}s] {s.text}" for s in mic_dropped), encoding="utf-8"
            )
        mic_labeled = transcriber.label_mic_segments(mic_result, speaker_name=mic_speaker_name)

        set_stage("轉錄其他與會者的音軌", 30)
        tab_result = transcriber.transcribe(tab_path)
        (work / "轉錄_其他與會者.txt").write_text(tab_result.text, encoding="utf-8")
        # Whisper 自己都沒把握的片段(多半是雜音被猜成亂碼)不進逐字稿,存一份事後可以檢查。
        low_conf = [("我的麥克風", seg) for seg in mic_low_conf] + [("其他與會者", seg) for seg in tab_result.low_confidence]
        if low_conf:
            (work / "被濾掉的低信心片段.txt").write_text(
                "\n".join(f"[{track} {seg.start:.1f}-{seg.end:.1f}s 信心 {seg.avg_logprob:.2f}] {seg.text}" for track, seg in low_conf),
                encoding="utf-8",
            )

        if len(other_names) == 1:
            # 從 Meet 名單直接知道「其他與會者」只有一個人,不需要語者分離、
            # 也不需要字幕時間軸比對,整條 tab 軌直接標成這個人的真名就好,
            # 完全不依賴任何 Meet DOM 的「誰在什麼時候講話」訊號。
            tab_labeled = transcriber.label_mic_segments(tab_result, speaker_name=other_names[0])
        else:
            set_stage("辨識每個人的聲音(語者分離)", 55)
            tab_wav = tab_path.with_suffix(".wav")
            num_speakers = len(other_names) or None
            try:
                turns, overlap_regions = transcriber.diarize(tab_wav, num_speakers=num_speakers)
            except Exception as diarization_error:  # noqa: BLE001
                # 語者分離失敗(例如還沒設定 Hugging Face token)不應該讓整個轉錄失敗,
                # 退回成「其他與會者」整段不分人。
                turns, overlap_regions = [], []
                (work / "diarization_error.txt").write_text(
                    str(diarization_error), encoding="utf-8"
                )
            (work / "tab_diarization.json").write_text(
                json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (work / "tab_overlap.json").write_text(
                json.dumps(overlap_regions, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            caption_log_path = work / "caption_log.json"
            caption_log = (
                json.loads(caption_log_path.read_text(encoding="utf-8")) if caption_log_path.exists() else []
            )
            tab_labeled = transcriber.label_tab_segments(
                tab_result,
                turns,
                speaker_timeline,
                known_names=other_names,
                overlap_regions=overlap_regions,
                caption_log=caption_log,
            )

        set_stage("整理逐字稿格式", 60)
        merged_text = transcriber.merge_and_format(mic_labeled, tab_labeled)
        merged_text = transcriber.apply_glossary(merged_text)
        (work / "逐字稿_潤飾前.txt").write_text(merged_text, encoding="utf-8")

        display_text = merged_text
        # 潤飾要載入 7B 的 LLM,先把已經用完的 Whisper/pyannote/VAD 放掉,不要幾個模型同時佔著記憶體
        # (16GB 的機器上同時還開著 Chrome、VS Code、Docker,疊在一起會開始大量 swap,整台電腦變慢)。
        transcriber.release_models()
        try:
            set_stage("潤飾逐字稿(繁體中文、語句通順)——通常是最花時間的一步", 65)

            def report_polish_progress(done: int, total: int) -> None:
                # 潤飾是逐批處理的,把這批進度換算成 65~100 這段區間,不然使用者
                # 會盯著同一個百分比卡好幾分鐘,尤其長會議動輒切成十幾批。
                pct = 65 + round(35 * done / total) if total else 65
                set_stage(f"潤飾逐字稿(第 {done}/{total} 段)", pct)

            audio_seconds = max(
                [seg.end for seg in mic_result.segments + tab_result.segments] or [0.0]
            )
            throttle = PolishThrottle(audio_seconds)
            if job_id in pending_polish_duty:
                throttle.set_user_duty(pending_polish_duty.pop(job_id))
            polish_throttles[job_id] = throttle
            try:
                polished_text = transcriber.polish_transcript(
                    merged_text, on_progress=report_polish_progress, throttle=throttle
                )
            finally:
                polish_throttles.pop(job_id, None)
            display_text = polished_text
        except Exception as polish_error:  # noqa: BLE001
            # 後製失敗(例如模型下載失敗)不應該讓整個轉錄失敗,退回顯示未整理過的逐字稿。
            (work / "polish_error.txt").write_text(str(polish_error), encoding="utf-8")

        # 潤飾失敗時,最外層的逐字稿就放潤飾前的版本,使用者一樣只要看這一份。
        (out_dir / TRANSCRIPT_FILE).write_text(display_text, encoding="utf-8")

        # 轉錄用的 16kHz wav 是中間檔,佔一場會議約三分之二的空間(每分鐘約 3.8MB),處理完就刪;
        # 原始 webm 留著,要重跑時 transcribe() 會重新轉一次。
        for wav in (mic_path.with_suffix(".wav"), tab_path.with_suffix(".wav")):
            wav.unlink(missing_ok=True)

        warnings = _speaker_signal_warnings(work, speaker_timeline, other_names, bool(tab_result.segments))
        signal_issues = _signal_health(work, speaker_timeline, other_names, bool(tab_result.segments))

        elapsed = round(time.time() - jobs[job_id].get("started_at", time.time()))
        jobs[job_id] = {
            "warnings": warnings,
            # 不直接提醒使用者(他也沒辦法處理,壞掉的訊號程式已經自動不採用了);popup 只在剛好有新版時
            # 提示「更新後可能修好」。內容寫在 處理過程/訊號檢查.txt,回報問題時附上。
            "signal_issues": signal_issues,
            "status": "done",
            "text": display_text,
            "dir": str(out_dir),
            "elapsed_seconds": elapsed,
        }
    except Exception as e:  # noqa: BLE001
        jobs[job_id] = {"status": "error", "error": str(e), "stage": jobs.get(job_id, {}).get("stage")}
    finally:
        # 不管這次工作成功還是失敗,都要釋放模型佔用的記憶體——實測真的因為模型常駐
        # 沒釋放,疊加機器上其他本來就很吃記憶體的工具(Docker、IDE),把記憶體榨乾到
        # 觸發 kernel panic 強制重開機。詳見 transcriber.release_models() 的說明。
        transcriber.release_models()


def _speaker_signal_warnings(
    work: Path, speaker_timeline: list[dict], other_names: list[str], others_spoke: bool
) -> list[str]:
    """處理完檢查講者訊號有沒有缺。Meet 改版時擴充功能常常安靜地抓不到字幕或名單,逐字稿照樣產出、只是講者
    分得比較差——不提醒的話使用者要自己發現(2026-10-29 那場就是整場沒有字幕資料)。"""
    warnings = []
    # 只有自己一個人(例如自己錄音測試)時,其他人那一軌沒有內容,名單是空的很正常,不用提醒。
    if not other_names and others_spoke:
        warnings.append("沒有抓到與會者名單,其他人會顯示成「講者 N」。")
    try:
        captions = json.loads((work / "caption_log.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        captions = []
    if len(other_names) >= 2 and not captions:
        warnings.append("這場沒有收到任何字幕資料,講者可能分得不準(常見原因是 Google Meet 改版)。")
    if len(other_names) >= 2 and not speaker_timeline:
        warnings.append("這場沒有「誰在講話」的紀錄,其他人的發言無法對上名字。")
    return warnings


def _signal_health(work: Path, speaker_timeline: list[dict], other_names: list[str], others_spoke: bool) -> list[str]:
    """逐項檢查擴充功能讀到的每個訊號來源有沒有在運作,寫成 處理過程/訊號檢查.txt,回傳異常的重要項目。

    為什麼要有:講者判斷同時用好幾個訊號(字幕、名單燈號、視訊格燈號、靜音狀態),其中一個壞掉時,
    其他訊號會把結果撐住,逐字稿看起來還可以,問題就一直沒被發現——2026-10 中到 11-03 視訊格的
    講話訊號整個失效(每一格都被當成自己),就是這樣拖了快三週。每場都逐項檢查,壞掉當場就看得到。
    """
    try:
        debug = json.loads((work / "speaker_debug.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        debug = []
    try:
        captions = json.loads((work / "caption_log.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        captions = []
    heartbeats = [e for e in debug if e.get("event") == "heartbeat"]
    tile_events = [e for e in debug if e.get("src") == "tile" and "isSelf" in e]
    panel_events = [e for e in debug if "isSelf" in e and e.get("src") != "tile"]
    speakers = {e.get("speaker") for e in speaker_timeline}
    many_others = len(other_names) >= 2 and others_spoke

    def lit(events: list[dict]) -> int:
        return sum(1 for e in events if e.get("to") and "gjg47c" not in e["to"])

    def share(key: str) -> float:
        return sum(1 for h in heartbeats if h.get(key)) / len(heartbeats) if heartbeats else 0.0

    checks: list[tuple[bool, str, bool]] = []  # (正常?, 說明, 異常時要不要提醒使用者)
    if many_others:
        checks.append((share("captionRegionFound") >= 0.8, f"字幕區塊:{share('captionRegionFound'):.0%} 的時間找得到", True))
        checks.append((len(captions) > 0, f"字幕內容:{len(captions)} 段", True))
        checks.append((share("panelRowCount") >= 0.8, f"與會者名單:{share('panelRowCount'):.0%} 的時間找得到", True))
        checks.append((lit(panel_events) > 0, f"名單上的講話燈號:亮過 {lit(panel_events)} 次", False))
    if heartbeats and share("mainStageFound") >= 0.5 and many_others:
        self_tiles = {e.get("id") for e in tile_events if e.get("isSelf")}
        named = sum(1 for e in tile_events if e.get("name") and not e["name"].startswith("("))
        checks.append((len(self_tiles) <= 1, f"視訊格「哪一格是自己」:{len(self_tiles)} 格被當成自己(應該 ≤1)", True))
        checks.append((not tile_events or named / len(tile_events) >= 0.5,
                       f"視訊格名字:{named}/{len(tile_events)} 筆讀得到", True))
        checks.append((lit(tile_events) > 0, f"視訊格的講話燈號:亮過 {lit(tile_events)} 次", True))
        has_muted = any((sp or "").startswith(transcriber.MUTED_TIMELINE_PREFIX) for sp in speakers)
        checks.append((has_muted, "其他人的靜音狀態:" + ("有記錄" if has_muted else "沒有記錄"), True))
    if heartbeats:
        checks.append((transcriber.UNMUTED_TIMELINE_SPEAKER in speakers or share("micUnmuted") == 0,
                       "你的麥克風開關:" + ("有記錄" if transcriber.UNMUTED_TIMELINE_SPEAKER in speakers else "整場靜音或沒記錄"), False))
        checks.append((transcriber.SELF_TIMELINE_SPEAKER in speakers, "你自己的講話燈號:" +
                       ("有記錄" if transcriber.SELF_TIMELINE_SPEAKER in speakers else "沒有記錄"), False))
        checks.append((share("callEndFound") >= 0.8, f"掛斷按鈕:{share('callEndFound'):.0%} 的時間找得到", False))
    errors = [e for e in debug if e.get("event") == "error"]
    checks.append((not errors, f"擴充功能執行錯誤:{len(errors)} 次" + (f"(例如 {errors[0].get('where')}: {errors[0].get('message')})" if errors else ""), True))

    lines = [("✅ " if ok else "⚠️ ") + text for ok, text, _ in checks]
    (work / "訊號檢查.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [text for ok, text, notify in checks if not ok and notify]


def _new_meeting_dir(title: str) -> Path:
    """「2026-10-29 10.21 會議名稱」。分頁標題是「Meet - xxx」,前綴拿掉;同一分鐘撞名就補秒數。"""
    now = datetime.now()
    title = re.sub(r"^Meet\s*[-–—]\s*", "", title or "").strip()
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-.()")[:60].strip() or "會議"
    out_dir = RECORDINGS_DIR / f"{now:%Y-%m-%d %H.%M} {safe_title}"
    if out_dir.exists():
        out_dir = RECORDINGS_DIR / f"{now:%Y-%m-%d %H.%M.%S} {safe_title}"
    return out_dir


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict:
    return updates.status()


@app.post("/upload")
async def upload(
    mic_audio: UploadFile = File(...),
    tab_audio: UploadFile = File(...),
    title: str = Form("meeting"),
    speaker_timeline: UploadFile | str = Form("[]"),
    speaker_debug: UploadFile | str = Form("[]"),
    caption_log: UploadFile | str = Form("[]"),
    mic_speaker_name: str = Form("你"),
    other_names: str = Form("[]"),
) -> JSONResponse:
    # 這三個欄位擴充功能用檔案形式送(文字欄位有 1MB 上限,長會議會超過);手動用 curl 測時
    # 直接給字串也可以。
    async def as_text(v: UploadFile | str) -> str:
        return v if isinstance(v, str) else (await v.read()).decode("utf-8")

    speaker_timeline = await as_text(speaker_timeline)
    speaker_debug = await as_text(speaker_debug)
    caption_log = await as_text(caption_log)
    job_id = str(uuid.uuid4())
    out_dir = _new_meeting_dir(title)
    work = out_dir / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)

    mic_path = work / MIC_AUDIO
    mic_path.write_bytes(await mic_audio.read())
    tab_path = work / TAB_AUDIO
    tab_path.write_bytes(await tab_audio.read())

    try:
        timeline = json.loads(speaker_timeline)
    except (ValueError, TypeError):
        timeline = []
    (work / "speaker_timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 除錯用:content.js 裡 People 面板每一列語者指示器的狀態變化紀錄(見那邊
    # panelDebugLog 的說明)。只在懷疑即時語者偵測本身有問題時需要看,不影響
    # 逐字稿產出邏輯,單純多存一份診斷資料。
    try:
        debug_log = json.loads(speaker_debug)
    except (ValueError, TypeError):
        debug_log = []
    try:
        captions = json.loads(caption_log)
        if not isinstance(captions, list):
            captions = []
    except (ValueError, TypeError):
        captions = []
    (work / "caption_log.json").write_text(
        json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (work / "speaker_debug.json").write_text(
        json.dumps(debug_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:
        parsed_other_names = json.loads(other_names)
        if not isinstance(parsed_other_names, list):
            parsed_other_names = []
    except (ValueError, TypeError):
        parsed_other_names = []

    # 存下擴充功能實際送來的講者資訊——content.js 抓錯名字(例如把螢幕分享相關的
    # UI 按鈕文字誤判成參與者)時,單看最終逐字稿常常看不出原因,要靠這份原始輸入
    # 才能回頭確認問題出在「抓到的名字本身就是錯的」還是「後面比對邏輯用錯」。
    (work / "roster_input.json").write_text(
        json.dumps(
            {"mic_speaker_name": mic_speaker_name, "other_names": parsed_other_names},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    jobs[job_id] = {"status": "processing", "stage": "準備中", "percent": 0, "started_at": time.time()}
    thread = threading.Thread(
        target=_run_transcription,
        args=(
            job_id,
            mic_path,
            tab_path,
            out_dir,
            timeline,
            mic_speaker_name,
            parsed_other_names,
        ),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.post("/polish-speed/{job_id}")
def set_polish_speed(job_id: str, payload: dict) -> JSONResponse:
    """popup 調整潤飾速度:payload {"duty": 0.15~1.0},實際使用時仍會被夾在當下允許的範圍內。
    還沒進到潤飾階段時先記下來,開始潤飾時套用。"""
    try:
        duty = float(payload.get("duty"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "duty 要是數字"}, status_code=400)
    throttle = polish_throttles.get(job_id)
    if throttle is not None:
        throttle.set_user_duty(duty)
        return JSONResponse(throttle.snapshot())
    if jobs.get(job_id, {}).get("status") != "processing":
        return JSONResponse({"error": "這個工作已經處理完了"}, status_code=404)
    pending_polish_duty[job_id] = max(ABS_MIN, min(ABS_MAX, duty))
    return JSONResponse(_pending_speed(job_id))


def _pending_speed(job_id: str) -> dict:
    """還沒開始潤飾時給 popup 的速度資訊:範圍要到潤飾開始才依電腦狀況決定,先給完整範圍。"""
    return {"min": ABS_MIN, "max": ABS_MAX, "current": pending_polish_duty.get(job_id, ABS_MIN),
            "user_set": job_id in pending_polish_duty, "eta_seconds": None, "pending": True}


@app.get("/status/{job_id}")
def status(job_id: str) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)

    response = dict(job)
    started_at = response.pop("started_at", None)
    throttle = polish_throttles.get(job_id)
    if throttle is not None and job["status"] == "processing":
        response["polish_speed"] = throttle.snapshot()
    elif job["status"] == "processing":
        response["polish_speed"] = _pending_speed(job_id)
    if job["status"] == "processing" and started_at is not None:
        # 即時算出「已經處理多久」,不要把內部的 epoch 時間戳直接丟給前端。
        response["elapsed_seconds"] = round(time.time() - started_at)
    return JSONResponse(response)
