/**
 * 注入到 Google Meet 分頁,記錄「誰在什麼時候講話」——完全不需要開字幕。
 *
 * 訊號來源有兩層,People 面板列優先、主畫面視訊格當備援(2026-09-30 改成這樣,
 * 原因見下面 scanSpeaking() 的大段註解:視訊格在螢幕分享時會整個消失,面板列
 * 不會)。兩層用的是同一種判斷邏輯:格子/列裡有個元素的 class 安靜時固定帶
 * `gjg47c`,講話中這個 class 會換成別的值——這是實測驗證出來的(兩層都用兩個
 * 真實帳號、其中一個持續講話的情況下來回驗證過,安靜/講話切換都準確重現),
 * 跟字幕開不開完全無關。
 *
 * DOM 結構(2026-09 實測):
 *   .oZRSLe                        <- 一個人的視訊格(自己跟其他人都有)
 *     .ne2Ple-oshW8e-V67aGc        <- 名字標籤(這個 class 是 Google 到處重複
 *                                     使用的通用樣式 class,不是專門給名字用的——
 *                                     自己格子上會抓到別的文字如「You're
 *                                     continuously framed」,面板列上會抓到
 *                                     「More actions」。只有在「其他人的主畫面
 *                                     視訊格」這個情境下,實測碰巧一直是名字,
 *                                     不能當作通用的「這是名字」判斷依據)
 *     .DYfzY                       <- 講話中指示器,安靜時固定帶 `gjg47c`,
 *                                     講話中這個 class 會消失、換成別的值
 *   自己的視訊格判斷方式:格子裡有「More options for 名字」(中文「更多名字的選項」)按鈕
 *   的就是自己(這個按鈕只會出現在自拍格裡)。
 *
 * 重要:`.oZRSLe` 這個 class 不是主畫面視訊格專用的——實測發現 People(參與者
 * 名單)面板打開時,面板裡每一列參與者**也套用同一個 class**(`data-participant-id`
 * 也一樣),只是外層多包了 `[role="listitem"]`。如果直接對整個 `document` 查
 * `.oZRSLe`,只要有任何面板剛好開著,就會把面板列也一起掃進來,跟真正的視訊格
 * 混在一起,抓到面板控制項的文字當成參與者名字(這正是曾經真實發生過的 bug:
 * 抓到「Unpin your presentation from your main screen」這種控制項文字)。
 * 所以所有 `.oZRSLe` 查詢都要限定在 `getMainStage()`(主畫面 `<main>` 容器)
 * 裡面,不能對整個文件查。
 *
 * People 面板列的對應結構(2026-09-30 實測):
 *   [role="listitem"][data-participant-id]   <- 一列一個人
 *     .zWGUib                                <- 名字
 *     .NnTWjc                                <- 只有自己那列才有,內容「(You)」
 *     .IisKdb.GF8M7d.VeFZv                   <- 講話中指示器,邏輯跟 .DYfzY 一樣
 *                                                (安靜 = gjg47c),這三個 class
 *                                                是實測下來穩定存在的,中間夾雜
 *                                                的其他 class 是每個參與者各自
 *                                                不同的識別碼,不能拿來當選擇器
 *
 * 這些 class 名稱是 Google 內部編譯結果,未來可能會變,一旦抓不到就整段
 * 標成「不明」,不影響 Whisper 逐字稿本身的文字內容。
 */

const TILE_SELECTOR = ".oZRSLe";
const NAME_LABEL_SELECTOR = ".ne2Ple-oshW8e-V67aGc";
const SPEAK_INDICATOR_SELECTOR = ".DYfzY";
const QUIET_TOKEN = "gjg47c";

// 你自己那一列/那一格的講話指示器也記進 timeline,用這個固定名字(伺服器端
// transcriber.SELF_TIMELINE_SPEAKER 要一致)。你的聲音內容還是來自麥克風軌,這份
// 紀錄只用來過濾麥克風軌:Meet 沒偵測到你在講話的時段(靜音中、沒出聲),麥克風軌
// 錄到的東西(喇叭串音、安靜片段的幻覺字)就不算你講的。
const SELF_TIMELINE_SPEAKER = "__self__";
// 你在 Meet 裡「麥克風開著」的時段,跟講話時段放在同一份 timeline 送給伺服器(名字要跟
// transcriber.UNMUTED_TIMELINE_SPEAKER 一致)。麥克風軌不管 Meet 有沒有靜音都會錄,靜音時錄到的聲音
// 別人聽不到,不該進逐字稿(2026-11-02 站立會議:報告完靜音後錄到一聲「嗯」)。
const UNMUTED_TIMELINE_SPEAKER = "__unmuted__";

/** 你在 Meet 裡有沒有開麥克風:工具列的麥克風按鈕帶 `data-is-muted`(攝影機按鈕也有,用 mic 開頭的圖示分辨)。
 * 找不到就回 null,不記錄——伺服器沒收到這種時段,就退回只看講話指示器的舊做法,不會誤刪你的發言。 */
function micUnmuted() {
  for (const el of document.querySelectorAll("[data-is-muted]")) {
    if (/^mic/.test(iconName(el))) return el.getAttribute("data-is-muted") === "false";
  }
  return null;
}

// 真人的顯示名稱幾乎不會超過這個長度,額外的保險(見上面 .oZRSLe 的說明,
// 主要防線是把查詢限定在 getMainStage() 裡,這裡只是萬一還是漏網之魚時的
// 第二層防護,不依賴這個也不會有事)。
const MAX_PLAUSIBLE_NAME_LENGTH = 30;

function isLikelyRealName(name) {
  return !!name && name.length <= MAX_PLAUSIBLE_NAME_LENGTH;
}

/** 主畫面容器(視訊格所在的地方),不包含任何側邊面板。查 .oZRSLe 一定要從這裡查起。 */
function getMainStage() {
  return document.querySelector("main");
}

// People(參與者名單)面板:錄音開始/停止時讀「正確的名單」用的選擇器,也是
// scanSpeaking() 即時語者偵測現在的主要訊號來源(見下面 PEOPLE_PANEL_SPEAK_INDICATOR_SELECTOR
// 的說明)。面板本來就有名字,不會像視訊格自己那格一樣「這個 class 抓到別的文字」
// 的問題,讀到的名字比較準,也不會漏掉你自己的名字(面板裡的列有 (You) 可以判斷)。
// 這幾個選擇器一樣是 Google 內部編譯結果,一旦抓不到就整個放棄、退回視訊格版本。
/**
 * Meet 介面語言相關的文字。能用「圖示名稱」判斷的就用圖示(Material 圖示字,例如 `close`、
 * `translate_cc`,不隨介面語言改變);只有沒有圖示可用的地方才比對文字,這裡列出支援的語言。
 * 新增語言:在每個清單補上該語言的寫法(用該語言介面的真實 Meet 查,不要猜)。
 * 2026-10-29 用英文、繁體中文介面實測。
 */
