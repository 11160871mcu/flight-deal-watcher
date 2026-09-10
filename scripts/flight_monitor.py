import os
import json
import csv
import yaml
import time
import hashlib
import datetime
from collections import defaultdict
from dateutil.relativedelta import relativedelta

from fast_flights import FlightData, Passengers, create_filter

try:
    from fast_flights import get_flights_from_filter
except ImportError:
    from fast_flights.core import get_flights_from_filter


CONFIG_PATH = "config.yaml"


# ============================================================
# 設定 / 路徑
# ============================================================

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_paths(cfg):
    data_cfg = cfg.get("data", {})
    return {
        "history": data_cfg.get("history", "docs/data/history.csv"),
        "latest": data_cfg.get("latest", "docs/data/latest.json"),
        "state": data_cfg.get("state", "docs/data/state.json"),
    }


def ensure_dirs(paths):
    for path in paths.values():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)


# ============================================================
# 滾動一年 + 每條航線自己的任務表
# ============================================================

def rolling_dates(months_ahead):
    """
    每次執行都從「今天」開始，到今天 + months_ahead 個月。
    因此搜尋區間每天自動往前滾，不需要手動改日期。
    """
    today = datetime.date.today()
    end_date = today + relativedelta(months=months_ahead)

    dates = []
    current = today

    while current <= end_date:
        dates.append(current)
        current += datetime.timedelta(days=1)

    return dates


def stay_days_list(cfg):
    stay_cfg = cfg.get("stay_duration", {})
    min_days = int(stay_cfg.get("min_days", 5))
    max_days = int(stay_cfg.get("max_days", 15))
    step_days = int(stay_cfg.get("step_days", 1))

    if min_days <= 0:
        raise ValueError("stay_duration.min_days 必須大於 0")
    if max_days < min_days:
        raise ValueError("stay_duration.max_days 不能小於 min_days")
    if step_days <= 0:
        raise ValueError("stay_duration.step_days 必須大於 0")

    return list(range(min_days, max_days + 1, step_days))


def route_keys(cfg):
    origins = cfg.get("origins", ["TPE"])
    destinations = cfg.get("destinations", [])

    if not destinations:
        raise ValueError("config.yaml 的 destinations 不能是空的")

    return [
        (origin, destination)
        for origin in origins
        for destination in destinations
    ]


def build_tasks_by_route(cfg):
    """
    每個目的地建立自己的完整任務表：
        今天～未來一年 × 5～15 天

    之後 main() 不再從一條大 list 連續切 100 筆，
    而是對每個目的地公平分配查詢配額。
    """
    months_ahead = int(cfg.get("search_months_ahead", 12))
    dates = rolling_dates(months_ahead)
    stays = stay_days_list(cfg)

    tasks_by_route = {}

    for origin, destination in route_keys(cfg):
        key = f"{origin}|{destination}"
        tasks = []

        for departure_date in dates:
            for stay_days in stays:
                return_date = departure_date + datetime.timedelta(days=stay_days)

                tasks.append({
                    "origin": origin,
                    "destination": destination,
                    "departure_date": departure_date.isoformat(),
                    "return_date": return_date.isoformat(),
                    "stay_days": stay_days,
                })

        tasks_by_route[key] = tasks

    return tasks_by_route


def grid_signature(cfg):
    """
    搜尋條件改變時，自動判斷舊 state 已不適用並重置游標。
    """
    payload = {
        "origins": cfg.get("origins", ["TPE"]),
        "destinations": cfg.get("destinations", []),
        "months": int(cfg.get("search_months_ahead", 12)),
        "stays": stay_days_list(cfg),
        "adults": int(cfg.get("adults", 1)),
        "seat": str(cfg.get("seat", "economy")),
        "direct_only": bool(cfg.get("direct_only", True)),
    }

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ============================================================
# 公平輪詢 state
# ============================================================

def fresh_state(cfg):
    return {
        "version": 3,
        "grid_signature": grid_signature(cfg),
        "grid_start_date": datetime.date.today().isoformat(),
        "next_index_by_route": {},
        "extra_rotation": 0,
    }


