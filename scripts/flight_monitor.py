#!/usr/bin/env python3
import os
import csv
import json
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

AIRPORT_NAMES = {
    "TPE": "台北桃園", "NRT": "東京成田", "KIX": "大阪關西",
    "FUK": "福岡", "CTS": "札幌新千歲", "OKA": "沖繩那霸",
    "ICN": "首爾仁川", "PUS": "釜山金海",
}

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
    stay = cfg.get("stay_duration", {})
    min_days = stay.get("min_days", 5)
    max_days = stay.get("max_days", 15)
    step = stay.get("step_days", 2)
    durations = list(range(min_days, max_days + 1, step))
    if not durations or durations[-1] != max_days:
        durations.append(max_days)
    return durations

def build_combo_grid(cfg):
    dep_dates = daterange_step(cfg["date_window"]["start_date"], cfg["date_window"]["end_date"], cfg.get("date_step_days", 7))
    durations = duration_range(cfg)
    combos = []
    for origin in cfg["origins"]:
        for destination in cfg["destinations"]:
            for dep in dep_dates:
                for duration in durations:
                    ret = dep + timedelta(days=duration)
                    combos.append({
                        "origin": origin, "destination": destination,
                        "departure_date": dep.isoformat(), "return_date": ret.isoformat(),
                        "stay_days": duration,
                    })
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
    n = len(combos)
    if n == 0: return []
    start = state.get("next_index", 0) % n
    batch = [combos[(start + i) % n] for i in range(batch_size)]
    state["next_index"] = (start + batch_size) % n
    return batch

def parse_price(raw):
    if raw is None: return None, None
    digits = "".join(ch for ch in str(raw) if ch.isdigit() or ch == ".")
    try: return float(digits), str(raw)
    except ValueError: return None, str(raw)

def is_nonstop(flight):
    stops = getattr(flight, "stops", None)
    if stops is None: return True
    if isinstance(stops, (int, float)): return stops == 0
    return str(stops).strip().lower() in ("0", "nonstop", "non-stop", "direct", "0 stops", "0 stop")

def generate_google_flights_url(origin, dest, dep_date, ret_date):
    return f"https://www.google.com/travel/flights?hl=zh-TW&gl=TW#flt={origin}.{dest}.{dep_date}*{dest}.{origin}.{ret_date}"

def query_one_combo(combo, cfg):
    from fast_flights import get_flights
    try:
        result = get_flights(
            flight_data=[
                {"date": combo["departure_date"], "from": combo["origin"], "to": combo["destination"]},
                {"date": combo["return_date"], "from": combo["destination"], "to": combo["origin"]},
            ],
            trip="round-trip", seat=cfg.get("seat", "economy"), passengers={"adults": cfg.get("adults", 1)},
            currency="TWD", hl="zh-TW", gl="TW", fetch_mode="fallback",
        )
    except Exception as e:
        print(f"查詢失敗 {combo}: {e}")
        return None

    best_price, best_raw, best_airline = None, None, "依查詢結果為主"
    best_dep_time, best_arr_time = "", ""
    best_return_dep_time, best_return_arr_time = "", ""

    for flight in getattr(result, "flights", []):
        if cfg.get("direct_flights_only", True) and not is_nonstop(flight): continue
        price_num, price_raw = parse_price(getattr(flight, "price", ""))
        
        # 排除低於 1000 元的錯誤美金資料
        if price_num is None or price_num < 1000: continue
        if "NT" not in str(price_raw) and "TWD" not in str(price_raw): price_raw = f"NT${int(price_num):,}"
            
        if best_price is None or price_num < best_price:
            best_price = price_num
            best_raw = price_raw
            
            airline_val = getattr(flight, "name", None) or getattr(flight, "airline", None) or getattr(flight, "carrier", "依查詢結果為主")
            best_airline = ", ".join(airline_val) if isinstance(airline_val, list) else str(airline_val)
            
            # 分別抓取去程與回程時間
            sub_flights = getattr(flight, "flights", [])
            if len(sub_flights) >= 2:
                best_dep_time = str(getattr(sub_flights[0], "departure_time", getattr(sub_flights[0], "departure", "")))
                best_arr_time = str(getattr(sub_flights[0], "arrival_time", getattr(sub_flights[0], "arrival", "")))
                best_return_dep_time = str(getattr(sub_flights[1], "departure_time", getattr(sub_flights[1], "departure", "")))
                best_return_arr_time = str(getattr(sub_flights[1], "arrival_time", getattr(sub_flights[1], "arrival", "")))
            else:
                best_dep_time = str(getattr(flight, "departure_time", getattr(flight, "departure", "")))
                best_arr_time = str(getattr(flight, "arrival_time", getattr(flight, "arrival", "")))

    if best_price is None: return None
    flight_link = generate_google_flights_url(combo["origin"], combo["destination"], combo["departure_date"], combo["return_date"])
    return {
        "price": best_price, "price_raw": best_raw, "airline": best_airline,
        "dep_time": best_dep_time, "arr_time": best_arr_time,
        "return_dep_time": best_return_dep_time, "return_arr_time": best_return_arr_time,
        "link": flight_link
    }