const MEET_UI_TEXT = {
  // 開 People 面板的按鈕沒有 aria-label,內容是「People」+ 人數(例如 People3 / 參與者3)
  peopleButtonWords: ["People", "參與者", "参与者"],
  // 字幕區塊的 aria-label
  captionRegionLabels: ["Captions", "字幕"],
  // 字幕上自己的名字
  captionSelfNames: ["You", "你"],
  // 自己視訊格上的「更多選項」按鈕,用來讀自己的名字
  moreOptionsPatterns: [/^More options for (.+)$/, /^更多(.+)的選項$/, /^更多(.+)的选项$/],
};

function iconName(el) {
  return el.querySelector("i")?.textContent.trim() || "";
}

function findButtonByIcon(name, root = document) {
  return [...root.querySelectorAll("button")].find((b) => iconName(b) === name && b.offsetParent !== null) || null;
}

/** People 面板的關閉鈕:從面板列往上找,最近一個帶 `close` 圖示的按鈕(頁面上有好幾個關閉鈕)。 */
function findPeoplePanelCloseButton() {
  let el = document.querySelector('[role="listitem"][data-participant-id]');
  while (el && el !== document.body) {
    const btn = [...el.querySelectorAll("button")].find((b) => iconName(b) === "close");
    if (btn) return btn;
    el = el.parentElement;
  }
  return null;
}

function moreOptionsName(label) {
  for (const re of MEET_UI_TEXT.moreOptionsPatterns) {
    const m = (label || "").match(re);
    if (m) return m[1].trim();
  }
  return null;
}
const PEOPLE_PANEL_ROW_SELECTOR = '[role="listitem"][data-participant-id]';
const PEOPLE_PANEL_NAME_SELECTOR = ".zWGUib";
const PEOPLE_PANEL_SELF_MARKER_SELECTOR = ".NnTWjc"; // 內容是「(You)」

/**
 * People 面板列裡也有一份跟視訊格 `.DYfzY`/`gjg47c`同源的即時語者指示器——這是
 * 2026-09-30 用兩個真實帳號實測驗證出來的(其中一個帳號持續講話,同時輪詢兩邊
 * 的 DOM):面板列裡包住頭像的 div(`.IisKdb.GF8M7d.VeFZv` 這三個 class 在不同
 * 場次的實測裡都穩定存在,中間夾雜的其他 class 是每次參與者各自不同的識別碼,
 * 不能拿來當選擇器)安靜時帶 `gjg47c`,講話時會換成別的值(實測見過
 * `wEsLMd`/`OgVli`/`HX2H7`/`Oaajhc` 輪替,判斷邏輯只看「是不是 gjg47c」,不用
 * 窮舉每個「講話中」的值)。
 *
 * 這個訊號的價值在於:它是面板自己的獨立浮動視窗,不屬於主畫面版面,實測確認
 * 螢幕分享的時候(主畫面視訊格 `.oZRSLe` 會直接變成 0 個,見下面 scanSpeaking()
 * 的註解)面板列跟這個指示器完全不受影響、照常存在。前提是面板要整場開著,
 * 只在開始/停止錄音那一刻各開一次讀完就關掉是不夠的。
 */
const PEOPLE_PANEL_SPEAK_INDICATOR_SELECTOR = ".IisKdb.GF8M7d.VeFZv";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 找開啟 People 面板的按鈕。畫面右上角那顆「大頭貼+人數」的按鈕沒有 aria-label,
 * 沒辦法直接用 class 選(class 是 Google 內部編譯結果,而且同一個 class 還匹配到
 * 旁邊另一個「Take notes with Gemini」的按鈕,不夠獨特)——但它內部有一段給螢幕
 * 閱讀器看的隱藏文字剛好就是「People」加上人數(例如「People1」),用這個文字
 * 特徵找,比賭 class 名字穩定,也已經實測確認在頁面上是唯一命中。 */
function findPeoplePanelOpenButton() {
  return (
    [...document.querySelectorAll('[role="button"]')].find((el) =>
      MEET_UI_TEXT.peopleButtonWords.some((w) => new RegExp(`^${w}\\d*$`).test(el.textContent.trim()))
    ) || null
  );
}

/** 開一次 People 面板讀正確名單,讀完馬上關掉(如果是我們自己打開的話)。
 * 任何一步失敗(按鈕找不到、面板沒渲染出東西)就回傳 null,呼叫端要退回視訊格版本。 */
async function readRosterFromPeoplePanel() {
  // 面板已經開著(錄音期間整場都開著)就直接讀,不要先去找開啟按鈕——2026-10-05
  // 實測面板明明開著、25 列名字都在,這裡卻因為「找不到開啟按鈕就直接 return null」
  // 整個放棄,名單送出去是空的。開啟按鈕只有在面板真的沒開時才需要。
  const alreadyOpen = document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR).length > 0;
  if (!alreadyOpen) {
    const openBtn = findPeoplePanelOpenButton();
    if (!openBtn) return null;
    openBtn.click();
  }

  // 人數多的大型會議(例如十幾人的站立會議),面板列渲染完整的時間比小型通話明顯久,
  // 只等「出現任何一列」就急著讀,常常只讀到渲染到一半的前幾個人。改成等到列數連續
  // 三次檢查(約 600ms)都沒再增加,才當作大概渲染穩定了,同時整體設一個上限
  // (maxWaitMs),避免真的抓不到時卡住太久——這個等待不會擋住錄音或即時語者偵測,
  // 兩者都已經在呼叫這個函式之前就啟動了。
  let rows = [];
  let stableChecks = 0;
  const maxWaitMs = 6000;
  const intervalMs = 200;
  for (let elapsed = 0; elapsed < maxWaitMs; elapsed += intervalMs) {
    const current = document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR);
    if (current.length > 0 && current.length === rows.length) {
      stableChecks++;
      if (stableChecks >= 3) break;
    } else {
      stableChecks = 0;
    }
    rows = [...current];
    await sleep(intervalMs);
  }
  rows = [...document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR)];

  let myName = null;
  const otherNames = [];
  for (const row of rows) {
    const nameEl = row.querySelector(PEOPLE_PANEL_NAME_SELECTOR);
    if (!nameEl) continue;
    const name = nameEl.textContent.trim();
    if (!name) continue;
    if (row.querySelector(PEOPLE_PANEL_SELF_MARKER_SELECTOR)) {
      myName = name;
    } else {
      otherNames.push(name);
    }
  }

  if (!alreadyOpen) {
    findPeoplePanelCloseButton()?.click();
  }

  if (rows.length === 0) return null;
  return { myName, otherNames, totalCount: rows.length };
}

let pollTimer = null;
let recordingStartTime = null;
let timeline = [];
let speakingSince = new Map(); // 講者姓名 -> 開始講話的 elapsed ms
let weOpenedPeoplePanelForTracking = false; // 是不是我們自己為了録音開的面板,決定停止時要不要幫忙關掉

/**
 * 除錯用:2026-09-30 那場 20 幾人站立會議實測發現,16 個真實姓名裡只有 2 個人的
 * 即時語者訊號有被抓到,另外 14 個人即使明顯有講話也完全沒被偵測到,但當下無法
 * 判斷原因(會議結束後 DOM 就沒了)。這裡改成把每一列「有沒有找到語者指示器
 * 元素、找到的話 class 是什麼」的狀態變化記下來,只記變化不記每一次輪詢(300ms
 * 輪詢一次,整場會議記全部資料量太大),下次真實會議結束後直接看這份紀錄,
 * 就能分辨是「這一列的指示器元素根本從頭到尾都找不到」還是「有找到但沒有正確
 * 反映安靜/講話」,不用再用猜的。跟 timeline 一樣,走同樣的路徑存成
 * speaker_debug.json,不影響現有的講者標示邏輯本身。
 */
