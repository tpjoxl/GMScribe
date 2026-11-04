const SERVER = "http://localhost:8765";
// 背景輪詢間隔:popup 開著的時候每隔這麼久自動查一次進度,不用一直手動按按鈕。
// popup 關掉之後 JS 會被 Chrome 直接砍掉,輪詢本來就會停,下次打開 popup 會重新查一次現況。
const POLL_INTERVAL_MS = 4000;

const toggleBtn = document.getElementById("toggle");
const checkBtn = document.getElementById("check");
const statusEl = document.getElementById("status");
const progressEl = document.getElementById("progress");
const hideCaptionsEl = document.getElementById("hideCaptions");
const hidePeoplePanelEl = document.getElementById("hidePeoplePanel");
const speedBox = document.getElementById("speedBox");
const speedInput = document.getElementById("speed");
const speedVal = document.getElementById("speedVal");
const speedNote = document.getElementById("speedNote");
let speedJobId = null;
let speedDragging = false;
const transcriptEl = document.getElementById("transcript");
const copyBtn = document.getElementById("copy");
const retryBtn = document.getElementById("retry");
const serverBtn = document.getElementById("server");
const serverDot = document.getElementById("serverDot");
const serverText = document.getElementById("serverText");

// ---- 錄音伺服器開關 ----
// 擴充功能不能直接執行本機程式,透過 Native Messaging host(native_host/server_control.py,
// 用 native_host/install.sh 安裝一次)啟動/關閉 uvicorn。
const NATIVE_HOST = "com.gmscribe.server_control";
let serverUp = false;
let isRecording = false;
let jobProcessing = false;

function callServerControl(cmd) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd }, (resp) => {
        if (chrome.runtime.lastError) resolve({ error: chrome.runtime.lastError.message });
        else resolve(resp || {});
      });
    } catch (err) {
      resolve({ error: err.message });
    }
  });
}

function renderButtons() {
  serverDot.className = "dot" + (isRecording ? " rec" : serverUp ? " on" : "");
  serverText.textContent = isRecording ? "錄音中" : serverUp ? "伺服器運作中" : "伺服器未啟動";
  serverBtn.textContent = serverUp ? "關閉錄音伺服器" : "啟動錄音伺服器";
  serverBtn.className = serverUp ? "outline-danger" : "";
  // 錄音中、或還有逐字稿在處理時不能關伺服器(會讓上傳失敗或處理中斷)
  serverBtn.disabled = serverUp && (isRecording || jobProcessing);
  serverBtn.title = serverBtn.disabled ? "錄音中或逐字稿處理中,不能關閉伺服器" : "";
  toggleBtn.textContent = isRecording ? "停止錄音" : "開始錄音";
  toggleBtn.className = isRecording ? "danger" : "primary";
  // 停止錄音永遠可以按;開始錄音要伺服器有開
  toggleBtn.disabled = !isRecording && !serverUp;
}

async function refreshServerState() {
  serverUp = await serverIsUp();
  renderButtons();
}

serverBtn.addEventListener("click", async () => {
  const starting = !serverUp;
  serverBtn.disabled = true;
  serverBtn.textContent = starting ? "啟動中..." : "關閉中...";
  const resp = await callServerControl(starting ? "start" : "stop");
  if (resp.error) {
    statusEl.textContent = resp.error.includes("not found")
      ? "還沒安裝伺服器開關元件。請在終端機執行一次:./native_host/install.sh"
      : "伺服器開關失敗:" + resp.error;
  } else if (resp.message) {
    statusEl.textContent = resp.message;
  }
  chrome.runtime.sendMessage({ type: "REFRESH_SERVER_ICON", target: "background" });
  await refreshServerState();
});

