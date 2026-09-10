# 機票優惠搜尋器 Flight Deal Watcher（Adaptive Price Drop Detection Edition）

自動監測從台北桃園（TPE）出發、前往日本與韓國主要城市的**直飛來回機票**價格，
把結果整理成一個 GitHub Pages 網站，並在偵測到「突然降價」或「相對特價」時
發出通知。**完全免費、不需要 API 金鑰、不需要信用卡。**

跟第一版最大的不同：不再「平均分配」查詢資源去掃整個日期區間，而是把大部分
查詢額度優先投入「最可能突然變便宜」的位置，盡量不要錯過真正的低價機會。

## 這一版想解決的問題

你想去的地方不只一個，也沒有一定要哪天出發、待幾天——你要的是「不管去哪、
不管哪天，只要出現特別便宜的價格就告訴我」。但如果每次都掃過全部日期，
組合數量太龐大（8 個目的地 × 一整年的出發日 × 多種停留天數，動輒上萬組），
免費爬蟲頻率再快也掃不完，而且容易被 Google 暫時限流。

所以做法是：**不設定「我要哪天走」，而是設定「系統要怎麼分配注意力」**，
讓程式自己決定要優先查哪些日期，並且用「歷史價格」而不是「固定金額」來判斷
現在算不算便宜——這樣才抓得到你設的條件以外、但其實更便宜的時間點。

## 三層自適應搜尋機制

每次執行，系統把查詢額度分成三層，依序查詢：

### Layer 1：Urgent（高優先監控）
- 範圍：今天 ～ 未來 30 天
- 涵蓋這段期間**全部**停留天數（5～15 天）
- 查詢額度最高（`urgent_checks_per_run`）
- 原因：近期票價變化最快，促銷、臨時釋出票價、航空公司甩尾艙都容易出現在
  這個區間，所以優先集中火力在這裡。

### Layer 2：Hot（熱門價格監控）
- 範圍：今天 ～ 未來 180 天
- 只查常見的停留天數（5 / 7 / 10 / 14 天），不展開全部組合
- 查詢額度次高（`hot_checks_per_run`）
- 用來捕捉中短期的季節性、淡季優惠。

### Layer 3：Explore（全年探索）
- 範圍：未來 12 個月
- 查詢額度最低，但持續在背景累積（`explore_checks_per_run`）
- 用來避免完全錯過長期才會浮現的優惠，例如明年的櫻花季、暑假、連假機票。

三層各自用獨立的「游標（cursor）」記錄查到哪裡了，存在 `state.json` 裡，
每次執行接著上次的位置繼續查，掃過一輪之後自動從頭再來，不會一直重複查
同樣的日期，也不會有哪一段被永遠忽略。

## 怎麼判斷「現在算不算便宜」

系統不是用固定金額門檻（因為北海道跟福岡平常的票價本來就不一樣），而是用
兩種方式合併判斷：

1. **Cheap Score**：把這條航線近期（預設 60 天內）查到的所有價格當基準，
   算出這次價格贏過其中幾成。例如過去 100 筆裡有 92 筆比這次貴，
   Cheap Score 就是 92——代表這是這條航線近期相對排在前段的便宜價格。
   樣本數不足（預設低於 20 筆）時不計算 Score，避免太早期的資料誤判。
2. **Price Drop 偵測**：拿「同一組出發日/回程日組合」這次查到的價格，
   跟上一次查到的價格比較，如果跌幅超過設定值（預設 20%），標記為
   `PRICE_DROP`，代表這不是「本來就便宜」，而是「剛剛才變便宜」。

兩者互補：Cheap Score 抓「相對特價」，Price Drop 抓「突然變動」，
不管你原本設的日期範圍多寬，這兩種訊號都是拿真實累積的歷史資料去比較，
不會因為某個目的地平常就比較貴而被固定門檻濾掉。

## 查詢資料來源

