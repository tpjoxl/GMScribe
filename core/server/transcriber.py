"""
把一場會議的兩軌音訊(你的麥克風 / Meet 分頁裡其他人的聲音)轉成「每個人分開標示」的逐字稿。

三層資料來源,由穩到不穩排列:
1. 麥克風軌 = 你自己的聲音,天生就是分開的,不需要任何辨識,永遠準。
2. tab 軌(其他與會者混在一起)用 pyannote 做語者分離(diarization),
   純粹靠聲音特徵分群,不依賴 Google Meet 的網頁結構,Meet 怎麼改版都不會壞掉。
   分出來的群只有「講者 1」「講者 2」這種泛用標籤,不知道真實姓名。
3. Meet 字幕 DOM 記錄下來的「誰在什麼時候講話」(speaker_timeline,由 content.js 收集)
   拿來幫每個泛用群組「猜」一個真實姓名。這一層最容易因為 Meet 改版而失效,
   失效時就静静地退回顯示「講者 N」,不影響前两層。

全部都在本機跑,不需要任何雲端 API。
"""
from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .throttle import throttled_generate

import mlx_whisper
import numpy as np

# large-v3-turbo:比 medium 準確度更高,尤其中英夾雜跟專有名詞,
# 且是 OpenAI 官方為了兼顧速度而蒸餾的版本,在 M1 Pro GPU 上速度還是遠比即時快。
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

# 主要語言。強制指定可以大幅減少 Whisper 在不確定片段時亂切換成其他語言
# (例如把不清楚的音誤判成日文字符)的問題。中英夾雜的部分 Whisper 通常還是
# 能正確辨識,不會因為鎖定主要語言就完全聽不懂英文。如果會議主要語言不是
# 中文,改這裡就好。
PRIMARY_LANGUAGE = "zh"

# pyannote 的開源語者分離模型,免費但是 gated model,第一次使用前需要:
#   1. 到 https://huggingface.co/pyannote/speaker-diarization-community-1 按「Agree and access repository」
#   2. 到 https://huggingface.co/settings/tokens 建立一個 read 權限的 token
#   3. `hf auth login` 存到 ~/.cache/huggingface/token
# community-1 是 pyannote.audio 4.0 推出的新模型,pyannote 官方宣稱明顯優於舊版 3.1,
# 而且新版 pyannote.audio 套件載入 3.1 時本身就會依賴到 community-1 的元件,
# 所以直接指定用它,設定起來反而更單純。
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
# 鎖定模型的版本(Hugging Face 的 commit)。pyannote 載入模型檔用的是 torch.load(weights_only=False),
# 模型檔被換掉就能在你的電腦上執行任意程式;鎖定 commit 之後,就算模型 repo 之後被改,也只會下載這個版本。
DIARIZATION_REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"

# 逐字稿後製用的本機語言模型(不是雲端 API,不會有 token 費用,純粹消耗本機運算時間)。
# 只做「修順語句」這種保守的文字清理,不做摘要,繁簡轉換交給 OpenCC。
# 實測 3B 版本連「輸出行數要跟輸入一樣多」這種明確指令都做不到(會亂刪行),
# 換成 7B 版本(4-bit 量化約 4-5GB,16GB RAM 還撐得住)明顯可靠很多。
POLISH_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"

_diarization_pipeline = None
_polish_model = None
_polish_tokenizer = None
_vad_model = None


def release_models() -> None:
    """釋放這次工作用到的所有模型佔用的記憶體(Whisper、pyannote、VAD、潤飾用的 LLM)。

    刻意不讓模型「跨工作」常駐在記憶體裡——這個工具的使用模式是偶爾處理一場會議,
    不是連續密集跑很多場,常駐能省下的重新載入時間很小,代價卻是處理完之後背景
    永遠佔用 10GB 以上的記憶體,直到伺服器重啟為止。實測真的因為這樣爆過記憶體:
    同一台機器上還跑著 Docker、IDE 這類本身就很吃記憶體的工具,幾個大工作一起把
    記憶體榨乾,連系統自己的 watchdog 都排不到 CPU 回報存活,觸發 kernel panic
    強制重開機。app.py 每個工作處理完(不管成功失敗)都要呼叫這個函式清乾淨,
    下一個工作要用再重新載入,犧牲一點速度換取平常不佔記憶體。
    """
    global _diarization_pipeline, _polish_model, _polish_tokenizer, _vad_model

    _diarization_pipeline = None
    _polish_model = None
    _polish_tokenizer = None
    _vad_model = None

    # mlx_whisper 自己內部也有一份全域快取(transcribe.ModelHolder),不會因為我們
    # 清掉自己的變數就跟著釋放,要直接伸手清掉它的 class 屬性。
    try:
        import mlx_whisper.transcribe as _mlx_whisper_transcribe_module

        _mlx_whisper_transcribe_module.ModelHolder.model = None
        _mlx_whisper_transcribe_module.ModelHolder.model_path = None
    except Exception:  # noqa: BLE001
        pass

    import gc

    gc.collect()

    # 光是斷開 Python 參照、讓垃圾回收器回收物件還不夠——MLX 跟 PyTorch(MPS)
    # 各自有自己的記憶體配置器快取,不會因為物件被回收就馬上把記憶體還給作業系統,
    # 要各自明確呼叫清快取的函式才會真的釋放。
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:  # noqa: BLE001
        pass

    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:  # noqa: BLE001
        pass

# 一段沒人講話的靜音,如果比這個時間還短,VAD 不會把它切開(當作同一段話裡的
# 正常停頓,例如逗號、換氣);比這個時間長,才當作「這中間沒人講話」整段切掉。
_VAD_MIN_SILENCE_MS = 500
# 每段偵測到的語音前後多留一點緩衝,避免切到字音的開頭/結尾。
_VAD_SPEECH_PAD_MS = 200
# Whisper 每個 segment 的平均 log 機率低於這個值就不收進逐字稿。雜音、含糊的附和聲常被
# Whisper 硬猜成中英德泰混雜的亂碼(2026-10-29 實測:「山脊 rég 怕痛」「應該 versa」
# 「哎 technique」),這些片段的分數都在 -2 到 -4.8;正常語音在 -0.2 上下,短的
# 「哦」「好」「OK」也多在 -0.7 到 -1.0。取 -1.5 把兩群分開,偶爾會順帶丟掉一個
# 含糊的「對」,不影響閱讀。被丟掉的片段存在 TranscriptResult.low_confidence,另外寫檔備查。
_MIN_AVG_LOGPROB = -1.5


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]
    avg_logprob: float = 0.0


@dataclass
class TranscriptResult:
    text: str
    segments: list[Segment]
    low_confidence: list[Segment] = field(default_factory=list)


@dataclass
class LabeledSegment:
    start: float
    end: float
    text: str
    speaker: str


def convert_to_wav(input_path: Path, output_path: Path) -> None:
    """用 ffmpeg 轉成 Whisper/pyannote 都能吃的 16kHz 單聲道 wav。"""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "16000",
            "-ac", "1",
            "-vn",
            str(output_path),
        ],
        check=True,
        capture_output=True,
    )


_REPEAT_PATTERN = re.compile(r"(.{1,12}?)\1{3,}")


def _collapse_repetition(text: str) -> str:
    """防止 Whisper 偶爾出現的『重複幻覺』:同一個字/詞/短語連續出現一長串
    (例如中文的「西川西川西川...」或英文單字重複)。抓「1~12 個字元的片段
    連續重複 4 次以上」就砍到只留 1 次;正常說話(例如「喂喂」「你好你好」
    這種重複 2~3 次)不會被誤砍,只有明顯異常的重複才會處理。"""
    while True:
        new_text = _REPEAT_PATTERN.sub(r"\1", text)
        if new_text == text:
            return text
        text = new_text


def _get_vad_model():
    global _vad_model
    if _vad_model is None:
        from silero_vad import load_silero_vad

        _vad_model = load_silero_vad()
    return _vad_model


def _read_wav_mono16(wav_path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav_path), "rb") as w:
        sample_rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, sample_rate


def _detect_speech_regions(audio: np.ndarray, sample_rate: int) -> list[tuple[float, float]]:
    """用 Silero VAD 找出真的有人講話的時間區間(秒)。

    這一步是為了解決一個比幻覺更嚴重的問題:Whisper 遇到長時間的靜音之後,
    容易連緊接著的真實內容都轉錄失敗(不只是誤判成幻覺文字,是真的漏掉整段
    真實語音)。與其事後想辦法從幻覺裡搶救內容,不如讓 Whisper 從頭到尾都
    看不到長靜音——只把偵測到的語音片段接起來餵給它。
    """
    from silero_vad import get_speech_timestamps

    model = _get_vad_model()
    timestamps = get_speech_timestamps(
        audio,
        model,
        sampling_rate=sample_rate,
        return_seconds=True,
        min_silence_duration_ms=_VAD_MIN_SILENCE_MS,
        speech_pad_ms=_VAD_SPEECH_PAD_MS,
    )
    return [(t["start"], t["end"]) for t in timestamps]


def _write_wav_mono16(audio: np.ndarray, sample_rate: int, out_path: Path) -> None:
    pcm16 = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(sample_rate)
        dst.writeframes(pcm16.tobytes())


