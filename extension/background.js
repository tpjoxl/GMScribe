// 錄音時,Meet 分頁如果被切到背景太久,Chrome 可能會把它的 JavaScript 整個凍結,
// 導致 content.js 裡即時「誰在講話」的 300ms 輪詢完全停止回報(實測真的發生過:
// 連續 11 分鐘完全沒有任何一筆資料)。分頁自己的計時器撐不住背景凍結,但
// chrome.alarms 是瀏覽器擴充功能層級的排程,不受單一分頁的背景節流影響,而且
// 即使分頁被凍結了,Chrome 通常還是會為了送達擴充功能的訊息短暫喚醒它。所以
// 錄音期間每隔一段時間(SPEAKER_BURST_ALARM,最短間隔是 Chrome 允許的 1 分鐘)
// 就主動戳一下分頁,叫它密集偵測幾秒鐘再繼續睡——不是「一直保持清醒」(Chrome
// 沒有開放這種權限給擴充功能),是「定期被叫醒一下」,盡量減少完全沒資料的空窗。
const SPEAKER_BURST_ALARM = "speaker-burst-check";
const JOB_POLL_ALARM = "job-poll";
const SERVER = "http://localhost:8765";

// 工具列圖示上的狀態徽章:不用打開 popup 也看得出現在在做什麼。滑鼠移到圖示上會顯示文字說明。
const BADGES = {
  idle: { text: "", color: "#000000", title: "GMScribe" },
  recording: { text: "REC", color: "#DC2626", title: "GMScribe — 錄音中" },
  recordingWarn: { text: "REC!", color: "#D97706", title: "GMScribe — 錄音中,但找不到字幕或與會者名單,講者可能分不準。點開查看" },
  uploading: { text: "⇡", color: "#2563EB", title: "GMScribe — 上傳錄音中" },
  processing: { text: "…", color: "#D97706", title: "GMScribe — 轉錄處理中" },
  done: { text: "✓", color: "#16A34A", title: "GMScribe — 逐字稿完成,點開查看" },
  error: { text: "✕", color: "#DC2626", title: "GMScribe — 發生錯誤,點開查看" },
};

// 伺服器沒開:整個圖示變灰階(標籤照常顯示)。每 30 秒問一次 /health,popup 開關伺服器時也會馬上更新。
const HEALTH_ALARM = "server-health";
const ICON_SIZES = [16, 24, 32, 48, 64];

async function refreshServerIcon() {
  let up = false;
  try {
    up = (await fetch(`${SERVER}/health`, { signal: AbortSignal.timeout(3000) })).ok;
  } catch (_) {
    up = false;
  }
  const prefix = up ? "icons/icon" : "icons/gray";
  await chrome.action.setIcon({ path: Object.fromEntries(ICON_SIZES.map((s) => [s, `${prefix}${s}.png`])) });
  const { badgeState } = await chrome.storage.local.get("badgeState");
  if (!badgeState || badgeState === "idle") {
    await chrome.action.setTitle({ title: up ? "GMScribe — 錄音伺服器已啟動" : "GMScribe — 錄音伺服器未啟動" });
  }
  return up;
}

chrome.alarms.get(HEALTH_ALARM).then((a) => a || chrome.alarms.create(HEALTH_ALARM, { periodInMinutes: 0.5 }));
refreshServerIcon();

async function setBadge(state, { text, title } = {}) {
  const b = BADGES[state] || BADGES.idle;
  await chrome.action.setBadgeBackgroundColor({ color: b.color });
  if (chrome.action.setBadgeTextColor) await chrome.action.setBadgeTextColor({ color: "#FFFFFF" });
  await chrome.action.setBadgeText({ text: text ?? b.text });
  await chrome.action.setTitle({ title: title ?? b.title });
  await chrome.storage.local.set({ badgeState: state });
  if (state === "idle") await refreshServerIcon();
}

/** 處理中:每 30 秒問一次伺服器進度(popup 關著也會更新),徽章顯示百分比,完成或失敗就停。 */
async function pollJobForBadge() {
  const { lastJobId } = await chrome.storage.local.get("lastJobId");
  if (!lastJobId) return chrome.alarms.clear(JOB_POLL_ALARM);
  try {
    const data = await (await fetch(`${SERVER}/status/${lastJobId}`)).json();
    if (data.status === "processing") {
      const pct = typeof data.percent === "number" ? `${data.percent}%` : "…";
      await setBadge("processing", { text: pct, title: `GMScribe — ${data.stage || "處理中"}(${pct})` });
      return;
    }
    chrome.alarms.clear(JOB_POLL_ALARM);
    // not_found:伺服器重啟過、忘了這個工作(工作狀態只存在記憶體)。結果早就存進 recordings/ 了,
    // 不是失敗,不要顯示錯誤。
    if (data.status === "not_found") return setBadge("idle");
    await setBadge(data.status === "done" ? "done" : "error");
  } catch (_) {
    // 伺服器暫時連不到就下次再問
  }
}

function startJobPolling() {
  chrome.alarms.create(JOB_POLL_ALARM, { periodInMinutes: 0.5 });
  pollJobForBadge();
}

async function ensureOffscreenDocument() {
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "錄製 Google Meet 音訊並上傳做語音轉文字",
    });
  } catch (err) {
    // 已經有一個 offscreen document 存在時會噴這個錯誤,可以忽略
    if (!String(err).includes("single offscreen")) throw err;
  }
}

