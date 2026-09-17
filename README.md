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

### 2. 全年軌（annual track）— 廣度覆蓋
- 範圍：今天起 **12 個月內**出發的日期
- 每次執行查 **288 組**（8 條航線平均分配，每個目的地 36 組）

> 📌 這兩個數字（`near_checks_per_run` / `annual_checks_per_run`）比
> 最初設計高，是因為實際觸發頻率不一定很密（久久才跑一輪），所以把
> 單次執行的搜尋量調高。上限受 GitHub Actions 的 `timeout-minutes: 180`
> 限制，如果實測發現常跑到接近上限，到 `config.yaml` 把這兩個數字
> 往下調即可，不需要動程式碼。

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

`state.json` 裡每條航線、每個軌道的游標，**直接存一個真正的日期**，
例如：

```json
"near_cursor_by_route": {
  "TPE|CTS": { "date": "2026-10-09", "stay_index": 3 }
}
```

規則只有三條：

1. **游標日期比今天舊** → 直接跳到今天（過去的日期沒有意義，跳過不扣分）
2. **游標日期超過搜尋視窗尾端**（今天+90天 / 今天+12個月）→ **繞回視窗
   開頭（今天）重新開始一輪**。視窗尾端每次執行都是用當下的今天重新
   算出來的，所以搜尋範圍永遠是動態的「未來一年」，不會被寫死
3. 其餘狀況：照順序往下一個「日期 × 停留天數」前進

沒有任何位移補償的數學公式，游標本身就是一個具體日期，一天一天往前
走、超過視窗就繞回去。就算某一輪完全沒跑到，游標停在原地，下次接著
跑，不會被懲罰。

**怎麼驗證有在往前走**：`state.json` 打開來直接看 `date` 欄位，或看
網頁最下方「📡 掃描進度」，兩者都是真正的日期，不用心算。

---

## 🔔 通知：ntfy.sh 推播

### 通知規則：要同時符合三個條件
1. **`price_drop`**：跟自己上一次比，跌幅有達到 `price_drop_alert_percent`
   （預設 20%）
2. **`cheap_score >= notify_min_cheap_score`**（`config.yaml`，預設
   `70`）：跟同航線歷史價格池比，這個價格本身也真的算便宜
3. **這組航班是這一輪真的重新查到的**（詳見下方「避免重複通知」）

三者都符合，才代表「這是一個剛剛才變便宜、現在真的划算、而且是這輪
新發現的」機票。

### 🔁 避免同一組合被重複通知
早期版本的通知邏輯是：每輪執行完，重新檢查 `latest.json` 裡目前所有
航班「誰被標記為降價」，符合條件就通知。問題是——**這個標記只要沒有
新資料進來覆蓋，就會一直維持原樣**，於是同一組航班明明只在某一輪真的
被重新查過、判定降價，之後好幾輪游標根本沒再去查它，它卻因為「舊的
降價判定還在」而被**一輪又一輪重複通知**。

**修正做法**：`flight_monitor.py` 現在會記住「這一輪實際上重新查到
資料的組合」（`fresh_keys`），通知只從這個集合裡篩選。同一組航班只有
在**真的被重新查到、而且當下判定為降價**的那一輪才會通知一次；之後
即使 `latest.json` 裡的降價標記還維持著（給網頁顯示用），也不會再被
誤觸發通知。網頁上的「🔻 剛降價」徽章顯示邏輯不受影響。

### 通知內容範例

```text
🔥 偵測到 2 個又降價又划算的機票

札幌新千歲(CTS) → 台北桃園(TPE)
9/14 ~ 9/25（停留 11 天）
現在 NT$9,654
原價 NT$18,458，降了 47.7%
便宜指數 89/100

首爾仁川(ICN) → 台北桃園(TPE)
9/22 ~ 10/1（停留 9 天）
現在 NT$4,097
原價 NT$7,540，降了 45.7%
便宜指數 100/100

...等其餘 1 筆划算的降價
```

- 沒有符合條件的組合時**不會發通知**
- 若有設定 `site_url`，點通知會直接打開你的優惠網站

### 設定步驟
1. 手機安裝 **ntfy App**（iOS / Android 搜尋 "ntfy"），或用網頁版
   <https://ntfy.sh/app>
2. 想一個獨特的頻道名稱（**不要**取成一看就懂的名字，混一點隨機字元）
3. App 裡 **"+" → Subscribe to topic**，貼上剛剛想的名稱
4. GitHub repo → **Settings → Secrets and variables → Actions →
   New repository secret**：`NTFY_TOPIC` = 剛剛那組頻道名稱
5. （可選）`config.yaml` 的 `site_url` 填上你的網址
6. 想測試可以先把 `notify_min_cheap_score` 調低（例如 `0`）、
   `price_drop_alert_percent` 調低（例如 `1`），測完記得改回來

---

## ⏰ 自動化排程：兩條獨立管道

### 管道 1：GitHub Actions 自己的排程

```yaml
schedule:
  - cron: "17 */3 * * *"   # 每 3 小時整點過 17 分，一天八輪
```