def _whisper_transcribe_chunk(chunk_path: Path) -> dict:
    return mlx_whisper.transcribe(
        str(chunk_path),
        path_or_hf_repo=MODEL_REPO,
        verbose=False,
        language=PRIMARY_LANGUAGE,
        # 會議音訊常常有一大段沉默/停頓,容易誘發 Whisper 的重複幻覺(同一個詞洗版式
        # 重複)。關掉「用前一段文字當作下一段的上下文提示」可以大幅降低這個問題,
        # 代價是段落之間偶爾會少一點連貫性,對逐字稿來說划算。
        condition_on_previous_text=False,
        # 安靜片段容易被誤判成雜訊文字(例如亂噴符號),超過 2 秒的沉默直接跳過。
        # 上面的 VAD 已經把長靜音切掉了,這裡留著當作第二層防線。
        hallucination_silence_threshold=2.0,
        # 開字級時間戳:label_tab_segments() 靠這個把混到兩個語者的 segment
        # 從語者切換的地方拆開,不用整段賭同一個人。
        word_timestamps=True,
    )


def transcribe(audio_path: Path) -> TranscriptResult:
    wav_path = audio_path.with_suffix(".wav")
    convert_to_wav(audio_path, wav_path)

    audio, sample_rate = _read_wav_mono16(wav_path)
    regions = _detect_speech_regions(audio, sample_rate)
    if not regions:
        # VAD 完全沒偵測到語音(理論上不該發生,防禦性寫法):整份原始音檔當一段處理。
        regions = [(0.0, len(audio) / sample_rate)]

    # 每個 VAD 語音片段獨立、各自送進 Whisper 轉錄,而不是接起來一起轉——這樣每段
    # 都是乾淨的獨立音檔,時間戳記只要加回這段在原始音檔裡的起點就好,不會有「Whisper
    # 把兩個原本不相關、時間差很遠的片段誤判成同一個 segment」這種跨片段汙染的問題。
    all_segments: list[Segment] = []
    total_samples = len(audio)

    for start, end in regions:
        start_sample = max(0, int(start * sample_rate))
        end_sample = min(total_samples, int(end * sample_rate))
        if end_sample <= start_sample:
            continue
        offset = start_sample / sample_rate

        chunk_path = wav_path.with_name(f"{wav_path.stem}_chunk_{start_sample}.wav")
        _write_wav_mono16(audio[start_sample:end_sample], sample_rate, chunk_path)
        try:
            result = _whisper_transcribe_chunk(chunk_path)
        finally:
            chunk_path.unlink(missing_ok=True)

        for seg in result["segments"]:
            all_segments.append(
                Segment(
                    start=seg["start"] + offset,
                    end=seg["end"] + offset,
                    text=_collapse_repetition(seg["text"].strip()),
                    words=[
                        Word(start=w["start"] + offset, end=w["end"] + offset, text=w["word"])
                        for w in seg.get("words", [])
                    ],
                    avg_logprob=seg.get("avg_logprob", 0.0),
                )
            )

    kept = [s for s in all_segments if s.avg_logprob >= _MIN_AVG_LOGPROB]
    low_confidence = [s for s in all_segments if s.avg_logprob < _MIN_AVG_LOGPROB]
    full_text = _collapse_repetition(" ".join(s.text for s in kept))
    result = _strip_hallucinated_words(TranscriptResult(text=full_text, segments=kept))
    result.low_confidence = low_confidence
    return result


def _get_diarization_pipeline():
    global _diarization_pipeline
    if _diarization_pipeline is None:
        import torch
        from pyannote.audio import Pipeline

        _diarization_pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, revision=DIARIZATION_REVISION)
        device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
        _diarization_pipeline.to(device)
    return _diarization_pipeline


def diarize(wav_path: Path, num_speakers: int | None = None) -> tuple[list[dict], list[dict]]:
    """回傳 (turns, overlap_regions)。

    turns:[{start, end, cluster}],cluster 是 pyannote 給的泛用標籤(SPEAKER_00...)。
    用 exclusive_speaker_diarization(pyannote.audio 4.0 的新 API):同一時間只會有
    一個講者,跟 Whisper 的逐段文字對齊時比較單純,不用處理重疊語音的情況——
    也就是說這份結果本身「假裝」每個時間點都只有一個人講話,實際上是不是真的
    只有一個人,要另外看 overlap_regions。

    overlap_regions:[{start, end}],pyannote 的分群模型內部其實有偵測「這段時間
    有不只一個人同時在講話」的能力(non-exclusive 的 speaker_diarization 輸出,
    用 .get_overlap() 取得),只是分群結果(上面的 turns)為了跟 Whisper 對齊方便
    而簡化成同一時間只有一個人。這裡額外把重疊區間撈出來,讓 label_tab_segments()
    能誠實標示「這段有多人同時發言,內容可能不完整/不準確」,而不是硬猜一個名字。

    num_speakers:如果知道「其他與會者」總共有幾個人,傳進來可以強制分群數量,
    避免同一個人講話風格差異太大(例如念數字跟講一般對話語氣不同)被誤分成
    兩個不同的人。這一軌只有其他與會者的聲音(你的聲音在麥克風軌單獨處理),
    所以這裡的人數是「總與會人數 - 1」。
    """
    pipeline = _get_diarization_pipeline()
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    output = pipeline(str(wav_path), **kwargs)
    turns = [
        {"start": turn.start, "end": turn.end, "cluster": speaker}
        for turn, speaker in output.exclusive_speaker_diarization
    ]
    overlap_regions = [
        {"start": seg.start, "end": seg.end}
        for seg in output.speaker_diarization.get_overlap()
    ]
    return turns, overlap_regions


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _best_cluster_for_span(start: float, end: float, turns: list[dict]) -> str | None:
    best_cluster, best_overlap = None, 0.0
    for t in turns:
        overlap = _overlap(start, end, t["start"], t["end"])
        if overlap > best_overlap:
            best_overlap, best_cluster = overlap, t["cluster"]
    return best_cluster


def _cluster_display_names(
    turns: list[dict],
    speaker_timeline: list[dict],
    known_names: list[str] | None = None,
) -> dict[str, str]:
    """幫每個 pyannote cluster 決定顯示名稱:字幕 DOM 訊號重疊夠多就用真名,否則用「講者 N」。

    known_names:從 Meet 名單抓到的「其他與會者」真實姓名清單。有給的話,字幕 DOM
    訊號裡任何不在這份名單裡的名字都當噪音濾掉(例如你自己的「You」标籤沒濾乾淨,
    或字幕誤判成別的文字),避免比對到不存在的人。
    """
    if known_names:
        known_set = set(known_names)
        speaker_timeline = [e for e in speaker_timeline if e.get("speaker") in known_set]

    turns_by_cluster: dict[str, list[dict]] = {}
    for t in turns:
        turns_by_cluster.setdefault(t["cluster"], []).append(t)

    mapping: dict[str, str] = {}
    generic_counter = 1
    # 依照每個 cluster 第一次出現的時間排序,講者編號才會跟直覺的出場順序一致
    ordered_clusters = sorted(
        turns_by_cluster.items(), key=lambda kv: min(t["start"] for t in kv[1])
    )

    for cluster, cluster_turns in ordered_clusters:
        name_overlap_ms: dict[str, float] = {}
        total_ms = 0.0
        for t in cluster_turns:
            t_start_ms, t_end_ms = t["start"] * 1000, t["end"] * 1000
            total_ms += t_end_ms - t_start_ms
            for entry in speaker_timeline:
                overlap = _overlap(t_start_ms, t_end_ms, entry["startMs"], entry["endMs"])
                if overlap > 0:
                    name_overlap_ms[entry["speaker"]] = (
                        name_overlap_ms.get(entry["speaker"], 0.0) + overlap
                    )

        best_name = max(name_overlap_ms, key=name_overlap_ms.get) if name_overlap_ms else None
        if best_name and total_ms > 0 and name_overlap_ms[best_name] / total_ms > 0.4:
            mapping[cluster] = best_name
        else:
            mapping[cluster] = f"講者 {generic_counter}"
            generic_counter += 1

    return mapping


# 落在「多人同時發言」區間裡的內容,不猜是誰講的——重疊語音疊在一起,連
# pyannote 自己都只能說「這段時間不只一個人」,沒辦法真的分開兩人的內容,
# 硬要指定一個名字只是自欺欺人。潤飾階段(polish_transcript)看到這個標籤
# 會直接跳過 LLM、不嘗試「修順」,原因見那邊的註解。
_OVERLAP_LABEL = "多人同時發言"


def _in_overlap(start: float, end: float, overlap_regions: list[dict]) -> bool:
    return any(_overlap(start, end, r["start"], r["end"]) > 0 for r in overlap_regions)


