from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import time

from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml

from dateutil.relativedelta import relativedelta

from fast_flights import (
    FlightData,
    Passengers,
    create_filter,
)

try:
    from fast_flights import get_flights_from_filter
except ImportError:
    from fast_flights.core import get_flights_from_filter


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"


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

# 通知 / 各種顯示用的機場中文名，跟 index.html 裡的 AIRPORTS 對照表一致。
AIRPORT_NAMES = {
    "TPE": "台北桃園",
    "NRT": "東京成田",
    "HND": "東京羽田",
    "KIX": "大阪關西",
    "FUK": "福岡",
    "CTS": "札幌新千歲",
    "OKA": "沖繩那霸",
    "ICN": "首爾仁川",
    "PUS": "釜山金海",
}


# ============================================================
# CONFIG
# ============================================================

def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def path_from_config(cfg: dict[str, Any], key: str) -> Path:
    path = ROOT / cfg["data"][key]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def stays(cfg: dict[str, Any]) -> list[int]:
    settings = cfg.get("stay_duration", {})
    minimum = int(settings.get("min_days", 5))
    maximum = int(settings.get("max_days", 15))
    step = int(settings.get("step_days", 1))
    return list(range(minimum, maximum + 1, step))


# ============================================================
# ROUTE KEY
# ============================================================

def route_key(origin: str, destination: str) -> str:
    return f"{origin}|{destination}"


def deal_identity(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str,
    stay_days: int,
) -> tuple[str, str, str, str, int]:
    """一組航班的唯一識別：出發地/目的地/去程/回程/停留天數。
    用來判斷『這組航班這一輪是不是真的有重新查過』。
    """
    return (origin, destination, departure_date, return_date, int(stay_days))


# ============================================================
# STATE SIGNATURE
# 只要目的地、天數範圍、艙等…這些搜尋條件變過，舊游標的意義就
# 不成立了，直接重新開始一輪。
# ============================================================

def signature(cfg: dict[str, Any]) -> str:
    content = {
        "origins": cfg.get("origins", []),
        "destinations": cfg.get("destinations", []),
        "months": cfg.get("search_months_ahead", 12),
        "near_days": cfg.get("near_term_days", 90),
        "stay": cfg.get("stay_duration", {}),
        "adults": cfg.get("adults", 1),
        "seat": cfg.get("seat", "economy"),
        "direct_only": cfg.get("direct_only", True),
    }
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


# ============================================================
# 游標直接記錄「實際日期 + 第幾種停留天數」
#
# state.json 裡每條航線、每個軌道的游標，就是一個真正的日期字串，
# 打開檔案就能直接看懂「現在掃到哪一天」，不需要任何額外換算。
#
# 規則單純：
#   - 游標日期 < 今天 -> 直接跳到今天（過期日期沒有意義，跳過
#     不扣分）
#   - 游標日期 > 搜尋視窗尾端 -> 繞回視窗開頭（今天），因為視窗
#     尾端本身就是「今天 + 90 天 / 今天 + 12 個月」，每次執行都
#     用當下的「今天」重新計算，所以搜尋範圍永遠是動態的、真正
#     的「未來一年」，不會被寫死。
#   - 其餘情況：照順序往下一個 (日期, 停留天數) 前進。
# ============================================================

def new_state(
    cfg: dict[str, Any],
    today: dt.date,
    routes: list[str],
) -> dict[str, Any]:

    return {
        "version": 7,
        "grid_signature": signature(cfg),
        "near_cursor_by_route": {
            route: {"date": today.isoformat(), "stay_index": 0}
            for route in routes
        },
        "annual_cursor_by_route": {
            route: {"date": today.isoformat(), "stay_index": 0}
            for route in routes
        },
        "near_rotation": 0,
        "annual_rotation": 0,
        "total_attempts": 0,
    }


def normalize_cursor(raw: Any, today: dt.date) -> dict[str, Any]:
    """確保游標欄位是合法的 {date, stay_index}，格式壞掉就視為
    從今天重新開始（不會整個 state 重置，只有這一條航線受影響）。
    """

    if isinstance(raw, dict) and "date" in raw:
        try:
            dt.date.fromisoformat(str(raw["date"]))
            stay_index = int(raw.get("stay_index", 0))
            return {"date": str(raw["date"]), "stay_index": max(0, stay_index)}
        except Exception:
            pass

    return {"date": today.isoformat(), "stay_index": 0}