def load_state(cfg, state_path):
    """
    新版 state 使用每條航線獨立游標：
      TPE|NRT -> index
      TPE|HND -> index
      ...

    如果是舊版 state（只有 next_index）或 config 改變，
    自動重置，不需要手動刪 state.json。
    """
    state = fresh_state(cfg)

    if not os.path.exists(state_path):
        return state

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            old = json.load(f)

        if old.get("grid_signature") != state["grid_signature"]:
            print("ℹ️ 搜尋設定已改變，公平輪詢游標自動重置")
            return state

        if not isinstance(old.get("next_index_by_route"), dict):
            print("ℹ️ 偵測到舊版 state.json，公平輪詢游標自動重置")
            return state

        state["next_index_by_route"] = {
            str(k): int(v)
            for k, v in old.get("next_index_by_route", {}).items()
        }
        state["extra_rotation"] = int(old.get("extra_rotation", 0))

        # 滾動日期每天會往前移一天。
        # 為了讓游標仍指向接近原本的「絕對日期」，將游標同步往前調整。
        old_start_text = old.get("grid_start_date")
        if old_start_text:
            old_start = datetime.date.fromisoformat(old_start_text)
            today = datetime.date.today()
            shifted_days = (today - old_start).days

            if shifted_days > 0:
                per_day = len(stay_days_list(cfg))
                shift_slots = shifted_days * per_day

                for key in list(state["next_index_by_route"].keys()):
                    state["next_index_by_route"][key] = max(
                        0,
                        state["next_index_by_route"][key] - shift_slots
                    )

        state["grid_start_date"] = datetime.date.today().isoformat()
        return state

    except Exception as exc:
        print(f"⚠️ state.json 讀取失敗，將重新開始：{exc}")
        return state


def save_state(state, state_path):
    state["grid_start_date"] = datetime.date.today().isoformat()

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_fair_batch(cfg, tasks_by_route, state):
    """
    8 個目的地公平輪詢。

    例如 max_checks_per_run = 96：
      8 個目的地 -> 每個目的地 12 組。

    如果 max_checks_per_run = 100：
      每個目的地先 12 組，剩下 4 組輪流分配；
      下一次從不同目的地開始分額外配額，長期仍然公平。
    """
    keys = list(tasks_by_route.keys())
    route_count = len(keys)

    if route_count == 0:
        return [], {}, 0

    batch_size = max(route_count, int(cfg.get("max_checks_per_run", 96)))
    base = batch_size // route_count
    remainder = batch_size % route_count

    rotation = int(state.get("extra_rotation", 0)) % route_count

    quotas = {key: base for key in keys}

    for i in range(remainder):
        key = keys[(rotation + i) % route_count]
        quotas[key] += 1

    selected_by_route = {}
    next_indexes = {}

    for key in keys:
        tasks = tasks_by_route[key]

        if not tasks:
            selected_by_route[key] = []
            next_indexes[key] = 0
            continue

        start = int(state["next_index_by_route"].get(key, 0)) % len(tasks)
        quota = quotas[key]

        picked = [
            tasks[(start + offset) % len(tasks)]
            for offset in range(quota)
        ]

        selected_by_route[key] = picked
        next_indexes[key] = (start + quota) % len(tasks)

    # 真正執行時也交錯查詢：NRT 一筆、HND 一筆、KIX 一筆……
    # 避免前半段全打同一個目的地。
    selected = []
    max_quota = max(quotas.values())

    execution_keys = [
        keys[(rotation + i) % route_count]
        for i in range(route_count)
    ]

    for slot in range(max_quota):
        for key in execution_keys:
            rows = selected_by_route[key]
            if slot < len(rows):
                selected.append(rows[slot])

    next_rotation = (
        (rotation + remainder) % route_count
        if remainder
        else (rotation + 1) % route_count
    )

    return selected, next_indexes, next_rotation


# ============================================================
# 價格 / 航班資料解析
# ============================================================

def parse_price(raw_price):
    """
    強制避免美元數字被誤當成新台幣。
    """
    if raw_price is None:
        return None

    if isinstance(raw_price, (int, float)):
        value = int(raw_price)
        return value if value >= 1000 else None

    text = str(raw_price).strip()

    if not text:
        return None

    lower = text.lower()

    if "price unavailable" in lower or "check price" in lower:
        return None

    # "$250" 丟掉；"NT$8,525" 接受。
    if "$" in text and "NT$" not in text.upper():
        return None

    digits = "".join(ch for ch in text if ch.isdigit())

    if not digits:
        return None

    value = int(digits)
    return value if value >= 1000 else None