# ---- 輪流發言型會議(站立會議)的講者標示參數 ----
# 使用者的會議是一個人講完換下一個人,不會真的同時講話。pyannote 在換人的瞬間常常報出
# 零點幾秒的「重疊」(2026-10-06 那場 9 個重疊區間最長只有 0.66 秒),逐字比對時就會把
# 一句話中間的一兩個字切出去標成「多人同時發言」。短於這個長度的重疊區間一律忽略。
_MIN_OVERLAP_SEC = 2.0
# 逐字比對語者之後,短於這個長度(或只有一兩個字)的片段視為分群邊界的雜訊,併回相鄰
# 的片段,不讓「同一個人的一句話中間被插進別的講者」(例如「...開始進行 / 試 / 作 / 以上」)。
_MIN_RUN_SEC = 1.5
_MIN_RUN_WORDS = 3
# 前後都隔了這麼久的靜音,代表它是一句獨立的短發言(例如主持人說「下一位」),不是雜訊,保留。
_ISOLATED_GAP_SEC = 1.0
# Meet 的講話指示器是一閃一閃的,同一個人相隔這麼短的兩段視為連續講話。
_TIMELINE_MERGE_GAP_MS = 3000
# 一段發言的時間範圍裡,指示器顯示某人在講話的比例超過這個值,就直接採用那個名字
# (不必等整個分群都對得上)。
_RUN_NAME_MIN_COVERAGE = 0.3
# 輸出時,同一個講者的片段遇到超過這個長度的停頓就切開,讓 merge_and_format() 能跟麥克風軌交錯排序
_SPLIT_GAP_SEC = 1.0
# content.js 用這個名字記錄「你自己在 Meet 裡被偵測到講話」的時段(見 filter_mic_by_self_activity)。
SELF_TIMELINE_SPEAKER = "__self__"
# content.js 記錄「你在 Meet 裡麥克風開著」的時段(見 filter_mic_by_self_activity)。
UNMUTED_TIMELINE_SPEAKER = "__unmuted__"
# 按下開麥克風之後聲音要一點時間才送出去、講完最後一個字常常馬上按靜音,邊界前後多留一點。
_UNMUTED_PAD_BEFORE_SEC = 0.3
_UNMUTED_PAD_AFTER_SEC = 0.8
# 一段麥克風內容至少要有這個比例落在「麥克風開著」的時段裡才保留
_UNMUTED_MIN_SHARE = 0.5
# content.js 記錄其他與會者在 Meet 裡靜音的時段,名字前面加這個前綴(見 content.js 的 isTileMuted)。
MUTED_TIMELINE_PREFIX = "__muted__:"
# 靜音時段頭尾各縮這麼多:按下靜音前最後幾個字還在網路上、開麥的畫面更新跟聲音誰先到不一定。
_MUTED_EDGE_SEC = 1.0
# 一段話有這個比例以上的時間某人都在靜音,就不可能是他講的
_MUTED_MIN_SHARE = 0.6


def _merge_timeline(speaker_timeline: list[dict], gap_ms: float) -> list[dict]:
    """同一個人相隔很近的指示器片段接成一段。"""
    by_speaker: dict[str, list[dict]] = {}
    for e in speaker_timeline:
        by_speaker.setdefault(e["speaker"], []).append(e)
    merged: list[dict] = []
    for speaker, entries in by_speaker.items():
        entries = sorted(entries, key=lambda e: e["startMs"])
        cur = dict(entries[0])
        for e in entries[1:]:
            if e["startMs"] - cur["endMs"] <= gap_ms:
                cur["endMs"] = max(cur["endMs"], e["endMs"])
            else:
                merged.append(cur)
                cur = dict(e)
        merged.append(cur)
    return sorted(merged, key=lambda e: e["startMs"])


# Meet 的講話指示器比聲音慢:開口後約 1~1.7 秒才亮、講完後還多亮約 2.6~2.8 秒(2026-10~11 五場會議實測的
# 中位數,每場不太一樣)。沒校正的話,上一個人還亮著的指示器會蓋住下一個人開頭的幾個字,把它們算給上一個人。
# 每場會議自己量:拿指示器每段的開始/結束,對到它前面最近的「停頓後開口 / 停頓前講完」的時間,取中位數。
_LAG_PAUSE_SEC = 0.8
_LAG_MAX_SEC = 5.0
_LAG_MIN_SAMPLES = 5


def _estimate_timeline_lag(words: list[Word], timeline: list[dict]) -> tuple[float, float]:
    """回傳 (開始延遲, 結束延遲) 秒數;樣本太少就當作沒有延遲。"""
    import statistics

    if not words or not timeline:
        return 0.0, 0.0
    onsets = [w.start for i, w in enumerate(words) if i == 0 or w.start - words[i - 1].end >= _LAG_PAUSE_SEC]
    ends = [w.end for i, w in enumerate(words) if i == len(words) - 1 or words[i + 1].start - w.end >= _LAG_PAUSE_SEC]

    def lags(points: list[float], refs: list[float]) -> list[float]:
        out = []
        for p in points:
            cands = [p - r for r in refs if 0 <= p - r <= _LAG_MAX_SEC]
            if cands:
                out.append(min(cands))
        return out

    start_lags = lags([e["startMs"] / 1000 for e in timeline], onsets)
    end_lags = lags([e["endMs"] / 1000 for e in timeline], ends)
    start_lag = statistics.median(start_lags) if len(start_lags) >= _LAG_MIN_SAMPLES else 0.0
    end_lag = statistics.median(end_lags) if len(end_lags) >= _LAG_MIN_SAMPLES else 0.0
    return start_lag, end_lag


def _shift_timeline(timeline: list[dict], start_lag: float, end_lag: float) -> list[dict]:
    shifted = []
    for e in timeline:
        start = e["startMs"] - start_lag * 1000
        end = max(e["endMs"] - end_lag * 1000, start + 100)
        shifted.append({**e, "startMs": start, "endMs": end})
    return shifted


def _nearest_cluster(start: float, end: float, turns: list[dict], max_distance: float = 2.0) -> str | None:
    """字的時間範圍沒有落在任何分群片段裡時,找時間上最近的片段(太遠就放棄)。"""
    mid = (start + end) / 2
    best, best_dist = None, max_distance
    for t in turns:
        dist = 0.0 if t["start"] <= mid <= t["end"] else min(abs(mid - t["start"]), abs(mid - t["end"]))
        if dist < best_dist:
            best, best_dist = t["cluster"], dist
    return best


# 短片段如果本身就對應到 pyannote 一整段獨立的發言(不是邊界上零碎的一兩個字),代表真的有另一個人
# 插了一句短話(例如主持人說「○○,請說」),不能併掉。門檻:那段發言至少這麼長,而且蓋住
# 這個短片段的大部分時間。設為 None 表示關閉這個例外(舊行為)。
_KEEP_SHORT_TURN_SEC: float | None = 0.8
_KEEP_SHORT_TURN_COVER = 0.7


def _is_own_turn(run: dict, turns: list[dict] | None) -> bool:
    if not turns or _KEEP_SHORT_TURN_SEC is None:
        return False
    duration = max(run["end"] - run["start"], 1e-6)
    for t in turns:
        if t["cluster"] != run["key"] or t["end"] - t["start"] < _KEEP_SHORT_TURN_SEC:
            continue
        if _overlap(run["start"], run["end"], t["start"], t["end"]) / duration >= _KEEP_SHORT_TURN_COVER:
            return True
    return False


def _smooth_runs(runs: list[dict], turns: list[dict] | None = None) -> list[dict]:
    """把太短的片段併回相鄰片段,直到沒有可以併的為止(本身是一整段獨立發言的除外)。"""

    def coalesce(rs: list[dict]) -> list[dict]:
        out: list[dict] = []
        for r in rs:
            if out and out[-1]["key"] == r["key"]:
                out[-1]["end"] = r["end"]
                out[-1]["words"].extend(r["words"])
            else:
                out.append(r)
        return out

    runs = coalesce(runs)
    while len(runs) > 1:
        target = None
        for i, r in enumerate(runs):
            if r["key"] == _OVERLAP_LABEL:
                continue
            # 字幕文字對得上的片段是確定的,再短也不併掉(例如插一句「對,還沒」)
            if isinstance(r["key"], str) and r["key"].startswith(_CAPTION_NAME_PREFIX):
                continue
            if r["end"] - r["start"] >= _MIN_RUN_SEC and len(r["words"]) >= _MIN_RUN_WORDS:
                continue
            gap_prev = r["start"] - runs[i - 1]["end"] if i > 0 else float("inf")
            gap_next = runs[i + 1]["start"] - r["end"] if i < len(runs) - 1 else float("inf")
            if gap_prev > _ISOLATED_GAP_SEC and gap_next > _ISOLATED_GAP_SEC:
                continue
            if _is_own_turn(r, turns):
                continue
            target = (i, gap_prev, gap_next)
            break
        if target is None:
            break
        i, gap_prev, gap_next = target
        neighbor = i - 1 if gap_prev <= gap_next else i + 1
        runs[i]["key"] = runs[neighbor]["key"]
        runs = coalesce(runs)
    return runs


# 換人講話幾乎一定發生在停頓的地方,但 pyannote 的分群邊界常常晚(或早)幾個字,結果上一個人
# 結尾的「以上」被算給下一個人、或下一個人開頭的「我這邊目前」被算給上一個人。在邊界前後
# 各看這麼多個字,如果附近有明顯比較長的停頓,就把邊界移到那個停頓上。
_BOUNDARY_SNAP_WORDS = 8
_BOUNDARY_SNAP_MIN_GAP_SEC = 0.35


