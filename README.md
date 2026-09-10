# 機票優惠搜尋器 Flight Deal Watcher｜雙軌全年滾動版

這個專案用來自動監測從台北桃園機場（TPE）出發，前往日本與韓國 8 個目的地的直飛來回機票價格，並把結果整理到 GitHub Pages 網頁上。

目前系統的核心目標不是只找「今天看到的最低價」，而是持續累積價格歷史，判斷某一張票相對於同一路線近期價格是否真的便宜，並透過「近期高頻刷新 + 全年公平輪詢」雙軌機制，兼顧短期降價機會與未來一年的完整覆蓋。

---

## 1. 目前監測條件

出發地：

- TPE｜台北桃園

目的地：

- NRT｜東京成田
- HND｜東京羽田
- KIX｜大阪關西
- FUK｜福岡
- CTS｜札幌新千歲
- OKA｜沖繩那霸
- ICN｜首爾仁川
- PUS｜釜山金海

固定搜尋條件：

- 1 位成人
- 經濟艙
- 僅直飛
- 來回機票
- 幣別固定為 TWD
- 停留時間 5～15 天
- 停留天數逐日搜尋，亦即 5、6、7……15 天
- 搜尋日期永遠從「今天」開始往後 12 個月
- 日期區間每天自動向前滾動，不需要手動修改固定日期

---

## 2. 資料來源與查價方式

專案使用 `fast-flights==2.2` 查詢 Google Flights 背後的資料格式。

它不是傳統的 Selenium / Playwright 瀏覽器畫面爬蟲，也不是官方 Google Flights API。

`fast-flights` 會建立指定的：

「出發機場 + 目的地 + 出發日期 + 回程日期 + 乘客 + 艙等 + 直飛條件」

查詢，再取得 Google Flights 回傳的航班價格與可解析的航班資訊。

因此本系統無法只送出一句「幫我找未來一年最便宜的票」，而必須把未來一年的日期與停留天數展開成很多個實際查詢組合。

---

## 3. 為什麼不是每天把未來一年全部查完？

目前共有：

8 個目的地 × 約 366 個出發日期 × 11 種停留天數

也就是大約 32,000 個「目的地 × 出發日 × 停留天數」組合。

如果每天一次把 32,000 多組全部重新查詢，查詢量太大，容易遇到：

- Google / fast-flights 限流
- HTTP 錯誤
- 部分查詢無資料
- GitHub Actions 執行時間過長
- 多個 workflow 同時修改資料造成 Git 衝突

因此目前採用「雙軌搜尋」。

---

# 4. 雙軌搜尋機制

## 軌道 A：近期 90 天高頻刷新

近期機票最可能是實際準備購買的票，也最需要快速發現臨時降價。

因此每次執行會額外從「今天起未來 90 天」的日期組合中查一批。

目前設定：

- `near_term_days: 90`
- `near_checks_per_run: 96`

因為共有 8 個目的地，所以每輪近期軌原則上平均分配：

- NRT：12 組
- HND：12 組
- KIX：12 組
- FUK：12 組
- CTS：12 組
- OKA：12 組
- ICN：12 組
- PUS：12 組

合計 96 組。

近期軌不會每次永遠從第一天重新查，而是利用 `state.json` 記住每個目的地目前查到哪裡，下次繼續。

查完整個近期池後，再循環回頭重新刷新。

---

## 軌道 B：未來 12 個月全年公平輪詢

全年軌負責確保未來一整年的日期都有機會被搜尋。

目前設定：

- `search_months_ahead: 12`
- `annual_checks_per_run: 192`

8 個目的地平均分配後，每輪原則上：

- NRT：24 組
- HND：24 組
- KIX：24 組
- FUK：24 組
- CTS：24 組
- OKA：24 組
- ICN：24 組
- PUS：24 組

合計 192 組。

所謂「全年公平輪詢」不是每天把全年 32,000 多組全部搜尋一次。

它的意思是：

1. 未來 12 個月所有日期都建立在全年候選池中
2. 每次只搜尋候選池的一部分
3. 每個目的地都拿到公平的查詢配額
4. `state.json` 記住每個目的地搜尋到哪個位置
5. 下一輪從上一次位置繼續
6. 搜完一輪全年候選池後，再從頭開始刷新

因此不會發生東京一直被查，但札幌、沖繩長時間都沒被查到的情況。

---

## 5. 每輪實際搜尋量

目前每輪：

- 近期軌：96 組
- 全年軌：192 組
- 合計：288 組

也就是：

`96 + 192 = 288 組 / 每次執行`

