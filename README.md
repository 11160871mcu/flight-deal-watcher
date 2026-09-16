# ✈️ TPE 機票優惠雷達（Flight Deal Watcher）

全自動的機票比價系統，從台灣桃園機場（TPE）出發，24 小時不間斷追蹤日韓
8 條航線未來一年內的直飛來回票價，自動整理成網頁，並在偵測到「真正划算
的降價」時主動推播到手機。**完全免費、不需要 API 金鑰、不需要信用卡。**

線上網頁：`index.html`（由 Vercel 部署，讀取 `docs/data/latest.json`）

---

## 📍 涵蓋範圍

| 項目 | 內容 |
|---|---|
| 出發地 | TPE 台北桃園 |
| 目的地 | NRT 東京成田、HND 東京羽田、KIX 大阪關西、FUK 福岡、CTS 札幌新千歲、OKA 沖繩那霸、ICN 首爾仁川、PUS 釜山金海（共 8 條航線） |
| 艙等 / 人數 | 經濟艙、1 大人 |
| 航班限制 | 僅直飛 |
| 出發日期窗口 | 每次執行都用當下的「今天」重新算，永遠是**未來 12 個月** |
| 停留天數 | 來回停留 **5～15 天**，每天一格 |
| 幣別 | 新台幣（TWD）來回總價 |

---

## 🔍 搜尋策略：雙軌制（Dual-Track）

「未來一年 × 8 條航線 × 5～15 天停留」全部組合起來高達 3 萬多組，不可能
每次都掃過一遍，所以系統設計成兩條軌道同時、交錯進行：

### 1. 近期軌（near track）— 高頻更新
- 範圍：今天起 **90 天內**出發的日期
- 每次執行查 **144 組**（8 條航線平均分配，每個目的地 18 組）
- 目的：這段時間票價波動最大，最容易撿到快閃降價，查得密。

### 2. 全年軌（annual track）— 廣度覆蓋
- 範圍：今天起 **12 個月內**出發的日期
- 每次執行查 **288 組**（8 條航線平均分配，每個目的地 36 組）
- 目的：確保半年、十個月後的票價也會被慢慢輪過一遍，不漏掉遠期特價。

> 📌 **配額調整說明**：這兩個數字（`near_checks_per_run` /
> `annual_checks_per_run`）比最初設計的值高，是因為**實際觸發頻率
> 不一定很密**（久久才跑一輪），所以把單次執行的搜尋量調高，減少
> 資料更新速度對「觸發間隔」的依賴。上限受 GitHub Actions 的
> `timeout-minutes: 180` 限制，如果實測發現常跑到接近上限（甚至被
> 強制中止），到 `config.yaml` 把這兩個數字往下調即可，不需要動
> 程式碼。

兩軌交錯進行，並用 `state.json` 記錄每條航線目前查到的**確切日期**，
下次接著上次的地方繼續查，長期下來每個日期組合都會被輪到，且用公平
配額機制不讓某條航線一直被優先查、其他被冷落。

### 3. Cheap Score（便宜指數）—「這個價格算便宜嗎」
- 抓該航線近期（預設 60 天）查到的所有價格組成價格池
- 這次價格贏過價格池裡幾成 → 轉換成 0～100 分
- 是一種**相對排名**：回答「這個價格，在歷史紀錄裡算不算便宜」

### 4. Price Drop 偵測 —「這個價格是不是剛剛才變便宜」
跟 Cheap Score 是完全不同的訊號：不看歷史排名，只看**同一組**
出發日 / 回程日 / 停留天數，這次查到的價格比**上一次**查到的價格
跌了多少。

- 跌幅達到門檻（預設 **20%**，`price_drop_alert_percent` 可調）才標記
  `price_drop: true`
- 網站上這種航班會多一個閃爍的紅色徽章「🔻 剛降價 45.3%」
- 排序預設就是「剛降價優先」，一打開網站就看得到

> 為什麼兩個都要：Cheap Score 抓的是「這裡本來就划算」，Price Drop 抓的是
> 「這裡剛剛發生了不尋常的變化」。只看排名會錯過稍縱即逝的限時甩票，
> 只看漲跌又沒辦法判斷這個價格放進大盤裡算不算真的便宜。