def _moves_whole_turn(moved: list, kept: list, key, turns: list[dict] | None) -> bool:
    """要搬走的字如果本身就是 pyannote 一整段獨立發言(而且那段發言沒有延伸到留下來的字),
    代表它是另一個人插的一句話,不能因為旁邊有停頓就搬給別人。"""
    if not moved or not kept or not turns or _KEEP_SHORT_TURN_SEC is None:
        return False
    m_start, m_end = moved[0][2], moved[-1][3]
    k_start, k_end = kept[0][2], kept[-1][3]
    span = max(m_end - m_start, 1e-6)
    for t in turns:
        if t["cluster"] != key or t["end"] - t["start"] < _KEEP_SHORT_TURN_SEC:
            continue
        if _overlap(m_start, m_end, t["start"], t["end"]) / span < _KEEP_SHORT_TURN_COVER:
            continue
        if _overlap(k_start, k_end, t["start"], t["end"]) <= 0:
            return True
    return False


def _snap_boundaries_to_pauses(runs: list[dict], turns: list[dict] | None = None) -> list[dict]:
    for i in range(len(runs) - 1):
        a, b = runs[i], runs[i + 1]
        if _OVERLAP_LABEL in (a["key"], b["key"]):
            continue
        if any(isinstance(k, str) and k.startswith(_CAPTION_NAME_PREFIX) for k in (a["key"], b["key"])):
            continue
        tail = a["words"][-_BOUNDARY_SNAP_WORDS:]
        head = b["words"][:_BOUNDARY_SNAP_WORDS]
        # 兩邊至少各留一個字,不讓任何一段被搬空
        if len(tail) == len(a["words"]):
            tail = tail[1:]
        if len(head) == len(b["words"]):
            head = head[:-1]
        seq = tail + head
        cut = len(tail)
        if not seq or cut == 0 and not head:
            continue
        def gap_at(j: int) -> float:
            prev_end = seq[j - 1][3] if j > 0 else a["words"][len(a["words"]) - len(tail) - 1][3]
            next_start = seq[j][2] if j < len(seq) else b["words"][len(head)][2]
            return next_start - prev_end
        best_j, best_gap = cut, gap_at(cut)
        # 分群換人的位置本來就落在明顯的停頓上,就相信它:不然後面剛好有更長的停頓時,會把下一個人
        # 開頭的短話(例如主持人的「好,OK」)搬給上一個人(2026-10-07 站立會議實測)。
        if best_gap >= _UTTERANCE_GAP_SEC:
            continue
        a_fixed = a["words"][: len(a["words"]) - len(tail)]
        b_fixed = b["words"][len(head):]
        for j in range(0, len(seq) + 1):
            g = gap_at(j)
            if not (g >= _BOUNDARY_SNAP_MIN_GAP_SEC and g > best_gap + 0.15):
                continue
            if j < cut and _moves_whole_turn(seq[j:cut], a_fixed + seq[:j], a["key"], turns):
                continue
            if j > cut and _moves_whole_turn(seq[cut:j], seq[j:] + b_fixed, b["key"], turns):
                continue
            best_j, best_gap = j, g
        if best_j == cut:
            continue
        keep_a = a["words"][: len(a["words"]) - len(tail)] + seq[:best_j]
        keep_b = seq[best_j:] + b["words"][len(head):]
        a["words"], b["words"] = keep_a, keep_b
        a["end"], b["start"] = keep_a[-1][3], keep_b[0][2]
    return runs


_CAPTION_NAME_PREFIX = "name:"
# 字幕塊的文字只拿來跟 Whisper 的字做比對。字幕比聲音晚出現,所以往前多看幾秒。
_CAPTION_WINDOW_BEFORE_SEC = 8.0
_CAPTION_WINDOW_AFTER_SEC = 3.0
_CAPTION_MIN_MATCH_CHARS = 2


def _normalize_chars(text: str, t2s) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", t2s.convert(text).lower())


def _caption_word_speakers(words: list[Word], caption_log: list[dict]) -> list[str | None]:
    """把每個 Whisper 的字對到某一塊字幕(同一段文字),回傳每個字的講者(對不到就是 None)。

    做法:每一塊字幕只跟它出現時間附近的 Whisper 字比對(difflib 找共同片段,至少連續
    兩個字相同才算),對到的字記一票給那塊字幕的講者;一個字被多個講者對到,取票多的。
    字幕文字本身不會進逐字稿。
    """
    import difflib

    from opencc import OpenCC

    t2s = OpenCC("t2s")
    word_chars = [_normalize_chars(w.text, t2s) for w in words]
    votes: list[dict[str, int]] = [dict() for _ in words]
    for block in caption_log:
        speaker = block.get("speaker")
        if not speaker or speaker == SELF_TIMELINE_SPEAKER:
            continue
        cap = _normalize_chars(block.get("text", ""), t2s)
        if len(cap) < _CAPTION_MIN_MATCH_CHARS:
            continue
        lo = block["startMs"] / 1000 - _CAPTION_WINDOW_BEFORE_SEC
        hi = block["endMs"] / 1000 + _CAPTION_WINDOW_AFTER_SEC
        idx = [i for i, w in enumerate(words) if lo <= w.start <= hi and word_chars[i]]
        if not idx:
            continue
        stream = ""
        owner: list[int] = []
        for i in idx:
            stream += word_chars[i]
            owner.extend([i] * len(word_chars[i]))
        matcher = difflib.SequenceMatcher(None, cap, stream, autojunk=False)
        for m in matcher.get_matching_blocks():
            if m.size < _CAPTION_MIN_MATCH_CHARS:
                continue
            for k in range(m.b, m.b + m.size):
                i = owner[k]
                votes[i][speaker] = votes[i].get(speaker, 0) + 1
    result: list[str | None] = []
    for i, v in enumerate(votes):
        if not v:
            result.append(None)
            continue
        best = max(v, key=v.get)
        result.append(best if v[best] * 2 >= len(word_chars[i]) else None)
    return result


# 「同一口氣講的話」:中間沒有超過這個長度的停頓、聲音分群也是同一個人的連續字。換人幾乎一定發生在停頓上
# (2026-11-02 站立會議實測:26 人輪流報告,每次換人前都停了 1~2 秒,pyannote 的分群邊界也都正好切在停頓上)。
_UTTERANCE_GAP_SEC = 0.8
# 同一口氣裡,字幕連續對到另一個人這麼多個字,才當作真的換人(不被整段多數決蓋掉)。
_KEEP_CAPTION_RUN_CHARS = 6
# 字幕對到的字比單看指示器/分群命名的字可靠,計票時加權。
_CAPTION_VOTE_WEIGHT = 2


def _is_generic_name(name: str) -> bool:
    return name in (_OVERLAP_LABEL, "不明講者", "其他與會者") or name.startswith("講者 ")


def _muted_intervals(speaker_timeline: list[dict]) -> dict[str, list[tuple[float, float]]]:
    """從 timeline 取出每個人在 Meet 裡靜音的時段(秒,頭尾已經內縮)。"""
    out: dict[str, list[tuple[float, float]]] = {}
    for e in speaker_timeline:
        sp = e.get("speaker") or ""
        if not sp.startswith(MUTED_TIMELINE_PREFIX):
            continue
        start, end = e["startMs"] / 1000 + _MUTED_EDGE_SEC, e["endMs"] / 1000 - _MUTED_EDGE_SEC
        if end > start:
            out.setdefault(sp[len(MUTED_TIMELINE_PREFIX):], []).append((start, end))
    return out


# 靜音紀錄用之前先跟字幕對質:字幕顯示某人在講話的時間,有超過這個比例落在他的靜音時段裡,代表靜音的
# 判斷方式壞了(例如 Meet 改版換了圖示),整份不採用,退回不看靜音的判斷。正常時這個比例接近 0
# (11/03 實測:每個人都是先開麥才講話)。
_MUTED_MAX_CONTRADICTION = 0.3
# 你自己的「麥克風開著」紀錄也一樣:講話燈號亮著的時間,至少要有這個比例落在開麥時段裡才採用。
_UNMUTED_MIN_AGREEMENT = 0.5


def mute_data_consistent(muted: dict[str, list[tuple[float, float]]], caption_log: list[dict] | None) -> bool:
    if not muted:
        return False
    total = contradicted = 0.0
    for block in caption_log or []:
        name = block.get("speaker")
        if name not in muted:
            continue
        start, end = block["startMs"] / 1000, block["endMs"] / 1000
        total += end - start
        contradicted += sum(_overlap(start, end, a, b) for a, b in muted[name])
    # 沒有字幕可以對質時照用(字幕壞掉的場次,靜音反而是少數還能用的訊號)
    return total == 0 or contradicted / total <= _MUTED_MAX_CONTRADICTION


def unmuted_data_consistent(speaker_timeline: list[dict]) -> bool:
    own = [(e["startMs"] / 1000, e["endMs"] / 1000) for e in speaker_timeline if e.get("speaker") == SELF_TIMELINE_SPEAKER]
    unmuted = [
        (e["startMs"] / 1000 - _UNMUTED_PAD_BEFORE_SEC, e["endMs"] / 1000 + _UNMUTED_PAD_AFTER_SEC)
        for e in speaker_timeline
        if e.get("speaker") == UNMUTED_TIMELINE_SPEAKER
    ]
    if not unmuted:
        return False
    own_total = sum(b - a for a, b in own)
    if own_total < 5:
        return True
    agreed = sum(_overlap(a, b, c, d) for a, b in own for c, d in unmuted)
    return agreed / own_total >= _UNMUTED_MIN_AGREEMENT


