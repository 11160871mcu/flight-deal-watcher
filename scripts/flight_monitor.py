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


# ============================================================
# STATE SIGNATURE
# 用來判斷搜尋條件（目的地、天數範圍、艙等…）有沒有變過。
# 只要變了，舊游標的意義就不成立，直接重開一輪。
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
# 方案 B：固定起始日（epoch）的游標系統
#
# 舊設計每天用「今天」當清單起點重建整份清單，清單每天都在
# 位移，游標得靠一個「shift 補償公式」去猜「這個位置昨天對應
# 到哪一天」，只要漏跑一次、月份長度不同，或補償算錯，游標就
# 會被侵蝕、原地踏步，甚至跳號漏掉一批日期。
#
# 方案 B 改成：
#   - epoch_date 只在「第一次執行」時寫入一次，之後永遠不變。
#     不管今天是哪一天，第 0 天永遠指向同一個日期。
#   - 每個 (day_offset, stay) 組合在虛擬清單裡的 index，由
#     epoch_date 決定，是固定的，只會因為視窗右端往後延伸而
#     變長，不會因為「今天」往前挪而整份重新洗牌。
#   - 游標 cursor 是「這條航線、這個軌道，累計挑過或跳過幾
#     組」的總數，只會 += ，永遠不會被按天數往回扣。
#   - 出發日已經過去（永遠不會再有效）的組合，掃描到時直接
#     跳過，游標照樣 +1 往前，不會回頭重新檢查，也不會被罰分。
#   - 就算哪一輪完全沒跑到（workflow 沒觸發、失敗…），游標
#     停在原地不動，下次接著跑就好，不會因為「經過了幾天沒
#     跑」而被扣分或需要特別處理。
# ============================================================

def new_state(
    cfg: dict[str, Any],
    today: dt.date,
    routes: list[str],
) -> dict[str, Any]:

    return {
        "version": 6,
        "grid_signature": signature(cfg),
        "epoch_date": today.isoformat(),
        "near_cursor_by_route": {route: 0 for route in routes},
        "annual_cursor_by_route": {route: 0 for route in routes},
        "near_rotation": 0,
        "annual_rotation": 0,
        "total_attempts": 0,
    }


def load_state(
    cfg: dict[str, Any],
    path: Path,
    today: dt.date,
    routes: list[str],
) -> dict[str, Any]:

    fresh = new_state(cfg, today, routes)

    if not path.exists():
        return fresh

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fresh

    # 版本或搜尋條件不符 -> 舊游標邏輯已經不成立，重新開始一輪。
    if state.get("version") != 6 or state.get("grid_signature") != signature(cfg):
        return fresh

    # epoch_date 一旦寫入就不再更動。
    try:
        epoch_date = dt.date.fromisoformat(
            state.get("epoch_date", today.isoformat())
        )
    except Exception:
        epoch_date = today

    state["epoch_date"] = epoch_date.isoformat()

    for state_name in ("near_cursor_by_route", "annual_cursor_by_route"):
        state.setdefault(state_name, {})
        for route in routes:
            state[state_name].setdefault(route, 0)

    state.setdefault("near_rotation", 0)
    state.setdefault("annual_rotation", 0)
    state.setdefault("total_attempts", 0)

    return state


# ============================================================
# FAIR QUOTA（各目的地公平配額，跟方案 B 無關，維持原邏輯）
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
# 方案 B：從固定 epoch 起算的虛擬清單裡挑任務
#
# 清單不會被實際建成一份 Python list（那樣跑久了會佔用越來越
# 多記憶體），而是用公式隨算隨用：
#     index = day_offset * 停留天數種類數 + stay 序號
# 邊界永遠是 [epoch_date, window_end]。window_end（近期軌是
# 今天+90天、全年軌是今天+12個月）會隨「今天」往前推進而跟著
# 延伸，但起點 epoch_date 永遠固定 —— 所以同一個 index 永遠對
# 應同一組 (day_offset, stay)，不會被重新洗牌。
#
# 游標 cursor 是「這條航線這個軌道，累計挑過/跳過幾組」的總
# 數，只會累加，從不倒退、從不被按天數扣分。
# ============================================================

def pick_from_epoch_pool(
    cfg: dict[str, Any],
    route: str,
    cursor: int,
    quota: int,
    epoch_date: dt.date,
    today: dt.date,
    window_end: dt.date,
    used: set[tuple[str, str, str, str, int]],
    track: str,
) -> tuple[list[dict[str, Any]], int]:

    if quota <= 0:
        return [], cursor

    stay_list = stays(cfg)
    n = len(stay_list)

    total_days = (window_end - epoch_date).days + 1

    if total_days <= 0 or n <= 0:
        return [], cursor

    pool_len = total_days * n

    origin, destination = route.split("|")

    chosen: list[dict[str, Any]] = []

    idx = cursor % pool_len
    scanned = 0

    while len(chosen) < quota and scanned < pool_len:

        day_offset, stay_idx = divmod(idx, n)
        departure = epoch_date + dt.timedelta(days=day_offset)

        idx = (idx + 1) % pool_len
        scanned += 1

        # 出發日已經過去，這組永遠不會再有效，直接永久跳過，
        # 游標照樣往前走，不會回頭重新檢查這組。
        if departure < today or departure > window_end:
            continue

        stay = stay_list[stay_idx]
        return_date = departure + dt.timedelta(days=stay)

        key = (
            origin,
            destination,
            departure.isoformat(),
            return_date.isoformat(),
            stay,
        )

        # 避免近期軌與全年軌在同一次執行查到同一組日期
        if key in used:
            continue

        used.add(key)

        chosen.append(
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure.isoformat(),
                "return_date": return_date.isoformat(),
                "stay_days": stay,
                "track": track,
            }
        )

    new_cursor = cursor + scanned
    return chosen, new_cursor


