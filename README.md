# 機票特價看板 Flight Deal Watcher（免費爬蟲版）

自動監測多個航線的來回機票價格,在你設定的日期區間裡找出目前最便宜的組合,
更新到一個 GitHub Pages 網站,出現破盤價時推播通知到手機。**完全免費、不需要
申請任何 API 金鑰、不需要信用卡。**

## 這個專案怎麼抓資料,跟你原本想的爬蟲有什麼不一樣

你參考的那個 `Flightradar24_crawler` 抓的是**飛機即時位置/航班時刻**(給飛機
攝影愛好者用的),跟機票**價格**是完全不同的資料來源,沒辦法直接拿來查便宜機票。

這裡改用 [`fast-flights`](https://github.com/AWeirdDev/flights) —— 一個把
Google Flights 網頁背後傳輸資料格式(base64 編碼的 Protobuf)還原出來直接查詢的
Python 套件。不用註冊、不用金鑰,查詢邏輯上就是一種爬蟲,只是不用真的開瀏覽器
去解析 HTML,速度快很多。

**老實說的取捨:**
- 這是非官方、逆向工程出來的方式,Google 隨時可能改版讓它壞掉。如果某天程式
  突然查不到資料,先去 [fast-flights 的 GitHub](https://github.com/AWeirdDev/flights)
  看是不是套件更新了、用法變了。
- 查太頻繁、太多次,IP 有可能被 Google 暫時限流(尤其 GitHub Actions 用的是
  機房 IP,比一般家用網路更容易被注意到)。所以這個專案**故意不會每次把整個
  日期區間全部查完**,而是每次排程只抽一小批,慢慢累積,詳見下一段。

如果之後真的需要更穩定、即時的報價,`fast-flights` 也支援接第三方的付費 API
(SearchApi、Bright Data)當備援,但那些要收費,不是這個免費版的預設行為。

## 「不用固定天數,讓程式自己找最便宜」是怎麼做到的

因為免費方案沒辦法像付費 API 一樣「給一段日期區間,直接回傳裡面最便宜的組合」,
只能一組一組固定的「出發日 + 回程日」去查,所以程式的做法是:

1. 把你在 `config.yaml` 設的日期區間 x 停留天數選項,展開成一大串候選組合
   (例如：每隔 7 天抽一個出發日,每個出發日試 4 種停留天數)
2. 每次排程執行,只抽其中一小批去查(用 `docs/data/state.json` 記住上次抽到哪,
   下次接著抽,抽完一輪自動從頭開始),避免一次查太多被限流
3. 每次查到的價格都累積進 `docs/data/history.csv`,**永久保留**
4. 每次執行都用「目前累積到的全部歷史資料」重新算出每條航線目前已知最便宜的
   組合,更新到網站上

也就是說:剛上線的前幾天,網站顯示的「最便宜」只是掃過的那一小部分裡最便宜的,
隨著排程一天天累積,涵蓋的日期組合越多,結果會越接近真正的全域最低價。網站上
會顯示目前掃過幾組、總共有幾組候選組合,方便你知道累積進度。

## 專案結構

```
flight-deal-watcher/
├── config.yaml                  # 你在這裡設定：出發地/目的地、日期範圍、抽樣頻率、通知門檻
├── requirements.txt
├── scripts/
│   └── flight_monitor.py        # 主程式：抽一批候選組合去查、累積歷史、更新資料、判斷通知
├── docs/                         # 這個資料夾會變成你的 GitHub Pages 網站
│   ├── index.html                # 網站首頁
│   └── data/
│       ├── latest.json          # 目前最新的排行結果（網站讀這個檔案）
│       ├── history.csv          # 每次查到的價格都累積在這裡
│       └── state.json           # 記錄抽樣抽到哪裡了，不用手動管
└── .github/workflows/
    └── check-flights.yml        # 排程：預設每天一次自動執行，並自動部署網站
```

## 操作步驟

### 1. 建立 GitHub repo 並上傳這個專案

```bash
cd flight-deal-watcher
git init
git add .
git commit -m "init flight deal watcher"
git branch -M main
git remote add origin https://github.com/<你的帳號>/flight-deal-watcher.git
git push -u origin main
```

### 2.（可選）設定手機通知

1. 手機下載 App「ntfy」(iOS / Android),或用網頁版 https://ntfy.sh
2. App 裡點「Subscribe to topic」,自己取一個獨特的名字,例如 `tw-flight-alerts-a8x92k`
   (topic 名字沒有帳號保護,取夠獨特的字串就好,不要用 `test` 這種常見字)
3. 到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**,
   新增一個 `NTFY_TOPIC`,值就是剛剛取的名字
4. 沒設定這個 secret 也沒關係,程式一樣會正常查價、更新網站,只是不會推播通知

### 3. 開啟 GitHub Pages

到 repo → **Settings → Pages**：
- Source 選擇 **"GitHub Actions"**（不是選 branch,因為部署是由 workflow 觸發的）

第一次 workflow 跑完後,網址會出現在 **Settings → Pages** 頁面上方,
格式通常是 `https://<你的帳號>.github.io/flight-deal-watcher/`。

### 4. 依你的需求調整 `config.yaml`

打開 `config.yaml`：

- `origins` / `destinations`：想從哪裡出發、想去哪些地方
- `date_window`：要監測的日期範圍
- `date_step_days`：出發日抽樣間隔（數字越小掃得越細，但查詢量越大、越容易被限流）
- `duration_options`：想比較的停留天數清單（不用只填一個，可以列多個一起比較）
- `max_checks_per_run`：每次排程最多查幾組（配合下面的排程頻率調整）
- `notify`：通知門檻（比歷史最低價再便宜多少 % / 絕對金額 / Google 自己標「偏低」時）

改完後 commit + push 上去就會套用到下一次排程。

### 5. 手動測試一次

repo 頁面 → **Actions** → 選 "Check flight prices" → 右邊 **"Run workflow"**,
可以馬上手動觸發一次,不用等排程時間到,方便確認設定沒問題。也可以看 Actions
的執行紀錄,裡面會印出每一組查到的價格,方便確認 `fast-flights` 有沒有正常運作。

## 之後的日常使用

你什麼都不用做。GitHub Actions 會照排程自動：
1. 抽一批候選日期組合去查詢
2. 累積進歷史紀錄,重新算出每條航線目前已知最便宜的組合
3. 更新網站排行榜
4. 出現破盤價就推播通知

想改監測條件、想加目的地,都只要改 `config.yaml` 再 push 一次就好。

## 想調整查詢頻率或抽樣密度？

修改 `.github/workflows/check-flights.yml` 的 cron,或 `config.yaml` 的
`date_step_days` / `duration_options` / `max_checks_per_run`。抓資料越密集,
越快掃完整個區間、結果越準,但也越容易觸發 Google 的限流。如果發現查詢
常常失敗或回不到資料,就把這幾個數字調鬆一點。