let panelDebugLog = [];
let panelDebugLastState = new Map(); // data-participant-id -> 上次記錄的狀態字串
const PANEL_DEBUG_MAX_ENTRIES = 10000; // 防止極端情況下無限增長

// 2026-10-01 那場會議實測發現 panelDebugLog 整場是完全空的陣列——代表面板那支
// 分支(`if (panelRows.length > 0)`)從頭到尾一次都沒有進去過,但原本的記錄邏輯
// 只在「有抓到列」的時候才會寫資料,所以「完全沒抓到」這件事本身反而沒有被記錄
// 下來,看著空陣列完全無法判斷是「一次都沒檢查過」還是「檢查了但一直是 0」。
// 加兩個東西補這個洞:(1) 不管面板有沒有抓到列,每隔幾秒都強制記一筆「現在的
// 環境快照」(面板列數、主畫面視訊格數、面板開啟按鈕找不找得到);(2) 把
// scanSpeaking() 整個包進 try/catch,例外被默默吞掉也會留下紀錄,不會再像
// 這次一樣完全是空的看不出原因。
let lastHeartbeatAt = -Infinity;
let lastRosterCacheAt = -Infinity;
const HEARTBEAT_INTERVAL_MS = 3000;

/**
 * People 面板列的語者指示器,只有「捲動範圍內看得到的列」才會更新——2026-10-05
 * 那場 25 人會議的 speaker_debug.json 直接證實:面板依 DOM 順序的前 8 列(剛好是
 * 面板一個畫面裝得下的數量)整場都有安靜/講話的變化,第 9 列以後的 17 個人整場
 * 只有一筆「安靜」、從頭到尾沒動過,即使他們明顯有發言。列本身都在 DOM 裡(名字
 * 都讀得到),只是 Meet 不幫畫面外的列更新音量指示。
 *
 * 做法是把面板內容用 CSS zoom 縮小到所有列都擠進可視範圍(人多時面板字會變很小,
 * 這是刻意的,錄音結束會還原)。每隔幾秒重新檢查一次,有人中途加入、視窗被縮小
 * 都會再調整。這個修法是根據上面的資料推論的,還沒用真實大型會議驗證過——
 * heartbeat 會記錄 rowsInView,下次直接看有沒有等於 panelRowCount、以及第 9 列
 * 以後的人有沒有開始出現狀態變化。
 */
let panelZoomedElements = [];
let panelZoom = 1;
let lastPanelFitCheckAt = -Infinity;
const PANEL_FIT_CHECK_INTERVAL_MS = 3000;
const PANEL_MIN_ZOOM = 0.15;

function findScrollContainer(el) {
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    const overflowY = getComputedStyle(p).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return p;
  }
  return null;
}

/** 回傳 [可視範圍內的列數, 有實際高度的列數](收合起來、高度 0 的列不算)。 */
function countPanelRowsInView(panelRows) {
  if (!panelRows.length) return [0, 0];
  const scroller = findScrollContainer(panelRows[0]);
  const box = scroller ? scroller.getBoundingClientRect() : null;
  const top = Math.max(box ? box.top : 0, 0);
  const bottom = Math.min(box ? box.bottom : window.innerHeight, window.innerHeight);
  let inView = 0;
  let rendered = 0;
  panelRows.forEach((row) => {
    const r = row.getBoundingClientRect();
    if (r.height === 0) return;
    rendered++;
    if (r.top >= top - 1 && r.bottom <= bottom + 1) inView++;
  });
  return [inView, rendered];
}

function fitAllPanelRowsInView(panelRows, now) {
  if (now - lastPanelFitCheckAt < PANEL_FIT_CHECK_INTERVAL_MS) return;
  lastPanelFitCheckAt = now;

  const [inViewBefore, rendered] = countPanelRowsInView(panelRows);
  if (inViewBefore === rendered) return;

  const scroller = findScrollContainer(panelRows[0]);
  if (scroller) scroller.scrollTop = 0;
  if (!panelZoomedElements.length) {
    panelZoomedElements = scroller ? [...scroller.children] : [panelRows[0].parentElement];
  }
  let inView = inViewBefore;
  while (inView < rendered && panelZoom > PANEL_MIN_ZOOM) {
    panelZoom = Math.max(PANEL_MIN_ZOOM, panelZoom * 0.85);
    panelZoomedElements.forEach((el) => (el.style.zoom = String(panelZoom)));
    inView = countPanelRowsInView(panelRows)[0];
  }
  if (panelDebugLog.length < PANEL_DEBUG_MAX_ENTRIES) {
    panelDebugLog.push({
      t: now,
      event: "fit",
      scrollerFound: !!scroller,
      rendered,
      inViewBefore,
      inViewAfter: inView,
      zoom: panelZoom,
    });
  }
}

/**
 * 名單快取。2026-10-06(以及 10/01、10/05)的 roster_input.json 都是空的,原因是
 * 使用者很自然地「先離開會議、再按停止錄音」——最後一筆 heartbeat 明確記到面板列
 * 0、主畫面不存在。停止當下才去讀名單,那時 Meet 已經是「你已離開會議」的畫面,
 * 什麼都讀不到。改成錄音期間每幾秒把看到的名字累積起來,停止時跟當下讀到的合併。
 * 用聯集(中途離開的人也可能講過話)。
 */
let cachedMyName = null;
let cachedOtherNames = new Set();

function updateRosterCache(panelRows) {
  panelRows.forEach((row) => {
    const name = getPanelRowName(row);
    if (!name) return;
    if (isPanelRowSelf(row)) cachedMyName = name;
    else cachedOtherNames.add(name);
  });
  if (!cachedMyName) cachedMyName = getMyDisplayName();
  if (!panelRows.length) {
    const roster = getParticipantRoster();
    if (roster) roster.otherNames.forEach((n) => cachedOtherNames.add(n));
  }
}

/**
 * 視訊格指示器的狀態變化紀錄(格式比照面板列,多一個 src: "tile")。10/06 那場
 * 主畫面整場都有 24 個視訊格,但面板開著時原本的邏輯完全不看視訊格,所以不知道
 * 視訊格的指示器是不是 24 個人都會更新。現在兩邊都讀、都記。
 */
let tileDebugLastState = new Map();

function recordTileDebugSnapshot(tiles, now) {
  if (panelDebugLog.length >= PANEL_DEBUG_MAX_ENTRIES) return;
  tiles.forEach((tile) => {
    const name = getTileName(tile) || "(讀不到名字)";
    const id = tile.getAttribute("data-participant-id") || name;
    const el = tile.querySelector(SPEAK_INDICATOR_SELECTOR);
    const state = el ? el.className : null;
    const prev = tileDebugLastState.get(id);
    if (prev === state) return;
    tileDebugLastState.set(id, state);
    panelDebugLog.push({ t: now, src: "tile", id, name, isSelf: isSelfTile(tile), from: prev, to: state });
  });
}

