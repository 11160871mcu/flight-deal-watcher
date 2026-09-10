#!/usr/bin/env python3
"""
flight_monitor.py
------------------
用免費、不需要金鑰的 fast-flights（爬 Google Flights 背後資料）
監測多條航線的來回機票價格。

這一版新增：
1. 每筆最低價紀錄現在會附上：航空公司、去程起飛/抵達時間、訂票連結
   （連到 Google Flights 對應搜尋結果，方便直接點進去訂票）。
2. 機場代碼旁邊會顯示中英文全名（見 AIRPORTS 對照表）。
3. 「比近期平均低 X%」：拿這條航線最近 N 天（config.yaml 的
   recent_average_days，預設 60 天）查到的價格算平均，跟這次最低價比較。
4. 網站排行榜的排序改成「Cheap Score 最高排前面」，並額外算出
   「今日最推薦」（cheap score 最高的那條航線）。
5. 「便不便宜」還是用 Cheap Score（贏過過去幾成的價格），不是固定金額門檻。
6. 另外保留一個獨立的「歷史最低價」警報，只要打破該航線有史以來最低價
   就一定通知。

需要的環境變數：
  NTFY_TOPIC   （可選，沒設就不發通知，只更新網站資料）
"""

import os
import csv
import json
import statistics
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import yaml
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "docs", "data")
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

HISTORY_FIELDS = [
    "checked_at",
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "stay_days",
    "price",
    "price_raw",
    "google_price_level",
    "airline",
    "dep_time",
    "arr_time",
    "booking_link",
]

# 機場代碼 -> (中文名, 英文名)。要加新機場的話在這裡補一筆就好。
AIRPORTS = {
    "TPE": ("台北桃園", "Taipei Taoyuan"),
    "NRT": ("東京成田", "Tokyo Narita"),
    "HND": ("東京羽田", "Tokyo Haneda"),
    "KIX": ("大阪關西", "Osaka Kansai"),
    "FUK": ("福岡", "Fukuoka"),
    "CTS": ("札幌新千歲", "Sapporo New Chitose"),
    "OKA": ("沖繩那霸", "Okinawa Naha"),
    "ICN": ("首爾仁川", "Seoul Incheon"),
    "PUS": ("釜山金海", "Busan Gimhae"),
}


def airport_label(code):
    """回傳「中文名 英文名 (代碼)」的顯示字串，沒登記過的代碼就只顯示代碼本身"""
    zh, en = AIRPORTS.get(code, (None, None))
    if zh and en:
        return {"code": code, "zh": zh, "en": en}
    return {"code": code, "zh": code, "en": code}


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


def duration_range(cfg):
    """把 stay_duration 的 min/max/step 展開成停留天數清單，確保 max_days 一定被涵蓋到"""
    stay = cfg.get("stay_duration", {})
    min_days = stay.get("min_days", 5)
    max_days = stay.get("max_days", 15)
    step = stay.get("step_days", 2)
    durations = list(range(min_days, max_days + 1, step))
    if not durations or durations[-1] != max_days:
        durations.append(max_days)
    return durations


def build_combo_grid(cfg):
    """把 origins x destinations x 候選出發日 x 停留天數，展開成完整清單"""
    dep_dates = daterange_step(
        cfg["date_window"]["start_date"],
        cfg["date_window"]["end_date"],
        cfg.get("date_step_days", 7),
    )
    durations = duration_range(cfg)
    combos = []
    for origin in cfg["origins"]:
        for destination in cfg["destinations"]:
            for dep in dep_dates:
                for duration in durations:
                    ret = dep + timedelta(days=duration)
                    combos.append(
                        {
                            "origin": origin,
                            "destination": destination,
                            "departure_date": dep.isoformat(),
                            "return_date": ret.isoformat(),
                            "stay_days": duration,
                        }
                    )
    return combos