---

## 🧭 游標系統：直接用「日期」記錄進度（動態、不會被侵蝕）

### 設計原則
`state.json` 裡每條航線、每個軌道的游標，**直接存一個真正的日期**，
例如：

```json
"near_cursor_by_route": {
  "TPE|CTS": { "date": "2026-10-09", "stay_index": 3 }
}
```

規則只有三條，簡單到不會有隱藏的計算誤差：

1. **游標日期比今天舊** → 直接跳到今天（過去的日期沒有意義，跳過
   不扣分）
2. **游標日期超過搜尋視窗尾端**（今天+90天 / 今天+12個月）→ **繞回
   視窗開頭（今天）重新開始一輪**。因為視窗尾端每次執行都是用**當下
   的今天**重新算出來的，所以搜尋範圍永遠是動態的「未來一年」，不會
   被寫死在某個固定日期
3. 其餘狀況：照順序往下一個「日期 × 停留天數」前進，一次執行完直接
   把走到的那個確切日期寫回 `state.json`

### 為什麼這樣就不會被侵蝕
沒有任何位移補償的數學公式（那正是最容易出 bug 的地方）。游標本身就
是一個具體日期，一天一天往前走、超過視窗就繞回去，邏輯單純到不需要
「猜測昨天的位置換算到今天是哪裡」。就算某一輪完全沒跑到，游標停在
原地，下次接著跑，不會被懲罰。

### 怎麼親眼確認真的有在往前走
不用心算圈數，直接看：
- `state.json` 打開來，`date` 欄位本身就是實際日期，跟上次比對就知道
  有沒有前進
- 網頁最下方（篩選面板裡）的「📡 掃描進度」直接顯示「近期軌各航線目前
  掃到 O月O日 ~ O月O日」，過幾輪再看，這個範圍應該會持續往後移動

---

## 🔔 通知：ntfy.sh 推播（跟網頁卡片一樣好讀，且只通知真正划算的）

### 通知規則：要同時符合兩個條件
1. **`price_drop`**：跟自己上一次比，跌幅有達到 `price_drop_alert_percent`
   （預設 20%）
2. **`cheap_score >= notify_min_cheap_score`**（`config.yaml`，預設
   `70`，對應網頁分級「70~89 不錯以上」）：跟同航線歷史價格池比，
   這個價格本身也真的算便宜，避免「跌了 50% 但本來就貴，跌完還是貴」
   這種沒意義的通知

### 通知內容範例（跟網頁卡片同樣的資訊、同樣好讀）
🔥 **偵測到 2 個又降價又划算的機票**

### ✈️ 札幌新千歲 (CTS) → 台北桃園 (TPE)
- 📅 9/14 ~ 9/25（停留 11 天）
- 💰 現在 **NT$9,654**
- 📉 原價 NT$18,458，**降了 47.7%**
- 🔥 便宜指數 **89/100**

### ✈️ 首爾仁川 (ICN) → 台北桃園 (TPE)
- 📅 9/22 ~ 10/1（停留 9 天）
- 💰 現在 **NT$4,097**
- 📉 原價 NT$7,540，**降了 45.7%**
- 🔥 便宜指數 **100/100**

...等其餘 **1 筆划算的降價**

- 沒有任何符合「既降價又划算」的組合時**不會發通知**
- 若有設定 `site_url`，點通知會直接打開你的優惠網站

### 設定步驟
1. 手機安裝 **ntfy App**（iOS / Android 搜尋 "ntfy"），或用網頁版
   <https://ntfy.sh/app>
2. 想一個獨特的頻道名稱（**不要**取成一看就懂的名字，混一點隨機字元，
   例如 `tw-tpe-flight-a8x92k7f`）
3. App 裡 **"+" → Subscribe to topic**，貼上剛剛想的名稱
4. GitHub repo → **Settings → Secrets and variables → Actions →
   New repository secret**：`NTFY_TOPIC` = 剛剛那組頻道名稱
5. （可選）`config.yaml` 的 `site_url` 填上你的網址
6. 想測試可以先把 `notify_min_cheap_score` 調低（例如 `0`）、
   `price_drop_alert_percent` 調低（例如 `1`），測完記得改回來