/** 目前在可視範圍內的列,在清單裡是第幾列到第幾列(1 起算),給實驗判讀用。 */
function panelRowsInViewRange(panelRows) {
  if (!panelRows.length) return null;
  const scroller = findScrollContainer(panelRows[0]);
  const box = scroller ? scroller.getBoundingClientRect() : null;
  const top = Math.max(box ? box.top : 0, 0);
  const bottom = Math.min(box ? box.bottom : window.innerHeight, window.innerHeight);
  let first = null;
  let last = null;
  panelRows.forEach((row, i) => {
    const r = row.getBoundingClientRect();
    if (r.height === 0 || r.top < top - 1 || r.bottom > bottom + 1) return;
    if (first === null) first = i + 1;
    last = i + 1;
  });
  return first === null ? null : [first, last];
}

function restorePanelZoom() {
  panelZoomedElements.forEach((el) => (el.style.zoom = ""));
  panelZoomedElements = [];
  panelZoom = 1;
}

function recordHeartbeat(now, extra) {
  if (panelDebugLog.length >= PANEL_DEBUG_MAX_ENTRIES) return;
  if (now - lastHeartbeatAt < HEARTBEAT_INTERVAL_MS) return;
  lastHeartbeatAt = now;
  const stage = getMainStage();
  const rowsNow = document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR);
  panelDebugLog.push({
    t: now,
    event: "heartbeat",
    panelRowCount: rowsNow.length,
    captionRegionFound: !!findCaptionRegion(),
    captionBlocks: document.querySelectorAll(CAPTION_BLOCK_SELECTOR).length,
    rowsInView: countPanelRowsInView(rowsNow)[0],
    inViewRange: panelRowsInViewRange(rowsNow),
    panelZoom,
    mainStageFound: !!stage,
    callEndFound: !!findButtonByIcon("call_end"),
    micUnmuted: micUnmuted(),
    tileCount: stage ? stage.querySelectorAll(TILE_SELECTOR).length : null,
    ...extra,
  });
}

function findMoreOptionsButton(root = document) {
  return [...root.querySelectorAll("button[aria-label]")].find((b) => moreOptionsName(b.getAttribute("aria-label"))) || null;
}

// 視訊格跟 People 面板列帶同一個 `data-participant-id`(2026-11-03 實測)。視訊格上的名字標籤 class 改版後
// 讀不到、「更多選項」按鈕也變成每一格都有(isSelfTile 因此把每一格都當成自己,視訊格的講話訊號從 10 月中
// 起整個被略過)。改用編號對面板:名字跟「哪一格是自己」都從面板來,不看介面文字,中英文介面都適用。
const panelIdNames = new Map();
let panelSelfId = null;

function refreshPanelIds(panelRows) {
  panelRows.forEach((row) => {
    const id = row.getAttribute("data-participant-id");
    if (!id) return;
    if (isPanelRowSelf(row)) panelSelfId = id;
    const name = getPanelRowName(row);
    if (name) panelIdNames.set(id, name);
  });
}

function getMyDisplayName() {
  if (panelSelfId && panelIdNames.has(panelSelfId)) return panelIdNames.get(panelSelfId);
  const btn = findMoreOptionsButton();
  return btn ? moreOptionsName(btn.getAttribute("aria-label")) : null;
}

// 自己那一格才有的控制項圖示(取景、視覺效果),別人的格子上是「把對方靜音」的按鈕(2026-11-03 實測)
const SELF_TILE_ICONS = new Set(["frame_person", "visual_effects"]);

/** 哪一格是自己:先用編號對面板,對不到再看「只有自己那格才有」的圖示。 */
function isSelfTileRaw(tile) {
  const id = tile.getAttribute("data-participant-id");
  if (id && panelSelfId) return id === panelSelfId;
  return [...tile.querySelectorAll("i")].some((i) => SELF_TILE_ICONS.has(i.textContent.trim()));
}

// 每次掃描時算出來的「自己那一格」;不可能有兩格以上是自己,真的判斷出好幾格就當作這個判斷壞了,
// 一格都不算自己(自己的名字之後會被伺服器的名單過濾掉,不會被當成別人)。10 月中到 11/03 就是
// 每一格都被當成自己,視訊格的講話訊號整個被略過,這層保護讓同樣的問題最多只是少一個判斷。
let selfTilesThisScan = new Set();

function computeSelfTiles(tiles) {
  const selves = [...tiles].filter(isSelfTileRaw);
  selfTilesThisScan = new Set(selves.length === 1 ? selves : []);
}

function isSelfTile(tile) {
  return selfTilesThisScan.has(tile);
}

/** 視訊格的名字:編號對面板 → 舊的名字標籤 → 格子裡按鈕標籤提到的已知與會者名字(不看標籤文字的語言)。 */
function getTileName(tile) {
  const id = tile.getAttribute("data-participant-id");
  if (id && panelIdNames.has(id)) return panelIdNames.get(id);
  const el = tile.querySelector(NAME_LABEL_SELECTOR);
  const labelName = el ? el.textContent.trim() : "";
  if (labelName && isLikelyRealName(labelName)) return labelName;
  const known = [...panelIdNames.values(), ...cachedOtherNames];
  if (!known.length) return null;
  const labels = [...tile.querySelectorAll("[aria-label]")].map((x) => x.getAttribute("aria-label") || "");
  const hits = new Set(known.filter((n) => labels.some((l) => l.includes(n))));
  return hits.size === 1 ? [...hits][0] : null;
}

// 視訊格的講話燈號同時亮好幾格、而且持續一陣子,代表燈號的判斷方式壞了(Meet 改版換了 class),
// 這場剩下的時間就不採用視訊格的講話燈號,避免把「誰都在講」記進 timeline。
const TILE_LIT_BROKEN_COUNT = 4;
const TILE_LIT_BROKEN_SCANS = 34; // 約 10 秒(每 300ms 掃一次);多人笑出聲、搶話不會持續這麼久
let tileLitStreak = 0;
let tileSpeakingDisabled = false;

// 其他與會者在 Meet 裡靜音的時段,用 `__muted__:名字` 記進 timeline。靜音中的人不可能是講者,伺服器拿來排除
// 候選人(2026-11-03 站立會議實測:24 個人的開麥時段都蓋住自己的字幕時段,而且早 2~4 秒開麥;也有人講完忘了
// 關,所以只能拿「靜音」來排除,不能拿「開麥」當作在講話)。只有視訊格看得到別人的靜音狀態(People 面板沒有),
// 圖示 `mic_off` 是靜音、`mic_none` 是開著;沒有視訊格的人就不記,伺服器當作不知道,不排除。
const MUTED_TIMELINE_PREFIX = "__muted__:";

function isTileMuted(tile) {
  return [...tile.querySelectorAll("i")].some((i) => i.textContent.trim() === "mic_off");
}

function isTileSpeaking(tile) {
  const el = tile.querySelector(SPEAK_INDICATOR_SELECTOR);
  if (!el) return false;
  return !el.classList.contains(QUIET_TOKEN);
}

function isPanelRowSelf(row) {
  return !!row.querySelector(PEOPLE_PANEL_SELF_MARKER_SELECTOR);
}

function getPanelRowName(row) {
  const nameEl = row.querySelector(PEOPLE_PANEL_NAME_SELECTOR);
  if (!nameEl) return null;
  const name = nameEl.textContent.trim();
  return isLikelyRealName(name) ? name : null;
}

