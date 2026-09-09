#!/usr/bin/env python3
"""
flight_monitor.py
------------------
監測直飛機票價格，完整抓取航空公司、起降時間、新台幣價格、計算 Cheap Score
與「比近期平均低 X%」，並更新看板。
"""

import csv
import json
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "docs", "data")
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
STATE_JSON = os.path.join(DATA_DIR, "state.json")

# 中英文機場名稱
AIRPORT_NAMES = {
    "TPE": {"cn": "台北桃園", "en": "Taiwan Taoyuan International Airport"},
    "NRT": {"cn": "東京成田", "en": "Narita International Airport"},
    "KIX": {"cn": "大阪關西", "en": "Kansai International Airport"},
    "FUK": {"cn": "福岡", "en": "Fukuoka Airport"},
    "CTS": {"cn": "札幌新千歲", "en": "New Chitose Airport"},
    "OKA": {"cn": "沖繩那霸", "en": "Naha Airport"},
    "ICN": {"cn": "首爾仁川", "en": "Incheon International Airport"},
    "PUS": {"cn": "釜山金海", "en": "Gimhae International Airport"},
}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def airport_info(iata):
    info = AIRPORT_NAMES.get(iata, {"cn": iata, "en": ""})
    return {
        "cn": info.get("cn", iata),
        "en": info.get("en", ""),
    }


def airport_label(iata):
    info = airport_info(iata)
    return f'{info["cn"]} {info["en"]} ({iata})' if info["en"] else f'{info["cn"]} ({iata})'


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
        json.dump(state, f, ensure_ascii=False, indent=2)


def pick_batch(combos, state, batch_size):
    n = len(combos)
    if n == 0:
        return []
    start = state.get("next_index", 0) % n
    batch = [combos[(start + i) % n] for i in range(batch_size)]
    state["next_index"] = (start + batch_size) % n
    return batch