def _was_muted(muted: dict[str, list[tuple[float, float]]], name: str, start: float, end: float) -> bool:
    spans = muted.get(name)
    if not spans:
        return False
    covered = sum(_overlap(start, end, a, b) for a, b in spans)
    return covered / max(end - start, 1e-6) >= _MUTED_MIN_SHARE


def _consolidate_utterances(
    items: list[dict],
    turns: list[dict],
    muted: dict[str, list[tuple[float, float]]] | None = None,
    known_names: list[str] | None = None,
) -> None:
    """同一口氣講的話,整段一起決定是誰講的(就地修改每個字的 speaker)。

    逐小段命名會被兩種雜訊切碎(2026-11-02 站立會議實測三處換人都錯):
    - Meet 的講話指示器比聲音晚 2~3 秒,下一個人開頭幾個字還在上一個人的指示器時間裡,被算給上一個人。
    - 「這邊」「在 OP」這種常見短字,會誤對到時間附近別人的字幕。
    整段計票(字數,字幕對到的字加權;「講者 N」這類代號不算票)取最多的人,只有字幕連續對到另一個人
    夠多個字(`_KEEP_CAPTION_RUN_CHARS`)才保留成換人。

    有其他與會者的靜音紀錄時(`muted`):這段話期間在 Meet 裡靜音的人不可能是講者,他的票不算;名單上的人
    只剩一個沒在靜音的話,就是他(站立會議通常只有正在報告的人開著麥克風)。沒有視訊格、沒被記到的人當作
    不知道,不會被排除。
    """
    muted = muted or {}
    if not items:
        return

    def cluster_of(it: dict):
        return _best_cluster_for_span(it["start"], it["end"], turns) or _nearest_cluster(it["start"], it["end"], turns)

    blocks: list[list[dict]] = []
    prev = None
    for it in items:
        if it["speaker"] == _OVERLAP_LABEL:
            prev = None
            continue
        c = cluster_of(it)
        if prev is not None and it["start"] - prev["end"] < _UTTERANCE_GAP_SEC and c == prev["cluster"]:
            blocks[-1].append(it)
        else:
            blocks.append([it])
        it["cluster"] = c
        prev = it

    for block in blocks:
        b_start, b_end = block[0]["start"], block[-1]["end"]
        votes: dict[str, float] = {}
        for it in block:
            if _is_generic_name(it["speaker"]):
                continue
            if _was_muted(muted, it["speaker"], b_start, b_end):
                continue
            weight = _CAPTION_VOTE_WEIGHT if it["caption"] else 1
            votes[it["speaker"]] = votes.get(it["speaker"], 0.0) + weight * max(len(it["chars"]), 1)
        possible = [n for n in (known_names or []) if not _was_muted(muted, n, b_start, b_end)] if muted else []
        if len(possible) == 1:
            for it in block:
                it["speaker"] = possible[0]
            continue
        if not votes:
            continue
        winner = max(votes, key=votes.get)
        # 字幕連續對到同一個非多數的人夠多字的片段,保留
        keep: set[int] = set()
        i = 0
        while i < len(block):
            j = i
            if block[i]["caption"] and block[i]["speaker"] != winner:
                while j + 1 < len(block) and block[j + 1]["caption"] and block[j + 1]["speaker"] == block[i]["speaker"]:
                    j += 1
                if sum(len(block[k]["chars"]) for k in range(i, j + 1)) >= _KEEP_CAPTION_RUN_CHARS:
                    keep.update(range(i, j + 1))
            i = j + 1
        for k, it in enumerate(block):
            if k not in keep:
                it["speaker"] = winner


def label_tab_segments(
    result: TranscriptResult,
    turns: list[dict],
    speaker_timeline: list[dict],
    known_names: list[str] | None = None,
    overlap_regions: list[dict] | None = None,
    caption_log: list[dict] | None = None,
) -> list[LabeledSegment]:
    """幫 tab 軌的每個字決定講者,平滑掉分群邊界的雜訊,再幫每一段發言決定名字。

    名字的來源有兩層:這一段發言當下 Meet 指示器顯示誰在講話(最直接,逐段判斷),
    對不上才退回「整個分群對到誰」(`_cluster_display_names`),再不行才是「講者 N」。
    """
    overlap_regions = [
        r for r in (overlap_regions or []) if r["end"] - r["start"] >= _MIN_OVERLAP_SEC
    ]

    if not turns:
        # 語者分離失敗(例如沒有 HF token)時,整段就標成單一個「其他與會者」
        return [
            LabeledSegment(start=s.start, end=s.end, text=s.text, speaker="其他與會者")
            for s in result.segments
        ]

    muted = _muted_intervals(speaker_timeline)
    if not mute_data_consistent(muted, caption_log):
        muted = {}
    speaker_timeline = [
        e
        for e in speaker_timeline
        if e.get("speaker") not in (SELF_TIMELINE_SPEAKER, UNMUTED_TIMELINE_SPEAKER)
        and not (e.get("speaker") or "").startswith(MUTED_TIMELINE_PREFIX)
    ]
    if known_names:
        known_set = set(known_names)
        speaker_timeline = [e for e in speaker_timeline if e.get("speaker") in known_set]
    speaker_timeline = _merge_timeline(speaker_timeline, _TIMELINE_MERGE_GAP_MS) if speaker_timeline else []
    all_words = [w for seg in result.segments for w in seg.words]
    start_lag, end_lag = _estimate_timeline_lag(all_words, speaker_timeline)
    speaker_timeline = _shift_timeline(speaker_timeline, start_lag, end_lag)

    display_names = _cluster_display_names(turns, speaker_timeline, known_names=known_names)

    # 1. 逐字決定講者:先看字幕文字對不對得上(最準,見 _caption_word_speakers),
    #    對不上才用聲音分群(沒有字級時間戳的 segment 整段當成一個字)
    flat: list[tuple[int, Word]] = []
    for seg_index, seg in enumerate(result.segments):
        for w in seg.words or [Word(start=seg.start, end=seg.end, text=seg.text)]:
            flat.append((seg_index, w))
    caption_speakers = (
        _caption_word_speakers([w for _, w in flat], caption_log) if caption_log else [None] * len(flat)
    )
    runs: list[dict] = []
    for (seg_index, w), cap_speaker in zip(flat, caption_speakers):
        if True:
            if cap_speaker:
                key = _CAPTION_NAME_PREFIX + cap_speaker
            elif _in_overlap(w.start, w.end, overlap_regions):
                key = _OVERLAP_LABEL
            else:
                key = _best_cluster_for_span(w.start, w.end, turns) or _nearest_cluster(w.start, w.end, turns)
            runs.append({"key": key, "start": w.start, "end": w.end, "words": [(seg_index, w.text, w.start, w.end)]})

    # 2. 平滑,再把換人的位置對齊到附近最明顯的停頓
    runs = _snap_boundaries_to_pauses(_smooth_runs(runs, turns), turns)

    # 3. 逐段決定名字
    def run_name_from_timeline(run: dict) -> str | None:
        duration_ms = max((run["end"] - run["start"]) * 1000, 1.0)
        coverage: dict[str, float] = {}
        for e in speaker_timeline:
            ov = _overlap(run["start"] * 1000, run["end"] * 1000, e["startMs"] - 500, e["endMs"] + 500)
            if ov > 0:
                coverage[e["speaker"]] = coverage.get(e["speaker"], 0.0) + ov
        candidates = {
            n: c
            for n, c in coverage.items()
            if c / duration_ms >= _RUN_NAME_MIN_COVERAGE and not _was_muted(muted, n, run["start"], run["end"])
        }
        if not candidates:
            return None
        # 換人瞬間常常兩個人的指示器都亮著(上一個人的還沒熄);聲音分群對到的那個人也在裡面的話就用他
        cluster_name = display_names.get(run["key"]) if run["key"] else None
        if cluster_name in candidates:
            return cluster_name
        return max(candidates, key=candidates.get)

    from opencc import OpenCC

    t2s = OpenCC("t2s")
    items: list[dict] = []
    for run in runs:
        is_caption = isinstance(run["key"], str) and run["key"].startswith(_CAPTION_NAME_PREFIX)
        if run["key"] == _OVERLAP_LABEL:
            speaker = _OVERLAP_LABEL
        elif is_caption:
            speaker = run["key"][len(_CAPTION_NAME_PREFIX):]
        else:
            speaker = run_name_from_timeline(run) or (
                display_names.get(run["key"], "不明講者") if run["key"] else "不明講者"
            )
        for word in run["words"]:
            items.append({"speaker": speaker, "caption": is_caption, "word": word, "start": word[2], "end": word[3],
                          "chars": _normalize_chars(word[1], t2s)})

    # 4. 同一口氣講的話整段決定講者
    _consolidate_utterances(items, turns, muted, known_names)

    labeled: list[LabeledSegment] = []
    named: list[tuple[str, dict]] = []
    for it in items:
        if named and named[-1][0] == it["speaker"]:
            named[-1][1]["words"].append(it["word"])
            named[-1][1]["end"] = it["end"]
        else:
            named.append((it["speaker"], {"start": it["start"], "end": it["end"], "words": [it["word"]]}))

    # 同一個講者相鄰的片段先接起來再組文字(字幕對到的字跟沒對到的字會分在不同片段,
    # 分開組字會在同一句話中間多出空格)。但遇到超過 _SPLIT_GAP_SEC 的停頓要切開:tab 軌裡「同一個人
    # 連續兩段」中間,常常是使用者在麥克風軌講話(2026-10 實測:另一位與會者的 4 個問題中間夾著使用者的回答,
    # 被接成 14~112 秒的一整段,合併後一問一答的順序全亂)。切開後交給 merge_and_format() 依時間跟麥克風軌
    # 交錯排序;中間沒有別人說話的話,它會再把同一個人的段落接回去,所以一般情況不受影響。
    for speaker, run in named:
        parts: list[str] = []
        last_seg = None
        piece_start = None
        last_end = None
        for seg_index, text, w_start, w_end in run["words"]:
            if last_end is not None and w_start - last_end > _SPLIT_GAP_SEC and parts:
                labeled.append(
                    LabeledSegment(start=piece_start, end=last_end, text="".join(parts).strip(), speaker=speaker)
                )
                parts, last_seg, piece_start = [], None, None
            if last_seg is not None and seg_index != last_seg:
                parts.append(" ")
            parts.append(text)
            last_seg = seg_index
            piece_start = w_start if piece_start is None else piece_start
            last_end = w_end
        if parts:
            labeled.append(
                LabeledSegment(start=piece_start, end=last_end, text="".join(parts).strip(), speaker=speaker)
            )
    return labeled