> ntfy 免費公開伺服器沒有加密、也不保證送達，拿來收機票降價通知完全沒問題，
> 但不要拿它傳真正機密的東西。

---

## ⏰ 自動化排程：兩條獨立管道

### 管道 1：GitHub Actions 自己的排程（維持不變）

```yaml
schedule:
  - cron: "17 */3 * * *"   # 每 3 小時整點過 17 分，一天八輪
```

換算成台灣時間：**08:17 / 11:17 / 14:17 / 17:17 / 20:17 / 23:17 /
02:17 / 05:17**。也支援手動觸發（`workflow_dispatch`）。

### 管道 2：Vercel Cron → 呼叫 GitHub API → 觸發同一個 workflow

只有 GitHub Actions 自己的排程時，缺點是它在整點附近常常是全球最壅塞
的時段，偶爾會延遲甚至被跳過。這條管道加了一條**完全獨立的觸發路徑**：
Vercel Cron（排程系統）
↓ 定時打
`/api/trigger-scan`（Vercel 上的一支很小的 API，不爬蟲）
↓ 呼叫
GitHub REST API：`POST .../actions/workflows/check-flights.yml/dispatches`
↓ 觸發
你原本就有的 `workflow_dispatch`，**實際爬蟲工作 100% 還是在 GitHub Actions 上跑**

`/api/trigger-scan.js` 這支 API 本身**完全不會去打 Google Flights**，
只是負責「按下 GitHub Actions 的開始按鈕」。兩條管道有機會前後腳觸發，
但 `check-flights.yml` 裡已經設定 `concurrency` 群組鎖，就算撞在一起
也不會互相干擾、不會同時寫壞資料檔。

#### 設定步驟

1. **建立 GitHub Personal Access Token**（讓 Vercel 有權限觸發你的
   workflow）：
   - GitHub → 右上角頭像 → **Settings → Developer settings →
     Personal access tokens → Fine-grained tokens → Generate new token**
   - Repository access：選你這個 repo
   - Permissions：**Actions → Read and write**
   - 建立後複製這串 token（只會顯示一次）

2. **到 Vercel 專案 → Settings → Environment Variables**，新增：

   | Name | Value |
   |---|---|
   | `GH_TRIGGER_TOKEN` | 剛剛建立的 GitHub token |
   | `GH_OWNER` | 你的 GitHub 帳號 / 組織名稱 |
   | `GH_REPO` | repo 名稱 |
   | `CRON_SECRET` | 自己隨便打一串亂碼（擋掉非 Vercel Cron 的呼叫） |

3. `vercel.json` 已經設定好 `crons`，重新部署（push 到 main）後，
   Vercel 會依照排程自動打 `/api/trigger-scan`。

4. 想手動測試：瀏覽器打開 `https://你的網址/api/trigger-scan`（若有設
   `CRON_SECRET`，直接用瀏覽器打會被擋 401，這是安全機制生效，正常
   現象；真正要測試，用帶 Authorization header 的工具打，或先把
   `CRON_SECRET` 暫時拿掉再測）

> ⚠️ **重要提醒**：Vercel **Hobby（免費）方案的 Cron Jobs 目前限制
> 每天最多觸發一次**，就算 `vercel.json` 設定「每 3 小時一次」，Hobby
> 方案下實際只會挑一個時段真正執行。這條管道在免費方案下的效果是
> 「每天多一次額外的保底觸發」；如果想要跟 GitHub Actions 一樣一天
> 8 次，需要升級 Vercel **Pro** 方案。

### 執行流程
1. Checkout 專案原始碼
2. 安裝 Python 3.11 及相依套件
3. 執行 `scripts/flight_monitor.py`：
   - 讀取每條航線目前的日期游標（`state.json`）
   - 依雙軌策略、公平配額挑出本輪要查的日期組合
   - 逐一查詢並寫入結果
   - 重新計算 Cheap Score、比對 Price Drop、更新掃描進度
   - 輸出最新的 `latest.json`（含 `scan_progress`）、更新 `state.json`
   - 有「既降價又真的划算」的組合就發送 ntfy 推播
4. 若資料有變動，自動 `git commit`（訊息含 `[skip ci]`）並 push 回 `main`
5. Push 前先 `git pull --rebase`，降低跟手動修改衝突的機率
6. `concurrency` 群組鎖避免兩條觸發管道同時寫入資料檔

