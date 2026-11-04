# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

GMScribe:Chrome 擴充功能 + 本機 Python 伺服器,錄下 Google Meet 會議,產出每一行標出真實姓名、繁體中文的逐字稿。
轉錄、語者分離、潤飾全部在本機(Apple Silicon、MLX)執行。使用方式看 `README.md`;這份是給改程式的人看的。

## 常用指令

```bash
./setup.sh                                   # 一鍵安裝(可重複執行);手動安裝見下面三行
python3.13 -m venv .venv && .venv/bin/pip install -r core/server/requirements.txt
# silero-vad 要分開裝、--no-deps(它要求 torchaudio<2.10,跟 pyannote.audio 4.0 衝突,一起裝會弄壞 torchcodec)
.venv/bin/pip install --no-deps silero-vad==6.2.2 onnxruntime==1.30.0

.venv/bin/python3 core/server/gmscribe_server.py   # 啟動伺服器(或 popup 的「啟動錄音伺服器」)
curl -s http://localhost:8765/health

# 不透過擴充功能跑整條流程(curl 沒有 Origin header,伺服器允許)
curl -s -X POST http://localhost:8765/upload -F "mic_audio=@mic.webm" -F "tab_audio=@tab.webm" -F "title=test" \
  -F "mic_speaker_name=我" -F 'other_names=["A","B"]' \
  -F "speaker_timeline=@speaker_timeline.json" -F "caption_log=@caption_log.json"
curl -s http://localhost:8765/status/<job_id>

# 單獨測某個函式(最快的迭代方式)
.venv/bin/python3 -c "import sys; sys.path.insert(0, 'core'); from server import transcriber as t; print(t.apply_glossary('Coreview'))"
```

沒有測試套件,擴充功能沒有 build 流程(純 MV3 JS)。改 `extension/*` 要在 `chrome://extensions` 重新載入
(`background.js` 會幫已開著的 Meet 分頁補注入 content script);改 `core/server/*` 要重啟伺服器。
驗證方式是拿 `recordings/<場次>/處理過程/` 裡的錄音與 JSON 重跑,新舊版比對輸出。

## 架構

```
popup.js ──▶ background.js(service worker)──▶ offscreen.js(MediaRecorder,兩條獨立音軌)
   │                │ chrome.alarms                        │ POST /upload
   ▼                ▼                                      ▼
core/native_host/  content.js(注入 Meet 分頁:         core/server/app.py ──▶ transcriber.py(背景 thread)
(開關伺服器)        名單、誰在講話、字幕、靜音狀態)
```

- 麥克風與分頁音訊錄成**兩個獨立的 MediaRecorder,絕不混音**:麥克風軌直接標成使用者本人,是講者標示的基礎。
- 伺服器是 FastAPI 單一 process;工作狀態存在記憶體的 `jobs`(重啟會遺失,檔案已在 `recordings/`)。
  `_job_lock` 讓工作一次只跑一個(兩份模型同時載入會爆記憶體)。
- 擴充功能只能透過 Native Messaging 開關伺服器,`server_control.py` 只接受 `status`/`start`/`stop`。
- 路徑都用 `Path(__file__).parents[...]` 推算;搬資料夾時要一起改 `app.py`、`transcriber.py`、`server_control.py`、
  `server_control.sh`、`install.sh`。`extension/` 搬家會讓未封裝擴充功能的 ID 改變。

## content.js:誰在講話

全部靠 Meet 未公開的 DOM(class 名稱隨時會變),原則是**優雅退化**:任何訊號失效只能讓講者標示變差,
不能影響錄音與轉錄。輔助步驟都包在 `safely()` 裡。介面文字相關的集中在 `MEET_UI_TEXT`(英文、繁中);
能用圖示名稱判斷的就不用文字(字幕開關 `translate_cc`、掛斷 `call_end`、靜音 `mic_off`)。