function isPanelRowSpeaking(row) {
  const el = row.querySelector(PEOPLE_PANEL_SPEAK_INDICATOR_SELECTOR);
  if (!el) return false;
  return !el.classList.contains(QUIET_TOKEN);
}

/** 記錄面板每一列的指示器狀態變化(見上面 panelDebugLog 的說明)。狀態字串是
 * `null`(完全找不到指示器元素)或找到的元素的完整 class 名稱,只有跟上次不同
 * 才寫進 log,避免資料量爆炸。 */
function recordPanelDebugSnapshot(panelRows, now) {
  if (panelDebugLog.length >= PANEL_DEBUG_MAX_ENTRIES) return;
  panelRows.forEach((row) => {
    const id = row.getAttribute("data-participant-id") || "(no-id)";
    const name = getPanelRowName(row) || "(讀不到名字)";
    const el = row.querySelector(PEOPLE_PANEL_SPEAK_INDICATOR_SELECTOR);
    const state = el ? el.className : null;
    const prev = panelDebugLastState.get(id);
    if (prev === state) return;
    panelDebugLastState.set(id, state);
    panelDebugLog.push({ t: now, id, name, isSelf: isPanelRowSelf(row), from: prev, to: state });
  });
}

/** 讀主畫面視訊格,直接拿到真實姓名跟總人數。People 面板讀不到時的備援方案。 */
function getParticipantRoster() {
  const stage = getMainStage();
  if (!stage) return null;
  const tiles = stage.querySelectorAll(TILE_SELECTOR);
  if (!tiles.length) return null;
  computeSelfTiles(tiles);
  const otherNames = [];
  tiles.forEach((tile) => {
    if (isSelfTile(tile)) return;
    const name = getTileName(tile);
    if (name) otherNames.push(name);
  });
  return { totalCount: tiles.length, otherNames };
}

/**
 * 主畫面視訊格在螢幕分享期間會完全消失,不是變少、是變成 0 個——2026-09-30
 * 實測驗證過(自己分享、也用第二個帳號模擬別人分享,兩種情況主畫面
 * `.oZRSLe` 都直接歸零,Meet 會把通話畫面整個丟到瀏覽器子母畫面或改顯示
 * 分享中的畫面)。這是比「分頁被背景凍結」更常發生的空窗成因,幾乎每場
 * 會議只要有人分享畫面就會踩到。
 *
 * 因此語者掃描改成優先讀 People 面板列(見上面 PEOPLE_PANEL_SPEAK_INDICATOR_SELECTOR
 * 的說明,不受螢幕分享影響,前提是麵板要整場開著,見 start-speaker-tracking 的
 * 處理),面板讀不到(還沒開好、被使用者手動關掉、選擇器失效)才退回視訊格版本
 * ——維持這個專案一貫的「優雅退化」原則,不會因為其中一層失效就整段完全沒資料。
 */
function scanSpeaking() {
  if (recordingStartTime == null) return;
  const now = Date.now() - recordingStartTime;

  // 2026-10-01 那場會議整場的除錯紀錄是完全空的陣列,懷疑是這整個函式某處噴了
  // 例外被 setInterval 默默吞掉(瀏覽器對 setInterval callback 裡的例外本來就
  // 不會中斷計時器本身,但也不會有任何痕跡)。包一層 try/catch,例外至少會被
  // 記下來,不會再像這次一樣完全看不出原因。
  try {
    scanSpeakingInner(now);
  } catch (err) {
    if (panelDebugLog.length < PANEL_DEBUG_MAX_ENTRIES) {
      panelDebugLog.push({ t: now, event: "error", message: String(err && err.message) });
    }
  }
}

/**
 * 字幕:目前判斷「誰在講話」的主要訊號(2026-10-06 起)。
 *
 * 面板列/視訊格的指示器在大型會議只有清單最前面約 10 人會更新(規則查不出來,見
 * CLAUDE.md),字幕則是 Google 伺服器端標的講者名字,每個發言的人都有、不受版面跟螢幕
 * 分享影響——Recall.ai 的自建 bot 教學跟多數商用擴充功能都是讀這個。只拿「名字」跟
 * 「什麼時候在更新」,字幕的文字內容完全不用(逐字稿還是 Whisper 轉的)。
 *
 * 2026-10-06 用兩個真實帳號實測確認的行為:
 *   [role="region"][aria-label="Captions"]   <- 字幕區塊
 *     .nMcdL                                 <- 一個人連續講的一段(同一個節點原地加長文字,
 *                                               換人講才會出現新的一塊;舊的會留著)
 *       .NWpY1d                              <- 講者名字;自己顯示成「You」
 *       .ygicle                              <- 字幕文字
 *   - 字幕只顯示在開的人自己的畫面,其他與會者看不到。
 *   - 區塊設成 display:none 之後內容照常更新(藏起來 60 秒內更新 152 次)。
 *   - 藏起來之後,Meet 還是會替字幕保留畫面下方一塊空間(主畫面視訊格會矮一截),
 *     試過覆寫 main 的 bottom,視訊格不會重新排版,這塊空白目前拿不掉。
 *
 * 判斷方式:每次輪詢看每一塊的文字長度,有變動的那一塊的講者就是正在講話的人;字幕是
 * 一陣一陣更新的,最近 CAPTION_HOLD_MS 內有變動都算還在講。字幕比聲音晚大約一兩秒,
 * 伺服器端比對時本來就有容忍範圍。
 */
const CAPTION_BLOCK_SELECTOR = ".nMcdL";
const CAPTION_NAME_SELECTOR = ".NWpY1d";
const CAPTION_TEXT_SELECTOR = ".ygicle";
const CAPTION_HOLD_MS = 1500;
let captionState = new WeakMap(); // 字幕塊節點 -> 上次看到的文字長度
let captionLastChange = new Map(); // 講者名字(或 SELF_TIMELINE_SPEAKER) -> 最後一次有變動的 now
let captionsPrimed = false;
let captionActiveLogged = "";
let weTurnedOnCaptions = false;
let captionToggleBusy = false;

/**
 * 講者訊號自我檢查:Meet 改版時,擴充功能常常「安靜地」找不到字幕或名單(2026-10-29 那場整場 409 次檢查都
 * 沒找到字幕區塊,開完會才發現講者分錯)。錄音開始 15 秒後,每次 heartbeat 檢查一次字幕和名單是不是真的開著,
 * 狀態有變化才通知 background:工具列標籤變成黃色 `REC!`、popup 顯示提醒,使用者可以當場手動打開。
 */
const SIGNAL_CHECK_GRACE_MS = 15000;
let lastSignalProblems = "";

/** 使用者還在通話中嗎:掛斷按鈕(圖示 `call_end`)或視訊畫面任一個還在就算。兩個都要不見才算離開,
 * 避免 Meet 改版只弄壞其中一個偵測時,就把真正需要的提醒也關掉。 */
function stillInCall() {
  return !!findButtonByIcon("call_end") || !!getMainStage();
}