async function handleStart(streamId, title, tabId) {
  await ensureOffscreenDocument();
  const resp = await chrome.runtime.sendMessage({
    type: "start-recording",
    target: "offscreen",
    streamId,
    title,
  });
  if (resp && resp.error) throw new Error(resp.error);
  // recordingTabId 存進 chrome.storage.local 而不是模組變數——MV3 的 service worker
  // 閒置一段時間會被 Chrome 自己終止、下次事件觸發時重新啟動,模組變數會被清空,
  // 但 chrome.alarms 排定的鬧鐘還是會準時把 service worker 叫醒,這時候要能從
  // storage 拿回「現在錄音的是哪個分頁」,不能只靠記憶體裡的變數。
  await chrome.storage.local.set({ recording: true, recordingTabId: tabId, signalProblems: [] });
  chrome.alarms.create(SPEAKER_BURST_ALARM, { periodInMinutes: 1 });
  await setBadge("recording");
  return {};
}

async function handleStop(speakerTimeline, speakerDebugLog, captionLog, myName, otherNames) {
  await setBadge("uploading");
  await chrome.storage.local.set({ signalProblems: [] });
  const resp = await chrome.runtime.sendMessage({
    type: "stop-recording",
    target: "offscreen",
    speakerTimeline,
    speakerDebugLog,
    captionLog,
    myName,
    otherNames,
  });
  // 不管上傳成不成功,錄音本身都已經停了——原本失敗時直接丟錯、狀態一直停在「錄音中」,
  // 使用者再按一次停止又會失敗一次(2026-10-12 實測)。失敗時錄音已存在 offscreen 的
  // IndexedDB,記一個 pendingUpload 讓 popup 顯示「重新上傳」。
  chrome.alarms.clear(SPEAKER_BURST_ALARM);
  if (resp && resp.error) {
    await chrome.storage.local.set({ recording: false, recordingTabId: null, pendingUpload: true });
    await setBadge("error", { title: "GMScribe — 上傳失敗,錄音已保存,點開重新上傳" });
    throw new Error(resp.error);
  }
  await chrome.storage.local.set({
    recording: false,
    lastJobId: resp.jobId,
    recordingTabId: null,
    pendingUpload: false,
  });
  chrome.alarms.clear(SPEAKER_BURST_ALARM);
  startJobPolling();
  try {
    await chrome.offscreen.closeDocument();
  } catch (_) {
    // 已經關掉就算了
  }
  return { jobId: resp.jobId };
}

async function handleGetState() {
  const v = await chrome.storage.local.get(["recording", "lastJobId", "pendingUpload", "recordingTabId", "signalProblems"]);
  return {
    recording: !!v.recording,
    lastJobId: v.lastJobId || null,
    pendingUpload: !!v.pendingUpload,
    recordingTabId: v.recordingTabId || null,
    signalProblems: v.signalProblems || [],
  };
}

// 重新載入擴充功能之後,已經開著的 Meet 分頁裡的 content script 會跟擴充功能斷線(收不到訊息,
// 講者資料整場是空的)。以前要使用者自己重新整理 Meet 分頁,現在載入/更新時直接幫所有開著的
// Meet 分頁補注入一份新的。
chrome.runtime.onInstalled.addListener(async (details) => {
  // 第一次安裝時自動打開麥克風授權頁,使用者不用自己去找「擴充功能選項」。
  if (details.reason === "install") chrome.tabs.create({ url: chrome.runtime.getURL("permission.html") });
  const tabs = await chrome.tabs.query({ url: "https://meet.google.com/*" });
  for (const tab of tabs) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    } catch (_) {
      // 分頁可能還在載入或已經關掉,略過
    }
  }
});

async function handleRetryUpload() {
  await ensureOffscreenDocument();
  const resp = await chrome.runtime.sendMessage({ type: "retry-upload", target: "offscreen" });
  if (resp && resp.error) throw new Error(resp.error);
  await chrome.storage.local.set({ lastJobId: resp.jobId, pendingUpload: false });
  startJobPolling();
  try {
    await chrome.offscreen.closeDocument();
  } catch (_) {
    // 已經關掉就算了
  }
  return { jobId: resp.jobId };
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === JOB_POLL_ALARM) return pollJobForBadge();
  if (alarm.name === HEALTH_ALARM) return refreshServerIcon();
  if (alarm.name !== SPEAKER_BURST_ALARM) return;
  const { recording, recordingTabId } = await chrome.storage.local.get(["recording", "recordingTabId"]);
  if (!recording || !recordingTabId) return;
  try {
    await chrome.tabs.sendMessage(recordingTabId, { type: "burst-check", target: "content" });
  } catch (_) {
    // 分頁可能已經關掉、還沒注入 content script,或這次真的叫不醒——忽略,下次鬧鐘再試,
    // 不能讓這個失敗中斷錄音本身。
  }
});

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.target !== "background") return;

  if (msg.type === "GET_STATE") return handleGetState();
  if (msg.type === "REFRESH_SERVER_ICON") return refreshServerIcon();
  if (msg.type === "SIGNAL_STATUS") {
    const problems = msg.problems || [];
    return chrome.storage.local.set({ signalProblems: problems }).then(async () => {
      const { recording } = await chrome.storage.local.get("recording");
      if (recording) await setBadge(problems.length ? "recordingWarn" : "recording");
    });
  }
  // popup 打開看過結果之後,把「完成/錯誤」徽章清掉
  if (msg.type === "SEEN_RESULT")
    return chrome.storage.local.get("badgeState").then(({ badgeState }) =>
      badgeState === "done" || badgeState === "error" ? setBadge("idle") : null
    );
  if (msg.type === "RETRY_UPLOAD") return handleRetryUpload().catch((err) => ({ error: err.message }));
  if (msg.type === "START")
    return handleStart(msg.streamId, msg.title, msg.tabId).catch((err) => ({ error: err.message }));
  if (msg.type === "STOP")
    return handleStop(msg.speakerTimeline, msg.speakerDebugLog, msg.captionLog, msg.myName, msg.otherNames).catch(
      (err) => ({ error: err.message })
    );
});
