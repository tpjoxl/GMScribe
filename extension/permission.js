// 承載 Chrome 麥克風授權視窗用的分頁。錄音在 offscreen 文件裡做,那裡跟 popup 都沒辦法跳出授權視窗,
// 一定要有一個分頁。這個頁面一打開就直接觸發 Chrome 的授權視窗,按「造訪這個網站時允許」後自己關掉。
const statusEl = document.getElementById("status");
const retryBtn = document.getElementById("grant");
const card = document.getElementById("card");

function show(text, kind) {
  statusEl.textContent = text;
  statusEl.className = kind || "";
  card.classList.toggle("waiting", !kind);
}

let stuckTimer = null;

async function requestMic() {
  retryBtn.style.display = "none";
  show("");
  // Chrome 有時不會直接跳出視窗(例如縮成網址列上的小圖示);等 5 秒還沒結果就提示,並給一個重新跳出的按鈕。
  clearTimeout(stuckTimer);
  stuckTimer = setTimeout(() => {
    show("沒看到詢問視窗嗎?請看網址列右側有沒有麥克風圖示,點它選「允許」;或按下面的按鈕重新跳出視窗。", "warn");
    retryBtn.textContent = "重新跳出授權視窗";
    retryBtn.style.display = "inline-block";
  }, 5000);
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    clearTimeout(stuckTimer);
    retryBtn.style.display = "none";
    show("✅ 授權完成,這個分頁馬上自動關閉", "ok");
    setTimeout(() => window.close(), 1200);
  } catch (err) {
    clearTimeout(stuckTimer);
    show(
      err && err.name === "NotAllowedError"
        ? "沒有取得麥克風權限。如果剛才關掉了視窗或選了「一律不允許」,請點網址列左邊的圖示,把麥克風改成「允許」,再按下面的按鈕。"
        : "麥克風無法使用(" + (err && err.message) + ")。請確認麥克風有接好、沒有被其他程式占用,再按下面的按鈕。",
      "warn"
    );
    retryBtn.textContent = "再試一次";
    retryBtn.style.display = "inline-block";
  }
}

retryBtn.addEventListener("click", requestMic);

navigator.permissions.query({ name: "microphone" }).then((p) => {
  if (p.state === "granted") {
    show("✅ 已經授權過麥克風,這個分頁馬上自動關閉", "ok");
    setTimeout(() => window.close(), 1200);
  } else {
    requestMic();
  }
});