def load_state(
    cfg: dict[str, Any],
    path: Path,
    today: dt.date,
    routes: list[str],
) -> dict[str, Any]:

    fresh = new_state(cfg, today, routes)

    if not path.exists():
        print("  state.json 不存在，從今天開始建立全新游標。")
        return fresh

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("  state.json 損毀，從今天開始建立全新游標。")
        return fresh

    if state.get("version") != 7 or state.get("grid_signature") != signature(cfg):
        print("  state.json 版本或搜尋條件不符，從今天開始建立全新游標。")
        return fresh

    for state_name in ("near_cursor_by_route", "annual_cursor_by_route"):
        state.setdefault(state_name, {})
        for route in routes:
            state[state_name][route] = normalize_cursor(
                state[state_name].get(route), today
            )

    state.setdefault("near_rotation", 0)
    state.setdefault("annual_rotation", 0)
    state.setdefault("total_attempts", 0)

    return state


# ============================================================
# FAIR QUOTA（各目的地公平配額）
# ============================================================

def fair_quotas(
    total: int,
    routes: list[str],
    rotation: int,
) -> tuple[dict[str, int], int]:

    base, extra = divmod(max(0, total), len(routes))
    quota = {route: base for route in routes}

    for i in range(extra):
        route = routes[(rotation + i) % len(routes)]
        quota[route] += 1

    next_rotation = (rotation + extra) % len(routes)
    return quota, next_rotation


# ============================================================
# 從目前游標日期往後挑任務
# ============================================================

def pick_from_cursor(
    cfg: dict[str, Any],
    route: str,
    cursor: dict[str, Any],
    quota: int,
    today: dt.date,
    window_end: dt.date,
    used: set[tuple[str, str, str, str, int]],
    track: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:

    if quota <= 0:
        return [], cursor

    stay_list = stays(cfg)
    n = len(stay_list)

    if n <= 0:
        return [], cursor

    origin, destination = route.split("|")

    try:
        date = dt.date.fromisoformat(cursor.get("date", today.isoformat()))
    except Exception:
        date = today

    stay_idx = int(cursor.get("stay_index", 0)) % n

    # 游標停在過去的日期 -> 沒有意義，直接跳到今天，不扣分也不用
    # 一天一天往前爬。
    if date < today:
        date = today
        stay_idx = 0

    chosen: list[dict[str, Any]] = []

    total_days = max(1, (window_end - today).days + 1)
    max_scan = total_days * n + n

    scanned = 0

    while len(chosen) < quota and scanned < max_scan:

        # 超過視窗尾端 -> 繞回視窗開頭（今天）重新開始一輪。
        # window_end 每次執行都是用當下的「今天」重新算出來的，
        # 所以這裡繞回去，搜尋範圍依然是動態的未來一年，不會被
        # 寫死在某個固定日期。
        if date > window_end:
            date = today
            stay_idx = 0

        stay = stay_list[stay_idx]
        return_date = date + dt.timedelta(days=stay)

        key = deal_identity(
            origin, destination, date.isoformat(), return_date.isoformat(), stay
        )

        if key not in used:
            used.add(key)
            chosen.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": date.isoformat(),
                    "return_date": return_date.isoformat(),
                    "stay_days": stay,
                    "track": track,
                }
            )

        stay_idx += 1
        if stay_idx >= n:
            stay_idx = 0
            date = date + dt.timedelta(days=1)

        scanned += 1

    new_cursor = {"date": date.isoformat(), "stay_index": stay_idx}
    return chosen, new_cursor


# ============================================================
# DUAL TRACK 批次
# ============================================================

