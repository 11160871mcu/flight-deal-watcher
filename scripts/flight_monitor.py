import os
import json
import csv
import yaml
import datetime
import itertools
from dateutil.relativedelta import relativedelta

from fast_flights import FlightData, Passengers, create_filter

try:
    from fast_flights import get_flights_from_filter
except ImportError:
    from fast_flights.core import get_flights_from_filter


CONFIG_PATH = "config.yaml"
HISTORY_CSV = "docs/data/history.csv"
LATEST_JSON = "docs/data/latest.json"
STATE_JSON = "docs/data/state.json"


# ============================================================
# 基本工具
# ============================================================

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_dirs():
    os.makedirs("docs/data", exist_ok=True)


def rolling_date_grid(months_ahead=12):
    """
    每次執行都從今天開始，自動往後滾動 months_ahead 個月。
    例如 2026-10 執行，就查到 2027-10；
    2026-11 執行，就自動查到 2027-11。
    """
    today = datetime.date.today()
    end = today + relativedelta(months=months_ahead)

    dates = []
    d = today
    while d <= end:
        dates.append(d)
        d += datetime.timedelta(days=1)

    return dates


def generate_search_tasks(cfg):
    """
    建立完整候選組合：
    所有出發日 × 所有目的地 × 所有停留天數。
    """
    origins = cfg.get("origins", ["TPE"])
    destinations = cfg.get("destinations", [])

    stay = cfg.get("stay_duration", {})
    min_days = int(stay.get("min_days", 7))
    max_days = int(stay.get("max_days", 21))
    step_days = int(stay.get("step_days", 1))

    if not destinations:
        raise ValueError("config.yaml 的 destinations 不能是空的")

    if min_days <= 0 or max_days < min_days or step_days <= 0:
        raise ValueError("stay_duration 設定不正確")

    tasks = []

    for dep in rolling_date_grid(int(cfg.get("search_months_ahead", 12))):
        for origin, dest in itertools.product(origins, destinations):
            for days in range(min_days, max_days + 1, step_days):
                ret = dep + datetime.timedelta(days=days)

                tasks.append({
                    "origin": origin,
                    "destination": dest,
                    "departure": dep.strftime("%Y-%m-%d"),
                    "return": ret.strftime("%Y-%m-%d"),
                    "stay_days": days,
                })

    return tasks


# ============================================================
# state.json
# ============================================================

def load_state():
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {"next_index": int(data.get("next_index", 0))}
        except Exception:
            pass

    return {"next_index": 0}


def save_state(index):
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(
            {"next_index": int(index)},
            f,
            ensure_ascii=False,
            indent=2,
        )


# ============================================================
# 價格 / 航班資料解析
# ============================================================