def filter_mic_by_self_activity(
    result: TranscriptResult, speaker_timeline: list[dict]
) -> tuple[TranscriptResult, list[Segment]]:
    """只保留「Meet 自己也偵測到你在講話」那些時段的麥克風軌內容,回傳 (保留的, 丟掉的)。

    麥克風軌是獨立錄的,不管 Meet 裡有沒有靜音都會錄到東西——喇叭傳出來的別人聲音、
    安靜片段的幻覺字(「嗯」「部份顏色做法」)都會被無條件標成使用者講的。Meet 的參與者
    面板上,使用者自己那一列的講話指示器只有在「沒靜音而且真的出聲」時才會亮
    (2026-10-06 實測:整場只有 234~279 秒亮過,正好是使用者唯一一次發言),拿它當過濾條件。
    時間軸裡完全沒有自己的紀錄(舊版擴充功能、指示器抓不到)時不過濾,維持原本行為。

    另外,有收到「麥克風開著」的時段(`UNMUTED_TIMELINE_SPEAKER`)時,在 Meet 裡靜音期間錄到的內容一律丟掉:
    別人聽不到的聲音不屬於這場會議(2026-11-02 站立會議:報告完靜音後錄到一聲「嗯」,指示器的延遲讓它
    落在講話時段的緩衝裡而沒被擋掉)。沒有這種時段(擴充功能抓不到開關狀態)時不套用這一層。
    """
    own = [e for e in speaker_timeline if e.get("speaker") == SELF_TIMELINE_SPEAKER]
    unmuted = [
        (e["startMs"] / 1000 - _UNMUTED_PAD_BEFORE_SEC, e["endMs"] / 1000 + _UNMUTED_PAD_AFTER_SEC)
        for e in speaker_timeline
        if e.get("speaker") == UNMUTED_TIMELINE_SPEAKER
    ] if unmuted_data_consistent(speaker_timeline) else []
    if not own and not unmuted:
        return result, []
    intervals = [
        (e["startMs"] / 1000 - 2.0, e["endMs"] / 1000 + 2.0) for e in _merge_timeline(own, 5000)
    ]

    def heard_in_meet(seg: Segment) -> bool:
        if not unmuted:
            return True
        share = sum(_overlap(seg.start, seg.end, a, b) for a, b in unmuted) / max(seg.end - seg.start, 1e-6)
        return share >= _UNMUTED_MIN_SHARE

    kept: list[Segment] = []
    dropped: list[Segment] = []
    for seg in result.segments:
        speaking = not own or any(_overlap(seg.start, seg.end, a, b) > 0 for a, b in intervals)
        if speaking and heard_in_meet(seg):
            kept.append(seg)
        else:
            dropped.append(seg)
    return TranscriptResult(text=" ".join(s.text for s in kept), segments=kept), dropped


def label_mic_segments(result: TranscriptResult, speaker_name: str = "你") -> list[LabeledSegment]:
    return [
        LabeledSegment(start=s.start, end=s.end, text=s.text, speaker=speaker_name)
        for s in result.segments
    ]


def merge_and_format(*segment_lists: list[LabeledSegment]) -> str:
    """把麥克風跟 tab 兩邊的標記結果依時間排序合併,同一講者連續的話合成一段。"""
    all_segments = sorted(
        (seg for lst in segment_lists for seg in lst), key=lambda s: s.start
    )

    lines: list[str] = []
    buffer: list[str] = []
    last_speaker: str | None = None

    def flush() -> None:
        if buffer:
            # 重複幻覺有時會被切到兩個相鄰的 Whisper 段落裡,單一段落各自都不到
            # 「連續4次」的清理門檻,合併成一整段講者發言後再清一次才清得掉。
            lines.append(f"[{last_speaker}] " + _collapse_repetition(" ".join(buffer)))
            buffer.clear()

    for seg in all_segments:
        if seg.speaker != last_speaker:
            flush()
            last_speaker = seg.speaker
        buffer.append(seg.text)
    flush()

    return "\n".join(lines)


def _get_polish_model():
    global _polish_model, _polish_tokenizer
    if _polish_model is None:
        import mlx_lm

        _polish_model, _polish_tokenizer = mlx_lm.load(POLISH_MODEL)
    return _polish_model, _polish_tokenizer


_POLISH_SYSTEM_PROMPT = (
    "你是逐字稿校對助手。使用者會給你一份會議逐字稿,每一行格式是「[講者] 內容」。"
    "請完成以下工作,但不要做任何超出範圍的事:\n"
    "1. 修順因為語音辨識造成的語句不通順、重複的贊詞贊字(例如「喂喂喂」可以修成「喂」),"
    "但保留原本語意,不要改變說話者原本想表達的內容。\n"
    "2. 輸出的行數必須跟輸入完全一樣多,絕對不能刪除、合併、拆分任何一行,"
    "即使某一行內容看起來奇怪或跟上下文無關,也只能在那一行內部修飾文字,不能整行刪掉。\n"
    "3. 完整保留每一行開頭的「[講者]」標籤格式,講者標籤本身不要翻譯或修改。\n"
    "4. 不要新增原文沒有的內容、不要做摘要、不要加任何說明或評論,只輸出整理後的逐字稿本身。\n"
    "簡體字/繁體字都可以,不用特別轉換,後面會有專門的工具處理文字轉換。"
)

# 曾經試過讓這個模型順便修正英文技術詞彙的語音辨識誤判(例如「Chrome Job」→「Cron Job」),
# 測過三種做法都失敗:逐段修正、加具體範例示範、把整份逐字稿一次讀完抽取「誤判詞清單」——
# 本機這顆 7B 模型(Qwen2.5-7B-Instruct-4bit)沒有能力可靠做這種需要跨行、跨上下文推斷的
# 任務,即使給了完全對應的範例也不會用,抽取清單那個做法甚至會生出「1248 -> 128」這種
# 沒意義的錯誤「修正」。這是模型能力的天花板,不是 prompt 寫法的問題,之後不要再花時間
# 用同一個模型重試這個方向;真的要做,需要換一個更大的本機模型,是完全不同層級的投資
# (下載大小、記憶體用量、每次處理時間都會明顯增加),要先跟使用者確認要不要投入才做。