def build_dual_track_batch(
    cfg: dict[str, Any],
    state: dict[str, Any],
    today: dt.date,
):

    annual_end = today + relativedelta(
        months=int(cfg.get("search_months_ahead", 12))
    )

    near_end = min(
        annual_end,
        today + dt.timedelta(days=int(cfg.get("near_term_days", 90))),
    )

    routes = [
        route_key(origin, destination)
        for origin in cfg["origins"]
        for destination in cfg["destinations"]
    ]

    near_quota, next_rotation = fair_quotas(
        int(cfg.get("near_checks_per_run", 96)),
        routes,
        int(state.get("near_rotation", 0)),
    )
    state["near_rotation"] = next_rotation

    annual_quota, next_rotation = fair_quotas(
        int(cfg.get("annual_checks_per_run", 192)),
        routes,
        int(state.get("annual_rotation", 0)),
    )
    state["annual_rotation"] = next_rotation

    used: set[tuple[str, str, str, str, int]] = set()

    near_selected: dict[str, list[dict[str, Any]]] = {}
    annual_selected: dict[str, list[dict[str, Any]]] = {}

    for route in routes:

        near_cursor = state["near_cursor_by_route"][route]
        chosen, new_cursor = pick_from_cursor(
            cfg, route, near_cursor, near_quota[route],
            today, near_end, used, "near",
        )
        near_selected[route] = chosen
        state["near_cursor_by_route"][route] = new_cursor

        annual_cursor = state["annual_cursor_by_route"][route]
        chosen, new_cursor = pick_from_cursor(
            cfg, route, annual_cursor, annual_quota[route],
            today, annual_end, used, "annual",
        )
        annual_selected[route] = chosen
        state["annual_cursor_by_route"][route] = new_cursor

    batch: list[dict[str, Any]] = []

    maximum = max(
        max(len(near_selected[route]), len(annual_selected[route]))
        for route in routes
    )

    for index in range(maximum):
        for route in routes:
            if index < len(near_selected[route]):
                batch.append(near_selected[route][index])
            if index < len(annual_selected[route]):
                batch.append(annual_selected[route][index])

    total_grid = (
        len(routes)
        * ((annual_end - today).days + 1)
        * len(stays(cfg))
    )

    return batch, near_quota, annual_quota, total_grid, annual_end, near_end


# ============================================================
# PRICE
# ============================================================

def parse_price(value: Any) -> int | None:

    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        return number if number >= 1000 else None

    text = str(value).strip()
    upper = text.upper()

    if not text:
        return None

    if any(phrase in upper for phrase in ("UNAVAILABLE", "CHECK PRICE")):
        return None

    if "NT$" not in upper and "TWD" not in upper:
        return None

    digits = re.sub(r"[^0-9]", "", text)

    if not digits:
        return None

    number = int(digits)
    return number if number >= 1000 else None


# ============================================================
# FAST-FLIGHTS
# ============================================================