def load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_index": 0, "last_notified": {}}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


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
    """把 fast-flights 回傳的價格字串（例如 'NT$12,345'）轉成數字，同時保留原始字串"""
    if raw is None:
        return None, None
    digits = "".join(ch for ch in str(raw) if ch.isdigit() or ch == ".")
    try:
        return float(digits), str(raw)
    except ValueError:
        return None, str(raw)


def is_nonstop(flight):
    """判斷一筆航班是不是直飛。fast-flights 的欄位在不同版本可能叫法不同，
    這裡盡量涵蓋常見的表示方式；如果套件版本改了欄位名稱，這裡可能要跟著調整
    （可以先印出 flight.__dict__ 確認實際欄位）。"""
    stops = getattr(flight, "stops", None)
    if stops is None:
        return True  # 沒有欄位可判斷時，保守放行，避免整批都被濾掉
    if isinstance(stops, (int, float)):
        return stops == 0
    s = str(stops).strip().lower()
    return s in ("0", "nonstop", "non-stop", "direct", "0 stops", "0 stop")


def build_booking_link(origin, destination, departure_date, return_date):
    """組一個 Google Flights 搜尋連結，讓使用者可以直接點進去訂票。
    fast-flights 本身不會回傳深連結，所以用自然語言查詢字串讓 Google
    自己解析成對應的來回機票搜尋結果（不保證每次都精準命中同一班機，
    但會落在同一組出發地/目的地/日期的搜尋結果頁）。"""
    query = (
        f"Flights from {origin} to {destination} on {departure_date} "
        f"through {return_date}"
    )
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(query)


def query_one_combo_details(combo, cfg):
    """對單一「出發日+回程日」組合，實際打去 Google Flights，
    回傳這組裡面（符合直飛條件的話）最便宜的那筆航班的完整細節。"""
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

    direct_only = cfg.get("direct_flights_only", True)

    best_price = None
    best_raw = None
    best_flight = None
    any_flight_seen = False
    for flight in result.flights:
        any_flight_seen = True
        if direct_only and not is_nonstop(flight):
            continue
        price_num, price_raw = parse_price(getattr(flight, "price", None))
        if price_num is None:
            continue
        if best_price is None or price_num < best_price:
            best_price = price_num
            best_raw = price_raw
            best_flight = flight

    google_price_level = getattr(result, "current_price", None)  # low/typical/high

    # 如果有查到航班，但全部都被「只要直飛」濾掉了，回傳 None 讓上層知道
    # 這組是「有資料但不符合直飛條件」，跟「完全查不到資料」分開印訊息比較好debug
    filtered_out_by_direct = any_flight_seen and best_price is None and direct_only

    # fast-flights 回傳的欄位主要對應「去程」那一段的顯示資訊（Google Flights
    # 列表本身就是這樣呈現，回程的詳細時間要點進去才會有），這裡盡量抓能拿到的：
    airline = getattr(best_flight, "name", None) if best_flight else None
    dep_time = getattr(best_flight, "departure", None) if best_flight else None
    arr_time = getattr(best_flight, "arrival", None) if best_flight else None

    booking_link = build_booking_link(
        combo["origin"], combo["destination"], combo["departure_date"], combo["return_date"]
    )

    return {
        "price": best_price,
        "price_raw": best_raw,
        "google_price_level": google_price_level,
        "filtered_out_by_direct": filtered_out_by_direct,
        "airline": airline,
        "dep_time": dep_time,
        "arr_time": arr_time,
        "booking_link": booking_link,
    }


def append_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def load_history_rows():
    """讀出目前為止累積的全部歷史資料（每一列都是 dict），沒有資料就回傳空清單。
    同時把 price 轉成 float、checked_at 轉成 datetime，方便後面計算用。"""
    rows = []
    if not os.path.exists(HISTORY_CSV):
        return rows
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["price"] = float(row["price"])
            except (ValueError, KeyError, TypeError):
                continue
            try:
                row["checked_at_dt"] = datetime.fromisoformat(row["checked_at"])
            except (ValueError, KeyError, TypeError):
                row["checked_at_dt"] = None
            rows.append(row)
    return rows