# Whisper 對中文安靜片段有幾個全網知名的固定幻覺句(訓練資料汙染,模型看過太多這種
# 影片結尾字幕/工作人員名單),幾乎每次安靜片段都可能觸發,直接用規則砍掉,不靠 LLM 猜。
# 這些幻覺句有時候是整行,有時候會黏在一句真實內容的後面,所以用取代掉「命中的那一段
# 文字」而不是整行刪除,兩種情況都能處理。
_KNOWN_HALLUCINATION_PATTERNS = [
    # 「請不吝點讚 訂閱 轉發 打賞支持明鏡與點點欄目」及簡體版,只鎖住關鍵字本身跟
    # 關鍵字「之間」的間隔,不往前吃字元,才不會誤刪掉黏在前面的真實內容。
    re.compile(r"请?不吝.{0,4}点赞.{0,10}订阅.{0,10}转发.{0,10}打赏.{0,20}明镜与点点.{0,10}"),
    re.compile(r"請?不吝.{0,4}點讚.{0,10}訂閱.{0,10}轉發.{0,10}打賞.{0,20}明鏡與點點.{0,10}"),
    # 「字幕志願者 XX」這種影片工作人員名單汙染,後面只允許接一個人名長度的文字。
    re.compile(r"字幕(志愿者|志願者)[:：]?\s*[^\s，,。！!]{0,10}"),
    # 「感謝觀看」是另一個全網知名的 YouTube 影片結尾字幕汙染,真實會議結尾大多說
    # 「謝謝大家」而不會說「感謝觀看」(那是對著鏡頭觀眾說的話,不是對著會議參與者)。
    re.compile(r"感(谢|謝)(观看|觀看)"),
    # 「優優獨播劇場——YoYo Television Series Exclusive」:另一個全網知名的影片片頭字幕汙染
    # (2026-10-07 週會實測出現在安靜片段)。
    re.compile(r"(优优|優優)?(独播|獨播)(剧场|劇場)[—\-\s]*(YoYo[ A-Za-z]*Exclusive)?", re.IGNORECASE),
    re.compile(r"YoYo Television Series Exclusive", re.IGNORECASE),
    # 越南文的「記得訂閱、支持我的頻道喔」,也是 YouTube 片尾字幕汙染(2026-10-29 週會實測
    # 黏在一句真實內容前面)。
    re.compile(r"(và\s*)?(hãy\s*)?đăng ký kênh(\s*để ủng hộ kênh)?(\s*của mình)?\s*(nhé|nha)?[.!]?", re.IGNORECASE),
    # 以下參考網路上整理的 Whisper 幻覺句(openai/whisper discussion #1873、Trusted-Transcription 的
    # phantom_phrases、Koenecke et al. 2024 "Careless Whisper"):來源都是訓練資料裡的影片字幕。收錄原則一樣是
    # 「會議裡不可能有人這樣說」:字幕組署名、影片的訂閱呼籲、片尾語。指定中文時 Whisper 也會冒出英文版。
    # 不收「謝謝大家收看」「本期就到這裡」(線上講座、衝刺檢討真的可能這樣講)和網址(會議本來就會講)。
    re.compile(r"(字幕|中文字幕)?(由|by)?\s*Amara\.org\s*(社區|社区|社群)?(提供)?(的字幕)?", re.IGNORECASE),
    re.compile(r"(please\s+)?(like\s+and\s+)?subscribe\s+to\s+(my|the|our)\s+channel[.!]?", re.IGNORECASE),
    re.compile(r"thank(s| you)\s+for\s+watching[.!]?", re.IGNORECASE),
    re.compile(r"see\s+you\s+in\s+the\s+next\s+video[.!]?", re.IGNORECASE),
    # 「點讚、訂閱」本身可能是在講行銷數字(「這支影片的點讚、訂閱數」),要有「請/記得」開頭或「頻道」結尾才算呼籲
    re.compile(r"(請|请|記得|记得)+\s*(點讚|点赞)[、,,\s]*(訂閱|订阅)(我的|我們的|我们的)?(頻道|频道)?"),
    re.compile(r"(點讚|点赞)?[、,,\s]*(訂閱|订阅)(一下)?(我的|我們的|我们的)(頻道|频道)"),
    re.compile(r"(我們|我们)?下期(影片|視頻|视频|節目|节目)?再見|(我們|我们)?下期(影片|視頻|视频|節目|节目)?再见"),
]


# 只由「中英文以外的文字」組成的字(俄文、泰文、韓文、日文假名)。會議鎖定中文,這類字幾乎都是安靜片段
# 的幻覺(2026-10 實測:「х」「일단」單獨出現在麥克風軌),整個字丟掉。
_FOREIGN_SCRIPT_ONLY = re.compile(r"[\s\u0400-\u04FF\u0E00-\u0E7F\uAC00-\uD7AF\u1100-\u11FF\u3040-\u30FF.,!?]+")