如果 GitHub Actions 每天執行 4 次，理論查詢量約為：

`288 × 4 = 1,152 組 / 天`

其中：

- 近期 90 天會得到額外高頻更新
- 全年 12 個月仍然持續向前輪詢

全年軌每天約搜尋：

`192 × 4 = 768 組`

若全年總網格約 32,000 組，單純以理論值估算：

`32,000 ÷ 768 ≈ 42 天`

約 42 天可以把全年網格輪過一遍。

這不代表近期票 42 天才更新一次，因為近期 90 天還有獨立的高頻搜尋軌道。

---

## 6. 每天自動滾動一年

程式不是寫死例如：

2026-09-10 ～ 2027-09-10

而是每次執行時重新取得台灣日期。

例如：

今天執行：

`今天 → 今天 + 12 個月`

明天執行：

`明天 → 明天 + 12 個月`

因此搜尋窗口永遠維持未來一年。

昨天已經過期的出發日會逐步離開有效範圍，新出現的未來日期則會自動加入。

不需要每天修改 `config.yaml`。

---

# 7. 公平輪詢如何避免重複與偏差

每一條：

`TPE → destination`

都有自己的搜尋 cursor。

例如：

- TPE → NRT 有自己的進度
- TPE → HND 有自己的進度
- TPE → KIX 有自己的進度
- …
- TPE → PUS 有自己的進度

程式不再使用單一大清單直接切：

`tasks[start:start+batch]`

而是先替每個目的地分配 quota，再分別從每個目的地自己的 cursor 取資料。

此外，同一次執行中，近期軌與全年軌若碰到完全相同的：

`origin + destination + departure_date + return_date + stay_days`

會避免在同一輪重複搜尋。

---

# 8. 查詢失敗不會讓整輪停止

機票資料來源可能偶爾發生：

- 查詢沒有價格
- HTTP 錯誤
- Google 暫時限流
- 航空公司資訊沒有解析成功
- 起飛 / 抵達時間沒有解析成功

程式採取「單筆失敗，繼續下一筆」的方式。

不會因為其中一組失敗，就中止整個 288 組搜尋。

---

# 9. 同價航班處理

對同一組：

`目的地 + 出發日 + 回程日`

程式會先找到本次查詢真正的最低價格。

如果同一最低價格同時有多個不同航班，例如：

- 台灣虎航｜14:30 → 18:30｜NT$6,200
- 樂桃航空｜15:20 → 19:20｜NT$6,200
- 星宇航空｜16:00 → 20:05｜NT$6,200

網站會把這些同價選項全部保留，而不是只顯示其中一個。

可辨識的差異包含：

- 航空公司
- 起飛時間
- 抵達時間
- 飛行時間
- stops

如果 `fast-flights` 成功取得價格，但沒有解析出航空公司或時間，程式不會自行猜測或偽造航空公司資料。

---

# 10. 航空公司 metadata 重試

如果第一次查到價格，但沒有成功解析航空公司 / 時間資料，程式可以再查一次。

目前：

- `metadata_retry_count: 1`
- `metadata_retry_delay_seconds: 1.0`

目的只是提高 metadata 成功率。

即使重試後仍然沒有航空公司名稱，價格仍可保留。

---

# 11. TWD 價格防呆

網站所有價格都應該是新台幣。

因此程式要求查詢：

`currency="TWD"`

並且價格解析器不會單純把任何數字都當成 TWD。

例如：

`US$220`

不能被錯誤顯示成：

`NT$220`

程式只接受合理的 TWD 價格資料，並排除過低或無法確認幣別的價格。

---

# 12. history.csv 是整個系統最重要的歷史資料

每次成功取得價格後，都會新增到：

`docs/data/history.csv`

這個檔案不是單純存「目前最低價」。

它是長期價格歷史資料庫。

例如同一張日期組合可能先後被記錄：

- NT$8,200
- NT$7,600
- NT$6,900
- NT$7,300

這些資料可以用來判斷現在的 NT$6,900 是否真的相對便宜。

---

# 13. Cheap Score 怎麼算？

Cheap Score 不是單純用固定價格區間評分。

它是比較同一路線的近期價格池。

目前預設參考最近：

`history_days: 60`

假設東京近期價格池共有 100 筆，而某張票目前是 NT$6,500。

如果 92 筆價格都大於或等於 NT$6,500：

`Cheap Score = 92 / 100 × 100 = 92`

可以理解為：

這張票的價格比同一路線近期約 92% 的有效價格更便宜或相同。

因此：

