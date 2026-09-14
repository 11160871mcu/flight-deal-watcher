# ✈️ TPE 機票優惠雷達（Flight Deal Watcher）

全自動的機票比價系統，從台灣桃園機場（TPE）出發，24 小時不間斷追蹤日韓
8 條航線未來一年內的直飛來回票價，自動整理成網頁，並在偵測到「突然降價」
時主動推播到手機。**完全免費、不需要 API 金鑰、不需要信用卡。**

線上網頁：`index.html`（由 Vercel 部署，讀取 `docs/data/latest.json`）

---

## 📍 涵蓋範圍

| 項目 | 內容 |
|---|---|
| 出發地 | TPE 台北桃園 |
| 目的地 | NRT 東京成田、HND 東京羽田、KIX 大阪關西、FUK 福岡、CTS 札幌新千歲、OKA 沖繩那霸、ICN 首爾仁川、PUS 釜山金海（共 8 條航線） |
| 艙等 / 人數 | 經濟艙、1 大人 |
| 航班限制 | 僅直飛 |
| 出發日期窗口 | 每天滾動更新，從「今天」往後 **12 個月** |
| 停留天數 | 來回停留 **5～15 天**，每天一格 |
| 幣別 | 新台幣（TWD）來回總價 |

---

## 🔍 搜尋策略：雙軌制（Dual-Track）

「未來一年 × 8 條航線 × 5～15 天停留」全部組合起來高達 3 萬多組，不可能
每次都掃過一遍，所以系統設計成兩條軌道同時、交錯進行：

### 1. 近期軌（near track）— 高頻更新
- 範圍：今天起 **90 天內**出發的日期
- 每次執行查 **48 組**（8 條航線平均分配）
- 目的：這段時間票價波動最大，最容易撿到快閃降價，查得密。

### 2. 全年軌（annual track）— 廣度覆蓋
- 範圍：今天起 **12 個月內**出發的日期
- 每次執行查 **96 組**（8 條航線平均分配）
- 目的：確保半年、十個月後的票價也會被慢慢輪過一遍，不漏掉遠期特價。

兩軌交錯進行，並用 `state.json` 記錄每條航線目前查到的游標位置，下次
接著上次的地方繼續查，長期下來每個日期組合都會被輪到，且用公平配額
機制不讓某條航線一直被優先查、其他被冷落。

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

## 🔔 通知：ntfy.sh 推播

偵測到 Price Drop 時，系統會透過 **ntfy.sh** 主動推播到你手機，不用自己
盯著網站看。ntfy 是完全免費的推播服務，不用註冊帳號、不用申請機器人，
是三個常見選項（ntfy / Telegram / Discord）裡設定最快的。

- 每輪最多列出 `notify_max_items`（預設 10 筆）降價最多的組合，避免通知太長
- 沒有任何降價時**不會發通知**，不會每輪都吵你
- 通知內容範例：

  ```
  ✈️ 偵測到 3 個航班突然降價
  TPE→PUS 2026-09-16 ~ 2026-09-30｜NT$3,730 ↓45.3%，原 NT$6,819
  TPE→ICN 2026-09-15 ~ 2026-09-30｜NT$4,498 ↓38.5%，原 NT$7,311
  ...等其餘 1 筆
  ```

- 若有設定 `site_url`，點通知會直接打開你的優惠網站

### 設定步驟

1. **手機安裝 ntfy App**（iOS App Store / Google Play 搜尋 "ntfy"），
   或直接用網頁版 <https://ntfy.sh/app>
2. **想一個獨特的頻道名稱**。ntfy 公開伺服器上的頻道是公開的，只要知道
   名字任何人都能訂閱或發送，所以**不要取成 `flight-alert` 這種一看就懂
   的名字**，建議混一些隨機字元，例如 `tw-tpe-flight-a8x92k7f`
3. App 裡點 **"+" → Subscribe to topic**，貼上剛剛想的名稱，訂閱起來
4. 到你的 GitHub repo → **Settings → Secrets and variables → Actions →
   New repository secret**：
   - Name：`NTFY_TOPIC`
   - Value：剛剛想的那組頻道名稱
5. （可選）在 `config.yaml` 把 `site_url` 填成你的 GitHub Pages /
   Vercel 網址，通知點下去就會直接開啟優惠列表
