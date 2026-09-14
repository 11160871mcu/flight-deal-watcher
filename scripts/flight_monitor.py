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

# 通知用的機場中文名，跟 index.html 裡的 AIRPORTS 對照表一致，
# 讓 ntfy 通知跟網頁卡片看起來是同一套語言，不用自己翻譯機場代碼。
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
# 方案 B：固定起始日（epoch）的游標系統
#
# epoch_date 只在第一次建立 state 時寫入一次，之後永遠不變。
# 每個 (day_offset, stay) 組合在虛擬清單裡的 index 只跟 epoch_date
# 有關，不會因為「今天」往前推進而被重新洗牌。游標只會累加，永遠
# 不會被按天數往回扣分，也不會因為漏跑一輪而受罰。
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
) -> tuple[dict[str, Any], bool]:
    """
    回傳 (state, was_reset)。
    was_reset 代表這次是「全新建立 / 因版本或搜尋條件不符而重置」的
    state，讓 main() 知道要不要嘗試用既有 history.csv 幫游標抓一個
    比較好的起跑點（bootstrap），避免升版後把已經查過的近期日期
    整批重新掃一輪，讓人誤以為「游標卡住沒有前進」。
    """

    fresh = new_state(cfg, today, routes)

    if not path.exists():
        return fresh, True

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fresh, True

    if state.get("version") != 6 or state.get("grid_signature") != signature(cfg):
        return fresh, True

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

    return state, False


def bootstrap_cursors_from_history(
    cfg: dict[str, Any],
    state: dict[str, Any],
    history_rows: list[dict[str, Any]],
    routes: list[str],
    epoch_date: dt.date,
    near_end: dt.date,
    annual_end: dt.date,
) -> None:
    """
    只在『state.json 剛被升版 / 重置』的那一次執行。

    利用既有 history.csv 裡「這條航線目前已經查過的最遠出發日」，
    幫新的固定 epoch 游標抓一個合理的起跑點，這樣升版之後不會把
    已經查過的近期日期整批重新掃一輪，才不會讓使用者誤以為『游標
    好像卡住沒有前進』。

    就算估計得不夠精準也沒關係——之後游標一樣只增不減，會繼續
    照原本的規則往前走，不會漏天，也不會被扣分，這裡純粹是避免
    『重複做白工』而已。
    """

    stay_list = stays(cfg)
    n = len(stay_list)

    if n <= 0:
        return

    for route in routes:

        origin, destination = route.split("|")

        near_max_offset = -1
        annual_max_offset = -1

        for row in history_rows:

            if row.get("origin") != origin or row.get("destination") != destination:
                continue

            try:
                departure = dt.date.fromisoformat(str(row["departure_date"]))
            except Exception:
                continue

            if departure < epoch_date:
                continue

            offset = (departure - epoch_date).days

            if departure <= near_end and offset > near_max_offset:
                near_max_offset = offset

            if departure <= annual_end and offset > annual_max_offset:
                annual_max_offset = offset

        if near_max_offset >= 0:
            state["near_cursor_by_route"][route] = (near_max_offset + 1) * n

        if annual_max_offset >= 0:
            state["annual_cursor_by_route"][route] = (annual_max_offset + 1) * n


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
# 方案 B：從固定 epoch 起算的虛擬清單裡挑任務
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
# NTFY 推播（Price Drop 專用，且要「真的便宜」才通知）
# ============================================================

NTFY_SERVER = "https://ntfy.sh"


def airport_label(code: str) -> str:
    name = AIRPORT_NAMES.get(code, code)
    return f"{name}({code})"


def format_date_range(departure: str, return_date: str, stay_days: int) -> str:
    try:
        d = dt.date.fromisoformat(departure)
        r = dt.date.fromisoformat(return_date)
        return f"{d.month}/{d.day} ~ {r.month}/{r.day}（停留{stay_days}天）"
    except Exception:
        return f"{departure} ~ {return_date}（停留{stay_days}天）"


def format_drop_line(deal: dict[str, Any]) -> str:

    route = f"{airport_label(deal['origin'])} → {airport_label(deal['destination'])}"

    dates = format_date_range(
        deal["departure_date"], deal["return_date"], deal["stay_days"]
    )

    price = int(deal["price"])

    drop_percent = deal.get("price_drop_percent")
    drop_text = f"剛降價 {drop_percent}%" if drop_percent is not None else ""

    previous_price = deal.get("previous_price")
    previous_text = f"（原 NT${previous_price:,}）" if previous_price else ""

    score = deal.get("cheap_score")
    score_text = f" · 便宜指數 {score}/100" if score is not None else ""

    return (
        f"{route}\n"
        f"{dates}\n"
        f"NT${price:,} {drop_text}{previous_text}{score_text}"
    )