function checkSpeakerSignals(now, panelRows) {
  if (now < SIGNAL_CHECK_GRACE_MS) return;
  // 先結束會議才按停止錄音時,離開畫面上當然沒有字幕和名單,不是壞掉(2026-11-02 實測誤報一次 REC!)
  if (!stillInCall()) {
    if (lastSignalProblems !== "") {
      lastSignalProblems = "";
      chrome.runtime.sendMessage({ type: "SIGNAL_STATUS", target: "background", problems: [] }).catch(() => {});
    }
    return;
  }
  const problems = [];
  if (!findCaptionRegion()) problems.push("captions");
  if (!panelRows.length) problems.push("people");
  const key = problems.join(",");
  if (key === lastSignalProblems) return;
  lastSignalProblems = key;
  panelDebugLog.push({ t: now, event: "signalCheck", problems });
  chrome.runtime.sendMessage({ type: "SIGNAL_STATUS", target: "background", problems }).catch(() => {});
}

function isSelfCaptionName(name) {
  return MEET_UI_TEXT.captionSelfNames.includes(name);
}

/**
 * 字幕文字紀錄:每一塊字幕(一個人連續講的一段)的講者、第一次/最後一次更新時間、最新文字。
 * 文字內容不會進逐字稿,只給伺服器拿來「對齊」Whisper 的字——Whisper 的某個字如果跟某人
 * 字幕裡的字對得上,那個字就是那個人講的。這比「那個時間點誰的字幕在動」精準得多:
 * 2026-10-07 週會裡有位與會者(A)回一句「對,還沒」,當下兩個人的字幕都在更新、聲音分群也把它分給
 * 了另一位(B),只有字幕文字本身能分辨。Meet 會修正已經出現的字幕文字,所以每次都存最新的整段。
 */
let captionBlocks = new Map(); // 字幕塊節點 -> 紀錄物件
let captionBlockLog = []; // 依出現順序的紀錄物件(節點被 Meet 移除後紀錄還在)
const CAPTION_LOG_MAX_CHARS = 400000;
let captionLogChars = 0;

function findCaptionRegion() {
  return (
    [...document.querySelectorAll('[role="region"][aria-label]')].find((r) =>
      MEET_UI_TEXT.captionRegionLabels.includes(r.getAttribute("aria-label"))
    ) ||
    document.querySelector(CAPTION_BLOCK_SELECTOR)?.closest('[role="region"]') ||
    null
  );
}

/**
 * 開/關字幕。2026-10 Meet 改版後,工具列上是「字幕和翻譯」按鈕(圖示 `translate_cc`,開著時
 * aria-pressed="true"),按下去會跳出小選單,裡面才是開關:關著時是 `closed_caption_off` 圖示的
 * 「開啟字幕」,開著時是 `check` 圖示的「關閉即時字幕」。選單用完要再按一次主按鈕收起來。
 * 舊版介面是工具列上直接一顆 `closed_caption_off`/`closed_caption` 按鈕,留著當備援。
 * 全部用圖示判斷,不看文字,中英文介面都適用。
 */
async function setCaptions(on) {
  const main = findButtonByIcon("translate_cc");
  if (main) {
    const isOn = () => main.getAttribute("aria-pressed") === "true";
    if (isOn() === on) return false; // 已經是想要的狀態,沒動
    if (main.getAttribute("aria-expanded") !== "true") {
      main.click();
      await sleep(800);
    }
    const toggle = on
      ? findButtonByIcon("closed_caption_off")
      : [...document.querySelectorAll('button[jsname="RrG0hf"]')].find((b) => iconName(b) === "check");
    toggle?.click();
    for (let i = 0; i < 25 && isOn() !== on; i++) await sleep(200); // 實測狀態有時要 2 秒以上才更新
    if (main.getAttribute("aria-expanded") === "true") main.click();
    return !!toggle;
  }
  const legacy = findButtonByIcon(on ? "closed_caption_off" : "closed_caption");
  legacy?.click();
  return !!legacy;
}

/** 字幕現在是不是開著。新版介面看主按鈕的 aria-pressed;字幕區塊在關掉後可能還留在頁面上一陣子,
 * 不能拿「區塊在不在」判斷。沒有主按鈕(舊版介面)才退回看區塊。 */
function captionsAreOn() {
  const main = findButtonByIcon("translate_cc");
  return main ? main.getAttribute("aria-pressed") === "true" : !!findCaptionRegion();
}

/**
 * 字幕跟與會者名單要不要在畫面上藏起來,由使用者在 popup 決定(預設不藏)。不管藏不藏,錄音期間
 * 字幕跟名單都一定要開著——那是辨識「誰在講話」的訊號來源。藏的方式是 display:none:
 * - 字幕藏起來內容照常更新(實測過)。
 * - 名單藏的是整個側邊面板(`aside`)。藏起來之後面板列的講話指示器會不會照常更新沒有驗證過;
 *   字幕才是主要訊號,影響有限。
 * - 兩者藏起來後,Meet 原本保留給它們的位置(畫面下方/右側)都會留白,覆寫 main 的位置視訊格也不會
 *   重新排版,拿不掉。
 */
let hideCaptionsSetting = false;
let hidePeoplePanelSetting = false;

function findPeoplePanelAside() {
  return document.querySelector(PEOPLE_PANEL_ROW_SELECTOR)?.closest("aside") || null;
}

function applyVisibility() {
  const region = findCaptionRegion();
  const wantCaptions = hideCaptionsSetting ? "none" : "";
  if (region && region.style.display !== wantCaptions) region.style.display = wantCaptions;
  const aside = findPeoplePanelAside();
  const wantPanel = hidePeoplePanelSetting ? "none" : "";
  if (aside && aside.style.display !== wantPanel) aside.style.display = wantPanel;
}

/** 開字幕(如果還沒開),並依使用者設定決定要不要藏起來。每幾秒呼叫一次,Meet 重畫後也會再套用一次。 */
function ensureCaptionsOnAndHidden() {
  applyVisibility();
  if (captionsAreOn() || captionToggleBusy) return;
  captionToggleBusy = true;
  setCaptions(true)
    .then((clicked) => {
      if (clicked) weTurnedOnCaptions = true;
      applyVisibility();
    })
    .finally(() => {
      captionToggleBusy = false;
    });
}

function restoreCaptions() {
  const region = findCaptionRegion();
  if (region) region.style.display = "";
  const aside = findPeoplePanelAside();
  if (aside) aside.style.display = "";
  if (weTurnedOnCaptions) {
    setCaptions(false);
    weTurnedOnCaptions = false;
  }
}