沿用 [`fast-flights`](https://github.com/AWeirdDev/flights)，把 Google
Flights 網頁背後的資料格式還原出來直接查詢，不用開瀏覽器、不用註冊、
不用金鑰。跟 Selenium 爬蟲或 Google 官方 API 都不一樣——這仍然是非官方、
逆向工程出來的方式，Google 隨時可能改版讓它壞掉，如果哪天突然查不到資料，
先去套件的 GitHub 看看是不是有更新。

查詢流程：出發機場 + 目的地 + 出發日期 + 回程日期 + 乘客 + 艙等 + 直飛限制
→ 送進 Google Flights 的資料格式 → 解析價格與航班資訊 → 寫入歷史資料庫。

## 專案結構

```
flight-deal-watcher/
├── config.yaml                  # 出發地/目的地、三層搜尋額度、通知門檻
├── requirements.txt
├── scripts/
│   └── flight_monitor.py        # 主程式：三層自適應搜尋、降價偵測、Cheap Score
├── docs/                         # GitHub Pages 網站
│   ├── index.html
│   └── data/
│       ├── latest.json          # 目前最新的排行結果（網站讀這個檔案）
│       ├── history.csv          # 每次查到的價格都累積在這裡，是 Score/降價判斷的基礎
│       └── state.json           # 三層搜尋各自的游標、上次價格記憶
└── .github/workflows/
    └── check-flights.yml        # 排程執行 + 自動部署網站
```

## 目前監測的航線

- 出發地：`TPE` 台北桃園
- 目的地：`NRT` 東京成田・`HND` 東京羽田・`KIX` 大阪關西・`FUK` 福岡・
  `CTS` 札幌新千歲・`OKA` 沖繩那霸・`ICN` 首爾仁川・`PUS` 釜山金海
- 固定條件：來回、僅直飛、經濟艙、成人 1 人
- 停留天數：5～15 天（`config.yaml` 可調整）

想改目的地或艙等、人數，都在 `config.yaml` 改完 push 上去即可，下次排程會套用。

## `config.yaml` 主要設定說明

```yaml
origins: [TPE]
destinations: [NRT, HND, KIX, FUK, CTS, OKA, ICN, PUS]

stay_duration:          # 停留天數範圍，程式會展開成清單
  min_days: 5
  max_days: 15
  step_days: 1

# 三層搜尋的範圍與每次查詢額度
urgent_days: 30
urgent_checks_per_run: 480     # 8 個目的地 → 每個目的地 60 組

hot_days: 180
hot_checks_per_run: 400        # 8 個目的地 → 每個目的地 50 組

explore_months_ahead: 12
explore_checks_per_run: 120    # 8 個目的地 → 每個目的地 15 組

price_drop_alert_percent: 20   # 同一組合價格跌幅達這個 % 才標記突降
history_days: 60               # Cheap Score 基準取近期幾天的歷史價格
min_score_samples: 20          # 樣本數不足就不計算 Score，避免太早誤判
```

**額度怎麼抓比較好？** `urgent + hot + explore` 加起來，就是這次排程
總共會查詢的組合數（以預設值來說是 480 + 400 + 120 = 1000 組）。
數字越大，掃得越快、越不容易漏掉短期突降，但也越容易觸發 Google 的
暫時限流，也代表單次執行時間更長。如果常常查詢失敗或 GitHub Actions
跑到超時，把三個 `*_checks_per_run` 調小,或是拉長 `.github/workflows/
check-flights.yml` 的排程間隔。

## 通知怎麼收到

打破近期沒查到過的低價（Cheap Score 高，或觸發 `PRICE_DROP`）時，
資料會寫進 `history.csv` 並反映在 `latest.json` 裡；如果有設定推播
（沿用第一版 ntfy.sh 的方式,見 `NTFY_TOPIC` secret),對應的組合會被
推到手機上,不需要你自己盯著網站看。

## 網站功能

網站只讀 `docs/data/latest.json`,不會即時重新查詢,所以開網頁不會
產生額外的爬蟲流量。頁面上可以依你當下想法篩選:

- 目的地(可複選)
- 航空公司(可複選)
- 價格上限
- 出發日期區間
- 停留天數區間
- Cheap Score 門檻(例如只看 80 分以上)

篩選只影響前端顯示,不會觸發新的查詢——真正決定「查什麼、多常查」的
還是 `config.yaml` 裡的三層搜尋設定。

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
3. 到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**,
   新增一個 `NTFY_TOPIC`,值就是剛剛取的名字
4. 沒設定這個 secret 也沒關係,程式一樣會正常查價、更新網站,只是不會推播通知

### 3. 開啟 GitHub Pages

到 repo → **Settings → Pages**,Source 選 **"GitHub Actions"**。
第一次 workflow 跑完後,網址會出現在 **Settings → Pages** 頁面上方。

### 4. 依你的需求調整 `config.yaml`

依上面的說明調整目的地、停留天數、三層搜尋額度、降價/Score 門檻,
改完 commit + push 就會套用到下一次排程。

### 5. 手動測試一次

repo 頁面 → **Actions** → 選 "Check flight prices" → **"Run workflow"**,
可以馬上手動觸發一次,不用等排程時間到。Actions 的執行紀錄裡會印出
每一組查到的價格與所屬的層級(urgent/hot/explore),方便確認設定沒問題。

## 之後的日常使用

你什麼都不用做。GitHub Actions 會照排程自動:

1. 依三層額度組出這次要查的批次(urgent → hot → explore)
2. 查詢、寫入 `history.csv`
3. 計算 Cheap Score、偵測突然降價
4. 更新 `latest.json`、部署網站
5. 出現特價或突降就推播通知

想改監測條件、想加目的地,都只要改 `config.yaml` 再 push 一次就好——
不需要自己決定「哪天出發最划算」,那是系統要幫你找的事。

## 老實說的取捨

- 這是非官方、逆向工程出來的查詢方式,Google 隨時可能改版讓它壞掉。
- 查太頻繁、太多次,IP(尤其 GitHub Actions 的機房 IP)有可能被暫時限流,
  所以額度不是設得越高越好,超時或失敗率變高時要往下調。
- 三層搜尋是「優先分配注意力」,不是「保證掃過所有組合」——explore 層
  額度低,涵蓋全年的速度本來就會比 urgent 層慢很多,這是刻意的取捨,
  用意是把資源留給更可能突然變便宜的近期日期。