| 訊號 | 寫進 timeline 的名字 | 備註 |
|---|---|---|
| 字幕(主要) | 講者名 | 每個發言者都有名字;同一人連續講話是同一個節點原地加長。另存 `caption_log.json` 給伺服器做文字對齊 |
| People 面板列燈號 | 講者名 | 大型會議只有清單前 8~10 列會更新 |
| 視訊格燈號 | 講者名 | 名字與「哪一格是自己」靠 `data-participant-id` 對面板;螢幕分享時視訊格消失 |
| 自己的燈號 | `__self__` | 只看面板自己那列與字幕上的自己 |
| 自己的麥克風 | `__unmuted__` | 工具列 `[data-is-muted]` 且圖示以 `mic` 開頭的按鈕 |
| 別人的靜音 | `__muted__:名字` | 只有視訊格看得到(`mic_off`);沒有視訊格的人不記 |

**自我保護**(使用者不會看除錯資料,所以每個訊號都要有備援讀法,讀到不可能的結果就自動不採用):
- `isSelfTile()`:編號對面板 → 自己才有的圖示(`frame_person`/`visual_effects`);一次判斷出 >1 格就一格都不算。
- `getTileName()`:編號對面板 → 舊名字標籤 → 格子內 aria-label 提到的已知與會者(只有一個符合才採用)。
- 視訊格燈號 ≥4 格同時亮約 10 秒 → 這場停用。伺服器端:`mute_data_consistent()` 拿字幕對質別人的靜音紀錄、
  `unmuted_data_consistent()` 拿自己的燈號對質開麥紀錄,矛盾就整份不用。
- 新增訊號時照這個模式做,並在 `app._signal_health()` 加一項檢查(寫進 `處理過程/訊號檢查.txt`,給回報問題用)。
- 錄音中 `checkSpeakerSignals()` 找不到字幕/名單時徽章變 `REC!`(離開會議後不檢查)。

其他:名單在錄音中每 3 秒累積、停止時再讀一次(使用者常先離開會議才按停止);背景分頁會被凍結,
`background.js` 用 alarms 定期叫醒;`speaker_debug.json` 是診斷紀錄,講者標錯時先看它,不要用猜的。

## 伺服器處理流程(`_run_transcription`)

1. **轉錄**(mlx-whisper `large-v3-turbo`,`PRIMARY_LANGUAGE="zh"`),兩軌分開:silero-vad 切出有聲片段各自送
   Whisper;`avg_logprob < _MIN_AVG_LOGPROB` 的段落不收(雜音被猜成的亂碼);`_strip_hallucinated_words()` 在字級
   砍掉已知幻覺句(影片片尾、字幕組署名;只收「會議裡不可能有人這樣說」的)。
2. **麥克風軌** `filter_mic_by_self_activity()`:只留 `__self__` 附近、而且在 `__unmuted__` 時段內的內容
   (靜音時錄到的別人聽不到)。沒有這些紀錄時不過濾。
3. **其他人那一軌**:一個其他與會者就整軌標他;兩人以上用 pyannote `community-1` 分群後 `label_tab_segments()`:
   - 逐字決定講者:字幕文字對得上(`_caption_word_speakers()`)優先,其餘用分群。
   - `_estimate_timeline_lag()`:Meet 燈號比聲音慢(開口後 1~2 秒亮、講完多亮 2~3 秒),每場量出來校正。
   - 每段命名看校正後的 timeline;好幾人都符合時選聲音分群對到的;靜音中的人不列入候選。
   - `_consolidate_utterances()`:停頓 <0.8 秒且分群相同的字視為同一口氣,整段計票決定講者;
     名單上只剩一個沒靜音的人就直接是他。換人位置已經落在明顯停頓上就不再搬動。
   - 同一講者遇到 >1 秒停頓就切開,讓 `merge_and_format()` 跟麥克風軌依時間交錯(一問一答才不會被接成兩大段)。
4. **合併** → `apply_glossary()`(內建 `core/server/common_corrections.txt` + 使用者的 `glossary.txt`,後者優先)
   → `處理過程/逐字稿_潤飾前.txt`。
5. **潤飾** `polish_transcript()` → `逐字稿.txt`。
6. 不管成功失敗都 `release_models()`(模型常駐曾吃光記憶體)。

## 潤飾的規則

- 本機 `Qwen2.5-7B-Instruct-4bit`,一次一行,講者標籤不經過 LLM。
- **LLM 只負責標點**:`_project_punctuation()` 把 LLM 輸出對齊原文,只搬回標點,字一律用原文的(LLM 被要求
  不改寫還是會換字)。只有緊鄰重複的贅字跟著刪。
