#!/usr/bin/env python3
"""
flight_monitor.py
------------------
監測直飛機票價格，完整抓取航空公司、起降時間、新台幣價格、計算 Cheap Score 並更新看板。
"""

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

# 機場代碼中文對照表
AIRPORT_NAMES = {
    "TPE": "台北桃園",
    "NRT": "東京成田",
    "KIX": "大阪關西",
    "FUK": "福岡",
    "CTS": "札幌新千歲",
    "OKA": "沖繩那霸",
    "ICN": "首爾仁川",
    "PUS": "釜山金海",
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
    if raw is None:
        return None, None
    digits = "".join(ch for ch in str(raw) if ch.isdigit() or ch == ".")
    try:
        return float(digits), str(raw)
    except ValueError:
        return None, str(raw)


def is_nonstop(flight):
    stops = getattr(flight, "stops", None)
    if stops is None:
        return True
    if isinstance(stops, (int, float)):
        return stops == 0
    s = str(stops).strip().lower()
    return s in ("0", "nonstop", "non-stop", "direct", "0 stops", "0 stop")


def generate_google_flights_url(origin, dest, dep_date, ret_date):
    """產生精準直達 Google Flights 的查詢網址"""
    return f"https://www.google.com/travel/flights?q=flights%20from%20{origin}%20to%20{dest}%20on%20{dep_date}%20through%20{ret_date}"


def query_one_combo(combo, cfg):
    from fast_flights import get_flights

    try:
        result = get_flights(
            flight_data=[
                {
                    "date": combo["departure_date"],
                    "from": combo["origin"],
                    "to": combo["destination"],
                },
                {
                    "date": combo["return_date"],
                    "from": combo["destination"],
                    "to": combo["origin"],
                },
            ],
            trip="round-trip",
            seat=cfg.get("seat", "economy"),
            passengers={"adults": cfg.get("adults", 1)},
            currency="TWD",  # 強制指定新台幣
            fetch_mode="fallback",
        )
    except Exception as e:
        print(f"查詢失敗 {combo}: {e}")
        return None, None, "直飛航空", "", "", "", None, False

    direct_only = cfg.get("direct_flights_only", True)
    best_price = None
    best_raw = None
    best_airline = "直飛航空"
    best_dep_time = ""
    best_arr_time = ""
    any_flight_seen = False

    flights_list = getattr(result, "flights", [])
    for flight in flights_list:
        any_flight_seen = True
        if direct_only and not is_nonstop(flight):
            continue
        
        price_num, price_raw = parse_price(getattr(flight, "price", None))
        if price_num is None:
            continue
            
        if best_price is None or price_num < best_price:
            best_price = price_num
            # 確保價格文字包含 NT$
            best_raw = price_raw if "NT" in str(price_raw) else f"NT${price_raw}"
            
            # 抓取航空公司與時間屬性（相容多種版本欄位）
            best_airline = getattr(flight, "airline", None) or getattr(flight, "airlines", "直飛航空")
            if isinstance(best_airline, list):
                best_airline = ", ".join(best_airline)
                
            dep_t = getattr(flight, "departure_time", None) or getattr(flight, "dep_time", "")
            arr_t = getattr(flight, "arrival_time", None) or getattr(flight, "arr_time", "")
            best_dep_time = str(dep_t) if dep_t else ""
            best_arr_time = str(arr_t) if arr_t else ""

    google_price_level = getattr(result, "current_price", None)
    filtered_out_by_direct = any_flight_seen and best_price is None and direct_only
    
    flight_link = generate_google_flights_url(
        combo["origin"], combo["destination"], combo["departure_date"], combo["return_date"]
    )

    return best_price, best_raw, str(best_airline), best_dep_time, best_arr_time, flight_link, google_price_level, filtered_out_by_direct


def append_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "checked_at", "origin", "destination", "departure_date", "return_date",
                "stay_days", "price", "price_raw", "airline", "dep_time", "arr_time", "link"
            ],
        )
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def load_history_prices_by_route():
    prices_by_route = {}
    if not os.path.exists(HISTORY_CSV):
        return prices_by_route
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                price = float(row["price"])
            except (ValueError, KeyError, TypeError):
                continue
            key = (row["origin"], row["destination"])
            prices_by_route.setdefault(key, []).append(price)
    return prices_by_route


def cheap_score(price, past_prices):
    if not past_prices:
        return None
    n = len(past_prices)
    more_expensive = sum(1 for p in past_prices if p > price)
    return round(100 * more_expensive / n)


def summarize_history():
    if not os.path.exists(HISTORY_CSV):
        return []

    all_prices = load_history_prices_by_route()
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
                orig = row["origin"]
                dest = row["destination"]
                best_by_route[key] = {
                    "origin": orig,
                    "origin_cn": AIRPORT_NAMES.get(orig, orig),
                    "destination": dest,
                    "destination_cn": AIRPORT_NAMES.get(dest, dest),
                    "best_price": price,
                    "best_price_raw": row.get("price_raw", f"NT${price:,.0f}"),
                    "best_departure_date": row["departure_date"],
                    "best_return_date": row["return_date"],
                    "best_stay_days": row.get("stay_days", ""),
                    "airline": row.get("airline", "直飛航空"),
                    "dep_time": row.get("dep_time", ""),
                    "arr_time": row.get("arr_time", ""),
                    "link": row.get("link", "#"),
                }

    routes = []
    for key, data in best_by_route.items():
        past = all_prices.get(key, [])
        score = cheap_score(data["best_price"], past)
        avg_price = sum(past) / len(past) if past else data["best_price"]
        diff_percent = round(100 * (avg_price - data["best_price"]) / avg_price) if avg_price > 0 else 0

        data["cheap_score"] = score if score is not None else 50
        data["diff_percent"] = diff_percent
        routes.append(data)

    routes.sort(key=lambda r: r["cheap_score"], reverse=True)
    return routes


def main():
    cfg = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)

    combos = build_combo_grid(cfg)
    state = load_state()
    batch = pick_batch(combos, state, cfg.get("max_checks_per_run", 8))

    prices_by_route = load_history_prices_by_route()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    history_rows = []

    for combo in batch:
        try:
            res = query_one_combo(combo, cfg)
            price, price_raw, airline, dep_time, arr_time, link, google_level, filtered = res
        except Exception as e:
            print(f"Error processing combo {combo}: {e}")
            continue

        if price is None:
            continue

        route_key = (combo["origin"], combo["destination"])
        
        history_rows.append(
            {
                "checked_at": now,
                "origin": combo["origin"],
                "destination": combo["destination"],
                "departure_date": combo["departure_date"],
                "return_date": combo["return_date"],
                "stay_days": combo["stay_days"],
                "price": price,
                "price_raw": price_raw,
                "airline": airline,
                "dep_time": dep_time,
                "arr_time": arr_time,
                "link": link,
            }
        )
        prices_by_route.setdefault(route_key, []).append(price)

    save_state(state)
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


if __name__ == "__main__":
    main()