def fetch_candidates(task: dict[str, Any], cfg: dict[str, Any]):

    kwargs = {
        "flight_data": [
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
        "trip": "round-trip",
        "seat": cfg.get("seat", "economy"),
        "passengers": Passengers(
            adults=int(cfg.get("adults", 1)),
            children=int(cfg.get("children", 0)),
            infants_in_seat=int(cfg.get("infants_in_seat", 0)),
            infants_on_lap=int(cfg.get("infants_on_lap", 0)),
        ),
    }

    if cfg.get("direct_only", True):
        kwargs["max_stops"] = 0

    search_filter = create_filter(**kwargs)

    result = get_flights_from_filter(search_filter, currency="TWD")

    flights = getattr(result, "flights", None) or []

    candidates = []

    for flight in flights:
        raw_price = getattr(flight, "price", None)
        price = parse_price(raw_price)

        if price is None:
            continue

        candidates.append(
            {
                "price": price,
                "price_raw": str(raw_price or ""),
                "airline": getattr(flight, "name", None),
                "departure": getattr(flight, "departure", None),
                "arrival": getattr(flight, "arrival", None),
                "duration": getattr(flight, "duration", None),
                "stops": getattr(flight, "stops", None),
            }
        )

    return candidates


# ============================================================
# SAME PRICE OPTIONS
# ============================================================

def option_key(option: dict[str, Any]):
    return tuple(
        str(option.get(key) or "")
        for key in ("airline", "departure", "arrival", "duration", "stops")
    )


def cheapest(candidates: list[dict[str, Any]]):

    if not candidates:
        return None, "", []

    lowest = min(int(item["price"]) for item in candidates)

    same_price = [item for item in candidates if int(item["price"]) == lowest]

    options = []
    seen = set()

    for item in same_price:
        if not any(item.get(key) for key in ("airline", "departure", "arrival")):
            continue

        option = {
            "airline": item.get("airline"),
            "departure": item.get("departure"),
            "arrival": item.get("arrival"),
            "duration": item.get("duration"),
            "stops": item.get("stops"),
        }

        key = option_key(option)

        if key in seen:
            continue

        seen.add(key)
        options.append(option)

    raw_price = next(
        (item["price_raw"] for item in same_price if item.get("price_raw")),
        f"NT${lowest}",
    )

    return lowest, raw_price, options


# ============================================================
# SEARCH ONE
# ============================================================

def search_one(task: dict[str, Any], cfg: dict[str, Any]):

    all_candidates = []

    retries = int(cfg.get("metadata_retry_count", 1))
    retry_delay = float(cfg.get("metadata_retry_delay_seconds", 1.0))

    for attempt in range(retries + 1):

        try:
            candidates = fetch_candidates(task, cfg)
            all_candidates.extend(candidates)
        except Exception as exc:
            print("  ERROR:", type(exc).__name__, str(exc))

        price, raw_price, options = cheapest(all_candidates)

        if price is not None and (options or attempt == retries):
            return {
                "origin": task["origin"],
                "destination": task["destination"],
                "departure_date": task["departure_date"],
                "return_date": task["return_date"],
                "stay_days": task["stay_days"],
                "price": price,
                "price_raw": raw_price,
                "currency": "TWD",
                "passengers": int(cfg.get("adults", 1)),
                "flight_options_json": json.dumps(
                    options, ensure_ascii=False, separators=(",", ":")
                ),
                "checked_at": dt.datetime.now(dt.timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),
            }

        if attempt < retries:
            time.sleep(retry_delay)

    return None


# ============================================================
# HISTORY
# ============================================================

def parse_options(raw: Any):
    try:
        value = raw if isinstance(raw, list) else json.loads(raw or "[]")
        if isinstance(value, list):
            return value
    except Exception:
        pass
    return []


def load_history(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def append_history(path: Path, rows: list[dict[str, Any]]):
    if not rows:
        return

    exists = path.exists() and path.stat().st_size > 0

    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS, extrasaction="ignore")

        if not exists:
            writer.writeheader()

        writer.writerows(rows)


# ============================================================
# HISTORY PRICE
# ============================================================

def row_price(row: dict[str, Any]):
    try:
        price = int(float(str(row.get("price", "0"))))
        currency = str(row.get("currency", "TWD")).upper()

        if price >= 1000 and currency == "TWD":
            return price
    except Exception:
        pass

    return None


def checked_time(raw: str):
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


# ============================================================
# MERGE OPTIONS
# ============================================================

def merge_options(first, second):
    result = []
    seen = set()

    for option in first + second:
        key = option_key(option)
        if key in seen:
            continue
        seen.add(key)
        result.append(option)

    return result


# ============================================================
# LATEST COMBOS
# ============================================================

def latest_combos(history, cfg, today, annual_end):

    valid_routes = {
        (origin, destination)
        for origin in cfg["origins"]
        for destination in cfg["destinations"]
    }

    valid_stays = set(stays(cfg))

    latest = {}
    previous_price_map = {}

    ordered_history = sorted(
        history,
        key=lambda row: checked_time(str(row.get("checked_at", ""))),
    )

    for row in ordered_history:

        price = row_price(row)
        if price is None:
            continue

        if (row.get("origin"), row.get("destination")) not in valid_routes:
            continue

        try:
            departure = dt.date.fromisoformat(str(row["departure_date"]))
            stay = int(row["stay_days"])
        except Exception:
            continue

        if departure < today or departure > annual_end or stay not in valid_stays:
            continue

        key = deal_identity(
            row["origin"], row["destination"],
            row["departure_date"], row["return_date"], stay,
        )

        options = parse_options(row.get("flight_options_json"))

        previous = latest.get(key)

        if previous:
            previous_price_map[key] = row_price(previous)

        if previous and row_price(previous) == price:
            options = merge_options(
                parse_options(previous.get("flight_options_json")), options
            )

        item = dict(row)
        item["price"] = price
        item["stay_days"] = stay
        item["flight_options_json"] = json.dumps(
            options, ensure_ascii=False, separators=(",", ":")
        )

        latest[key] = item

    return latest, previous_price_map


# ============================================================
# BUILD LATEST.JSON
# ============================================================

def build_latest(
    cfg, history, today, annual_end, near_end, state,
    total_grid, near_quota, annual_quota, run_success, attempted,
    scan_progress,
):

    current, previous_price_map = latest_combos(history, cfg, today, annual_end)

    price_drop_alert_percent = float(cfg.get("price_drop_alert_percent", 20))

    grouped = defaultdict(list)

    for key, row in current.items():
        grouped[route_key(row["origin"], row["destination"])].append((key, row))

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=int(cfg.get("history_days", 60))
    )

    min_samples = int(cfg.get("min_score_samples", 20))

    deals = []

    for rows in grouped.values():

        recent_prices = [
            int(row["price"])
            for _, row in rows
            if checked_time(str(row.get("checked_at", ""))) >= cutoff
        ]

        pool = recent_prices if recent_prices else [int(row["price"]) for _, row in rows]

        if not pool:
            continue

        average = sum(pool) / len(pool)

        for key, row in rows:

            price = int(row["price"])

            at_or_above = sum(1 for other_price in pool if other_price >= price)
            cheap_score = round(at_or_above / len(pool) * 100)

            diff_percent = ((average - price) / average * 100) if average else 0

            options = parse_options(row.get("flight_options_json"))

            previous_price = previous_price_map.get(key)

            price_drop_percent = None
            price_drop = False

            if previous_price and previous_price > 0:
                drop_percent = (previous_price - price) / previous_price * 100
                price_drop_percent = round(drop_percent, 1)

                if drop_percent >= price_drop_alert_percent:
                    price_drop = True

            deals.append(
                {
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "departure_date": row["departure_date"],
                    "return_date": row["return_date"],
                    "stay_days": int(row["stay_days"]),
                    "price": price,
                    "currency": "TWD",
                    "passengers": int(row.get("passengers") or 1),
                    "flight_options": options,
                    "same_price_option_count": len(options),
                    "recent_average_price": round(average),
                    "diff_percent": round(diff_percent, 1),
                    "cheap_score": cheap_score,
                    "score_sample_size": len(pool),
                    "score_reliable": len(pool) >= min_samples,
                    "previous_price": previous_price,
                    "price_drop_percent": price_drop_percent,
                    "price_drop": price_drop,
                    "checked_at": row.get("checked_at", ""),
                }
            )

    deals.sort(
        key=lambda deal: (
            0 if deal["price_drop"] else 1,
            -deal["cheap_score"],
            -deal["diff_percent"],
            deal["price"],
        )
    )

    limit = int(cfg.get("max_deals_to_publish", 3000))

    if limit > 0:
        deals = deals[:limit]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "search_start_date": today.isoformat(),
        "search_end_date": annual_end.isoformat(),
        "near_term_end_date": near_end.isoformat(),
        "origins": cfg["origins"],
        "destinations": cfg["destinations"],
        "adults": int(cfg.get("adults", 1)),
        "stay_min_days": min(stays(cfg)),
        "stay_max_days": max(stays(cfg)),
        "history_days": int(cfg.get("history_days", 60)),
        "checked_this_run": run_success,
        "attempted_this_run": attempted,
        "near_checks_planned": sum(near_quota.values()),
        "annual_checks_planned": sum(annual_quota.values()),
        "total_checks_so_far": int(state.get("total_attempts", 0)),
        "total_combos_in_grid": total_grid,
        "route_near_quota": near_quota,
        "route_annual_quota": annual_quota,
        "scan_progress": scan_progress,
        "deal_count": len(deals),
        "deals": deals,
    }