def parse_price(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    # 只取價格數字，顯示時統一由程式格式化成 NT$
    cleaned = re.sub(r"[^0-9.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def is_nonstop(flight):
    stops = getattr(flight, "stops", None)
    if stops is None:
        return True
    if isinstance(stops, (int, float)):
        return stops == 0
    s = str(stops).strip().lower()
    return s in ("0", "nonstop", "non-stop", "direct", "0 stops", "0 stop")


def first_attr(obj, names, default=None):
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:
            value = None
        if value not in (None, "", []):
            return value
    return default


def stringify(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " / ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


def extract_airline(flight):
    value = first_attr(flight, ["airline", "airlines", "name"], "")
    text = stringify(value).strip()
    return text or "航空公司資料未提供"


def extract_time(value):
    """從 07:30、TPE 07:30、2026-10-15 07:30 等字串取出 HH:MM。"""
    text = stringify(value)
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    return match.group(0) if match else text.strip()


def extract_leg_times(flight):
    dep_raw = first_attr(
        flight,
        ["departure_time", "dep_time", "departure", "depart"],
        "",
    )
    arr_raw = first_attr(
        flight,
        ["arrival_time", "arr_time", "arrival", "arrive"],
        "",
    )

    # 支援新舊 fast-flights 欄位，以及某些版本直接給 list/tuple 的情況。
    dep_text = stringify(dep_raw)
    arr_text = stringify(arr_raw)
    dep_time = extract_time(dep_text)
    arr_time = extract_time(arr_text)

    return dep_time, arr_time, dep_text, arr_text


def extract_roundtrip_times(flight):
    # 如果套件版本有明確提供回程欄位就一起抓；沒有則留空，不亂猜。
    ret_dep = first_attr(flight, ["return_departure_time", "return_dep_time", "inbound_departure"], "")
    ret_arr = first_attr(flight, ["return_arrival_time", "return_arr_time", "inbound_arrival"], "")
    return extract_time(ret_dep), extract_time(ret_arr)


def generate_google_flights_url(origin, dest, dep_date, ret_date):
    """產生包含日期的 Google Flights 查詢連結；若套件沒有 booking URL 就作為可靠 fallback。"""
    q = urllib.parse.quote(
        f"flights from {origin} to {dest} on {dep_date} through {ret_date}"
    )
    return f"https://www.google.com/travel/flights?q={q}&hl=zh-TW"


def best_available_link(result, flight, origin, dest, dep_date, ret_date):
    # 新版 fast-flights / 整合器可能直接提供 booking URL；有就優先使用。
    for obj in (flight, result):
        value = first_attr(obj, ["booking_url", "book_url", "url"], "")
        text = stringify(value).strip()
        if text.startswith("http"):
            return text
    return generate_google_flights_url(origin, dest, dep_date, ret_date)


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
            currency="TWD",
            language="zh-TW",
            fetch_mode="fallback",
        )
    except TypeError:
        # 舊版不接受 language 時，退回原有 API。
        try:
            result = get_flights(
                flight_data=[
                    {"date": combo["departure_date"], "from": combo["origin"], "to": combo["destination"]},
                    {"date": combo["return_date"], "from": combo["destination"], "to": combo["origin"]},
                ],
                trip="round-trip",
                seat=cfg.get("seat", "economy"),
                passengers={"adults": cfg.get("adults", 1)},
                currency="TWD",
                fetch_mode="fallback",
            )
        except Exception as e:
            print(f"查詢失敗 {combo}: {e}")
            return None
    except Exception as e:
        print(f"查詢失敗 {combo}: {e}")
        return None

    direct_only = cfg.get("direct_flights_only", True)
    best = None

    flights_list = getattr(result, "flights", []) or []
    for flight in flights_list:
        if direct_only and not is_nonstop(flight):
            continue

        price_num = parse_price(getattr(flight, "price", None))
        if price_num is None:
            continue

        dep_time, arr_time, dep_raw, arr_raw = extract_leg_times(flight)
        ret_dep_time, ret_arr_time = extract_roundtrip_times(flight)
        airline = extract_airline(flight)
        link = best_available_link(
            result,
            flight,
            combo["origin"],
            combo["destination"],
            combo["departure_date"],
            combo["return_date"],
        )

        candidate = {
            "price": price_num,
            "price_raw": f"NT${price_num:,.0f}",
            "airline": airline,
            "dep_time": dep_time,
            "arr_time": arr_time,
            "dep_raw": dep_raw,
            "arr_raw": arr_raw,
            "return_dep_time": ret_dep_time,
            "return_arr_time": ret_arr_time,
            "link": link,
        }
        if best is None or candidate["price"] < best["price"]:
            best = candidate

    if best is None:
        return None

    # result.current_price 是價格趨勢文字，不是票價，不拿來當票價。
    best["google_price_level"] = stringify(getattr(result, "current_price", ""))
    return best


def append_history(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "checked_at", "origin", "destination", "departure_date", "return_date",
                "stay_days", "price", "price_raw", "airline", "dep_time", "arr_time",
                "return_dep_time", "return_arr_time", "link",
            ],
            extrasaction="ignore",
        )
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def read_history_rows():
    if not os.path.exists(HISTORY_CSV):
        return []
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.DictReader(f):
            try:
                row["price"] = float(row["price"])
            except (ValueError, TypeError, KeyError):
                continue
            rows.append(row)
        return rows


def load_history_prices_by_combo():
    prices = {}
    for row in read_history_rows():
        key = (
            row.get("origin", ""),
            row.get("destination", ""),
            row.get("departure_date", ""),
            row.get("return_date", ""),
        )
        prices.setdefault(key, []).append(row["price"])
    return prices


def parse_checked_at(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def cheap_score(price, past_prices):
    if not past_prices:
        return 50
    more_expensive = sum(1 for p in past_prices if p > price)
    return round(100 * more_expensive / len(past_prices))


def summarize_history(cfg):
    rows = read_history_rows()
    if not rows:
        return []

    recent_days = int(cfg.get("recent_average_days", 60))
    dashboard_days = int(cfg.get("dashboard_recent_days", 14))
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)
    dashboard_cutoff = now - timedelta(days=dashboard_days)

    # 以「相同出發/回程日期組合」做歷史比較，避免不同日期的票價混在一起。
    by_combo = {}
    for row in rows:
        key = (
            row.get("origin", ""),
            row.get("destination", ""),
            row.get("departure_date", ""),
            row.get("return_date", ""),
        )
        checked = parse_checked_at(row.get("checked_at", ""))
        row["_checked"] = checked
        by_combo.setdefault(key, []).append(row)

    # 最近 N 天有重新查過的組合，才進入首頁排行榜。
    current_combos = []
    for key, combo_rows in by_combo.items():
        recent_rows = [r for r in combo_rows if r.get("_checked") and r["_checked"] >= dashboard_cutoff]
        if not recent_rows:
            continue
        best_row = min(recent_rows, key=lambda r: (r["price"], r["_checked"]))

        comparison_rows = [
            r for r in combo_rows
            if r.get("_checked") and r["_checked"] >= recent_cutoff
        ]
        comparison_prices = [r["price"] for r in comparison_rows]
        # 沒有足夠歷史時，以這個組合目前已查到的全部價格作備援。
        if not comparison_prices:
            comparison_prices = [r["price"] for r in combo_rows]

        avg_price = sum(comparison_prices) / len(comparison_prices)
        diff_percent = round(100 * (avg_price - best_row["price"]) / avg_price) if avg_price > 0 else 0
        score = cheap_score(best_row["price"], comparison_prices)

        orig = best_row.get("origin", "")
        dest = best_row.get("destination", "")
        orig_info = airport_info(orig)
        dest_info = airport_info(dest)

        current_combos.append({
            "origin": orig,
            "origin_cn": orig_info["cn"],
            "origin_en": orig_info["en"],
            "origin_label": airport_label(orig),
            "destination": dest,
            "destination_cn": dest_info["cn"],
            "destination_en": dest_info["en"],
            "destination_label": airport_label(dest),
            "best_price": round(best_row["price"]),
            "best_price_raw": f'NT${best_row["price"]:,.0f}',
            "best_departure_date": best_row.get("departure_date", ""),
            "best_return_date": best_row.get("return_date", ""),
            "best_stay_days": best_row.get("stay_days", ""),
            "airline": best_row.get("airline") or "航空公司資料未提供",
            "dep_time": best_row.get("dep_time", ""),
            "arr_time": best_row.get("arr_time", ""),
            "return_dep_time": best_row.get("return_dep_time", ""),
            "return_arr_time": best_row.get("return_arr_time", ""),
            "link": best_row.get("link") or generate_google_flights_url(
                orig, dest, best_row.get("departure_date", ""), best_row.get("return_date", "")
            ),
            "cheap_score": score,
            "diff_percent": diff_percent,
            "recent_average_price": round(avg_price),
            "history_count": len(comparison_prices),
            "last_checked_at": best_row.get("checked_at", ""),
        })

    # 每個目的地只留目前查到最有競爭力的一組日期；如此首頁會像真正的目的地排行榜。
    best_destination = {}
    for item in current_combos:
        dest = item["destination"]
        old = best_destination.get(dest)
        if old is None or (item["cheap_score"], -item["diff_percent"], -item["best_price"]) > (
            old["cheap_score"], -old["diff_percent"], -old["best_price"]
        ):
            best_destination[dest] = item

    routes = list(best_destination.values())
    routes.sort(key=lambda r: (-r["cheap_score"], -r["diff_percent"], r["best_price"]))
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
        try:
            result = query_one_combo(combo, cfg)
        except Exception as e:
            print(f"Error processing combo {combo}: {e}")
            continue

        if not result:
            continue

        history_rows.append(
            {
                "checked_at": now,
                "origin": combo["origin"],
                "destination": combo["destination"],
                "departure_date": combo["departure_date"],
                "return_date": combo["return_date"],
                "stay_days": combo["stay_days"],
                "price": result["price"],
                "price_raw": result["price_raw"],
                "airline": result["airline"],
                "dep_time": result["dep_time"],
                "arr_time": result["arr_time"],
                "return_dep_time": result["return_dep_time"],
                "return_arr_time": result["return_arr_time"],
                "link": result["link"],
            }
        )

    save_state(state)
    if history_rows:
        append_history(history_rows)

    routes_summary = summarize_history(cfg)
    latest_payload = {
        "generated_at": now,
        "currency": "TWD",
        "checked_this_run": len(history_rows),
        "total_combos_in_grid": len(combos),
        "recent_average_days": int(cfg.get("recent_average_days", 60)),
        "dashboard_recent_days": int(cfg.get("dashboard_recent_days", 14)),
        "routes": routes_summary,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