def clean_text(value):
    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() in {
        "none", "null", "unknown", "n/a", "nan"
    }:
        return None

    return text


def safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def flight_option(flight):
    return {
        "price": parse_price(getattr(flight, "price", None)),
        "airline": clean_text(getattr(flight, "name", None)),
        "departure": clean_text(getattr(flight, "departure", None)),
        "arrival": clean_text(getattr(flight, "arrival", None)),
        "duration": clean_text(getattr(flight, "duration", None)),
        "stops": safe_int(getattr(flight, "stops", None)),
    }


def option_identity(option):
    return (
        option.get("price"),
        option.get("airline"),
        option.get("departure"),
        option.get("arrival"),
        option.get("duration"),
        option.get("stops"),
    )


def same_price_options(candidates):
    """
    找出「最低價格」完全相同的所有可辨識航班，
    讓網頁把不同航空公司 / 不同起飛時間逐一列出。
    """
    prices = [
        parse_price(getattr(f, "price", None))
        for f in candidates
    ]
    prices = [p for p in prices if p is not None]

    if not prices:
        return None, []

    minimum = min(prices)

    options = []
    seen = set()

    for flight in candidates:
        if parse_price(getattr(flight, "price", None)) != minimum:
            continue

        option = flight_option(flight)

        # 完全沒有航空公司和時間的項目不另外列成「可選航班」。
        if not (
            option.get("airline")
            or option.get("departure")
            or option.get("arrival")
        ):
            continue

        identity = option_identity(option)

        if identity in seen:
            continue

        seen.add(identity)
        options.append(option)

    # 航空公司 + 起飛 + 抵達資料越完整，排越前面。
    def completeness(opt):
        return sum(
            bool(opt.get(k))
            for k in ("airline", "departure", "arrival")
        )

    options.sort(
        key=lambda opt: (
            -completeness(opt),
            str(opt.get("airline") or ""),
            str(opt.get("departure") or ""),
        )
    )

    return minimum, options


# ============================================================
# Google Flights 查詢
# ============================================================

def query_google(task, cfg):
    adults = int(cfg.get("adults", 1))
    seat = str(cfg.get("seat", "economy"))
    direct_only = bool(cfg.get("direct_only", True))

    flight_filter = create_filter(
        flight_data=[
            FlightData(
                date=task["departure_date"],
                from_airport=task["origin"],
                to_airport=task["destination"],
            ),
            FlightData(
                date=task["return_date"],
                from_airport=task["destination"],
                to_airport=task["origin"],
            ),
        ],
        trip="round-trip",
        passengers=Passengers(adults=adults),
        seat=seat,
        max_stops=0 if direct_only else None,
    )

    return get_flights_from_filter(
        flight_filter,
        currency="TWD",
    )