async function serverIsUp() {
  try {
    const res = await fetch(`${SERVER}/health`, { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

const SERVER_DOWN_MESSAGE =
  "連不到本機伺服器 (localhost:8765),請先啟動伺服器(雙擊 start_server.command)。";

let myName = null;
let otherNames = [];
let pollTimer = null;

function setUiRecording(recording) {
  isRecording = recording;
  renderButtons();
}

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

// 潤飾階段的速度控制:伺服器依電腦當下狀況給出允許範圍(min~max),預設在最低(最不影響電腦),
// 使用者拉高才會用比較多效能。見 server/throttle.py。
function renderSpeed(jobId, speed) {
  if (!speed) {
    speedBox.style.display = "none";
    return;
  }
  speedJobId = jobId;
  speedBox.style.display = "block";
  const lo = Math.round(speed.min * 100);
  const hi = Math.round(speed.max * 100);
  speedInput.min = lo;
  speedInput.max = hi;
  speedInput.disabled = lo >= hi;
  if (!speedDragging) speedInput.value = Math.round(speed.current * 100);
  if (speed.pending) {
    // 還在轉錄、分辨講者,這兩步的速度不能調;先選好,到潤飾那一步才套用。
    speedVal.textContent = `${speedInput.value}%`;
    speedNote.textContent = "只在最後的潤飾階段生效,可以先選好。";
    speedNote.style.display = "block";
    return;
  }
  speedVal.textContent = `${speedInput.value}% · 預估還要 ${formatElapsed(speed.eta_seconds)}`;
  // 只講使用者能做的事;可調範圍、為什麼這樣限制是內部細節,不用說
  // 可調範圍跟記憶體狀況要讓使用者知道;範圍怎麼算出來、多久更新一次是內部細節,不用說
  const range = lo >= hi ? "電腦目前比較忙,暫時不能調整。" : `目前可調範圍 ${lo}%~${hi}%。`;
  speedNote.textContent = range + (speed.memory_tight ? "記憶體不太夠,關掉暫時不用的程式會快很多。" : "");
  speedNote.style.display = "block";
}

speedInput.addEventListener("input", () => {
  speedDragging = true;
  speedVal.textContent = `${speedInput.value}%`;
});
speedInput.addEventListener("change", async () => {
  speedDragging = false;
  if (!speedJobId) return;
  try {
    const res = await fetch(`${SERVER}/polish-speed/${speedJobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duty: Number(speedInput.value) / 100 }),
    });
    if (res.ok) renderSpeed(speedJobId, await res.json());
  } catch (_) {
    // 連不到就算了,下次輪詢會顯示伺服器實際用的值
  }
});

// 字幕/與會者名單要不要在畫面上藏起來(預設不藏),存在 chrome.storage.local。
// 錄音中改設定會馬上套用到正在錄音的分頁。
function visibilityPrefs() {
  return { hideCaptions: hideCaptionsEl.checked, hidePeoplePanel: hidePeoplePanelEl.checked };
}

// 有新版本時提示(伺服器一天最多問一次 GitHub,只問版本號)。Meet 改版造成講者偵測失效時,更新是使用者唯一能做的事。
let updateInfo = null;
async function checkForUpdate() {
  try {
    updateInfo = await (await fetch(`${SERVER}/version`)).json();
  } catch (_) {
    return;
  }
  const banner = document.getElementById("updateBanner");
  if (!updateInfo.update_available) return;
  // 版本號來自 GitHub,只當文字放,不當 HTML 解讀
  const code = document.createElement("code");
  code.textContent = "./update.sh";
  banner.replaceChildren(
    `🆕 有新版本 ${updateInfo.latest}(目前 ${updateInfo.current})。在終端機到 GMScribe 資料夾執行 `,
    code,
    ",完成後到 chrome://extensions 按 GMScribe 的重新整理。"
  );
  banner.style.display = "block";
}
checkForUpdate();

// 同時開著多個 Meet 分頁時提醒:一次只錄一場,麥克風也分不出你是在哪一場講話。
chrome.tabs.query({ url: "https://meet.google.com/*" }).then((tabs) => {
  const meetings = tabs.filter((t) => /meet\.google\.com\/(meet\/)?[a-z]{3}-[a-z]{4}-[a-z]{3}/.test(t.url || ""));
  document.getElementById("multiMeetWarn").style.display = meetings.length > 1 ? "block" : "none";
});

// 設定區預設摺起來,摺疊時在標題列顯示目前的設定;展開/摺疊狀態記住。
const prefsBox = document.getElementById("prefsBox");
function renderPrefsSummary() {
  const hidden = [hideCaptionsEl.checked && "字幕", hidePeoplePanelEl.checked && "名單"].filter(Boolean);
  document.getElementById("prefsSummary").textContent = hidden.length ? `隱藏${hidden.join("、")}` : "全部顯示";
}

chrome.storage.local.get(["hideCaptions", "hidePeoplePanel", "prefsOpen"]).then((v) => {
  hideCaptionsEl.checked = !!v.hideCaptions;
  hidePeoplePanelEl.checked = !!v.hidePeoplePanel;
  prefsBox.open = !!v.prefsOpen;
  renderPrefsSummary();
});
prefsBox.addEventListener("toggle", () => chrome.storage.local.set({ prefsOpen: prefsBox.open }));

for (const el of [hideCaptionsEl, hidePeoplePanelEl]) {
  el.addEventListener("change", async () => {
    const prefs = visibilityPrefs();
    renderPrefsSummary();
    await chrome.storage.local.set(prefs);
    const state = await chrome.runtime.sendMessage({ type: "GET_STATE", target: "background" });
    if (state.recording && state.recordingTabId) {
      chrome.tabs.sendMessage(state.recordingTabId, { type: "set-visibility", target: "content", ...prefs }).catch(() => {});
    }
  });
}

function showProgress(stage, percent) {
  const label = document.getElementById("progressLabel");
  if (stage === null) {
    label.style.display = "none";
    progressEl.style.display = "none";
    return;
  }
  document.getElementById("progressStage").textContent = stage;
  document.getElementById("progressPct").textContent = percent !== null ? `${percent}%` : "";
  label.style.display = "flex";
  progressEl.style.display = percent !== null ? "block" : "none";
  if (percent !== null) progressEl.value = percent;
}

async function pollStatus(jobId) {
  stopPolling();
  try {
    const res = await fetch(`${SERVER}/status/${jobId}`);
    const data = await res.json();

    jobProcessing = data.status === "processing";
    renderButtons();
    renderSpeed(jobId, data.status === "processing" ? data.polish_speed : null);
    if (data.status === "processing") {
      const elapsed = typeof data.elapsed_seconds === "number" ? formatElapsed(data.elapsed_seconds) : "";
      const percent = typeof data.percent === "number" ? Math.max(0, Math.min(100, data.percent)) : null;
      // 目前步驟跟百分比放在進度條正上方,一眼看得到;下面的狀態文字只留經過時間和說明
      showProgress(data.stage || "處理中", percent);
      statusEl.textContent =
        (elapsed ? `已處理 ${elapsed}。` : "") +
        "這個視窗關掉也沒關係,轉錄會繼續在背景跑,之後重開再按「檢查逐字稿狀態」即可。";
      pollTimer = setTimeout(() => pollStatus(jobId), POLL_INTERVAL_MS);
    } else if (data.status === "done") {
      chrome.runtime.sendMessage({ type: "SEEN_RESULT", target: "background" });
      const elapsed = typeof data.elapsed_seconds === "number" ? `(共花了 ${formatElapsed(data.elapsed_seconds)})` : "";
      const warns = (data.warnings || []).map((w) => "\n⚠️ " + w).join("") +
        (data.signal_issues && data.signal_issues.length && updateInfo && updateInfo.update_available
          ? "\n⚠️ 這場有部分講者資訊沒抓到,可能是 Google Meet 改版了。已經有新版本,更新後通常就會恢復(見上方)。"
          : "");
      statusEl.textContent = `轉錄完成!${elapsed}已存到 recordings/ 資料夾,也可以直接複製。${warns}`;
      showProgress(null);
      transcriptEl.style.display = "block";
      transcriptEl.value = data.text;
      copyBtn.style.display = "block";
    } else if (data.status === "error") {
      chrome.runtime.sendMessage({ type: "SEEN_RESULT", target: "background" });
      const atStage = data.stage ? `(卡在「${data.stage}」這一步)` : "";
      statusEl.textContent = `轉錄失敗${atStage}:${data.error}`;
      showProgress(null);
    } else {
      statusEl.textContent = "找不到這個工作,可能伺服器重啟過。請到 recordings/ 資料夾找檔案。";
      showProgress(null);
    }
  } catch (err) {
    statusEl.textContent = "連不到本機伺服器,請確認 server 有啟動 (localhost:8765)。";
    showProgress(null);
    renderSpeed(null, null);
  }
}

// 錄音中找不到字幕/名單時的提醒(content.js 的 checkSpeakerSignals 偵測到後回報)。
// 多半是 Meet 改版讓擴充功能找不到按鈕;使用者手動打開就能補救,錄音本身不受影響。
function signalWarning(problems) {
  if (!problems || !problems.length) return "";
  const what = problems.map((p) => (p === "captions" ? "字幕" : "與會者名單")).join("和");
  return `\n\n⚠️ 擴充功能找不到${what},講者可能會分不準(錄音本身不受影響)。請在 Meet 手動打開${what};` +
    "如果一直出現,可能是 Google Meet 改版了,請回報。";
}

async function refreshFromState() {
  const state = await chrome.runtime.sendMessage({ type: "GET_STATE", target: "background" });
  setUiRecording(state.recording);
  retryBtn.style.display = state.pendingUpload ? "block" : "none";
  if (state.pendingUpload && !state.recording) {
    statusEl.textContent = "上次的錄音還沒上傳成功(錄音已保存)。確認伺服器有開之後,按「重新上傳上次的錄音」。";
  } else if (state.recording) {
    statusEl.textContent = "錄音中..." + signalWarning(state.signalProblems);
  } else if (state.lastJobId) {
    checkBtn.style.display = "block";
    // 一打開 popup 就自動查一次現況並開始自動輪詢,不用使用者自己按按鈕才看得到進度。
    pollStatus(state.lastJobId);
  }
}

toggleBtn.addEventListener("click", async () => {
  const state = await chrome.runtime.sendMessage({ type: "GET_STATE", target: "background" });

  if (!state.recording) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !tab.url.includes("meet.google.com")) {
      statusEl.textContent = "請先切到 Google Meet 分頁再按開始錄音。";
      return;
    }
    // 還沒授權麥克風就自動打開授權頁(錄音在 offscreen 文件裡做,那裡沒辦法跳出授權視窗)。
    const mic = await navigator.permissions.query({ name: "microphone" }).catch(() => null);
    if (mic && mic.state !== "granted") {
      chrome.tabs.create({ url: chrome.runtime.getURL("permission.html") });
      statusEl.textContent = "還沒授權麥克風,已經幫你打開授權頁。在跳出的視窗按「允許」後,回來再按一次開始錄音。";
      return;
    }
    // 伺服器沒開就不讓開始錄音,避免錄完整場才發現上傳不了(2026-10-12 實測發生過)。
    if (!(await serverIsUp())) {
      statusEl.textContent = SERVER_DOWN_MESSAGE;
      return;
    }
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const startTime = Date.now();
    let speakerTrackingWarning = "";
    try {
      // 分頁裡沒有可用的 content script(擴充功能重新載入過、分頁沒重新整理)就當場補注入一份。
      try {
        await chrome.tabs.sendMessage(tab.id, { type: "ping", target: "content" });
      } catch (_) {
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
      }
      const r = await chrome.tabs.sendMessage(tab.id, {
        type: "start-speaker-tracking",
        target: "content",
        startTime,
        ...visibilityPrefs(),
      });
      myName = r?.myName || null;
      otherNames = r?.roster?.otherNames || [];
      if (!myName) {
        speakerTrackingWarning =
          "⚠️ 沒抓到你的真實姓名(content script 有回應,但抓不到「More options for 你的名字」這個按鈕),會用「你」代替。";
      }
    } catch (err) {
      // content script 可能還沒注入(例如剛重新載入分頁),不影響錄音,只是不會有講者標示
      myName = null;
      otherNames = [];
      speakerTrackingWarning = "⚠️ 講者標示初始化失敗(" + err.message + "),不影響錄音,但不會有講者標示。";
    }
    const resp = await chrome.runtime.sendMessage({
      type: "START",
      target: "background",
      streamId,
      title: tab.title || "meeting",
      tabId: tab.id,
    });
    if (resp && resp.error) {
      statusEl.textContent = "啟動失敗:" + resp.error;
      return;
    }
    setUiRecording(true);
    statusEl.textContent =
      "錄音中... 請保持在 Google Meet 分頁。" +
      (speakerTrackingWarning ? "\n" + speakerTrackingWarning : "");
    checkBtn.style.display = "none";
    transcriptEl.style.display = "none";
    copyBtn.style.display = "none";
  } else {
    statusEl.textContent = "停止中,重新確認講者名單並上傳音訊給本機伺服器...";
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    let speakerTimeline = [];
    let speakerDebugLog = [];
    let captionLog = [];
    // myName/otherNames 這兩個模組變數,是「開始錄音」當下抓到的初步值——如果使用者
    // 中途關過 popup 再打開(幾乎每次都會,說明書也是這樣教的),這個 JS 執行環境會
    // 被 Chrome 整個砍掉重來,這兩個變數就會消失變回一開始的預設值。真正權威的名單
    // 要在這裡(停止錄音的當下)重新問 content script 一次——它整個會議期間持續
    // 存活,不受 popup 開關影響,而且這時候 Meet 畫面也一定已經穩定渲染好了。
    // 講者資料要跟「正在錄音的那個 Meet 分頁」拿,不是按停止當下畫面上的分頁——停止時人在
    // 別的分頁(或別的視窗)的話,原本會問錯分頁,整包講者資料變成空的。
    const speakerTabId = state.recordingTabId || tab?.id;
    try {
      const r = await chrome.tabs.sendMessage(speakerTabId, {
        type: "stop-speaker-tracking",
        target: "content",
      });
      speakerTimeline = r?.timeline || [];
      speakerDebugLog = r?.panelDebugLog || [];
      captionLog = r?.captionLog || [];
      if (r?.myName) myName = r.myName;
      if (r?.roster?.otherNames?.length) otherNames = r.roster.otherNames;
    } catch (_) {
      // 拿不到就算了,退回使用「開始錄音」當下的舊值(如果有的話),伺服器沒有
      // speaker_timeline 時會直接輸出沒有講者標示的逐字稿
    }
    const resp = await chrome.runtime.sendMessage({
      type: "STOP",
      target: "background",
      speakerTimeline,
      speakerDebugLog,
      captionLog,
      myName,
      otherNames,
    });
    setUiRecording(false);
    if (resp && resp.error) {
      statusEl.textContent =
        "上傳失敗:" + resp.error + "\n錄音已經保存下來,確認伺服器有開之後,按「重新上傳上次的錄音」。";
      retryBtn.style.display = "block";
      return;
    }
    checkBtn.style.display = "block";
    if (resp && resp.jobId) {
      // 直接開始自動輪詢進度,不用等使用者按「檢查逐字稿狀態」。
      pollStatus(resp.jobId);
    }
  }
});

retryBtn.addEventListener("click", async () => {
  if (!(await serverIsUp())) {
    statusEl.textContent = SERVER_DOWN_MESSAGE;
    return;
  }
  statusEl.textContent = "重新上傳中...";
  const resp = await chrome.runtime.sendMessage({ type: "RETRY_UPLOAD", target: "background" });
  if (resp && resp.error) {
    statusEl.textContent = "重新上傳失敗:" + resp.error;
    return;
  }
  retryBtn.style.display = "none";
  checkBtn.style.display = "block";
  if (resp && resp.jobId) pollStatus(resp.jobId);
});

checkBtn.addEventListener("click", async () => {
  const state = await chrome.runtime.sendMessage({ type: "GET_STATE", target: "background" });
  if (!state.lastJobId) {
    statusEl.textContent = "沒有找到工作紀錄。";
    return;
  }
  pollStatus(state.lastJobId);
});

copyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(transcriptEl.value);
  copyBtn.textContent = "已複製!";
  setTimeout(() => (copyBtn.textContent = "複製逐字稿"), 1500);
});

refreshFromState();
refreshServerState();
