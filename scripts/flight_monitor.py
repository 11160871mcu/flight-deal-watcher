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
    return (origin, destination, departure_date, return_date, int(stay_days))


# ============================================================
# STATE SIGNATURE
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
# 游標：直接記錄「實際日期 + 第幾種停留天數」
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
# FAIR QUOTA
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
#
# 重要修正：除了回傳「這一批選中的任務」跟「跑完整批後游標會停在
# 哪」之外，這裡也回傳每一筆任務被選中『當下』的游標快照
# (cursor_after)。如果這一輪執行到一半因為連續失敗而提前中止，
# main() 會用這些快照，把游標精準地retreat 回「實際上真的有嘗試
# 查詢」的那個位置，而不是繼續讓游標停在「本來計畫要查、但根本
# 沒機會執行」的更遠位置。這樣游標才不會騙自己說已經查到很後面，
# 但資料其實完全是空的。
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:

    if quota <= 0:
        return [], [], cursor

    stay_list = stays(cfg)
    n = len(stay_list)

    if n <= 0:
        return [], [], cursor

    origin, destination = route.split("|")

    try:
        date = dt.date.fromisoformat(cursor.get("date", today.isoformat()))
    except Exception:
        date = today

    stay_idx = int(cursor.get("stay_index", 0)) % n

    if date < today:
        date = today
        stay_idx = 0

    chosen: list[dict[str, Any]] = []
    cursor_after: list[dict[str, Any]] = []

    total_days = max(1, (window_end - today).days + 1)
    max_scan = total_days * n + n

    scanned = 0

    while len(chosen) < quota and scanned < max_scan:

        if date > window_end:
            date = today
            stay_idx = 0

        stay = stay_list[stay_idx]
        return_date = date + dt.timedelta(days=stay)

        key = deal_identity(
            origin, destination, date.isoformat(), return_date.isoformat(), stay
        )

        will_append = key not in used
        if will_append:
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

        if will_append:
            # 這是「如果只做到剛剛這一筆」游標應該停在哪的快照
            cursor_after.append({"date": date.isoformat(), "stay_index": stay_idx})

    new_cursor = {"date": date.isoformat(), "stay_index": stay_idx}
    return chosen, cursor_after, new_cursor


# ============================================================
# DUAL TRACK 批次
#
# 這裡不再直接把游標寫進 state（那樣一旦這一輪中途被迫中止，
# 游標就會虛報進度）。改成先把「規劃結果」整理好回傳給 main()，
# 包含每個 (route, track) 的原始起點游標、每一筆的快照、跟
# 「跑完整批」時的最終游標，等 main() 真正執行完（或提前中止）
# 之後，再依照「實際跑到第幾筆」去決定要採用哪個游標值。
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

    # plan[(route, track)] = {
    #   "start_cursor": 這一輪開始前的游標,
    #   "cursor_after": [每一筆選中任務對應的游標快照],
    #   "final_cursor": 整批都跑完時最終會停在哪,
    # }
    plan: dict[tuple[str, str], dict[str, Any]] = {}

    near_selected: dict[str, list[dict[str, Any]]] = {}
    annual_selected: dict[str, list[dict[str, Any]]] = {}

    for route in routes:

        near_start = dict(state["near_cursor_by_route"][route])
        chosen, cursor_after, final_cursor = pick_from_cursor(
            cfg, route, near_start, near_quota[route],
            today, near_end, used, "near",
        )
        near_selected[route] = chosen
        plan[(route, "near")] = {
            "start_cursor": near_start,
            "cursor_after": cursor_after,
            "final_cursor": final_cursor,
        }

        annual_start = dict(state["annual_cursor_by_route"][route])
        chosen, cursor_after, final_cursor = pick_from_cursor(
            cfg, route, annual_start, annual_quota[route],
            today, annual_end, used, "annual",
        )
        annual_selected[route] = chosen
        plan[(route, "annual")] = {
            "start_cursor": annual_start,
            "cursor_after": cursor_after,
            "final_cursor": final_cursor,
        }

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

    return batch, plan, near_quota, annual_quota, total_grid, annual_end, near_end