6. 到 repo 的 **Actions** 分頁 → 選 workflow → **Run workflow** 手動觸發
   一次，測試看看能不能收到推播
   - 如果這次剛好沒有任何降價，就不會收到通知，這是設計上刻意的
   - 想確認推播管線真的有接通，可以先把 `price_drop_alert_percent`
     臨時調低（例如 `1`），故意讓它更容易觸發，測完記得改回 `20`

> ntfy 免費公開伺服器沒有加密、也不保證送達，拿來收機票降價通知完全沒問題，
> 但不要拿它傳真正機密的東西。

---

## ⏰ 自動化排程

透過 **GitHub Actions**（`.github/workflows/check-flights.yml`）排程，
完全不需要手動觸發：

```yaml
schedule:
  - cron: "0 */3 * * *"   # 每 3 小時整點，一天 8 輪
```

換算成台灣時間：**08 / 11 / 14 / 17 / 20 / 23 / 02 / 05 點**，每天固定跑
8 輪。也支援手動觸發（`workflow_dispatch`），方便測試或想馬上刷新資料時
使用。

### 執行流程
1. Checkout 專案原始碼
2. 安裝 Python 3.11 及 `requirements.txt` 相依套件（含 `fast-flights`
   爬取 Google Flights、`requests` 發送 ntfy 推播）
3. 執行 `scripts/flight_monitor.py`（`NTFY_TOPIC` 從 GitHub Secrets 帶入）：
   - 依雙軌策略挑出本輪要查的日期組合
   - 逐一查詢並寫入結果
   - 重新計算 Cheap Score、比對 Price Drop
   - 輸出最新的 `latest.json`、更新查詢游標 `state.json`
   - 有偵測到降價就發送 ntfy 推播
4. 若資料有變動，自動 `git commit`（訊息含 `[skip ci]`）並 push 回 `main`
5. Push 前先 `git pull --rebase`，降低跟手動修改衝突的機率
6. `concurrency` 群組鎖避免兩次排程重疊時同時寫入資料檔

> Job 逾時上限 180 分鐘。由於單次查詢量已經配合排程頻率砍半
> （近期軌 48 組 / 全年軌 96 組，維持每天總量不變），180 分鐘緩衝相當充足。

---

## 🗂️ 專案結構

```
.
├── .github/workflows/check-flights.yml   # 排程設定 + NTFY_TOPIC 環境變數
├── scripts/flight_monitor.py             # 爬蟲 + 雙軌搜尋 + Cheap Score + Price Drop + ntfy 推播
├── config.yaml                           # 所有搜尋 / 通知參數設定
├── docs/
│   ├── index.html                        # 前端網頁（篩選 / 排序 / 降價徽章）
│   └── data/
│       ├── latest.json                   # 最新一批優惠（含 Score、降價標記）
│       ├── history.csv                   # 所有歷史查詢紀錄
│       └── state.json                    # 雙軌搜尋游標 + 累計嘗試次數
├── requirements.txt
└── vercel.json
```

## 🖥️ 前端功能（`index.html`）
- 目的地 / 航空公司（想要／排除）多選篩選
- 出發日期範圍、停留天數、最高價格、最低 Cheap Score 篩選
- 排序：**剛降價優先**（預設）、相對便宜優先、價格高低、折扣幅度、出發日期、停留天數
- 剛降價的卡片有紅色閃爍徽章，標示跌幅與原價
- 「各出發日期最低票價」快速下拉選單
- 手機版響應式排版

---

## ⚙️ `config.yaml` 主要設定說明

```yaml
# 雙軌搜尋額度（每次執行）
near_term_days: 90
near_checks_per_run: 48
annual_checks_per_run: 96

# Price Drop：跌幅達這個 % 才標記為突然降價
price_drop_alert_percent: 20

# ntfy 推播行為（頻道名稱本身是機密，走 GitHub Secrets 不寫在這裡）
notify_max_items: 10
site_url: ""

# Cheap Score
history_days: 60
min_score_samples: 20
```

---

## 💡 之後可以延伸的方向
- 把 Price Drop 通知也接上 Telegram / Discord，多一個備援管道
- 依季節性（連假前後）動態調整搜尋頻率權重
- 增加更多目的地或改成多出發地（目前僅 TPE 單一出發地）