def send_ntfy_notification(deals: list[dict[str, Any]], cfg: dict[str, Any]) -> None:

    topic = os.environ.get("NTFY_TOPIC", "").strip()

    if not topic:
        print("  未設定 NTFY_TOPIC，略過推播")
        return

    # 只通知「真的划算」的降價：
    # 1. price_drop：跟自己上一次比，跌幅有達到門檻
    # 2. cheap_score >= notify_min_cheap_score：跟同航線歷史價格
    #    池比，這個價格本身也算便宜，不是「跌了但還是貴」。
    min_cheap_score = int(cfg.get("notify_min_cheap_score", 70))

    drops = [
        deal
        for deal in deals
        if deal.get("price_drop") and int(deal.get("cheap_score") or 0) >= min_cheap_score
    ]

    if not drops:
        print(
            "  本輪沒有『既降價、又真的划算』的組合（可能有降價但仍偏貴），不發送推播"
        )
        return

    drops.sort(key=lambda deal: -(deal.get("price_drop_percent") or 0))

    max_items = int(cfg.get("notify_max_items", 10))
    shown = drops[:max_items]
    remaining = len(drops) - len(shown)

    lines = [format_drop_line(deal) for deal in shown]

    message = "\n\n".join(lines)

    if remaining > 0:
        message += f"\n\n...等其餘 {remaining} 筆划算的降價"

    payload: dict[str, Any] = {
        "topic": topic,
        "title": f"🔥 偵測到 {len(drops)} 個真正划算的降價航班",
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

    state, was_reset = load_state(cfg, state_path, today, routes)

    # 升版 / 重置後，用既有 history.csv 幫游標抓一個合理的起跑點，
    # 避免把已經查過的近期日期整批重新掃一輪，讓人誤以為卡住了。
    if was_reset:

        history_for_bootstrap = load_history(history_path)

        if history_for_bootstrap:

            epoch_date = dt.date.fromisoformat(state["epoch_date"])

            annual_end_for_bootstrap = today + relativedelta(
                months=int(cfg.get("search_months_ahead", 12))
            )
            near_end_for_bootstrap = min(
                annual_end_for_bootstrap,
                today + dt.timedelta(days=int(cfg.get("near_term_days", 90))),
            )

            bootstrap_cursors_from_history(
                cfg, state, history_for_bootstrap, routes,
                epoch_date, near_end_for_bootstrap, annual_end_for_bootstrap,
            )

            print(
                "  偵測到 state.json 升版重置，已利用既有 history.csv "
                "幫游標抓起跑點，避免重新掃描已查過的近期日期。"
            )
        else:
            print("  這是全新環境（沒有 history.csv 可用），游標從頭開始累積。")

    (
        batch, near_quota, annual_quota,
        total_grid, annual_end, near_end,
    ) = build_dual_track_batch(cfg, state, today)

    epoch_date = dt.date.fromisoformat(state["epoch_date"])
    n_stay = len(stays(cfg))

    near_pool_size = ((near_end - epoch_date).days + 1) * n_stay
    annual_pool_size = ((annual_end - epoch_date).days + 1) * n_stay

    near_cursor_avg = sum(state["near_cursor_by_route"].values()) / len(routes)
    annual_cursor_avg = sum(state["annual_cursor_by_route"].values()) / len(routes)

    scan_progress = {
        "epoch_date": state["epoch_date"],
        "near_pool_size_per_route": near_pool_size,
        "annual_pool_size_per_route": annual_pool_size,
        "near_cursor_avg": round(near_cursor_avg, 1),
        "annual_cursor_avg": round(annual_cursor_avg, 1),
        "near_laps_completed_avg": (
            round(near_cursor_avg / near_pool_size, 3) if near_pool_size else 0
        ),
        "annual_laps_completed_avg": (
            round(annual_cursor_avg / annual_pool_size, 3) if annual_pool_size else 0
        ),
        "near_cursor_by_route": dict(state["near_cursor_by_route"]),
        "annual_cursor_by_route": dict(state["annual_cursor_by_route"]),
    }

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
    print(
        "游標進度（只會增加，可用來驗證真的有在往前掃）：\n"
        f"  近期軌已繞 {scan_progress['near_laps_completed_avg']} 圈\n"
        f"  全年軌已繞 {scan_progress['annual_laps_completed_avg']} 圈"
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
        scan_progress,
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