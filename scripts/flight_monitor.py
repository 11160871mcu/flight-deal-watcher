#!/usr/bin/env python3
"""
flight_monitor.py
------------------
用免費、不需要金鑰的 fast-flights（爬 Google Flights 背後資料）
監測多條航線的來回機票價格。

因為爬蟲一次只能查「固定出發日 + 固定回程日」這一組，沒辦法像付費 API
那樣直接說「幫我查這整段日期最便宜的」，所以這支程式會：

1. 把 config.yaml 裡的日期區間 x 停留天數選項，展開成一大串候選組合
2. 每次執行只抽其中一小批去查（用 docs/data/state.json 記住抽到哪裡了，
   下次接著抽，避免一次查太多被 Google 暫時限流）
3. 把查到的價格寫進 docs/data/history.csv（永久累積）
4. 每次都用「目前累積到的全部歷史資料」重新算出每條航線目前已知最便宜的
   組合，更新 docs/data/latest.json（給網站顯示）
5. 如果符合任何通知條件，透過 ntfy.sh 推播

需要的環境變數：
  NTFY_TOPIC   （可選，沒設就不發通知，只更新網站資料）
"""

import os
import csv
import json
import time
from datetime import date, datetime, timedelta, timezone

import yaml
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "docs", "data")
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
STATE_JSON = os.path.join(DATA_DIR, "state.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def daterange_step(start_str, end_str, step_days):
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    d = start
    dates = []
    while d <= end:
        dates.append(d)
        d += timedelta(days=step_days)
    return dates


def build_combo_grid(cfg):
    """把 origins x destinations x 候選出發日 x 停留天數，展開成完整清單"""
    dep_dates = daterange_step(
        cfg["date_window"]["start_date"],
        cfg["date_window"]["end_date"],
        cfg.get("date_step_days", 7),
    )
    combos = []
    for origin in cfg["origins"]:
        for destination in cfg["destinations"]:
            for dep in dep_dates:
                for duration in cfg.get("duration_options", [7]):
                    ret = dep + timedelta(days=duration)
                    combos.append(
                        {
                            "origin": origin,
                            "destination": destination,
                            "departure_date": dep.isoformat(),
                            "return_date": ret.isoformat(),
                        }
                    )
    return combos


def load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_index": 0}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f)


def pick_batch(combos, state, batch_size):
    """輪替抽一批要查的組合，抽完整份清單後自動從頭再來一輪"""
    n = len(combos)
    if n == 0:
        return []
    start = state.get("next_index", 0) % n
    batch = []
    for i in range(batch_size):
        batch.append(combos[(start + i) % n])
    state["next_index"] = (start + batch_size) % n
    return batch


def parse_price(raw):
    """把 fast-flights 回傳的價格字串（例如 'NT$12,345' 或 '$123'）轉成數字，
    同時保留原始字串，因為實際顯示的貨幣是 Google 依伺服器判斷的，不完全可控。"""
    if raw is None:
        return None, None
    digits = "".join(ch for ch in str(raw) if ch.isdigit() or ch == ".")
    try:
        return float(digits), str(raw)
    except ValueError:
        return None, str(raw)


def query_one_combo(combo, cfg):
    """對單一「出發日+回程日」組合，實際打去 Google Flights"""
    from fast_flights import FlightData, Passengers, get_flights

    flight_data = [
        FlightData(
            date=combo["departure_date"],
            from_airport=combo["origin"],
            to_airport=combo["destination"],
        ),
        FlightData(
            date=combo["return_date"],
            from_airport=combo["destination"],
            to_airport=combo["origin"],
        ),
    ]
    result = get_flights(
        flight_data=flight_data,
        trip="round-trip",
        seat=cfg.get("seat", "economy"),
        passengers=Passengers(adults=cfg.get("adults", 1)),
        fetch_mode="fallback",
    )

    best_price = None
    best_raw = None
    for flight in result.flights:
        price_num, price_raw = parse_price(getattr(flight, "price", None))
        if price_num is None:
            continue
        if best_price is None or price_num < best_price:
            best_price = price_num
            best_raw = price_raw

    google_price_level = getattr(result, "current_price", None)  # low/typical/high
    return best_price, best_raw, google_price_level


def append_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "checked_at",
                "origin",
                "destination",
                "departure_date",
                "return_date",
                "price",
                "price_raw",
                "google_price_level",
            ],
        )
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def summarize_history():
    """讀取目前為止全部累積的歷史資料，算出每條航線目前已知最便宜的組合。
    這是給網站顯示用的，會隨著抽樣輪數增加越來越準。"""
    if not os.path.exists(HISTORY_CSV):
        return []

    best_by_route = {}
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                price = float(row["price"])
            except (ValueError, KeyError):
                continue
            key = (row["origin"], row["destination"])
            current = best_by_route.get(key)
            if current is None or price < current["best_price"]:
                best_by_route[key] = {
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "best_price": price,
                    "best_price_raw": row.get("price_raw", ""),
                    "best_departure_date": row["departure_date"],
                    "best_return_date": row["return_date"],
                }

    routes = list(best_by_route.values())
    routes.sort(key=lambda r: r["best_price"])
    return routes