# ============================================================
# SAVE JSON
# ============================================================

def save_json(path: Path, data: Any):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# ============================================================
# NTFY 推播
#
# 重要修正：只通知「這一輪真的有重新查到資料」的組合。
#
# latest.json 裡的 price_drop 是拿『整份歷史紀錄裡，這組航班最新
# 兩筆資料』去比較算出來的，只要這組航班沒有被新資料覆蓋，這個
# 判斷結果就會一直維持不變。如果通知邏輯只看 latest.json 裡
# 「誰是 price_drop」，同一組航班只要一直沒被重新查過，就會一輪
# 又一輪被重複挑出來通知，即使根本沒有發生任何新事件。
#
# 修正做法：main() 執行完會知道「這一輪實際上查了哪些組合」
# （fresh_keys），通知只從這個集合裡篩選，確保每組航班只有在
# 「真的重新查到、而且判定為降價」的那一輪才會通知一次。
# ============================================================

NTFY_SERVER = "https://ntfy.sh"


def airport_label(code: str) -> str:
    name = AIRPORT_NAMES.get(code, code)
    return f"{name}({code})"


def format_date_range(departure: str, return_date: str, stay_days: int) -> str:
    try:
        d = dt.date.fromisoformat(departure)
        r = dt.date.fromisoformat(return_date)
        return f"{d.month}/{d.day} ~ {r.month}/{r.day}（停留 {stay_days} 天）"
    except Exception:
        return f"{departure} ~ {return_date}（停留 {stay_days} 天）"