def search_one(task, cfg):
    """
    如果第一輪有價格、但完全抓不到航空公司/時間，
    可依 config 的 metadata_retry_count 再重試。
    """
    retry_count = max(0, int(cfg.get("metadata_retry_count", 1)))
    retry_delay = max(0.0, float(cfg.get("metadata_retry_delay_seconds", 1.0)))

    best_result = None

    for attempt in range(retry_count + 1):
        try:
            result = query_google(task, cfg)

            flights = getattr(result, "flights", None) or []

            candidates = [
                f
                for f in flights
                if parse_price(getattr(f, "price", None)) is not None
            ]

            if not candidates:
                if attempt < retry_count:
                    time.sleep(retry_delay)
                    continue
                return None

            minimum, options = same_price_options(candidates)

            if minimum is None:
                return None

            row = {
                "origin": task["origin"],
                "destination": task["destination"],
                "departure_date": task["departure_date"],
                "return_date": task["return_date"],
                "stay_days": task["stay_days"],
                "price": minimum,
                "price_raw": f"NT${minimum}",
                "currency": "TWD",
                "passengers": int(cfg.get("adults", 1)),
                "flight_options_json": json.dumps(
                    options,
                    ensure_ascii=False,
                ),
                "checked_at": datetime.datetime.now().isoformat(),
            }

            best_result = row

            if options:
                if len(options) > 1:
                    print(
                        f"    ✅ NT${minimum:,}："
                        f"{len(options)} 個同價航班，全部保留"
                    )
                return row

            # 有價格但沒有 metadata，重試一次看看。
            if attempt < retry_count:
                print(
                    f"    ↻ NT${minimum:,} 有價格但航空公司/時間未解析，重試..."
                )
                time.sleep(retry_delay)
                continue

            print(
                f"    ⚠️ NT${minimum:,} 有價格，"
                "但本次資料來源仍未解析出航空公司/時間"
            )
            return row

        except Exception as exc:
            if attempt < retry_count:
                print(f"    ↻ 查詢失敗，重試：{exc}")
                time.sleep(retry_delay)
                continue

            print(
                f"    ❌ {task['origin']}->{task['destination']} "
                f"{task['departure_date']}~{task['return_date']}：{exc}"
            )
            return best_result

    return best_result


# ============================================================
# history.csv
# ============================================================

HISTORY_FIELDS = [
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "stay_days",
    "price",
    "price_raw",
    "currency",
    "passengers",
    "flight_options_json",
    "checked_at",
]