def _strip_hallucinated_words(result: TranscriptResult) -> TranscriptResult:
    """跟 `_strip_known_hallucinations()` 做一樣的事,但作用在剛轉錄完、還沒被拆成
    講者行之前的原始 segment/word 層級,不是最終合併好的逐字稿文字。

    這一層一定要有,不能只靠 `polish_transcript()` 那邊的行級清除:已知幻覺句常常
    出現在語者分離也很不穩定的安靜/雜訊片段,`label_tab_segments()` 的逐字語者
    拆分邏輯(見那邊的說明)可能會把同一句幻覺句切成好幾個貼著不同假講者標籤的
    小行——實測真的發生過(字幕志願者 XX 這句被切成「貓 中文字幕」/「志」/「願者」/
    「李宗盛」四行,每行貼不同的假講者)。切開之後幻覺句的關鍵字就不再連續出現在
    同一行文字裡,靠行為單位比對的行級清除規則就抓不到,幻覺句反而會冒充成好幾個
    人講的話流出去。在這裡、幻覺句還沒被拆開之前,直接以「字」為單位砍掉相關的
    Word,拆分邏輯根本看不到這些字,就沒有東西可以拆。
    """
    kept_segments: list[Segment] = []
    for seg in result.segments:
        if not seg.words:
            cleaned = seg.text
            for pattern in _KNOWN_HALLUCINATION_PATTERNS:
                cleaned = pattern.sub("", cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                kept_segments.append(Segment(start=seg.start, end=seg.end, text=cleaned, words=[], avg_logprob=seg.avg_logprob))
            continue

        offsets = []
        pos = 0
        for w in seg.words:
            offsets.append((pos, pos + len(w.text)))
            pos += len(w.text)
        full = "".join(w.text for w in seg.words)

        removed_ranges: list[tuple[int, int]] = []
        for pattern in _KNOWN_HALLUCINATION_PATTERNS:
            removed_ranges.extend((m.start(), m.end()) for m in pattern.finditer(full))

        def is_removed(w_start: int, w_end: int) -> bool:
            return any(w_start < r_end and w_end > r_start for r_start, r_end in removed_ranges)

        kept_words = [
            w
            for w, (o_start, o_end) in zip(seg.words, offsets)
            if not is_removed(o_start, o_end) and not _FOREIGN_SCRIPT_ONLY.fullmatch(w.text.strip() or "x")
        ]
        cleaned_text = "".join(w.text for w in kept_words).strip()
        if not kept_words or not cleaned_text:
            continue
        kept_segments.append(
            Segment(start=kept_words[0].start, end=kept_words[-1].end, text=cleaned_text, words=kept_words, avg_logprob=seg.avg_logprob)
        )

    full_text = _collapse_repetition(" ".join(s.text for s in kept_segments))
    return TranscriptResult(text=full_text, segments=kept_segments)


def _strip_known_hallucinations(text: str) -> str:
    lines = text.split("\n")
    kept = []
    for line in lines:
        cleaned = line
        for pattern in _KNOWN_HALLUCINATION_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        cleaned = cleaned.strip()
        # 整行都是幻覺句、清完變成只剩講者標籤或完全空白的話,直接丟掉這一行
        if not cleaned or re.fullmatch(r"\[.*\]\s*", cleaned):
            continue
        # 幻覺句黏在行首被清掉時,講者標籤後面會多出空白
        cleaned = re.sub(r"^(\[[^\]]*\])\s+", r"\1 ", cleaned)
        kept.append(cleaned)
    return "\n".join(kept)


_POLISH_LINE_SYSTEM_PROMPT = (
    "你是逐字稿校對助手。使用者會給你會議中「同一個人連續說的一段話」,是語音辨識的結果,"
    "通常沒有標點符號。請完成以下工作,不要做任何超出範圍的事:\n"
    "1. 加上正確的標點符號(,。?、),在語意完整的地方斷句。\n"
    "2. 只修掉明顯重複的贅字(例如「然後然後」),不改寫、不摘要、不增加也不刪除內容。\n"
    "3. 英文單字、數字、工單編號(例如 ABC1234)、人名一律原樣保留,不要翻譯或改寫。\n"
    "4. 只輸出整理後的那段話本身,不要加講者名稱、引號或任何說明。"
)


# 簡轉繁用 OpenCC 的 s2tw(只轉字,不換詞)。不用 s2twp(會連兩岸用語一起換):Whisper 寫下的就是說話的人
# 實際講的詞,只是用簡體字,換詞等於改掉別人說的話——11/03 統計所有逐字稿,s2twp 把「優化」換成「最佳化」
# 20 幾次、「文件」→「檔案」、「腳本」→「指令碼」、「審核」→「稽核」,甚至「圖像」→「影象」、「權限」→「許可權」。
# 下面是 OpenCC 一個字對到好幾個繁體字、看不到上下文時固定會選錯的,轉完再改回來
# (2026-10-29 週會實測:「这只有」→「這隻有」、「签协议」→「籤協議」;「脚本里面」的「里」不會轉)。
_TW_FIXES = [
    (re.compile(r"隻(有|是|要|能|針對|好|會|剩|差|在|不過)"), r"只\1"),
    (re.compile(r"籤((這|那|了|過|一)?(個|份)?(約|協議|合約|署|名|核|收|字))"), r"簽\1"),
    (re.compile(r"里(面|頭|邊)"), r"裡\1"),
]


def _to_traditional(text: str, converter) -> str:
    text = converter.convert(text)
    for pattern, repl in _TW_FIXES:
        text = pattern.sub(repl, text)
    return text


_PUNCT_CHARS = ",，。、?？!！;；:："
_IS_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def _project_punctuation(original: str, polished: str, t2s) -> str:
    """只採用 LLM 加的標點,字一律用原文的。

    LLM 就算被要求「不改寫」,還是會順手換字(2026-11-03 實測:「還有文件」→「和檔案」、「開始進到」→
    「進入到」)。這裡把 LLM 的輸出跟原文逐字對齊(都先轉簡體、去掉標點空白再比),LLM 在某個字後面
    加的標點就加在原文同一個字後面;LLM 換掉、加進來的字都不採用。原文有、LLM 拿掉的字也保留,
    只有「緊鄰重複」的贅字(例如「然後然後」)才跟著刪。
    """
    import difflib

    def norm(ch: str) -> str:
        return t2s.convert(ch).lower()

    orig_chars = [c for c in original if not c.isspace() and c not in _PUNCT_CHARS]
    # 原文裡每個字前面有沒有空白(英文單字之間要保留)
    space_before: list[bool] = []
    prev_space = False
    for c in original:
        if c.isspace():
            prev_space = True
            continue
        if c in _PUNCT_CHARS:
            continue
        space_before.append(prev_space)
        prev_space = False

    pol_chars: list[str] = []
    punct_after: dict[int, str] = {}
    for c in polished:
        if c in _PUNCT_CHARS:
            if pol_chars:
                punct_after[len(pol_chars) - 1] = c
        elif not c.isspace():
            pol_chars.append(c)

    a = "".join(norm(c) for c in orig_chars)
    b = "".join(norm(c) for c in pol_chars)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    keep = [True] * len(orig_chars)
    punct_for_orig: dict[int, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                if j1 + k in punct_after:
                    punct_for_orig[i1 + k] = punct_after[j1 + k]
        elif tag == "delete":
            seg = a[i1:i2]
            if a[max(0, i1 - len(seg)):i1] == seg or a[i2:i2 + len(seg)] == seg:
                for k in range(i1, i2):
                    keep[k] = False
        elif tag == "replace" and i2 > i1 and (j2 - 1) in punct_after:
            # LLM 換掉的那段字不採用,但它後面加的標點要留在原文那段的後面
            punct_for_orig[i2 - 1] = punct_after[j2 - 1]
        elif tag == "insert" and i1 > 0 and (j2 - 1) in punct_after:
            punct_for_orig.setdefault(i1 - 1, punct_after[j2 - 1])

    out: list[str] = []
    prev_char = ""
    for i, c in enumerate(orig_chars):
        if not keep[i]:
            continue
        if space_before[i] and out and _IS_ASCII_WORD.match(c) and _IS_ASCII_WORD.match(prev_char):
            out.append(" ")
        out.append(c)
        prev_char = c
        if i in punct_for_orig:
            out.append(punct_for_orig[i])
            prev_char = punct_for_orig[i]
    return "".join(out)


def _polish_line(line: str, model, tokenizer, converter, throttle=None) -> str:
    """一次只修飾一行(一個人的一段發言)。

    原本是 12 行一批丟給 LLM、再檢查輸出行數有沒有一樣,對不上就整批放棄修飾。實測
    (2026-10-06)五批裡有四批行數對不上、整批退回沒有標點的原始文字,只有中間一批有
    標點,同一份逐字稿前後風格不一致、大部分沒有斷句。一次只給一行,就不存在「行數對
    不上」這件事,講者標籤也完全不經過 LLM。內容不遺失的保證改用長度檢查:輸出長度
    偏離原文太多(被刪減或被加料)就放棄修飾,用原始文字。

    「多人同時發言」的行、以及太短的行不送進 LLM(見 `_OVERLAP_LABEL` 的說明:不確定的
    內容只標示、不讓 LLM 腦補)。
    """
    import mlx_lm
    from mlx_lm.sample_utils import make_sampler

    fallback = _to_traditional(line, converter)
    match = re.match(r"^(\[[^\]]*\])\s*(.*)$", line)
    if not match:
        return fallback
    label, body = match.group(1), match.group(2).strip()
    if label == f"[{_OVERLAP_LABEL}]" or len(body) < 8:
        return fallback

    messages = [
        {"role": "system", "content": _POLISH_LINE_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    # temp=0(貪婪解碼):校對工作要的是穩定、保守,不需要創造力。
    # 用 stream_generate 逐段拿輸出,才能每算一小段就依節流比例歇一下(見 throttle.py)。
    result = throttled_generate(
        mlx_lm.stream_generate(
            model,
            tokenizer,
            prompt,
            max_tokens=len(body) * 2 + 64,
            sampler=make_sampler(temp=0.0),
        ),
        throttle,
    )
    result = "".join(l.strip() for l in result.split("\n") if l.strip())
    result = re.sub(r"^\[[^\]]*\]\s*", "", result)
    from opencc import OpenCC

    result = _project_punctuation(body, result, OpenCC("t2s"))

    def content_length(text: str) -> int:
        return len(re.sub(r"[\s,,。.、?？!!;;::「」\"']", "", text))

    original_len = content_length(body)
    if not result or not (0.85 * original_len <= content_length(result) <= 1.15 * original_len):
        return fallback
    return f"{label} {_to_traditional(result, converter)}"


GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "glossary.txt"  # 專案最外層,使用者會自己改
# 隨專案附上、所有人適用的常見辨識錯誤(只收換掉絕對不會誤判的,見檔案開頭的收錄原則)
COMMON_CORRECTIONS_PATH = Path(__file__).resolve().parent / "common_corrections.txt"


def apply_glossary(text: str) -> str:
    """套用 `glossary.txt` 的固定錯字對照(每次呼叫都重讀檔案,改完存檔就生效)。

    Whisper 對同事名字跟常用工作詞會穩定地聽錯成同幾種寫法(例如同一個內部用語每次都被聽成同一個錯字),
    換模型、加提示詞都試過沒有穩定改善(見 CLAUDE.md),用確定性的字串替換最可靠。
    左邊的寫法會連同簡體/繁體版本一起比對;含英文字母的不分大小寫。長的先換,避免短詞先吃掉。
    """
    sources = [p for p in (COMMON_CORRECTIONS_PATH, GLOSSARY_PATH) if p.exists()]
    if not sources:
        return text
    from opencc import OpenCC

    s2t, t2s = OpenCC("s2t"), OpenCC("t2s")
    rules: list[tuple[str, str]] = []
    lines = [l for p in sources for l in p.read_text(encoding="utf-8").splitlines()]
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrongs, right = line.split("=>", 1)
        right = right.strip()
        for w in wrongs.split("|"):
            w = w.strip()
            if not w:
                continue
            for variant in {w, s2t.convert(w), t2s.convert(w)}:
                rules.append((variant, right))
    # 同一個錯的寫法兩邊都有時,以使用者自己的 glossary.txt(後讀)為準
    rules = list(dict(rules).items())
    for wrong, right in sorted(rules, key=lambda r: -len(r[0])):
        flags = re.IGNORECASE if re.search(r"[A-Za-z]", wrong) else 0
        pattern = re.escape(wrong)
        # 開頭/結尾是英文字母或數字時,前後不能緊接著同類字元(「10座」不能吃掉「OP110座」裡的字)
        if re.match(r"[A-Za-z0-9]", wrong):
            pattern = r"(?<![A-Za-z0-9])" + pattern
        if re.search(r"[A-Za-z0-9]$", wrong):
            pattern += r"(?![A-Za-z0-9])"
        text = re.sub(pattern, lambda m: right, text, flags=flags)
    return text


def polish_transcript(
    text: str, on_progress: Callable[[int, int], None] | None = None, throttle=None
) -> str:
    """把逐字稿修順語句、拔掉明顯的辨識錯誤/幻覺雜訊句子,並統一轉成繁體中文。

    分兩層,各自做自己最可靠的事:
    - 語句通順/拔雜訊句子:交給本機小型語言模型(mlx-lm,不是雲端 API,沒有 token 費用),
      這種需要理解語意的工作才是 LLM 的強項。一次只處理一行(一個人的一段發言),見 `_polish_line()`。
    - 簡體轉繁體:交給 OpenCC(專門的簡繁轉換函式庫,一個字一個字精確轉換,不會漏)。
      LLM 在「同時要修語句又要精確轉換每個字」這種機械式任務上不夠可靠(實測會漏字),
      拆開來各自用最適合的工具處理才穩。

    on_progress:選填,每處理完一行就呼叫一次 `on_progress(已完成行數, 總行數)`——
    這是逐字稿處理過程中最耗時的一步,分批回報進度給 `app.py` 更新工作狀態用
    (見那邊的 `set_stage`),讓使用者在這一步不會盯著同一個百分比卡很久。
    """
    if not text.strip():
        return text

    text = _strip_known_hallucinations(text)
    if not text.strip():
        return text

    lines = [l for l in text.split("\n") if l.strip()]

    from opencc import OpenCC

    converter = OpenCC("s2tw")
    model, tokenizer = _get_polish_model()
    if throttle is not None:
        throttle.start(len(lines))

    polished_all: list[str] = []
    for i, line in enumerate(lines):
        polished_all.append(_polish_line(line, model, tokenizer, converter, throttle))
        if throttle is not None:
            throttle.line_done()
        if on_progress:
            on_progress(i + 1, len(lines))

    return apply_glossary("\n".join(polished_all))