def format_drop_line(deal: dict[str, Any]) -> str:
    """跟網頁卡片同樣的資訊、同樣的口吻，一段一行，不擠在一起。"""

    route = f"{airport_label(deal['origin'])} → {airport_label(deal['destination'])}"
    dates = format_date_range(
        deal["departure_date"], deal["return_date"], deal["stay_days"]
    )

    price = int(deal["price"])
    previous_price = deal.get("previous_price")
    drop_percent = deal.get("price_drop_percent")
    score = deal.get("cheap_score")

    lines = [route, dates, f"現在 NT${price:,}"]

    if previous_price and drop_percent is not None:
        lines.append(f"原價 NT${previous_price:,}，降了 {drop_percent}%")

    if score is not None:
        lines.append(f"便宜指數 {score}/100")

    return "\n".join(lines)


def send_ntfy_notification(
    deals: list[dict[str, Any]],
    cfg: dict[str, Any],
    fresh_keys: set[tuple[str, str, str, str, int]],
) -> None:

    topic = os.environ.get("NTFY_TOPIC", "").strip()

    if not topic:
        print("  未設定 NTFY_TOPIC，略過推播")
        return

    min_cheap_score = int(cfg.get("notify_min_cheap_score", 70))

    drops = [
        deal
        for deal in deals
        if deal.get("price_drop")
        and int(deal.get("cheap_score") or 0) >= min_cheap_score
        and deal_identity(
            deal["origin"], deal["destination"],
            deal["departure_date"], deal["return_date"], deal["stay_days"],
        ) in fresh_keys
    ]

    if not drops:
        print(
            "  本輪沒有『這輪剛查到、又降價、又真的划算』的組合，不發送推播"
        )
        return

    drops.sort(key=lambda deal: -(deal.get("price_drop_percent") or 0))

    max_items = int(cfg.get("notify_max_items", 10))
    shown = drops[:max_items]
    remaining = len(drops) - len(shown)

    message = "\n\n".join(format_drop_line(deal) for deal in shown)

    if remaining > 0:
        message += f"\n\n...等其餘 {remaining} 筆划算的降價"

    payload: dict[str, Any] = {
        "topic": topic,
        "title": f"🔥 偵測到 {len(drops)} 個又降價又划算的機票",
        "message": message,
        "tags": ["airplane", "moneybag"],
        "priority": 4,
    }

    site_url = str(cfg.get("site_url") or "").strip()

    if site_url:
        payload["click"] = site_url

    try:
        response = requests.post(NTFY_SERVER, json=payload, timeout=10)
        response.raise_for_status()
        print(f"  已推播 {len(drops)} 筆划算降價通知")
    except Exception as exc:
        print("  推播失敗（不影響資料更新）：", type(exc).__name__, str(exc))


# ============================================================
# MAIN
# ============================================================