/** 回傳字幕顯示「現在正在講話」的名字(自己回傳 SELF_TIMELINE_SPEAKER)。 */
function captionSpeakersNow(now) {
  const region = findCaptionRegion();
  if (!region) return [];
  region.querySelectorAll(CAPTION_BLOCK_SELECTOR).forEach((block) => {
    const rawName = block.querySelector(CAPTION_NAME_SELECTOR)?.textContent.trim();
    if (!rawName) return;
    const fullText = block.querySelector(CAPTION_TEXT_SELECTOR)?.textContent || "";
    const length = fullText.length;
    const previous = captionState.get(block);
    if (captionsPrimed && previous !== length) {
      let rec = captionBlocks.get(block);
      if (!rec) {
        rec = { speaker: isSelfCaptionName(rawName) ? SELF_TIMELINE_SPEAKER : rawName, startMs: now, endMs: now, text: "" };
        captionBlocks.set(block, rec);
        captionBlockLog.push(rec);
      }
      if (captionLogChars - rec.text.length + fullText.length <= CAPTION_LOG_MAX_CHARS) {
        captionLogChars += fullText.length - rec.text.length;
        rec.text = fullText;
      }
      rec.endMs = now;
    }
    captionState.set(block, length);
    // 開始錄音當下就已經在畫面上的舊字幕不算;之後新出現的一塊、或文字有變動才算。
    if (!captionsPrimed || previous === length) return;
    const name = isSelfCaptionName(rawName) ? SELF_TIMELINE_SPEAKER : rawName;
    if (name !== SELF_TIMELINE_SPEAKER && !isLikelyRealName(name)) return;
    captionLastChange.set(name, now);
    if (name !== SELF_TIMELINE_SPEAKER) cachedOtherNames.add(name);
  });
  captionsPrimed = true;
  const active = [];
  for (const [name, t] of captionLastChange.entries()) {
    if (now - t <= CAPTION_HOLD_MS) active.push(name);
  }
  const key = active.slice().sort().join("|");
  if (key !== captionActiveLogged && panelDebugLog.length < PANEL_DEBUG_MAX_ENTRIES) {
    captionActiveLogged = key;
    panelDebugLog.push({ t: now, src: "caption", active });
  }
  return active;
}

// 診斷紀錄、名單快取、面板縮放/實驗這些「輔助」步驟各自包起來:它們出錯只記一筆
// (最多記 20 筆,避免每 300ms 一筆把紀錄塞爆),絕對不能讓語者偵測本身跟著停擺。
let debugErrorCount = 0;
function safely(where, now, fn) {
  try {
    return fn();
  } catch (err) {
    if (debugErrorCount++ < 20) {
      panelDebugLog.push({ t: now, event: "error", where, message: String(err && err.message) });
    }
    return undefined;
  }
}

function scanSpeakingInner(now) {
  const currentlySpeaking = new Set();

  const panelRows = document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR);
  // 不管這次有沒有抓到面板列,都先記一筆環境快照(見 recordHeartbeat() 的說明)——
  // 這樣「面板列數量整場都是 0」這件事本身也會被明確記錄下來,不會因為舊版邏輯
  // 只在「有抓到列」時才寫資料,導致完全沒抓到的情況反而留下一片空白看不出原因。
  safely("heartbeat", now, () => recordHeartbeat(now, { panelRowCount: panelRows.length }));

  if (now - lastRosterCacheAt >= HEARTBEAT_INTERVAL_MS) {
    lastRosterCacheAt = now;
    safely("rosterCache", now, () => updateRosterCache(panelRows));
    safely("captionsOn", now, () => ensureCaptionsOnAndHidden());
    safely("signalCheck", now, () => checkSpeakerSignals(now, panelRows));
  }

  // 字幕是主要訊號(每個發言的人都有名字);面板列跟視訊格留著當輔助,三邊取聯集。
  (safely("captions", now, () => captionSpeakersNow(now)) || []).forEach((name) =>
    currentlySpeaking.add(name)
  );

  // 面板列跟視訊格兩邊都讀、取聯集(任何一邊顯示在講話就算)。原本是「面板有列
  // 就只看面板」,但 10/05、10/06 兩場都證實面板只有清單最前面 8~10 列會更新,
  // 其他人只能指望視訊格(主畫面沒被螢幕分享佔掉的時候)。
  if (panelRows.length > 0) {
    safely("panelIds", now, () => refreshPanelIds(panelRows));
    safely("panelSnapshot", now, () => recordPanelDebugSnapshot(panelRows, now));
    panelRows.forEach((row) => {
      if (isPanelRowSelf(row)) {
        if (isPanelRowSpeaking(row)) currentlySpeaking.add(SELF_TIMELINE_SPEAKER);
        return;
      }
      const name = getPanelRowName(row);
      if (!name) return;
      if (isPanelRowSpeaking(row)) currentlySpeaking.add(name);
    });
  }
  const stage = getMainStage();
  if (stage) {
    const tiles = stage.querySelectorAll(TILE_SELECTOR);
    safely("selfTiles", now, () => computeSelfTiles(tiles));
    safely("tileSnapshot", now, () => recordTileDebugSnapshot(tiles, now));
    const litNow = [...tiles].filter((t) => !isSelfTile(t) && isTileSpeaking(t)).length;
    tileLitStreak = litNow >= TILE_LIT_BROKEN_COUNT ? tileLitStreak + 1 : 0;
    if (!tileSpeakingDisabled && tileLitStreak >= TILE_LIT_BROKEN_SCANS) {
      tileSpeakingDisabled = true;
      panelDebugLog.push({ t: now, event: "signalDisabled", signal: "tileSpeaking", lit: litNow });
    }
    tiles.forEach((tile) => {
      // 自己的講話狀態不看視訊格:2026-10-07 實測 isSelfTile() 判斷到的那一格,整場只要
      // 有人講話就亮(1564 次),把「誰都在講」當成「你在講」,麥克風軌過濾因此完全失效。
      // 自己只看面板自己那一列跟字幕上的「You」,這兩個實測都只在真的講話時才亮。
      if (isSelfTile(tile)) return;
      const name = getTileName(tile);
      if (!name) return;
      if (!tileSpeakingDisabled && isTileSpeaking(tile)) currentlySpeaking.add(name);
      if (isTileMuted(tile)) currentlySpeaking.add(MUTED_TIMELINE_PREFIX + name);
    });
  }

  if (safely("micState", now, () => micUnmuted()) === true) currentlySpeaking.add(UNMUTED_TIMELINE_SPEAKER);

  for (const name of currentlySpeaking) {
    if (!speakingSince.has(name)) {
      speakingSince.set(name, now);
    }
  }

  // 收尾:剛剛還在講話、現在不講了的人,存進 timeline
  for (const [name, startMs] of speakingSince.entries()) {
    if (!currentlySpeaking.has(name)) {
      timeline.push({ speaker: name, startMs, endMs: now });
      speakingSince.delete(name);
    }
  }
}

// 分頁被 Chrome 背景凍結太久時,上面 setInterval(scanSpeaking, 300) 那個計時器會
// 完全停止回報(實測真的發生過:連續 11 分鐘沒有任何一筆資料)。background.js 用
// chrome.alarms(不受單一分頁背景節流影響)每隔約 1 分鐘戳一次這裡,叫它密集
// 偵測幾秒鐘再停——不是奢望分頁能一直保持清醒(Chrome 沒開放這種權限),只是
// 盡量在長時間背景的空窗期裡,還能留下幾段準確的樣本,而不是完全沒有任何資料。
let burstInProgress = false;
async function burstCheck(durationMs = 4000, intervalMs = 300) {
  if (burstInProgress || recordingStartTime == null) return;
  burstInProgress = true;
  try {
    const cycles = Math.ceil(durationMs / intervalMs);
    for (let i = 0; i < cycles; i++) {
      scanSpeaking();
      await sleep(intervalMs);
    }
  } finally {
    burstInProgress = false;
  }
}