def commit_cursors(
    state: dict[str, Any],
    plan: dict[tuple[str, str], dict[str, Any]],
    executed_batch: list[dict[str, Any]],
) -> None:
    """
    根據『這一輪實際真正執行到的任務』，幫每個 (route, track) 決定
    游標最終該停在哪裡。如果整批都順利跑完，這裡的結果會跟原本
    plan 裡的 final_cursor 一致；如果中途被迫提前中止，這裡會讓
    每個 (route, track) 的游標，精準地停在「最後一筆真的有被執行
    到」的那個位置，不會虛報成『整批都做完了』。
    """

    executed_count: dict[tuple[str, str], int] = defaultdict(int)

    for task in executed_batch:
        route = route_key(task["origin"], task["destination"])
        executed_count[(route, task["track"])] += 1

    for (route, track), info in plan.items():

        count = executed_count.get((route, track), 0)
        cursor_after = info["cursor_after"]

        if count <= 0:
            new_cursor = info["start_cursor"]
        elif count >= len(cursor_after):
            new_cursor = info["final_cursor"]
        else:
            new_cursor = cursor_after[count - 1]

        target_key = "near_cursor_by_route" if track == "near" else "annual_cursor_by_route"
        state[target_key][route] = new_cursor


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
    scan_progress, aborted_early, executed_this_run,
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
        "executed_this_run": executed_this_run,
        "aborted_early": aborted_early,
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
        print("  本輪沒有『這輪剛查到、又降價、又真的划算』的組合，不發送推播")
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
        batch, plan, near_quota, annual_quota,
        total_grid, annual_end, near_end,
    ) = build_dual_track_batch(cfg, state, today)

    print("=== Flight Deal Watcher / 雙軌搜尋（日期游標，含連續失敗保護） ===")
    print(f"今天：{today}")
    print(f"全年：{today} -> {annual_end}")
    print(f"近期：{today} -> {near_end}")
    print(f"停留：{min(stays(cfg))}～{max(stays(cfg))} 天")
    print(f"全年網格（此刻有效組合數）：{total_grid:,} 組")
    print(
        "本輪計畫：近期 "
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
    executed_batch: list[dict[str, Any]] = []

    delay = float(cfg.get("request_delay_seconds", 0.0))

    # 連續失敗自動中止：一旦連續失敗次數達到門檻，判定為「疑似被
    # Google Flights 暫時限制/封鎖」，立刻停止這一輪剩下的查詢，
    # 避免對著已經被擋的服務繼續送出更多注定失敗的請求，也避免
    # 讓游標虛報成『整批都查完了』。
    consecutive_failure_stop = int(cfg.get("consecutive_failure_stop", 15))
    consecutive_failures = 0
    aborted_early = False

    for index, task in enumerate(batch, 1):

        print(
            f"[{index}/{len(batch)}] {task['track']:6s} "
            f"{task['origin']}->{task['destination']} "
            f"{task['departure_date']}~{task['return_date']} {task['stay_days']}d"
        )

        row = search_one(task, cfg)

        state["total_attempts"] = int(state.get("total_attempts", 0)) + 1
        executed_batch.append(task)

        if row:
            successful_rows.append(row)
            consecutive_failures = 0
            print(
                "  OK "
                f"NT${row['price']:,} / options="
                f"{len(parse_options(row['flight_options_json']))}"
            )
        else:
            consecutive_failures += 1
            print(f"  NO VALID RESULT（連續失敗 {consecutive_failures} 次）")

        if consecutive_failures >= consecutive_failure_stop:
            aborted_early = True
            print(
                f"  ⚠️ 連續失敗達到 {consecutive_failure_stop} 次，"
                "判定為疑似遭遇暫時限制/封鎖，提前中止本輪剩餘查詢。"
            )
            break

        if delay > 0 and index < len(batch):
            time.sleep(delay)

    # 依照『這一輪實際真正執行到的任務』，把游標精準地設到正確位置。
    # 就算中途提前中止，也不會有任何一條航線的游標被虛報成『查完了』。
    commit_cursors(state, plan, executed_batch)

    fresh_keys = {
        deal_identity(
            row["origin"], row["destination"],
            row["departure_date"], row["return_date"], row["stay_days"],
        )
        for row in successful_rows
    }

    append_history(history_path, successful_rows)

    history = load_history(history_path)

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

    latest = build_latest(
        cfg, history, today, annual_end, near_end, state,
        total_grid, near_quota, annual_quota,
        len(successful_rows), len(batch),
        scan_progress, aborted_early, len(executed_batch),
    )

    save_json(latest_path, latest)
    save_json(state_path, state)

    send_ntfy_notification(latest["deals"], cfg, fresh_keys)

    print("=== 完成 ===")
    print(f"本次計畫：{len(batch)}　實際執行：{len(executed_batch)}")
    print(f"成功取得價格：{len(successful_rows)}")
    print(f"提前中止：{'是' if aborted_early else '否'}")
    print(f"網站 deals：{len(latest['deals'])}")


if __name__ == "__main__":
    main()