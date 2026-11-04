const SERVER = "http://localhost:8765";

let micRecorder = null;
let tabRecorder = null;
let micChunks = [];
let tabChunks = [];
let micStream = null;
let tabStream = null;
let playbackCtx = null;
let currentTitle = "meeting";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target !== "offscreen") return;

  if (msg.type === "start-recording") {
    startRecording(msg.streamId, msg.title)
      .then(() => sendResponse({}))
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }

  if (msg.type === "retry-upload") {
    retryPendingUpload()
      .then((jobId) => sendResponse({ jobId }))
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }

  if (msg.type === "stop-recording") {
    stopRecordingAndUpload(
      msg.speakerTimeline || [],
      msg.speakerDebugLog || [],
      msg.captionLog || [],
      msg.myName,
      msg.otherNames || []
    )
      .then((jobId) => sendResponse({ jobId }))
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }
});

async function startRecording(streamId, title) {
  currentTitle = title;
  micChunks = [];
  tabChunks = [];

  // 麥克風(你自己說的話)。第一次使用前要先去 permission.html 授權過。
  // 這條麥克風串流完全獨立於 Google Meet 自己的音訊管線,不會跟著 Meet 的靜音鍵走——
  // 不管你在 Meet 裡有沒有把自己的 mic 按靜音,這裡都會持續錄下麥克風實際收到的聲音。
  // 如果開會用喇叭(不是耳機)放對方的聲音,喇叭聲很容易透過空氣被自己的麥克風錄進去
  // (回音/串音),因為麥克風軌的內容無條件全部標成你的名字,串音進來的內容會被誤判成
  // 你講的話。明確開啟瀏覽器內建的回音消除/噪音抑制盡量緩解,但這只能減少、不能保證
  // 完全消除串音——用耳機開會是唯一能從根本避免這個問題的方法。
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  // Meet 分頁裡其他人的聲音
  tabStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
    },
  });

  // tabCapture 預設會讓分頁靜音,這裡把聲音接回喇叭,不然開會時你會聽不到對方說話
  playbackCtx = new AudioContext();
  const tabSource = playbackCtx.createMediaStreamSource(tabStream);
  tabSource.connect(playbackCtx.destination);

  // 麥克風跟分頁音訊分開錄,不混音,這樣才能各自獨立轉錄跟做語者分離
  micRecorder = new MediaRecorder(micStream, { mimeType: "audio/webm;codecs=opus" });
  micRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) micChunks.push(e.data);
  };

  tabRecorder = new MediaRecorder(tabStream, { mimeType: "audio/webm;codecs=opus" });
  tabRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) tabChunks.push(e.data);
  };

  micRecorder.start(1000);
  tabRecorder.start(1000);
}

function stopRecorderAndGetBlob(recorder, chunks) {
  return new Promise((resolve) => {
    recorder.onstop = () => resolve(new Blob(chunks, { type: "audio/webm" }));
    recorder.stop();
  });
}

// ---- 上傳失敗時保住錄音 ----
// 2026-10-12 實測:伺服器沒開,按停止後上傳失敗,錄音只剩記憶體裡一份,差點整場遺失(最後是從
// DevTools 手動救回聲音,名單跟字幕則隨 Meet 分頁關掉而消失)。現在上傳前先把整包表單內容
// (兩條音軌 + 講者資料)存進這個擴充功能的 IndexedDB,上傳成功才刪掉;失敗的話 popup 會出現
// 「重新上傳」按鈕,關掉 Chrome 重開也還在。
const PENDING_DB = "gmscribe";
const PENDING_STORE = "pending";

function openPendingDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(PENDING_DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(PENDING_STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function pendingOp(mode, fn) {
  const db = await openPendingDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(PENDING_STORE, mode);
    const req = fn(tx.objectStore(PENDING_STORE));
    tx.oncomplete = () => resolve(req && req.result);
    tx.onerror = () => reject(tx.error);
  });
}

async function uploadFields(fields) {
  const formData = new FormData();
  formData.append("mic_audio", fields.micBlob, "mic.webm");
  formData.append("tab_audio", fields.tabBlob, "tab.webm");
  // 講者資料改用檔案的形式送:伺服器(Starlette)對一般文字欄位有 1MB 上限,長會議的字幕紀錄
  // (中文 UTF-8 一字 3 bytes)或除錯紀錄超過就會整包上傳被拒。檔案欄位沒有這個限制。
  const FILE_FIELDS = ["speaker_timeline", "speaker_debug", "caption_log"];
  for (const [k, v] of Object.entries(fields.text)) {
    if (FILE_FIELDS.includes(k)) formData.append(k, new Blob([v], { type: "application/json" }), `${k}.json`);
    else formData.append(k, v);
  }
  const res = await fetch(`${SERVER}/upload`, { method: "POST", body: formData });
  if (!res.ok) throw new Error("本機伺服器回應錯誤:" + res.status);
  const data = await res.json();
  await pendingOp("readwrite", (store) => store.delete("last"));
  return data.job_id;
}

async function retryPendingUpload() {
  const fields = await pendingOp("readonly", (store) => store.get("last"));
  if (!fields) throw new Error("沒有待重新上傳的錄音");
  return uploadFields(fields);
}

async function stopRecordingAndUpload(speakerTimeline, speakerDebugLog, captionLog, myName, otherNames) {
  if (!micRecorder || !tabRecorder) throw new Error("目前沒有在錄音");

  const [micBlob, tabBlob] = await Promise.all([
    stopRecorderAndGetBlob(micRecorder, micChunks),
    stopRecorderAndGetBlob(tabRecorder, tabChunks),
  ]);

  [micStream, tabStream].forEach((s) => s && s.getTracks().forEach((t) => t.stop()));
  if (playbackCtx) await playbackCtx.close();
  micRecorder = null;
  tabRecorder = null;

  const fields = {
    micBlob,
    tabBlob,
    text: {
      title: currentTitle,
      speaker_timeline: JSON.stringify(speakerTimeline || []),
      speaker_debug: JSON.stringify(speakerDebugLog || []),
      caption_log: JSON.stringify(captionLog || []),
      mic_speaker_name: myName || "你",
      other_names: JSON.stringify(otherNames || []),
    },
  };
  await pendingOp("readwrite", (store) => store.put(fields, "last"));
  return uploadFields(fields);
}