- Score 越高，代表相對越便宜
- Score 不是不同目的地直接拿絕對票價比較
- 東京與札幌會分別依自己的價格分布評分

同時網頁還會顯示：

「較近期平均低 XX%」

計算概念：

`(近期平均價格 - 現價) / 近期平均價格 × 100%`

Cheap Score 是相對排名概念；

「低於平均 XX%」則是價格差距幅度。

---

# 14. Score 樣本數

價格資料剛開始累積時，Cheap Score 可能不夠穩定。

例如一條航線只有 3 筆價格，最低價很容易得到 Score 100。

因此目前設定：

`min_score_samples: 20`

網頁可以顯示 Score 使用的樣本數，方便判斷分數是否已有足夠歷史基礎。

---

# 15. 網頁篩選機制

網站讀取：

`docs/data/latest.json`

網頁的篩選只是在已經搜尋完成的資料中快速篩選，不會每按一次按鈕就向 Google Flights 發出幾萬次搜尋。

目前網頁需求：

### 目的地

可複選，預設 8 個目的地全部勾選。

提供：

- 全選
- 全部取消

### 想要的航空公司

可複選，預設所有目前資料中出現的航空公司全部勾選。

提供：

- 全選
- 全部取消

### 不要的航空公司

可複選，預設全部不勾。

提供：

- 全選
- 全部取消

### 航空公司互斥

同一家航空公司不能同時出現在：

- 想要
- 不要

例如目前：

`想要：☑ 中華航空`

之後在「不要」勾選中華航空：

`不要：☑ 中華航空`

系統會自動取消「想要」中的中華航空。

反過來亦相同。

### 其他篩選

還可以設定：

- 最少停留天數
- 最多停留天數
- 最高來回價格
- 最早出發日期
- 最晚出發日期
- 最低 Cheap Score

所有條件設定完成後，按：

`🔎 套用篩選條件`

才一次套用。

---

# 16. 結果排序

「結果排序」不放在篩選區，而放在：

`📊 符合條件的航班`

旁邊。

目前可選：

- 相對便宜優先
- 價格低 → 高
- 價格高 → 低
- 低於近期平均最多
- 最近出發優先
- 停留天數短 → 長

更改結果排序後，不需要再次按「套用篩選條件」。

它只會重新排列目前已符合條件的結果。

---

# 17. 航空公司中文名稱

網頁會使用中英文對照，例如：

- 中華航空 · China Airlines
- 長榮航空 · EVA Air
- 星宇航空 · STARLUX Airlines
- 台灣虎航 · Tigerair Taiwan
- 日本航空 · Japan Airlines
- 全日空 · All Nippon Airways
- 樂桃航空 · Peach Aviation
- 大韓航空 · Korean Air
- 韓亞航空 · Asiana Airlines
- 濟州航空 · Jeju Air
- 真航空 · Jin Air
- 德威航空 · T'way Air
- 釜山航空 · Air Busan
- 酷航 · Scoot
- 亞洲航空 · AirAsia
- 國泰航空 · Cathay Pacific

如果 Google / fast-flights 回傳一個目前對照表中沒有的英文名稱，網站會保留原始英文名稱，而不是自行猜中文翻譯。

---

# 18. 專案結構

```text
flight-deal-watcher/
│
├─ README.md
├─ config.yaml
├─ requirements.txt
│
├─ scripts/
│  └─ flight_monitor.py
│
├─ docs/
│  ├─ index.html
│  │
│  └─ data/
│     ├─ history.csv
│     ├─ latest.json
│     └─ state.json
│
└─ .github/
   └─ workflows/
      └─ check-flights.yml
```

---

# 19. 三個 data 檔案分別是什麼？

## `docs/data/history.csv`

用途：

保存每一次成功查詢到的歷史價格。

這是 Cheap Score、近期平均價格與後續價格分析的基礎。

### 不建議刪除。

正常升級程式、修改網頁、增加雙軌搜尋，都不需要刪除 `history.csv`。

只有以下情況才考慮清空：

1. 歷史資料確定受到錯誤幣別污染
2. 資料結構嚴重損壞
3. 明確想把所有歷史價格基準全部重新開始

只要目前歷史價格已經是正常 TWD，就應該保留。

---

## `docs/data/latest.json`

用途：

給 GitHub Pages 網頁讀取目前最新整理結果。

它是「產出檔」，不是核心歷史資料。

### 不需要刪除。

每次 `flight_monitor.py` 成功執行，都會重新產生 / 更新它。

如果手動刪除，下一次成功執行後可以重新建立，但在重新建立之前網頁可能沒有資料可顯示。