def send_ntfy_notification(topic, title, message):
    if not topic or topic == "your-unique-flight-topic-name":
        print("尚未設定真正的 ntfy topic，略過通知。")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "high",
                "Tags": "airplane,money_with_wings",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"通知發送失敗（不影響資料更新）：{e}")


def main():
    cfg = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)

    combos = build_combo_grid(cfg)
    state = load_state()
    batch = pick_batch(combos, state, cfg.get("max_checks_per_run", 6))
    save_state(state)

    print(f"這一批共 {len(batch)} 組（全部候選組合共 {len(combos)} 組）")

    now = datetime.now(timezone.utc).isoformat()
    history_rows = []
    notifications = []
    notify_cfg = cfg.get("notify", {})

    for combo in batch:
        label = f"{combo['origin']}->{combo['destination']} {combo['departure_date']}~{combo['return_date']}"
        try:
            price, price_raw, google_level = query_one_combo(combo, cfg)
        except Exception as e:  # 爬蟲本質上不穩定，單一組合失敗不該中斷整批
            print(f"  {label} 查詢失敗：{e}")
            continue

        if price is None:
            print(f"  {label} 查無報價，略過。")
            continue

        print(f"  {label} -> {price_raw} (google_price_level={google_level})")

        history_rows.append(
            {
                "checked_at": now,
                "origin": combo["origin"],
                "destination": combo["destination"],
                "departure_date": combo["departure_date"],
                "return_date": combo["return_date"],
                "price": price,
                "price_raw": price_raw,
                "google_price_level": google_level,
            }
        )

        # 通知判斷 1：Google 自己標「偏低」
        reasons = []
        if notify_cfg.get("notify_on_google_price_low", True) and google_level == "low":
            reasons.append("Google Flights 標示這組日期目前價格偏低")

        # 通知判斷 2：比目前累積歷史最低價再便宜一定比例
        # （用這次查詢之前的歷史資料，所以先跑一次 summarize，之後才 append）
        past_best = None
        for r in summarize_history():
            if r["origin"] == combo["origin"] and r["destination"] == combo["destination"]:
                past_best = r["best_price"]
                break
        drop_percent = notify_cfg.get("price_drop_percent", 10)
        if past_best is not None and price <= past_best * (1 - drop_percent / 100):
            reasons.append(f"比目前已知最低價 {past_best:.0f} 再便宜 {drop_percent}% 以上")

        # 通知判斷 3：絕對金額門檻
        abs_limit = notify_cfg.get("absolute_price_thresholds", {}).get(
            combo["destination"]
        )
        if abs_limit is not None and price <= abs_limit:
            reasons.append(f"低於你設定的絕對門檻 {abs_limit}")

        if reasons:
            notifications.append({**combo, "price_raw": price_raw, "reasons": reasons})

    if history_rows:
        append_history(history_rows)

    routes_summary = summarize_history()
    latest_payload = {
        "generated_at": now,
        "checked_this_run": len(history_rows),
        "total_combos_in_grid": len(combos),
        "routes": routes_summary,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, ensure_ascii=False, indent=2)

    ntfy_topic = os.environ.get("NTFY_TOPIC") or notify_cfg.get("ntfy_topic")
    for n in notifications:
        title = f"✈️ {n['origin']}→{n['destination']} 特價 {n['price_raw']}"
        message = (
            f"去程 {n['departure_date']}／回程 {n['return_date']}\n"
            f"原因：{'；'.join(n['reasons'])}"
        )
        print(title)
        send_ntfy_notification(ntfy_topic, title, message)

    print(
        f"完成。這次查了 {len(history_rows)} 組，累積歷史共涵蓋 {len(routes_summary)} 條航線，"
        f"觸發 {len(notifications)} 筆通知。"
    )


if __name__ == "__main__":
    main()