def group_rows_by_route(rows):
    by_route = {}
    for row in rows:
        key = (row["origin"], row["destination"])
        by_route.setdefault(key, []).append(row)
    return by_route


def cheap_score(price, past_prices):
    """Cheap Score = 這次價格贏過過去查到的價格中的幾成（0~100）。
    past_prices 不包含這次剛查到的價格。樣本太少就回傳 None（不夠可信）。"""
    if not past_prices:
        return None
    n = len(past_prices)
    more_expensive = sum(1 for p in past_prices if p > price)
    return round(100 * more_expensive / n)


def recent_average_price(route_rows, days, now_dt):
    """算這條航線「最近 N 天查到的價格」的平均值，當作『近期平均價』的基準。
    如果最近 N 天內樣本太少（例如剛上線沒幾天），就退而使用全部歷史當基準。"""
    cutoff = now_dt - timedelta(days=days)
    recent_prices = [
        r["price"] for r in route_rows
        if r.get("checked_at_dt") is not None and r["checked_at_dt"] >= cutoff
    ]
    if len(recent_prices) < 3:
        recent_prices = [r["price"] for r in route_rows]
    if not recent_prices:
        return None
    return statistics.mean(recent_prices)


def summarize_history(cfg, now_dt):
    """算出每條航線目前已知最便宜的組合，附上：航空公司/時間/訂票連結、
    Cheap Score、比近期平均低多少 %，給網站排行榜跟「今日最推薦」用。
    排序改成 Cheap Score 高的排前面（同分再比價格），比純粹「價格最低排前面」
    更符合「相對特價」的邏輯。"""
    rows = load_history_rows()
    if not rows:
        return []

    by_route = group_rows_by_route(rows)
    recent_average_days = cfg.get("recent_average_days", 60)

    routes = []
    for (origin, destination), route_rows in by_route.items():
        best_row = min(route_rows, key=lambda r: r["price"])
        all_prices = [r["price"] for r in route_rows]
        other_prices = list(all_prices)
        other_prices.remove(best_row["price"])
        score = cheap_score(best_row["price"], other_prices)

        avg = recent_average_price(route_rows, recent_average_days, now_dt)
        discount_pct = None
        if avg and avg > 0:
            discount_pct = round((avg - best_row["price"]) / avg * 100)

        routes.append(
            {
                "origin": origin,
                "destination": destination,
                "origin_airport": airport_label(origin),
                "destination_airport": airport_label(destination),
                "best_price": best_row["price"],
                "best_price_raw": best_row.get("price_raw", ""),
                "best_departure_date": best_row["departure_date"],
                "best_return_date": best_row["return_date"],
                "best_stay_days": best_row.get("stay_days", ""),
                "airline": best_row.get("airline") or None,
                "dep_time": best_row.get("dep_time") or None,
                "arr_time": best_row.get("arr_time") or None,
                "booking_link": best_row.get("booking_link") or None,
                "cheap_score": score,
                "recent_average_price": round(avg) if avg else None,
                "discount_pct_recent": discount_pct,
                "sample_size": len(route_rows),
            }
        )

    # Cheap Score 高的排前面；沒有分數（樣本太少）的排最後；同分再比價格便宜的排前面
    routes.sort(
        key=lambda r: (
            r["cheap_score"] is None,
            -(r["cheap_score"] or 0),
            r["best_price"],
        )
    )
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
    state.setdefault("last_notified", {})
    batch = pick_batch(combos, state, cfg.get("max_checks_per_run", 8))

    print(f"這一批共 {len(batch)} 組（全部候選組合共 {len(combos)} 組）")

    # 這次查詢開始「之前」的歷史資料，當作 Cheap Score / 近期平均的基準
    prior_rows = load_history_rows()
    prior_by_route = group_rows_by_route(prior_rows)

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    today_str = now_dt.date().isoformat()

    history_rows = []
    notifications = []
    notify_cfg = cfg.get("notify", {})
    min_samples = notify_cfg.get("min_history_samples", 8)
    instant_threshold = notify_cfg.get("cheap_score_instant", 90)
    throttled_threshold = notify_cfg.get("cheap_score_throttled", 80)

    for combo in batch:
        label = (
            f"{combo['origin']}->{combo['destination']} "
            f"{combo['departure_date']}~{combo['return_date']}（{combo['stay_days']}天）"
        )
        try:
            details = query_one_combo_details(combo, cfg)
        except Exception as e:  # 爬蟲本質上不穩定，單一組合失敗不該中斷整批
            print(f"  {label} 查詢失敗：{e}")
            continue

        price = details["price"]
        if price is None:
            reason = "沒有直飛航班" if details["filtered_out_by_direct"] else "查無報價"
            print(f"  {label} {reason}，略過。")
            continue

        route_key = (combo["origin"], combo["destination"])
        past_prices = [r["price"] for r in prior_by_route.get(route_key, [])]
        score = cheap_score(price, past_prices)
        historical_min = min(past_prices) if past_prices else None

        score_label = f"score={score}" if score is not None else f"score=N/A（樣本僅{len(past_prices)}筆）"
        print(f"  {label} -> {details['price_raw']} ({score_label}, google={details['google_price_level']})")

        history_rows.append(
            {
                "checked_at": now,
                "origin": combo["origin"],
                "destination": combo["destination"],
                "departure_date": combo["departure_date"],
                "return_date": combo["return_date"],
                "stay_days": combo["stay_days"],
                "price": price,
                "price_raw": details["price_raw"],
                "google_price_level": details["google_price_level"],
                "airline": details["airline"],
                "dep_time": details["dep_time"],
                "arr_time": details["arr_time"],
                "booking_link": details["booking_link"],
            }
        )
        # 讓同一次執行裡後面抽到的同航線組合，也能看到這一筆剛查到的價格
        prior_by_route.setdefault(route_key, []).append({"price": price})

        reasons = []
        is_historical_low = historical_min is not None and price < historical_min
        if notify_cfg.get("historical_low_alert", True) and is_historical_low:
            reasons.append(
                f"打破歷史最低價（原本 {historical_min:.0f}，現在 {price:.0f}）"
            )

        if score is not None and len(past_prices) >= min_samples:
            route_key_str = f"{combo['origin']}-{combo['destination']}"
            if score >= instant_threshold:
                reasons.append(f"Cheap Score {score}（比過去 {score}% 的價格都便宜）")
            elif score >= throttled_threshold:
                last_date = state["last_notified"].get(route_key_str)
                if last_date != today_str:
                    reasons.append(f"Cheap Score {score}（比過去 {score}% 的價格都便宜）")
                    state["last_notified"][route_key_str] = today_str
                else:
                    print(f"    （score={score} 達門檻，但今天這條航線已經通知過一次，略過）")

        if reasons:
            notifications.append({**combo, "price_raw": details["price_raw"], "score": score, "reasons": reasons})

    save_state(state)

    if history_rows:
        append_history(history_rows)

    routes_summary = summarize_history(cfg, now_dt)
    top_pick = routes_summary[0] if routes_summary else None

    latest_payload = {
        "generated_at": now,
        "checked_this_run": len(history_rows),
        "total_combos_in_grid": len(combos),
        "recent_average_days": cfg.get("recent_average_days", 60),
        "top_pick": top_pick,
        "routes": routes_summary,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, ensure_ascii=False, indent=2)

    ntfy_topic = os.environ.get("NTFY_TOPIC") or notify_cfg.get("ntfy_topic")
    for n in notifications:
        title = f"✈️ {n['origin']}→{n['destination']} 特價 {n['price_raw']}"
        message = (
            f"去程 {n['departure_date']}／回程 {n['return_date']}（{n['stay_days']}天）\n"
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