因此正常升級時直接保留即可。

---

## `docs/data/state.json`

用途：

保存：

- 近期軌每條航線目前搜尋到哪
- 全年軌每條航線目前搜尋到哪
- 輪詢 rotation
- 搜尋窗口起始日期
- 總查詢次數

雙軌新版會使用新的 state version。

如果舊版 `state.json` 與新版不相容，程式應該自動建立新的雙軌 state。

### 一般不需要手動刪除。

只有想「強制讓雙軌搜尋從第一個候選組合重新開始」時，才需要刪 `state.json`。

刪 `state.json` 不會刪除 `history.csv`。

---

# 20. 升級到雙軌版時，到底要不要刪資料？

正常情況：

```text
history.csv  → 不刪
latest.json  → 不刪
state.json   → 不刪
```

尤其 `history.csv` 不應該因為換新版程式就刪除。

推薦做法：

保留所有資料，讓新版程式自行更新 `latest.json`，並在 state version 不相容時自行重建輪詢狀態。

---

# 21. config.yaml 主要設定

核心設定示例：

```yaml
origins:
  - TPE

destinations:
  - NRT
  - HND
  - KIX
  - FUK
  - CTS
  - OKA
  - ICN
  - PUS

timezone: Asia/Taipei

search_months_ahead: 12

near_term_days: 90

near_checks_per_run: 96

annual_checks_per_run: 192

stay_duration:
  min_days: 5
  max_days: 15
  step_days: 1

adults: 1

seat: economy

direct_only: true

history_days: 60

min_score_samples: 20
```

---

# 22. GitHub Actions 自動執行

只保留一個機票查詢 workflow：

`.github/workflows/check-flights.yml`

不要同時再建立另一個會執行相同查詢的 `update-flights.yml`。

否則兩個 workflow 可能同時：

- 搜尋重複日期
- 寫入 history.csv
- 修改 latest.json
- 修改 state.json
- push main

容易產生資料重複與 Git 衝突。

目前建議每天執行 4 次。

如果 workflow 使用 UTC cron：

```yaml
schedule:
  - cron: "0 4,10,16,22 * * *"
```

大致對應台灣時間：

- 12:00
- 18:00
- 00:00
- 06:00

GitHub Actions 的排程可能不是精準到秒執行，因此實際啟動時間可能稍有延遲。

---

# 23. 本機執行前先做語法檢查

目前 Windows 環境：

```text
(myenv) C:\Users\emily\desktop\flight-deal-watcher>
```

先檢查 Python：

```cmd
python -m py_compile scripts\flight_monitor.py
```

如果沒有任何錯誤輸出，就代表 Python 語法通過。

再檢查 YAML：

```cmd
python -c "import yaml; c=yaml.safe_load(open('config.yaml',encoding='utf-8')); print(c['origins']); print(c['destinations']); print(c['near_checks_per_run'], c['annual_checks_per_run'])"
```

正常應顯示：

```text
['TPE']
['NRT', 'HND', 'KIX', 'FUK', 'CTS', 'OKA', 'ICN', 'PUS']
96 192
```

需要本機實際執行時：

```cmd
python scripts\flight_monitor.py
```

---

# 24. 雙軌正常啟動時應看到什麼？

開頭應類似：

```text
=== Flight Deal Watcher / 雙軌搜尋 ===
全年：今天 -> 未來12個月
近期：今天 -> 未來90天
停留：5～15 天
全年網格：約 32,000 組
本輪：近期 96 + 全年 192 = 288 組
```

並看到每個目的地：

```text
TPE -> NRT：近期 12 / 全年 24
TPE -> HND：近期 12 / 全年 24
TPE -> KIX：近期 12 / 全年 24
TPE -> FUK：近期 12 / 全年 24
TPE -> CTS：近期 12 / 全年 24
TPE -> OKA：近期 12 / 全年 24
TPE -> ICN：近期 12 / 全年 24
TPE -> PUS：近期 12 / 全年 24
```

---

# 25. requirements.txt

目前建議：

```text
fast-flights==2.2
PyYAML>=6.0
requests>=2.31.0
playwright
python-dateutil>=2.9.0
```

安裝：

```cmd
python -m pip install -r requirements.txt
```

---

# 26. 如何上傳到 GitHub

不要使用：

```cmd
git add .
```

因為本機可能還有不想提交的：

- `.vscode/`
- 意外建立的 `{` 檔案

只加入本次真正要修改的檔案。

如果這次修改：

- README.md
- config.yaml
- requirements.txt
- scripts/flight_monitor.py
- docs/index.html
- .github/workflows/check-flights.yml