- 簡轉繁用 OpenCC **`s2tw`(只轉字、不換詞)**:Whisper 寫的就是說話者用的詞,`s2twp` 會把優化換成最佳化、
  文件換成檔案。`_TW_FIXES` 修一字多繁固定選錯的。
- **不確定的內容不給 LLM 修**(多人同時發言、太短的行):LLM 修不了它沒聽過的音訊,只會編出通順但錯的句子。
- 錯字修正只用確定性的字串替換。內建清單只收「錯的寫法在任何情境下都不是正確用詞」的項目,加之前要套到所有
  舊逐字稿逐筆確認沒有誤判。
- 效能節流 `throttle.py`:每算約 0.2 秒就歇一下;記憶體吃緊時反而拉高下限(歇著的空檔模型會被擠到 swap)。
  popup 滑桿呼叫 `POST /polish-speed/{job_id}`,潤飾前選的值存在 `pending_polish_duty`。

## 安全

- `only_local_extension` middleware:Host 必須是 localhost(防 DNS rebinding);有 Origin 時必須是這個擴充功能
  (ID 在 `core/native_host/.extension_id`)。光靠 CORS 不夠:multipart 是簡單請求,CORS 不擋送出。
- `manifest.json` 的 CSP 只允許連 `localhost:8765`。
- pyannote 載模型用 `weights_only=False`,所以 `DIARIZATION_REVISION` 鎖定 commit。
- 會議紀錄 skill:逐字稿與與會者名稱是任何人都能控制的內容(prompt injection 入口),skill 用 `disallowed-tools`
  拿掉 Bash/WebFetch/WebSearch/Agent,`scripts/prepare.py` 先用規則掃描可疑內容。
- 對外連線只有 Hugging Face 模型下載,和 `updates.py` 一天最多一次查 GitHub 最新 release 版本號
  (`UPDATE_REPO`;release tag 要跟 `manifest.json` 的 version 對得上;`.no-update-check` 檔案可關掉)。
- 不進版控:`recordings/`、`glossary.txt`、`core/logs/`、`.extension_id`、`.no-update-check`。

## 每場會議的輸出(`recordings/<YYYY-MM-DD HH.MM> <會議名稱>/`)

最外層只有 `逐字稿.txt`(與 skill 產生的 `會議紀錄_*.md`),其餘在 `處理過程/`:兩條原始錄音(重跑的唯一來源)、
各軌轉錄、`逐字稿_潤飾前.txt`、被濾掉的內容(麥克風、低信心)、`訊號檢查.txt`、`roster_input.json`、
`speaker_timeline.json`、`caption_log.json`、`speaker_debug.json`、pyannote 結果。
除錯順序:`訊號檢查.txt` → `roster_input.json` → `speaker_debug.json` → `caption_log.json` → `tab_diarization.json`。

## 試過、不採用的做法

- **換 Qwen3-ASR**:專有名詞互有勝負、會漏數字;逐字時間戳配 pyannote 讓分人結果破碎很多。
- **給 Whisper `initial_prompt`(詞彙或名單)**:提示裡的詞會變對,其他地方會被改寫或變差(「以上」變「以下」)。
- **LLM 事後修專有名詞 / 讓 LLM 改字**:會把正確的詞帶偏。
- **拿 Meet 字幕當第二個辨識結果互相校正**:兩邊常錯在同一處,最需要修的人名反而救不回來。
- **讓 People 面板所有列都更新**(縮放、捲動):只有前 8~10 列會動。
- **用假麥克風訊號自動測試講話偵測**:Meet 不會重新呼叫 `getUserMedia`,只能真人測。
- **分會議類型的模式切換**:同一套流程要能處理站立會議與自由討論。
- **讓分頁保持清醒**:強制前景會搶焦點;播放靜音音訊會被錄進分頁音軌。

## 已知限制

- 依賴 Meet DOM;官方替代方案(Meet Add-ons 拿不到音訊、Meet Media API 需要 Workspace)不適用個人帳號。
- Meet 分頁音訊已混成單一聲道,真的同時講話無法分開。
- 聲音相近的人、同一人講很久,pyannote 可能分錯。