def _parse_price(raw_price):
    """
    將 fast-flights 回傳的價格轉成 int。
    查詢時強制 currency="TWD"。

    為避免 "$250" 被誤當 NT$250：
    - 明確美元 $xxx（但不是 NT$）直接丟掉
    - Price unavailable / Check price 直接丟掉
    - 最終數字小於 1000 直接丟掉
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

    if "$" in text and "NT$" not in text.upper():
        return None

    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None

    value = int(digits)
    return value if value >= 1000 else None


def _text_value(obj, attr):
    value = getattr(obj, attr, None)

    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() in {
        "none", "null", "unknown", "n/a", "nan"
    }:
        return None

    return text


def _safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flight_option(flight):
    """把一個 fast-flights Flight 物件整理成前端可顯示的同價航班選項。"""
    return {
        "price": _parse_price(getattr(flight, "price", None)),
        "airline": _text_value(flight, "name"),
        "departure": _text_value(flight, "departure"),
        "arrival": _text_value(flight, "arrival"),
        "duration": _text_value(flight, "duration"),
        "stops": _safe_int(getattr(flight, "stops", None)),
    }


def _option_identity(option):
    """用來去除同一批結果中的重複航班。"""
    return (
        option.get("airline"),
        option.get("departure"),
        option.get("arrival"),
        option.get("duration"),
        option.get("stops"),
        option.get("price"),
    )


def _build_same_price_options(candidates):
    """
    找出本次查詢「最低價完全相同」的所有航班，
    分別保留航空公司、起飛時間、抵達時間、飛行時間。

    例：
    NT$8,525
      - 台灣虎航 16:25 → 20:15
      - 另一航空公司 18:10 → 22:00

    兩筆都會存，不再只留其中一筆。
    """
    if not candidates:
        return None, []

    min_price = min(
        _parse_price(getattr(f, "price", None))
        for f in candidates
    )

    same_price = [
        f for f in candidates
        if _parse_price(getattr(f, "price", None)) == min_price
    ]

    options = []
    seen = set()

    for flight in same_price:
        option = _flight_option(flight)

        # 至少有航空公司或時間其中一項才值得當成選項顯示
        if not (
            option.get("airline")
            or option.get("departure")
            or option.get("arrival")
        ):
            continue

        key = _option_identity(option)
        if key in seen:
            continue

        seen.add(key)
        options.append(option)

    # 排序：資料完整的優先，其次按航空公司與起飛時間
    def completeness(opt):
        return sum(bool(opt.get(k)) for k in ("airline", "departure", "arrival"))

    options.sort(
        key=lambda opt: (
            -completeness(opt),
            str(opt.get("airline") or ""),
            str(opt.get("departure") or ""),
        )
    )

    return min_price, options


# ============================================================
# 單一日期組合查詢
# ============================================================

def search_one(task, cfg):
    try:
        adults = int(cfg.get("adults", 1))
        seat = str(cfg.get("seat", "economy"))

        flight_filter = create_filter(
            flight_data=[
                FlightData(
                    date=task["departure"],
                    from_airport=task["origin"],
                    to_airport=task["destination"],
                ),
                FlightData(
                    date=task["return"],
                    from_airport=task["destination"],
                    to_airport=task["origin"],
                ),
            ],
            trip="round-trip",
            passengers=Passengers(adults=adults),
            seat=seat,
            max_stops=0,
        )

        result = get_flights_from_filter(
            flight_filter,
            currency="TWD",
        )

        if not getattr(result, "flights", None):
            return None

        candidates = [
            f
            for f in result.flights
            if _parse_price(getattr(f, "price", None)) is not None
        ]

        if not candidates:
            return None

        min_price, same_price_options = _build_same_price_options(candidates)

        if min_price is None:
            return None

        # 如果最低價有多個可辨識航班，全部保留下來。
        # primary 只用來相容舊前端欄位；新版前端會讀 flight_options。
        primary = same_price_options[0] if same_price_options else {
            "price": min_price,
            "airline": None,
            "departure": None,
            "arrival": None,
            "duration": None,
            "stops": None,
        }

        if len(same_price_options) > 1:
            print(
                f"✅ NT${min_price:,} 找到 {len(same_price_options)} 個同價航班，"
                "全部保留供網頁選擇"
            )
        elif len(same_price_options) == 1:
            print(
                f"✅ NT${min_price:,} 找到 1 個可辨識航班"
            )
        else:
            print(
                f"⚠️ NT${min_price:,} 有價格，但本次沒有解析出航空公司/時間"
            )

        return {
            "origin": task["origin"],
            "destination": task["destination"],

            "departure_date": task["departure"],
            "return_date": task["return"],
            "stay_days": task["stay_days"],

            "price": min_price,
            "price_raw": f"NT${min_price}",
            "currency": "TWD",
            "passengers": adults,

            # 相容原本欄位
            "airline": primary.get("airline"),
            "departure": primary.get("departure"),
            "arrival": primary.get("arrival"),
            "duration": primary.get("duration"),
            "stops": primary.get("stops"),

            # 新增：同一最低價的所有可選航班
            "flight_options_json": json.dumps(
                same_price_options,
                ensure_ascii=False,
            ),

            "checked_at": datetime.datetime.now().isoformat(),
        }

    except Exception as e:
        print(
            f"Search failed: "
            f"{task['origin']}->{task['destination']} "
            f"{task['departure']}~{task['return']} | {e}"
        )
        return None


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
    "airline",
    "departure",
    "arrival",
    "duration",
    "stops",
    "flight_options_json",
    "checked_at",
]


def load_history():
    if not os.path.exists(HISTORY_CSV):
        return []

    rows = []

    with open(HISTORY_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if not row:
                continue

            price = _parse_price(row.get("price"))
            if price is None:
                continue

            cleaned = {
                field: row.get(field, "")
                for field in HISTORY_FIELDS
            }
            cleaned["price"] = price

            rows.append(cleaned)

    return rows


def save_history(history):
    with open(
        HISTORY_CSV,
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


def _parse_options_json(raw):
    if not raw:
        return []

    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ============================================================
# 排行 / 相對便宜程度
# ============================================================

def _parse_checked_at(value):
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def build_route_rankings(history, history_days=60):
    """
    首頁仍是一個目的地一張卡片，
    但卡片內會列出「該最低價的所有同價航班選項」。
    """
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=history_days)

    by_route = {}

    for row in history:
        price = _parse_price(row.get("price"))
        if price is None:
            continue

        key = (
            row.get("origin"),
            row.get("destination"),
        )

        entry = dict(row)
        entry["price"] = price

        by_route.setdefault(key, []).append(entry)

    routes = []

    for (origin, destination), rows in by_route.items():
        recent_rows = []

        for r in rows:
            checked_at = _parse_checked_at(r.get("checked_at"))
            if checked_at is None or checked_at >= cutoff:
                recent_rows.append(r)

        avg_pool = recent_rows if recent_rows else rows

        avg_price = (
            sum(r["price"] for r in avg_pool)
            / len(avg_pool)
        )

        # 找目前已知最低價
        best_price = min(r["price"] for r in rows)

        # 同一最低價可能存在不同日期；為了卡片不混淆，
        # 優先挑「同價航班選項最多」的那一個日期組合作為展示。
        best_price_rows = [
            r for r in rows
            if r["price"] == best_price
        ]

        def row_option_count(r):
            return len(_parse_options_json(r.get("flight_options_json")))

        best = max(
            best_price_rows,
            key=lambda r: (
                row_option_count(r),
                str(r.get("checked_at") or ""),
            ),
        )

        options = _parse_options_json(
            best.get("flight_options_json")
        )

        # 舊資料沒有 flight_options_json 時，至少把舊欄位轉成一個 option
        if not options and (
            best.get("airline")
            or best.get("departure")
            or best.get("arrival")
        ):
            options = [{
                "price": best["price"],
                "airline": best.get("airline") or None,
                "departure": best.get("departure") or None,
                "arrival": best.get("arrival") or None,
                "duration": best.get("duration") or None,
                "stops": _safe_int(best.get("stops")),
            }]

        diff_percent = 0.0
        if avg_price:
            diff_percent = max(
                0.0,
                (avg_price - best["price"])
                / avg_price
                * 100,
            )

        routes.append({
            "origin": origin,
            "destination": destination,

            "best_price": best["price"],
            "currency": best.get("currency") or "TWD",
            "passengers": _safe_int(best.get("passengers")) or 1,

            "best_departure_date": best.get("departure_date"),
            "best_return_date": best.get("return_date"),
            "best_stay_days": _safe_int(best.get("stay_days")),

            # 相容舊欄位
            "airline": best.get("airline") or None,
            "dep_time": best.get("departure") or None,
            "arr_time": best.get("arrival") or None,
            "duration": best.get("duration") or None,
            "stops": _safe_int(best.get("stops")),

            # 新欄位：同價選擇
            "flight_options": options,
            "same_price_option_count": len(options),

            "recent_average_price": round(avg_price),
            "diff_percent": round(diff_percent, 1),

            "last_checked_at": best.get("checked_at"),
            "sample_size": len(rows),
        })

    routes.sort(
        key=lambda r: r["diff_percent"],
        reverse=True,
    )

    total = len(routes)

    for i, r in enumerate(routes):
        if total <= 1:
            r["cheap_score"] = 100
        else:
            r["cheap_score"] = round(
                100 - (i / (total - 1)) * 20
            )

    return routes


# ============================================================
# 主程式
# ============================================================

def main():
    print("=== Flight crawler start ===")

    ensure_dirs()
    cfg = load_config()

    tasks = generate_search_tasks(cfg)

    if not tasks:
        print("沒有候選搜尋組合")
        return

    state = load_state()
    start = int(state.get("next_index", 0))

    if start >= len(tasks):
        start = 0

    batch = int(cfg.get("max_checks_per_run", 50))

    selected = tasks[start:start + batch]

    if len(selected) < batch:
        selected += tasks[:batch - len(selected)]

    new_rows = []

    print(
        f"候選組合總數：{len(tasks)} | "
        f"本次從 index {start} 開始查 {len(selected)} 組"
    )

    for idx, task in enumerate(selected, start=1):
        print(
            f"[{idx}/{len(selected)}] "
            f"{task['origin']} -> {task['destination']} "
            f"{task['departure']} ~ {task['return']} "
            f"(停留 {task['stay_days']} 天)"
        )

        result = search_one(task, cfg)

        if result:
            new_rows.append(result)

    next_index = (start + len(selected)) % len(tasks)
    save_state(next_index)

    history = load_history()
    history.extend(new_rows)

    if not history:
        print(
            "本次沒有任何成功查詢結果；"
            "不更新 history.csv / latest.json"
        )
        return

    save_history(history)

    history_days = int(cfg.get("history_days", 60))

    route_rankings = build_route_rankings(
        history,
        history_days=history_days,
    )

    latest = {
        "generated_at": datetime.datetime.now().isoformat(),
        "adults": int(cfg.get("adults", 1)),
        "history_days": history_days,
        "checked_this_run": len(new_rows),
        "total_checks_so_far": len(history),
        "total_combos_in_grid": len(tasks),
        "routes": route_rankings,
    }

    with open(
        LATEST_JSON,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            latest,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=== 完成 ===")
    print(f"本次成功取得：{len(new_rows)} 筆")
    print(f"歷史累積：{len(history)} 筆")
    print(f"下一次從 index：{next_index}")
    print(f"已更新：{HISTORY_CSV}")
    print(f"已更新：{LATEST_JSON}")
    print(f"已更新：{STATE_JSON}")


if __name__ == "__main__":
    main()