# ============================================================
# DUAL TRACK 批次
# ============================================================

def build_dual_track_batch(
    cfg: dict[str, Any],
    state: dict[str, Any],
    today: dt.date,
):

    epoch_date = dt.date.fromisoformat(state["epoch_date"])

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

        near_cursor = int(state["near_cursor_by_route"].get(route, 0))
        chosen, new_cursor = pick_from_epoch_pool(
            cfg, route, near_cursor, near_quota[route],
            epoch_date, today, near_end, used, "near",
        )
        near_selected[route] = chosen
        state["near_cursor_by_route"][route] = new_cursor

        annual_cursor = int(state["annual_cursor_by_route"].get(route, 0))
        chosen, new_cursor = pick_from_epoch_pool(
            cfg, route, annual_cursor, annual_quota[route],
            epoch_date, today, annual_end, used, "annual",
        )
        annual_selected[route] = chosen
        state["annual_cursor_by_route"][route] = new_cursor

    batch: list[dict[str, Any]] = []

    maximum = max(
        max(len(near_selected[route]), len(annual_selected[route]))
        for route in routes
    )

    # 交錯執行：NRT 近期 -> NRT 全年 -> HND 近期 -> HND 全年 ...
    for index in range(maximum):
        for route in routes:
            if index < len(near_selected[route]):
                batch.append(near_selected[route][index])
            if index < len(annual_selected[route]):
                batch.append(annual_selected[route][index])

    # 這裡的「全年網格」只是給網頁顯示用的統計數字，代表「此刻」
    # 全年視窗內實際有效的組合數，跟游標內部用來算 index 的虛擬
    # 清單（會一直往後長，且含已過期的舊日期）是兩件不同的事。
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

    # 很重要：不把 US$220 誤當成 NT$220
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
        # 有價格但完全沒 metadata 仍保留價格，但不偽造航空公司
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

        key = (
            row["origin"],
            row["destination"],
            row["departure_date"],
            row["return_date"],
            stay,
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
# NTFY 推播（Price Drop 專用）
# ============================================================

NTFY_SERVER = "https://ntfy.sh"


def format_drop_line(deal: dict[str, Any]) -> str:

    route = f"{deal['origin']}→{deal['destination']}"
    dates = f"{deal['departure_date']} ~ {deal['return_date']}"

    drop_percent = deal.get("price_drop_percent")
    drop_text = f"↓{drop_percent}%" if drop_percent is not None else ""

    previous_price = deal.get("previous_price")
    previous_text = f"，原 NT${previous_price:,}" if previous_price else ""

    price = int(deal["price"])

    return f"{route} {dates}｜NT${price:,} {drop_text}{previous_text}"


def send_ntfy_notification(deals: list[dict[str, Any]], cfg: dict[str, Any]) -> None:

    topic = os.environ.get("NTFY_TOPIC", "").strip()

    if not topic:
        print("  未設定 NTFY_TOPIC，略過推播")
        return

    drops = [deal for deal in deals if deal.get("price_drop")]

    if not drops:
        print("  本輪沒有偵測到突然降價，不發送推播")
        return

    drops.sort(key=lambda deal: -(deal.get("price_drop_percent") or 0))

    max_items = int(cfg.get("notify_max_items", 10))
    shown = drops[:max_items]
    remaining = len(drops) - len(shown)

    lines = [format_drop_line(deal) for deal in shown]

    if remaining > 0:
        lines.append(f"...等其餘 {remaining} 筆")

    payload: dict[str, Any] = {
        "topic": topic,
        "title": f"✈️ 偵測到 {len(drops)} 個航班突然降價",
        "message": "\n".join(lines),
        "tags": ["airplane", "moneybag"],
        "priority": 4,
    }

    site_url = str(cfg.get("site_url") or "").strip()

    if site_url:
        payload["click"] = site_url

    try:
        response = requests.post(NTFY_SERVER, json=payload, timeout=10)
        response.raise_for_status()
        print(f"  已推播 {len(drops)} 筆降價通知")
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

    print("=== Flight Deal Watcher / 雙軌搜尋（方案 B：固定 epoch 游標） ===")
    print(f"epoch_date（固定起始日）：{state['epoch_date']}")
    print(f"全年：{today} -> {annual_end}")
    print(f"近期：{today} -> {near_end}")
    print(f"停留：{min(stays(cfg))}～{max(stays(cfg))} 天")
    print(f"全年網格（此刻有效組合數）：{total_grid:,} 組")
    print(
        "本輪：近期 "
        f"{sum(near_quota.values())} + 全年 {sum(annual_quota.values())} = {len(batch)} 組"
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

    append_history(history_path, successful_rows)

    history = load_history(history_path)

    latest = build_latest(
        cfg, history, today, annual_end, near_end, state,
        total_grid, near_quota, annual_quota,
        len(successful_rows), len(batch),
    )

    save_json(latest_path, latest)
    save_json(state_path, state)

    send_ntfy_notification(latest["deals"], cfg)

    print("=== 完成 ===")
    print(f"本次嘗試：{len(batch)}")
    print(f"成功取得價格：{len(successful_rows)}")
    print(f"網站 deals：{len(latest['deals'])}")


if __name__ == "__main__":
    main()