執行：

```cmd
git status

git add README.md
git add config.yaml
git add requirements.txt
git add scripts/flight_monitor.py
git add docs/index.html
git add .github/workflows/check-flights.yml

git commit -m "Upgrade flight watcher to dual track rolling search"
```

因為 GitHub Actions 也可能同時更新遠端 main，commit 後先同步：

```cmd
git fetch origin
git rebase origin/main
```

如果成功，再：

```cmd
git push origin main
```

---

# 27. 如果 git rebase 說有 unstaged changes

先執行：

```cmd
git status
```

不要直接 force push。

如果只是本機測試後 `docs/data/history.csv`、`latest.json`、`state.json` 被修改，而這次只想提交程式碼，可以先把這些資料變更暫存起來，再完成 rebase。

不要使用：

```cmd
git push -f
```

除非非常清楚正在覆蓋什麼歷史。

---

# 28. 為什麼經常出現 `fetch first`？

因為 GitHub Actions 會自動執行查價，然後把：

- history.csv
- latest.json
- state.json

commit 回 GitHub 的 `main`。

同一時間，本機也可能正在修改：

- index.html
- flight_monitor.py
- config.yaml

所以遠端 main 有時候會在本機修改期間又多一個新的 commit。

因此最安全流程是：

```cmd
git add 指定檔案
git commit -m "你的訊息"
git fetch origin
git rebase origin/main
git push origin main
```

---

# 29. GitHub Pages

網站主要由：

`docs/index.html`

讀取：

`docs/data/latest.json`

如果 GitHub Pages 已經設定完成，程式與資料 push 後，網站會自動顯示更新後資料。

前端篩選本身不需要重新爬機票。

例如只修改：

- 航空公司篩選
- 目的地篩選
- 排序
- 字體
- 按鈕

只需要修改 `docs/index.html`。

真正的機票價格則由 `flight_monitor.py` + GitHub Actions 自動刷新。

---

# 30. 日常使用

正常設定完成後，不需要每天手動執行。

GitHub Actions 會自動：

1. 取得今天日期
2. 建立今天到未來 12 個月的滾動搜尋池
3. 建立今天到未來 90 天的高頻搜尋池
4. 為 8 個目的地公平分配近期軌查詢
5. 為 8 個目的地公平分配全年軌查詢
6. 查詢 5～15 天的來回日期組合
7. 保存成功取得的價格
8. 累積到 `history.csv`
9. 更新 `state.json`
10. 重新計算近期平均、Cheap Score
11. 產生 `latest.json`
12. 網頁讀取最新結果

使用者主要只需要打開網站，設定：

- 目的地
- 想要航空公司
- 不要航空公司
- 停留天數
- 最高價格
- 日期
- Cheap Score

再按：

`套用篩選條件`

最後在「符合條件的航班」旁邊選擇：

- 相對便宜優先
- 價格低 → 高
- 價格高 → 低
- 其他排序方式

即可。

---

# 31. 重要原則

### 不要因為更新程式就刪 `history.csv`

歷史資料越多，Cheap Score 與近期平均越有參考價值。

### `latest.json` 是輸出，不是歷史資料庫

可以重建，但正常情況不需要刪。

### `state.json` 是搜尋進度

正常也不需要刪；只有想重新開始輪詢時才刪。

### 不要同時保留兩個會查機票的 workflow

只保留：

`.github/workflows/check-flights.yml`

### 不要把 Google Flights 查詢塞到前端按鈕

前端「套用篩選條件」只篩選現有資料。

真正查價由 GitHub Actions 在背景自動執行。

### 不要偽造航空公司或航班時間

來源沒解析成功時就標示資料未提供。

---

## 最終系統邏輯

```text
每天自動排程
      ↓
今天 → 未來12個月
      ↓
┌─────────────────┬──────────────────┐
│ 近期90天高頻軌 │ 全年12個月輪詢軌 │
│ 每輪 96 組     │ 每輪 192 組      │
└────────┬────────┴─────────┬────────┘
         ↓                  ↓
       8目的地公平分配
                ↓
         停留 5～15 天
                ↓
          Google Flights
                ↓
       最低價 + 同價航班
                ↓
          history.csv
                ↓
  近期平均 / Cheap Score 計算
                ↓
          latest.json
                ↓
          GitHub Pages
                ↓
  目的地 / 航空公司 / 日期 /
  停留時間 / 價格 / Score 篩選
                ↓
          套用篩選條件
                ↓
       符合條件的航班
                ↓
       使用者自由選擇排序
```