def load_history(history_path):
    if not os.path.exists(history_path):
        return []

    rows = []

    with open(
        history_path,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        for raw in reader:
            if not raw:
                continue

            price = parse_price(raw.get("price"))

            # 相容更舊版本的 best_price 欄位，但只接受合理 TWD 值。
            if price is None:
                price = parse_price(raw.get("best_price"))

            if price is None:
                continue

            departure_date = (
                raw.get("departure_date")
                or raw.get("best_departure_date")
            )
            return_date = (
                raw.get("return_date")
                or raw.get("best_return_date")
            )
            stay_days = (
                raw.get("stay_days")
                or raw.get("best_stay_days")
            )

            # 舊資料若沒有 options，嘗試從舊欄位組成一個 option。
            options_json = raw.get("flight_options_json", "")

            if not options_json:
                airline = raw.get("airline")
                departure = raw.get("departure") or raw.get("dep_time")
                arrival = raw.get("arrival") or raw.get("arr_time")
                duration = raw.get("duration")

                if airline or departure or arrival:
                    options_json = json.dumps(
                        [{
                            "price": price,
                            "airline": airline or None,
                            "departure": departure or None,
                            "arrival": arrival or None,
                            "duration": duration or None,
                            "stops": safe_int(raw.get("stops")),
                        }],
                        ensure_ascii=False,
                    )

            row = {
                "origin": raw.get("origin", ""),
                "destination": raw.get("destination", ""),
                "departure_date": departure_date or "",
                "return_date": return_date or "",
                "stay_days": safe_int(stay_days),
                "price": price,
                "price_raw": raw.get("price_raw") or f"NT${price}",
                "currency": "TWD",
                "passengers": safe_int(raw.get("passengers")) or 1,
                "flight_options_json": options_json,
                "checked_at": raw.get("checked_at") or raw.get("last_checked_at") or "",
            }

            if not row["origin"] or not row["destination"]:
                continue
            if not row["departure_date"] or not row["return_date"]:
                continue
            if row["stay_days"] is None:
                continue

            rows.append(row)

    return rows


def save_history(history, history_path):
    with open(
        history_path,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=HISTORY_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(history)


def parse_options(raw):
    if not raw:
        return []

    try:
        result = json.loads(raw)

        if isinstance(result, list):
            return result
    except Exception:
        pass

    return []


def merge_options(option_lists, target_price):
    """
    同一日期組合重複查價時：
    若價格相同，把不同輪抓到的航空公司/時間合併起來，
    可降低某一次解析失敗造成的「資料未提供」。
    """
    merged = []
    seen = set()

    for options in option_lists:
        for option in options:
            option_price = parse_price(option.get("price"))

            if option_price is not None and option_price != target_price:
                continue

            identity = option_identity(option)

            if identity in seen:
                continue

            seen.add(identity)
            merged.append(option)

    return merged


# ============================================================
# 產生可供網頁篩選的所有「目前候選優惠」
# ============================================================

def checked_at_value(row):
    try:
        return datetime.datetime.fromisoformat(str(row.get("checked_at", "")))
    except Exception:
        return datetime.datetime.min


def build_dashboard_data(history, cfg):
    """
    不再只輸出「每個目的地一張最低價」。

    改成：
      1. 每個 出發地+目的地+去程日+回程日+停留天數，
         取「最近一次」查到的價格。
      2. 計算該航線的相對平均價與 Cheap Score。
      3. 輸出很多筆 deals，讓網頁自己篩：
         目的地 / 日期 / 停留時間 / 價格 / 相對便宜程度。
    """
    today = datetime.date.today()
    end_date = today + relativedelta(
        months=int(cfg.get("search_months_ahead", 12))
    )

    valid_stays = set(stay_days_list(cfg))
    history_days = int(cfg.get("history_days", 60))
    cutoff = datetime.datetime.now() - datetime.timedelta(days=history_days)

    # 先只保留目前滾動一年窗口內、停留天數符合 config 的資料。
    valid_history = []

    for row in history:
        try:
            dep_date = datetime.date.fromisoformat(row["departure_date"])
        except Exception:
            continue

        stay = safe_int(row.get("stay_days"))
        price = parse_price(row.get("price"))

        if price is None or stay not in valid_stays:
            continue
        if dep_date < today or dep_date > end_date:
            continue

        valid_history.append(row)

    # 同一路線的近期價格池，用來計算「相對便宜」。
    route_price_pool = defaultdict(list)

    for row in valid_history:
        checked = checked_at_value(row)

        if checked == datetime.datetime.min or checked >= cutoff:
            route_price_pool[
                (row["origin"], row["destination"])
            ].append(parse_price(row["price"]))

    # 若某航線最近 history_days 沒資料，退回使用全部有效歷史。
    all_route_prices = defaultdict(list)

    for row in valid_history:
        all_route_prices[
            (row["origin"], row["destination"])
        ].append(parse_price(row["price"]))

    # 同一「日期組合」可能查過很多次。
    # 取最新價格，但如果同價格的舊紀錄有更完整的航班資訊就合併。
    combo_groups = defaultdict(list)

    for row in valid_history:
        key = (
            row["origin"],
            row["destination"],
            row["departure_date"],
            row["return_date"],
            safe_int(row["stay_days"]),
        )
        combo_groups[key].append(row)

    deals = []

    for key, rows in combo_groups.items():
        rows.sort(key=checked_at_value, reverse=True)
        latest_row = rows[0]

        current_price = parse_price(latest_row["price"])
        if current_price is None:
            continue

        # 只合併「目前同價格」的 metadata，避免把舊價格的航班錯配到新價格。
        option_lists = [
            parse_options(r.get("flight_options_json"))
            for r in rows
            if parse_price(r.get("price")) == current_price
        ]

        options = merge_options(option_lists, current_price)

        route = (latest_row["origin"], latest_row["destination"])
        pool = route_price_pool.get(route) or all_route_prices.get(route) or []

        if pool:
            average_price = sum(pool) / len(pool)
            diff_percent = (
                (average_price - current_price)
                / average_price
                * 100
            )

            # 價格越低，Cheap Score 越高。
            # = 此價格至少比該航線多少比例的近期報價便宜或相同。
            at_or_above = sum(1 for p in pool if p >= current_price)
            cheap_score = round(at_or_above / len(pool) * 100)
        else:
            average_price = current_price
            diff_percent = 0.0
            cheap_score = 50

        deals.append({
            "origin": latest_row["origin"],
            "destination": latest_row["destination"],
            "departure_date": latest_row["departure_date"],
            "return_date": latest_row["return_date"],
            "stay_days": safe_int(latest_row["stay_days"]),
            "price": current_price,
            "currency": "TWD",
            "passengers": safe_int(latest_row.get("passengers")) or 1,
            "flight_options": options,
            "same_price_option_count": len(options),
            "recent_average_price": round(average_price),
            "diff_percent": round(diff_percent, 1),
            "cheap_score": cheap_score,
            "checked_at": latest_row.get("checked_at"),
            "route_sample_size": len(pool),
        })

    # 預設先以「相對便宜」排序。
    deals.sort(
        key=lambda d: (
            -d["cheap_score"],
            -d["diff_percent"],
            d["price"],
            d["departure_date"],
        )
    )

    # 避免靜態 JSON 無限制長大；保留大量相對便宜候選供網頁篩選。
    # 設為 0 代表不限筆數。
    max_publish = int(cfg.get("max_deals_to_publish", 3000))

    if max_publish > 0:
        deals = deals[:max_publish]

    return deals


# ============================================================
# 主程式
# ============================================================

def main():
    print("=== Flight Deal Watcher ===")

    cfg = load_config()
    paths = get_paths(cfg)
    ensure_dirs(paths)

    tasks_by_route = build_tasks_by_route(cfg)
    total_combos = sum(len(v) for v in tasks_by_route.values())

    state = load_state(cfg, paths["state"])

    selected, next_indexes, next_rotation = build_fair_batch(
        cfg,
        tasks_by_route,
        state,
    )

    route_run_counts = defaultdict(int)

    for task in selected:
        route_run_counts[
            f"{task['origin']}|{task['destination']}"
        ] += 1

    print(
        f"滾動區間：{datetime.date.today()} ～ "
        f"{datetime.date.today() + relativedelta(months=int(cfg.get('search_months_ahead', 12)))}"
    )
    print(
        f"停留時間：{min(stay_days_list(cfg))}～{max(stay_days_list(cfg))} 天"
    )
    print(f"全部候選組合：{total_combos}")
    print(f"本次公平輪詢：{len(selected)} 組")

    print("本次各目的地配額：")
    for key in tasks_by_route:
        print(f"  {key.replace('|', ' -> ')}：{route_run_counts[key]} 組")

    new_rows = []

    request_delay = max(
        0.0,
        float(cfg.get("request_delay_seconds", 0.0))
    )

    for idx, task in enumerate(selected, start=1):
        print(
            f"[{idx}/{len(selected)}] "
            f"{task['origin']} -> {task['destination']} | "
            f"{task['departure_date']} ~ {task['return_date']} | "
            f"停留 {task['stay_days']} 天"
        )

        result = search_one(task, cfg)

        if result:
            new_rows.append(result)

        if request_delay > 0 and idx < len(selected):
            time.sleep(request_delay)

    state["next_index_by_route"] = next_indexes
    state["extra_rotation"] = next_rotation
    save_state(state, paths["state"])

    history = load_history(paths["history"])
    history.extend(new_rows)

    if not history:
        print("本次沒有成功取得任何價格，資料檔不更新")
        return

    save_history(history, paths["history"])

    deals = build_dashboard_data(history, cfg)

    latest = {
        "generated_at": datetime.datetime.now().isoformat(),
        "search_start_date": datetime.date.today().isoformat(),
        "search_end_date": (
            datetime.date.today()
            + relativedelta(months=int(cfg.get("search_months_ahead", 12)))
        ).isoformat(),
        "adults": int(cfg.get("adults", 1)),
        "stay_min_days": min(stay_days_list(cfg)),
        "stay_max_days": max(stay_days_list(cfg)),
        "history_days": int(cfg.get("history_days", 60)),
        "checked_this_run": len(new_rows),
        "attempted_this_run": len(selected),
        "total_checks_so_far": len(history),
        "total_combos_in_grid": total_combos,
        "route_checks_this_run": dict(route_run_counts),
        "deal_count": len(deals),
        "deals": deals,
    }

    with open(paths["latest"], "w", encoding="utf-8") as f:
        json.dump(
            latest,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    print()
    print("=== 完成 ===")
    print(f"本次嘗試：{len(selected)} 組")
    print(f"本次成功：{len(new_rows)} 筆")
    print(f"歷史累積：{len(history)} 筆")
    print(f"網頁可篩選優惠：{len(deals)} 筆")
    print(f"history：{paths['history']}")
    print(f"latest：{paths['latest']}")
    print(f"state：{paths['state']}")


if __name__ == "__main__":
    main()