def main():

    cfg = load_config()

    history_path = path_from_config(cfg, "history")
    latest_path = path_from_config(cfg, "latest")
    state_path = path_from_config(cfg, "state")

    timezone = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
    today = dt.datetime.now(timezone).date()

    routes = [
        route_key(origin, destination)
        for origin in cfg["origins"]
        for destination in cfg["destinations"]
    ]

    state = load_state(cfg, state_path, today, routes)

    (
        batch, near_quota, annual_quota,
        total_grid, annual_end, near_end,
    ) = build_dual_track_batch(cfg, state, today)

    near_dates = [
        dt.date.fromisoformat(v["date"])
        for v in state["near_cursor_by_route"].values()
    ]
    annual_dates = [
        dt.date.fromisoformat(v["date"])
        for v in state["annual_cursor_by_route"].values()
    ]

    scan_progress = {
        "near_cursor_min_date": min(near_dates).isoformat() if near_dates else None,
        "near_cursor_max_date": max(near_dates).isoformat() if near_dates else None,
        "annual_cursor_min_date": min(annual_dates).isoformat() if annual_dates else None,
        "annual_cursor_max_date": max(annual_dates).isoformat() if annual_dates else None,
        "near_cursor_by_route": {
            r: v["date"] for r, v in state["near_cursor_by_route"].items()
        },
        "annual_cursor_by_route": {
            r: v["date"] for r, v in state["annual_cursor_by_route"].items()
        },
    }

    print("=== Flight Deal Watcher / 雙軌搜尋（日期游標，動態未來一年） ===")
    print(f"今天：{today}")
    print(f"全年：{today} -> {annual_end}")
    print(f"近期：{today} -> {near_end}")
    print(f"停留：{min(stays(cfg))}～{max(stays(cfg))} 天")
    print(f"全年網格（此刻有效組合數）：{total_grid:,} 組")
    print(
        "本輪：近期 "
        f"{sum(near_quota.values())} + 全年 {sum(annual_quota.values())} = {len(batch)} 組"
    )
    print(
        "游標目前位置（可直接對照日期驗證有沒有往前走）：\n"
        f"  近期軌：{scan_progress['near_cursor_min_date']} ~ {scan_progress['near_cursor_max_date']}\n"
        f"  全年軌：{scan_progress['annual_cursor_min_date']} ~ {scan_progress['annual_cursor_max_date']}"
    )

    print("各目的地配額：")
    for route in routes:
        print(
            "  " + route.replace("|", " -> ")
            + "：近期 " + str(near_quota[route])
            + " / 全年 " + str(annual_quota[route])
        )

    successful_rows = []

    delay = float(cfg.get("request_delay_seconds", 0.0))

    for index, task in enumerate(batch, 1):

        print(
            f"[{index}/{len(batch)}] {task['track']:6s} "
            f"{task['origin']}->{task['destination']} "
            f"{task['departure_date']}~{task['return_date']} {task['stay_days']}d"
        )

        row = search_one(task, cfg)

        state["total_attempts"] = int(state.get("total_attempts", 0)) + 1

        if row:
            successful_rows.append(row)
            print(
                "  OK "
                f"NT${row['price']:,} / options="
                f"{len(parse_options(row['flight_options_json']))}"
            )
        else:
            print("  NO VALID RESULT")

        if delay > 0 and index < len(batch):
            time.sleep(delay)

    # 這一輪真的重新查到資料的組合，之後只有這些 key 有資格觸發通知，
    # 避免舊資料因為一直沒被重新查過，卻反覆被判定成「新降價」通知。
    fresh_keys = {
        deal_identity(
            row["origin"], row["destination"],
            row["departure_date"], row["return_date"], row["stay_days"],
        )
        for row in successful_rows
    }

    append_history(history_path, successful_rows)

    history = load_history(history_path)

    latest = build_latest(
        cfg, history, today, annual_end, near_end, state,
        total_grid, near_quota, annual_quota,
        len(successful_rows), len(batch),
        scan_progress,
    )

    save_json(latest_path, latest)
    save_json(state_path, state)

    send_ntfy_notification(latest["deals"], cfg, fresh_keys)

    print("=== 完成 ===")
    print(f"本次嘗試：{len(batch)}")
    print(f"成功取得價格：{len(successful_rows)}")
    print(f"網站 deals：{len(latest['deals'])}")


if __name__ == "__main__":
    main()