換算成台灣時間：**08:17 / 11:17 / 14:17 / 17:17 / 20:17 / 23:17 /
02:17 / 05:17**。也支援手動觸發（`workflow_dispatch`）。

### 管道 2：Vercel Cron → 呼叫 GitHub API → 觸發同一個 workflow

```text
Vercel Cron（排程系統）
   |
   |  定時打
   v
/api/trigger-scan（Vercel 上的一支很小的 API，不爬蟲）
   |
   |  呼叫
   v
GitHub REST API：POST .../actions/workflows/check-flights.yml/dispatches
   |
   |  觸發
   v
你原本就有的 workflow_dispatch
實際爬蟲工作 100% 還是在 GitHub Actions 上跑
```

`/api/trigger-scan.js` 完全不會去打 Google Flights，只是負責「按下
GitHub Actions 的開始按鈕」。兩條管道有機會前後腳觸發，但
`check-flights.yml` 裡的 `concurrency` 群組鎖會避免撞在一起時互相
干擾。

#### 設定步驟
1. GitHub → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens，建立一個只給這個 repo、Actions 權限為
   Read and write 的 token
2. Vercel 專案 → Settings → Environment Variables 新增：
   `GH_TRIGGER_TOKEN`、`GH_OWNER`、`GH_REPO`、`CRON_SECRET`
3. `vercel.json` 已設定好 `crons`，push 到 main 後 Vercel 會自動依排程
   打 `/api/trigger-scan`

> ⚠️ Vercel **Hobby（免費）方案的 Cron Jobs 目前限制每天最多觸發
> 一次**，實際效果是「每天多一次保底觸發」；要跟 GitHub Actions 一樣
> 一天 8 次需要升級 Pro 方案。

### 執行流程
1. Checkout 專案原始碼、安裝相依套件
2. 執行 `scripts/flight_monitor.py`：
   - 讀取每條航線目前的日期游標
   - 依雙軌策略、公平配額挑出本輪要查的日期組合
   - 逐一查詢並寫入結果
   - 重新計算 Cheap Score、比對 Price Drop、更新掃描進度
   - 輸出 `latest.json`、更新 `state.json`
   - **只針對這一輪真的重新查到、且符合降價／划算門檻的組合**發送 ntfy 推播
3. 若資料有變動，自動 `git commit`（訊息含 `[skip ci]`）並 push 回 `main`
4. Push 前先 `git pull --rebase`
5. `concurrency` 群組鎖避免兩條觸發管道同時寫入資料檔

---

## 🗂️ 專案結構

```text
.
├── api/
│   └── trigger-scan.js                   # Vercel Cron 用來觸發 GitHub Actions 的小 API
├── .github/workflows/check-flights.yml   # GitHub Actions 自己的排程
├── scripts/flight_monitor.py             # 爬蟲 + 雙軌搜尋(日期游標) + Cheap Score + Price Drop + ntfy 推播（含重複通知修正）
├── config.yaml                           # 所有搜尋 / 通知參數設定（含每輪查詢量）
├── docs/
│   ├── index.html                        # 前端網頁（篩選 / 排序 / 降價徽章 / 每日最低價定位捲動 / 掃描進度）
│   └── data/
│       ├── latest.json                   # 最新一批優惠（含 Score、降價標記、scan_progress）
│       ├── history.csv                   # 所有歷史查詢紀錄
│       └── state.json                    # 日期游標 + 累計嘗試次數（version 7）
├── requirements.txt
└── vercel.json                           # 部署設定 + crons 排程
```

## 🖥️ 前端功能（`index.html`）
- 目的地 / 航空公司（想要／排除）多選篩選
- 出發日期範圍、停留天數、最高價格、最低 Cheap Score 篩選
- 排序：剛降價優先（預設）、相對便宜優先、價格高低、折扣幅度、出發日期、停留天數
- 「各出發日期最低票價」快速下拉選單：選一個日期後自動放寬篩選、
  捲動並高亮到對應的優惠卡片
- 篩選面板**最上面（點標題列）跟最下面（收起篩選條件按鈕）都能收合**
- **📡 掃描進度**：直接顯示游標目前掃到哪一天
- 手機版響應式排版

---

## ⚙️ `config.yaml` 主要設定說明

```yaml
near_term_days: 90
near_checks_per_run: 144
annual_checks_per_run: 288

price_drop_alert_percent: 20
notify_min_cheap_score: 70   # 要「降價」且「Cheap Score 達這個分數以上」才會真的推播

notify_max_items: 10
site_url: ""

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

若 `version` 不是 `7`，或搜尋條件改變導致 `grid_signature` 不符，會
自動重置成從今天開始，不需要手動處理。

---

## 💡 之後可以延伸的方向
- 把 Price Drop 通知也接上 Telegram / Discord
- 依季節性（連假前後）動態調整搜尋頻率權重
- 增加更多目的地或改成多出發地（目前僅 TPE 單一出發地）
- 若配額調到很高、常常接近 180 分鐘 timeout，可考慮把單一 job 拆成
  多個平行 job（例如依目的地分組），縮短單次執行時間