---

## 🗂️ 專案結構
.
├── `api/`
│   └── `trigger-scan.js` # Vercel Cron 用來觸發 GitHub Actions 的小 API
├── `.github/workflows/check-flights.yml` # GitHub Actions 自己的排程
├── `scripts/flight_monitor.py` # 爬蟲 + 雙軌搜尋（日期游標）+ Cheap Score + Price Drop + ntfy 推播
├── `config.yaml` # 所有搜尋 / 通知參數設定（含每輪查詢量）
├── `docs/`
│   ├── `index.html` # 前端網頁（篩選 / 排序 / 降價徽章 / 每日最低價定位捲動 / 掃描進度）
│   └── `data/`
│       ├── `latest.json` # 最新一批優惠（含 Score、降價標記、scan_progress）
│       ├── `history.csv` # 所有歷史查詢紀錄
│       └── `state.json` # 日期游標 + 累計嘗試次數（version 7）
├── `requirements.txt`
└── `vercel.json` # 部署設定 + crons 排程

## 🖥️ 前端功能（`index.html`）
- 目的地 / 航空公司（想要／排除）多選篩選
- 出發日期範圍、停留天數、最高價格、最低 Cheap Score 篩選
- 排序：**剛降價優先**（預設）、相對便宜優先、價格高低、折扣幅度、出發日期、停留天數
- 剛降價的卡片有紅色閃爍徽章，標示跌幅與原價
- 「各出發日期最低票價」快速下拉選單：**選一個日期後，會自動放寬會擋
  到這筆資料的篩選條件，並捲動、高亮到對應的優惠卡片**，可以直接看到
  完整資訊（航空公司、起降時間、Score…），不用只看價格文字自己去找
- 篩選面板**最上面（點標題列）跟最下面（收起篩選條件按鈕）都能收合**，
  不用每次都滑回最上面才能收起來
- **📡 掃描進度**：直接顯示游標目前掃到哪一天，數字用日期本身呈現，
  可以直接驗證系統真的在往前掃、不是卡住
- 手機版響應式排版

---

## ⚙️ `config.yaml` 主要設定說明

```yaml
# 雙軌搜尋額度（每次執行）
# 因為觸發頻率不一定很密，配額調高以增加單次執行的搜尋量，
# 上限受 GitHub Actions timeout-minutes: 180 限制，跑起來若常常
# 接近上限，往下調這兩個數字即可。
near_term_days: 90
near_checks_per_run: 144
annual_checks_per_run: 288

# Price Drop：跌幅達這個 % 才標記為突然降價
price_drop_alert_percent: 20

# 通知門檻：要「降價」且「Cheap Score 達這個分數以上」才會真的推播
notify_min_cheap_score: 70

# ntfy 推播行為（頻道名稱本身是機密，走 GitHub Secrets 不寫在這裡）
notify_max_items: 10
site_url: ""

# Cheap Score
history_days: 60
min_score_samples: 20
```

---

## 🧾 `state.json` 格式（version 7）

```json
{
  "version": 7,
  "grid_signature": "...",
  "near_cursor_by_route": {
    "TPE|NRT": { "date": "2026-09-20", "stay_index": 3 }
  },
  "annual_cursor_by_route": {
    "TPE|NRT": { "date": "2026-12-02", "stay_index": 0 }
  },
  "near_rotation": 0,
  "annual_rotation": 0,
  "total_attempts": 0
}
```

- 每條航線的游標直接是**實際日期 + 停留天數序號**，打開檔案就能看懂
- 若 `version` 不是 `7`，或搜尋條件（目的地、天數範圍…）改變導致
  `grid_signature` 不符，會自動重置成從今天開始，不需要手動處理

---

## 💡 之後可以延伸的方向
- 把 Price Drop 通知也接上 Telegram / Discord，多一個備援管道
- 依季節性（連假前後）動態調整搜尋頻率權重
- 增加更多目的地或改成多出發地（目前僅 TPE 單一出發地）
- 若配額調到很高、常常接近 180 分鐘 timeout，可考慮把單一 job 拆成多個平行 job（例如依目的地分組），縮短單次執行時間