# 安全性回報 / Security Policy

## 回報安全漏洞

**請不要用公開的 Issue 回報安全漏洞。** 公開之後,修好之前任何人都看得到怎麼攻擊。

請改用 GitHub 的私下回報功能:

1. 到這個 repo 的 **Security** 分頁
2. 按 **Report a vulnerability**
3. 填寫內容後送出

這份回報只有維護者看得到,後續討論也在同一個私下頁面進行,不需要交換 email。

回報時請盡量附上:

- 問題出在哪個部分(擴充功能、本機伺服器、Native Messaging host、會議紀錄 skill)
- 重現步驟,或可以觸發問題的最小範例
- 可能造成的影響(例如:其他網頁能讀到逐字稿、能讓伺服器執行指令)
- 你的環境:macOS 版本、Chrome 版本、GMScribe 版本或 commit

這是個人維護的開源專案,會盡力處理,但沒辦法保證回覆時間。確認是漏洞後會先修正,再視情況公開說明;
如果你希望在公開說明裡被致謝,請在回報裡註明。

## 範圍

**算安全問題的:**

- 一般網頁、其他擴充功能或區網上的其他電腦,能連到本機伺服器(上傳檔案、讀取逐字稿、觸發運算)
- 擴充功能或伺服器洩漏會議內容到本機以外
- 透過 Native Messaging host 執行「啟動 / 關閉 / 查狀態」以外的動作
- 會議中有人說出或在名稱裡放入特定內容,就能讓會議紀錄 skill 執行指令、讀取不相關的檔案或連上網路(prompt injection)
- 安裝腳本或相依套件有被竄改、下載到非預期來源的風險

**不算安全問題、請用一般 Issue 回報的:**

- Google Meet 改版造成講者偵測失效、字幕抓不到
- 語音辨識或講者判斷不準確
- 需要先取得你電腦的登入權限才能做到的事(已經能操作你的電腦,就不需要透過 GMScribe)

## 支援的版本

只維護最新版本(`main` 分支)。修正不會回頭套用到舊版本。

---

**English summary:** Please do not open public issues for security vulnerabilities. Use GitHub's private
reporting instead: **Security** tab → **Report a vulnerability**. Only the maintainer can see the report.
This is a personally maintained project; reports are handled on a best-effort basis. Only the latest
version on `main` is supported.