def append_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "checked_at", "origin", "destination", "departure_date", "return_date", "stay_days", 
            "price", "price_raw", "airline", "dep_time", "arr_time", "return_dep_time", "return_arr_time", "link"
        ])
        if is_new: writer.writeheader()
        writer.writerows(rows)

def load_history_prices_by_route():
    prices_by_route = {}
    if not os.path.exists(HISTORY_CSV): return prices_by_route
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                price = float(row["price"])
                if price < 1000: continue
            except (ValueError, KeyError): continue
            prices_by_route.setdefault((row["origin"], row["destination"]), []).append(price)
    return prices_by_route

def cheap_score(price, past_prices):
    if not past_prices: return 50
    return round(100 * sum(1 for p in past_prices if p > price) / len(past_prices))

def summarize_history():
    if not os.path.exists(HISTORY_CSV): return []
    all_prices = load_history_prices_by_route()
    best_by_route = {}
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                price = float(row["price"])
                if price < 1000: continue
            except (ValueError, KeyError): continue
            key = (row["origin"], row["destination"])
            if key not in best_by_route or price < best_by_route[key]["best_price"]:
                best_by_route[key] = {
                    "origin": row["origin"], "origin_cn": AIRPORT_NAMES.get(row["origin"], row["origin"]),
                    "destination": row["destination"], "destination_cn": AIRPORT_NAMES.get(row["destination"], row["destination"]),
                    "best_price": price, "best_price_raw": row.get("price_raw", f"NT${price:,.0f}"),
                    "best_departure_date": row["departure_date"], "best_return_date": row["return_date"],
                    "best_stay_days": row.get("stay_days", ""), "airline": row.get("airline", "依查詢結果為主"),
                    "dep_time": row.get("dep_time", ""), "arr_time": row.get("arr_time", ""),
                    "return_dep_time": row.get("return_dep_time", ""), "return_arr_time": row.get("return_arr_time", ""),
                    "link": row.get("link", "#"),
                }
    routes = []
    for key, data in best_by_route.items():
        past = all_prices.get(key, [])
        avg_price = sum(past) / len(past) if past else data["best_price"]
        data["cheap_score"] = cheap_score(data["best_price"], past)
        data["diff_percent"] = round(100 * (avg_price - data["best_price"]) / avg_price) if avg_price > 0 else 0
        data["recent_average_price"] = avg_price
        routes.append(data)
    routes.sort(key=lambda r: r["cheap_score"], reverse=True)
    return routes

def main():
    cfg = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    combos = build_combo_grid(cfg)
    state = load_state()
    batch = pick_batch(combos, state, cfg.get("max_checks_per_run", 8))
    now = datetime.now(timezone.utc).isoformat()
    history_rows = []

    for combo in batch:
        res = query_one_combo(combo, cfg)
        if res:
            history_rows.append({"checked_at": now, **combo, **res})

    save_state(state)
    if history_rows: append_history(history_rows)

    latest_payload = {
        "generated_at": now, "checked_this_run": len(history_rows),
        "total_combos_in_grid": len(combos), "routes": summarize_history(),
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()