/** 優先開 People 面板讀名單(比較準,不會漏掉自己的名字),讀不到才退回視訊格版本。
 * 兩種都完全讀不到任何人的話,等個幾秒再整個重試一次。在 stop-speaker-tracking 呼叫,
 * 這時候不趕時間(使用者本來就要等上傳處理),值得花這個時間換取比較準的結果。 */
async function acquireRoster() {
  for (let attempt = 0; attempt < 2; attempt++) {
    let panelRoster = null;
    try {
      panelRoster = await readRosterFromPeoplePanel();
    } catch (_) {
      panelRoster = null;
    }
    if (panelRoster) {
      return {
        myName: panelRoster.myName || getMyDisplayName(),
        roster: { totalCount: panelRoster.totalCount, otherNames: panelRoster.otherNames },
      };
    }

    const myName = getMyDisplayName();
    const roster = getParticipantRoster();
    if (myName || (roster && roster.otherNames.length)) {
      return { myName, roster };
    }

    if (attempt === 0) await sleep(2000);
  }
  return { myName: getMyDisplayName(), roster: getParticipantRoster() };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target !== "content") return;

  // popup 用來確認這個分頁裡的 content script 還跟擴充功能連著(沒連著就會補注入)。
  if (msg.type === "ping") {
    sendResponse({ ok: true });
    return;
  }

  if (msg.type === "burst-check") {
    burstCheck();
    return;
  }

  if (msg.type === "set-visibility") {
    hideCaptionsSetting = !!msg.hideCaptions;
    hidePeoplePanelSetting = !!msg.hidePeoplePanel;
    safely("visibility", 0, () => applyVisibility());
    sendResponse({ ok: true });
    return;
  }

  if (msg.type === "start-speaker-tracking") {
    recordingStartTime = msg.startTime;
    lastSignalProblems = "";
    tileLitStreak = 0;
    tileSpeakingDisabled = false;
    hideCaptionsSetting = !!msg.hideCaptions;
    hidePeoplePanelSetting = !!msg.hidePeoplePanel;
    timeline = [];
    speakingSince = new Map();
    panelDebugLog = [];
    panelDebugLastState = new Map();
    lastHeartbeatAt = -Infinity;
    lastRosterCacheAt = -Infinity;
    lastPanelFitCheckAt = -Infinity;
    restorePanelZoom();
    cachedMyName = null;
    cachedOtherNames = new Set();
    tileDebugLastState = new Map();
    captionState = new WeakMap();
    captionBlocks = new Map();
    captionBlockLog = [];
    captionLogChars = 0;
    captionLastChange = new Map();
    captionsPrimed = false;
    captionActiveLogged = "";
    debugErrorCount = 0;

    // 開一次 People 面板,而且整場錄音期間都不關掉——scanSpeaking() 靠這個面板
    // 讀不受螢幕分享影響的即時語者訊號(見 scanSpeaking() 的註解)。跟抓名單那次
    // 「讀完馬上關掉」不一樣,這裡是故意要它開著。只在我們自己打開的情況下記錄
    // 起來,停止錄音時才知道要不要幫忙關掉、不要誤關使用者自己開的面板。
    weOpenedPeoplePanelForTracking = false;
    const alreadyOpenBeforeStart = document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR).length > 0;
    let openButtonFoundAtStart = alreadyOpenBeforeStart; // 本來就開著就不需要找按鈕
    if (!alreadyOpenBeforeStart) {
      const openBtn = findPeoplePanelOpenButton();
      openButtonFoundAtStart = !!openBtn;
      if (openBtn) {
        openBtn.click();
        weOpenedPeoplePanelForTracking = true;
      }
    }
    // 開始錄音當下「找不找得到開面板的按鈕」這件事本身先記一筆——2026-10-01 那場
    // 會議 speaker_debug.json 整場是空的,連這個最早的環節都無法回頭確認是否成功,
    // 這筆記錄讓下次出狀況時第一眼就能排除/鎖定這個環節。
    panelDebugLog.push({
      t: 0,
      event: "start",
      alreadyOpenBeforeStart,
      openButtonFoundAtStart,
      rowCountRightAfterStart: document.querySelectorAll(PEOPLE_PANEL_ROW_SELECTOR).length,
    });

    safely("captionsOn", 0, () => ensureCaptionsOnAndHidden());

    pollTimer = setInterval(scanSpeaking, 300);

    // 這裡只做一次快速、不等待的初步檢查,純粹是為了 popup 能馬上顯示「有沒有抓到
    // 名字」的提示——不能在這裡卡著等 People 面板慢慢渲染(尤其大型會議可能要等
    // 好幾秒),不然會拖到錄音本身開始的時間點。真正會送給伺服器用的權威名單,
    // 是停止錄音那一刻才讀的(見 stop-speaker-tracking),這裡的結果只是參考用。
    sendResponse({ ok: true, myName: getMyDisplayName(), roster: getParticipantRoster() });
    return;
  }

  if (msg.type === "stop-speaker-tracking") {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    const now = Date.now() - (recordingStartTime || Date.now());
    for (const [name, startMs] of speakingSince.entries()) {
      timeline.push({ speaker: name, startMs, endMs: now });
    }
    speakingSince = new Map();

    // 名單在這裡重新讀一次,是真正權威的來源——不能只靠 popup 在「開始錄音」當下
    // 抓到的值:popup 是一個使用者隨時會關掉的小視窗,關掉再打開就是全新的 JS
    // 執行環境,先前抓到的 myName/otherNames 會直接消失變回預設值(不是 content
    // script 抓錯,是 popup 自己的生命週期把資料弄丟的)。content script 在整個
    // 會議期間持續存活沒有這個問題,而且這時候 Meet 畫面一定已經渲染穩定,比錄音
    // 剛開始那一刻更可靠,值得花時間耐心讀(這裡不趕時間,反正使用者本來就要等
    // 上傳)。
    acquireRoster().then((result) => {
      restorePanelZoom();
      try {
        restoreCaptions();
      } catch (_) {
        // 已經離開會議、按鈕不在了也沒關係
      }
      // 錄音期間為了語者偵測開著的面板,結束後如果是我們自己開的就順手關掉,
      // 不要留一個使用者沒要求的面板佔著畫面。acquireRoster() 內部的
      // readRosterFromPeoplePanel() 因為面板這時候已經是開著的(alreadyOpen
      // 為真),不會重複開也不會自己關,所以這裡要自己補上這一步。
      if (weOpenedPeoplePanelForTracking) {
        findPeoplePanelCloseButton()?.click();
        weOpenedPeoplePanelForTracking = false;
      }
      // 當下讀到的名單跟錄音期間累積的快取合併(見 updateRosterCache() 的說明:
      // 使用者常常先離開會議才按停止,這時候當下什麼都讀不到)。
      const liveNames = (result.roster && result.roster.otherNames) || [];
      const myName = result.myName || cachedMyName;
      const otherNames = [...new Set([...cachedOtherNames, ...liveNames])].filter((n) => n !== myName);
      sendResponse({
        timeline,
        panelDebugLog,
        captionLog: captionBlockLog,
        myName,
        roster: { totalCount: otherNames.length + 1, otherNames },
      });
    });
    return true; // 非同步回應(要等 People 面板渲染),保持訊息通道開著
  }
});
