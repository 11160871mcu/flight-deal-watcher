from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import time

from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

    with CONFIG_PATH.open(
        "r",
        encoding="utf-8",
    ) as f:

        return yaml.safe_load(f) or {}


def path_from_config(
    cfg: dict[str, Any],
    key: str,
) -> Path:

    path = ROOT / cfg["data"][key]

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def stays(
    cfg: dict[str, Any],
) -> list[int]:

    settings = cfg.get(
        "stay_duration",
        {},
    )

    minimum = int(
        settings.get(
            "min_days",
            5,
        )
    )

    maximum = int(
        settings.get(
            "max_days",
            15,
        )
    )

    step = int(
        settings.get(
            "step_days",
            1,
        )
    )

    return list(
        range(
            minimum,
            maximum + 1,
            step,
        )
    )


# ============================================================
# TASK KEY
# ============================================================

def route_key(
    origin: str,
    destination: str,
) -> str:

    return f"{origin}|{destination}"


def task_key(
    task: dict[str, Any],
) -> tuple[str, str, str, str, int]:

    return (
        task["origin"],
        task["destination"],
        task["departure_date"],
        task["return_date"],
        int(task["stay_days"]),
    )


# ============================================================
# BUILD SEARCH GRID
# ============================================================

def build_tasks(
    cfg: dict[str, Any],
    start: dt.date,
    end: dt.date,
) -> dict[str, list[dict[str, Any]]]:

    result = {}

    stay_list = stays(cfg)

    for origin in cfg["origins"]:

        for destination in cfg["destinations"]:

            rk = route_key(
                origin,
                destination,
            )

            rows = []

            total_days = (
                end - start
            ).days

            for i in range(
                total_days + 1
            ):

                departure = (
                    start
                    + dt.timedelta(days=i)
                )

                for stay in stay_list:

                    return_date = (
                        departure
                        + dt.timedelta(
                            days=stay
                        )
                    )

                    rows.append(
                        {
                            "origin": origin,
                            "destination": destination,
                            "departure_date":
                                departure.isoformat(),
                            "return_date":
                                return_date.isoformat(),
                            "stay_days": stay,
                        }
                    )

            result[rk] = rows

    return result


# ============================================================
# STATE
# ============================================================

def signature(
    cfg: dict[str, Any],
) -> str:

    content = {

        "origins":
            cfg.get(
                "origins",
                [],
            ),

        "destinations":
            cfg.get(
                "destinations",
                [],
            ),

        "months":
            cfg.get(
                "search_months_ahead",
                12,
            ),

        "near_days":
            cfg.get(
                "near_term_days",
                90,
            ),

        "stay":
            cfg.get(
                "stay_duration",
                {},
            ),

        "adults":
            cfg.get(
                "adults",
                1,
            ),

        "seat":
            cfg.get(
                "seat",
                "economy",
            ),

        "direct_only":
            cfg.get(
                "direct_only",
                True,
            ),
    }

    raw = json.dumps(
        content,
        sort_keys=True,
        ensure_ascii=False,
    ).encode()

    return hashlib.sha256(
        raw
    ).hexdigest()[:20]


def new_state(
    cfg: dict[str, Any],
    today: dt.date,
    routes: list[str],
) -> dict[str, Any]:

    return {

        "version": 5,

        "grid_signature":
            signature(cfg),

        "grid_start_date":
            today.isoformat(),

        "near_cursor_by_route":
            {
                route: 0
                for route in routes
            },

        "annual_cursor_by_route":
            {
                route: 0
                for route in routes
            },

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

    fresh = new_state(
        cfg,
        today,
        routes,
    )

    if not path.exists():

        return fresh

    try:

        state = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return fresh

    if (
        state.get("version") != 5
        or
        state.get(
            "grid_signature"
        ) != signature(cfg)
    ):

        return fresh

    try:

        old_date = (
            dt.date.fromisoformat(
                state.get(
                    "grid_start_date",
                    today.isoformat(),
                )
            )
        )

        shift = (
            today - old_date
        ).days

    except Exception:

        shift = 0

    # 每天日期窗口會往前移。
    # 因此 cursor 也需要扣除已過期日期的組合。
    if shift:

        per_day = len(
            stays(cfg)
        )

        for state_name in (
            "near_cursor_by_route",
            "annual_cursor_by_route",
        ):

            cursor_map = (
                state.setdefault(
                    state_name,
                    {},
                )
            )

            for route in routes:

                value = int(
                    cursor_map.get(
                        route,
                        0,
                    )
                )

                if shift > 0:

                    value = max(
                        0,
                        value
                        -
                        shift * per_day,
                    )

                else:

                    value += (
                        abs(shift)
                        *
                        per_day
                    )

                cursor_map[route] = value

    state[
        "grid_start_date"
    ] = today.isoformat()

    for state_name in (
        "near_cursor_by_route",
        "annual_cursor_by_route",
    ):

        state.setdefault(
            state_name,
            {},
        )

        for route in routes:

            state[
                state_name
            ].setdefault(
                route,
                0,
            )

    return state


# ============================================================
# FAIR QUOTA
# ============================================================

def fair_quotas(
    total: int,
    routes: list[str],
    rotation: int,
) -> tuple[
    dict[str, int],
    int,
]:

    base, extra = divmod(
        max(
            0,
            total,
        ),
        len(routes),
    )

    quota = {
        route: base
        for route in routes
    }

    for i in range(extra):

        route = routes[
            (
                rotation + i
            )
            %
            len(routes)
        ]

        quota[route] += 1

    next_rotation = (
        rotation + extra
    ) % len(routes)

    return (
        quota,
        next_rotation,
    )


# ============================================================
# PICK TASKS
# ============================================================

def pick_by_route(
    pools:
        dict[
            str,
            list[
                dict[
                    str,
                    Any,
                ]
            ],
        ],
    cursors:
        dict[str, int],
    quotas:
        dict[str, int],
    used:
        set[
            tuple[
                str,
                str,
                str,
                str,
                int,
            ]
        ],
    track: str,
):

    chosen = {
        route: []
        for route in pools
    }

    for route, tasks in (
        pools.items()
    ):

        if not tasks:

            continue

        index = (
            int(
                cursors.get(
                    route,
                    0,
                )
            )
            %
            len(tasks)
        )

        scanned = 0

        while (
            len(
                chosen[route]
            )
            <
            quotas.get(
                route,
                0,
            )
            and
            scanned
            <
            len(tasks)
        ):

            task = tasks[index]

            index = (
                index + 1
            ) % len(tasks)

            scanned += 1

            key = task_key(
                task
            )

            # 避免近期軌與全年軌
            # 在同一次執行查同一組日期
            if key in used:

                continue

            item = dict(task)

            item[
                "track"
            ] = track

            chosen[
                route
            ].append(
                item
            )

            used.add(
                key
            )

        cursors[
            route
        ] = index

    return chosen


# ============================================================
# DUAL TRACK
# ============================================================

def build_dual_track_batch(
    cfg: dict[str, Any],
    state: dict[str, Any],
    today: dt.date,
):

    annual_end = (
        today
        +
        relativedelta(
            months=int(
                cfg.get(
                    "search_months_ahead",
                    12,
                )
            )
        )
    )

    near_end = min(

        annual_end,

        today
        +
        dt.timedelta(
            days=int(
                cfg.get(
                    "near_term_days",
                    90,
                )
            )
        ),
    )

    annual_pool = (
        build_tasks(
            cfg,
            today,
            annual_end,
        )
    )

    near_pool = (
        build_tasks(
            cfg,
            today,
            near_end,
        )
    )

    routes = list(
        annual_pool.keys()
    )

    near_quota, next_rotation = (
        fair_quotas(

            int(
                cfg.get(
                    "near_checks_per_run",
                    96,
                )
            ),

            routes,

            int(
                state.get(
                    "near_rotation",
                    0,
                )
            ),
        )
    )

    state[
        "near_rotation"
    ] = next_rotation

    annual_quota, next_rotation = (
        fair_quotas(

            int(
                cfg.get(
                    "annual_checks_per_run",
                    192,
                )
            ),

            routes,

            int(
                state.get(
                    "annual_rotation",
                    0,
                )
            ),
        )
    )

    state[
        "annual_rotation"
    ] = next_rotation

    used = set()

    near_selected = (
        pick_by_route(
            near_pool,
            state[
                "near_cursor_by_route"
            ],
            near_quota,
            used,
            "near",
        )
    )

    annual_selected = (
        pick_by_route(
            annual_pool,
            state[
                "annual_cursor_by_route"
            ],
            annual_quota,
            used,
            "annual",
        )
    )

    batch = []

    maximum = max(

        max(
            len(
                near_selected[
                    route
                ]
            ),
            len(
                annual_selected[
                    route
                ]
            ),
        )

        for route
        in routes
    )

    # 交錯執行：
    # NRT 近期 → NRT 全年 →
    # HND 近期 → HND 全年 ...
    for index in range(
        maximum
    ):

        for route in routes:

            if (
                index
                <
                len(
                    near_selected[
                        route
                    ]
                )
            ):

                batch.append(
                    near_selected[
                        route
                    ][index]
                )

            if (
                index
                <
                len(
                    annual_selected[
                        route
                    ]
                )
            ):

                batch.append(
                    annual_selected[
                        route
                    ][index]
                )

    total_grid = sum(
        len(rows)
        for rows
        in annual_pool.values()
    )

    return (
        batch,
        near_quota,
        annual_quota,
        total_grid,
        annual_end,
        near_end,
    )


# ============================================================
# PRICE
# ============================================================

def parse_price(
    value: Any,
) -> int | None:

    if value is None:

        return None

    if isinstance(
        value,
        (int, float),
    ) and not isinstance(
        value,
        bool,
    ):

        number = int(
            value
        )

        if number >= 1000:

            return number

        return None

    text = str(
        value
    ).strip()

    upper = (
        text.upper()
    )

    if not text:

        return None

    if any(
        phrase in upper
        for phrase in (
            "UNAVAILABLE",
            "CHECK PRICE",
        )
    ):

        return None

    # 很重要：
    # 不把 US$220 誤當成 NT$220
    if (
        "NT$"
        not in upper
        and
        "TWD"
        not in upper
    ):

        return None

    digits = re.sub(
        r"[^0-9]",
        "",
        text,
    )

    if not digits:

        return None

    number = int(
        digits
    )

    if number < 1000:

        return None

    return number


# ============================================================
# FAST-FLIGHTS
# ============================================================

def fetch_candidates(
    task: dict[str, Any],
    cfg: dict[str, Any],
):

    kwargs = {

        "flight_data": [

            FlightData(
                date=
                    task[
                        "departure_date"
                    ],
                from_airport=
                    task[
                        "origin"
                    ],
                to_airport=
                    task[
                        "destination"
                    ],
            ),

            FlightData(
                date=
                    task[
                        "return_date"
                    ],
                from_airport=
                    task[
                        "destination"
                    ],
                to_airport=
                    task[
                        "origin"
                    ],
            ),
        ],

        "trip":
            "round-trip",

        "seat":
            cfg.get(
                "seat",
                "economy",
            ),

        "passengers":
            Passengers(

                adults=int(
                    cfg.get(
                        "adults",
                        1,
                    )
                ),

                children=int(
                    cfg.get(
                        "children",
                        0,
                    )
                ),

                infants_in_seat=int(
                    cfg.get(
                        "infants_in_seat",
                        0,
                    )
                ),

                infants_on_lap=int(
                    cfg.get(
                        "infants_on_lap",
                        0,
                    )
                ),
            ),
    }

    if cfg.get(
        "direct_only",
        True,
    ):

        kwargs[
            "max_stops"
        ] = 0

    search_filter = (
        create_filter(
            **kwargs
        )
    )

    result = (
        get_flights_from_filter(
            search_filter,
            currency="TWD",
        )
    )

    flights = (
        getattr(
            result,
            "flights",
            None,
        )
        or
        []
    )

    candidates = []

    for flight in flights:

        raw_price = (
            getattr(
                flight,
                "price",
                None,
            )
        )

        price = (
            parse_price(
                raw_price
            )
        )

        if price is None:

            continue

        candidates.append(
            {
                "price":
                    price,

                "price_raw":
                    str(
                        raw_price
                        or
                        ""
                    ),

                "airline":
                    getattr(
                        flight,
                        "name",
                        None,
                    ),

                "departure":
                    getattr(
                        flight,
                        "departure",
                        None,
                    ),

                "arrival":
                    getattr(
                        flight,
                        "arrival",
                        None,
                    ),

                "duration":
                    getattr(
                        flight,
                        "duration",
                        None,
                    ),

                "stops":
                    getattr(
                        flight,
                        "stops",
                        None,
                    ),
            }
        )

    return candidates


# ============================================================
# SAME PRICE OPTIONS
# ============================================================

def option_key(
    option: dict[str, Any],
):

    return tuple(
        str(
            option.get(key)
            or
            ""
        )
        for key
        in (
            "airline",
            "departure",
            "arrival",
            "duration",
            "stops",
        )
    )


def cheapest(
    candidates:
        list[
            dict[
                str,
                Any,
            ]
        ],
):

    if not candidates:

        return (
            None,
            "",
            [],
        )

    lowest = min(
        int(
            item["price"]
        )
        for item
        in candidates
    )

    same_price = [

        item
        for item
        in candidates
        if
        int(
            item["price"]
        )
        ==
        lowest
    ]

    options = []

    seen = set()

    for item in same_price:

        # 有價格但完全沒 metadata
        # 仍然保留價格，
        # 但不偽造航空公司
        if not any(
            item.get(key)
            for key
            in (
                "airline",
                "departure",
                "arrival",
            )
        ):

            continue

        option = {

            "airline":
                item.get(
                    "airline"
                ),

            "departure":
                item.get(
                    "departure"
                ),

            "arrival":
                item.get(
                    "arrival"
                ),

            "duration":
                item.get(
                    "duration"
                ),

            "stops":
                item.get(
                    "stops"
                ),
        }

        key = option_key(
            option
        )

        if key in seen:

            continue

        seen.add(
            key
        )

        options.append(
            option
        )

    raw_price = next(

        (
            item[
                "price_raw"
            ]
            for item
            in same_price
            if item.get(
                "price_raw"
            )
        ),

        f"NT${lowest}",
    )

    return (
        lowest,
        raw_price,
        options,
    )


# ============================================================
# SEARCH ONE
# ============================================================

def search_one(
    task: dict[str, Any],
    cfg: dict[str, Any],
):

    all_candidates = []

    retries = int(
        cfg.get(
            "metadata_retry_count",
            1,
        )
    )

    retry_delay = float(
        cfg.get(
            "metadata_retry_delay_seconds",
            1.0,
        )
    )

    for attempt in range(
        retries + 1
    ):

        try:

            candidates = (
                fetch_candidates(
                    task,
                    cfg,
                )
            )

            all_candidates.extend(
                candidates
            )

        except Exception as exc:

            print(
                "  ERROR:",
                type(exc).__name__,
                str(exc),
            )

        (
            price,
            raw_price,
            options,
        ) = cheapest(
            all_candidates
        )

        if (
            price is not None
            and
            (
                options
                or
                attempt == retries
            )
        ):

            return {

                "origin":
                    task[
                        "origin"
                    ],

                "destination":
                    task[
                        "destination"
                    ],

                "departure_date":
                    task[
                        "departure_date"
                    ],

                "return_date":
                    task[
                        "return_date"
                    ],

                "stay_days":
                    task[
                        "stay_days"
                    ],

                "price":
                    price,

                "price_raw":
                    raw_price,

                "currency":
                    "TWD",

                "passengers":
                    int(
                        cfg.get(
                            "adults",
                            1,
                        )
                    ),

                "flight_options_json":
                    json.dumps(
                        options,
                        ensure_ascii=False,
                        separators=(
                            ",",
                            ":",
                        ),
                    ),

                "checked_at":
                    dt.datetime.now(
                        dt.timezone.utc
                    )
                    .replace(
                        microsecond=0
                    )
                    .isoformat(),
            }

        if attempt < retries:

            time.sleep(
                retry_delay
            )

    return None


# ============================================================
# HISTORY
# ============================================================

def parse_options(
    raw: Any,
):

    try:

        if isinstance(
            raw,
            list,
        ):

            value = raw

        else:

            value = json.loads(
                raw
                or
                "[]"
            )

        if isinstance(
            value,
            list,
        ):

            return value

    except Exception:

        pass

    return []


def load_history(
    path: Path,
):

    if (
        not path.exists()
        or
        path.stat().st_size == 0
    ):

        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        return list(
            csv.DictReader(
                file
            )
        )


def append_history(
    path: Path,
    rows: list[
        dict[
            str,
            Any,
        ]
    ],
):

    if not rows:

        return

    exists = (
        path.exists()
        and
        path.stat().st_size > 0
    )

    with path.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = (
            csv.DictWriter(
                file,
                fieldnames=
                    HISTORY_FIELDS,
                extrasaction=
                    "ignore",
            )
        )

        if not exists:

            writer.writeheader()

        writer.writerows(
            rows
        )


# ============================================================
# HISTORY PRICE
# ============================================================

def row_price(
    row: dict[str, Any],
):

    try:

        price = int(
            float(
                str(
                    row.get(
                        "price",
                        "0",
                    )
                )
            )
        )

        currency = str(
            row.get(
                "currency",
                "TWD",
            )
        ).upper()

        if (
            price >= 1000
            and
            currency == "TWD"
        ):

            return price

    except Exception:

        pass

    return None


def checked_time(
    raw: str,
):

    try:

        value = (
            dt.datetime.fromisoformat(
                raw.replace(
                    "Z",
                    "+00:00",
                )
            )
        )

        if value.tzinfo:

            return value

        return value.replace(
            tzinfo=
                dt.timezone.utc
        )

    except Exception:

        return (
            dt.datetime.min.replace(
                tzinfo=
                    dt.timezone.utc
            )
        )


# ============================================================
# MERGE OPTIONS
# ============================================================

def merge_options(
    first,
    second,
):

    result = []

    seen = set()

    for option in (
        first + second
    ):

        key = option_key(
            option
        )

        if key in seen:

            continue

        seen.add(
            key
        )

        result.append(
            option
        )

    return result


# ============================================================
# LATEST COMBOS
# ============================================================

def latest_combos(
    history,
    cfg,
    today,
    annual_end,
):

    valid_routes = {

        (
            origin,
            destination,
        )

        for origin
        in cfg["origins"]

        for destination
        in cfg["destinations"]
    }

    valid_stays = set(
        stays(cfg)
    )

    latest = {}

    ordered_history = sorted(

        history,

        key=lambda row:
            checked_time(
                str(
                    row.get(
                        "checked_at",
                        "",
                    )
                )
            ),
    )

    for row in ordered_history:

        price = row_price(
            row
        )

        if price is None:

            continue

        if (
            (
                row.get(
                    "origin"
                ),
                row.get(
                    "destination"
                ),
            )
            not in
            valid_routes
        ):

            continue

        try:

            departure = (
                dt.date.fromisoformat(
                    str(
                        row[
                            "departure_date"
                        ]
                    )
                )
            )

            stay = int(
                row[
                    "stay_days"
                ]
            )

        except Exception:

            continue

        if (
            departure < today
            or
            departure > annual_end
            or
            stay not in valid_stays
        ):

            continue

        key = (

            row[
                "origin"
            ],

            row[
                "destination"
            ],

            row[
                "departure_date"
            ],

            row[
                "return_date"
            ],

            stay,
        )

        options = (
            parse_options(
                row.get(
                    "flight_options_json"
                )
            )
        )

        previous = (
            latest.get(
                key
            )
        )

        # 如果同一日期組合價格沒變，
        # 可以保留之前成功抓到的航空公司/時間 metadata。
        if (
            previous
            and
            row_price(
                previous
            )
            ==
            price
        ):

            options = (
                merge_options(

                    parse_options(
                        previous.get(
                            "flight_options_json"
                        )
                    ),

                    options,
                )
            )

        item = dict(
            row
        )

        item[
            "price"
        ] = price

        item[
            "stay_days"
        ] = stay

        item[
            "flight_options_json"
        ] = json.dumps(

            options,

            ensure_ascii=False,

            separators=(
                ",",
                ":",
            ),
        )

        latest[
            key
        ] = item

    return latest


# ============================================================
# BUILD LATEST.JSON
# ============================================================

def build_latest(
    cfg,
    history,
    today,
    annual_end,
    near_end,
    state,
    total_grid,
    near_quota,
    annual_quota,
    run_success,
    attempted,
):

    current = (
        latest_combos(
            history,
            cfg,
            today,
            annual_end,
        )
    )

    grouped = defaultdict(
        list
    )

    for row in current.values():

        grouped[
            route_key(
                row[
                    "origin"
                ],
                row[
                    "destination"
                ],
            )
        ].append(
            row
        )

    cutoff = (
        dt.datetime.now(
            dt.timezone.utc
        )
        -
        dt.timedelta(
            days=int(
                cfg.get(
                    "history_days",
                    60,
                )
            )
        )
    )

    min_samples = int(
        cfg.get(
            "min_score_samples",
            20,
        )
    )

    deals = []

    for rows in grouped.values():

        recent_prices = [

            int(
                row[
                    "price"
                ]
            )

            for row in rows

            if checked_time(
                str(
                    row.get(
                        "checked_at",
                        "",
                    )
                )
            )
            >=
            cutoff
        ]

        if recent_prices:

            pool = recent_prices

        else:

            pool = [

                int(
                    row[
                        "price"
                    ]
                )

                for row
                in rows
            ]

        if not pool:

            continue

        average = (
            sum(pool)
            /
            len(pool)
        )

        for row in rows:

            price = int(
                row[
                    "price"
                ]
            )

            at_or_above = sum(

                1

                for other_price
                in pool

                if (
                    other_price
                    >=
                    price
                )
            )

            cheap_score = round(

                at_or_above
                /
                len(pool)
                *
                100
            )

            if average:

                diff_percent = (

                    (
                        average
                        -
                        price
                    )

                    /
                    average

                    *
                    100
                )

            else:

                diff_percent = 0

            options = (
                parse_options(
                    row.get(
                        "flight_options_json"
                    )
                )
            )

            deals.append(
                {

                    "origin":
                        row[
                            "origin"
                        ],

                    "destination":
                        row[
                            "destination"
                        ],

                    "departure_date":
                        row[
                            "departure_date"
                        ],

                    "return_date":
                        row[
                            "return_date"
                        ],

                    "stay_days":
                        int(
                            row[
                                "stay_days"
                            ]
                        ),

                    "price":
                        price,

                    "currency":
                        "TWD",

                    "passengers":
                        int(
                            row.get(
                                "passengers"
                            )
                            or
                            1
                        ),

                    "flight_options":
                        options,

                    "same_price_option_count":
                        len(
                            options
                        ),

                    "recent_average_price":
                        round(
                            average
                        ),

                    "diff_percent":
                        round(
                            diff_percent,
                            1,
                        ),

                    "cheap_score":
                        cheap_score,

                    "score_sample_size":
                        len(
                            pool
                        ),

                    "score_reliable":
                        (
                            len(pool)
                            >=
                            min_samples
                        ),

                    "checked_at":
                        row.get(
                            "checked_at",
                            "",
                        ),
                }
            )

    deals.sort(
        key=lambda deal:
            (
                -deal[
                    "cheap_score"
                ],
                -deal[
                    "diff_percent"
                ],
                deal[
                    "price"
                ],
            )
    )

    limit = int(
        cfg.get(
            "max_deals_to_publish",
            3000,
        )
    )

    if limit > 0:

        deals = deals[
            :limit
        ]

    return {

        "generated_at":
            dt.datetime.now(
                dt.timezone.utc
            )
            .replace(
                microsecond=0
            )
            .isoformat(),

        "search_start_date":
            today.isoformat(),

        "search_end_date":
            annual_end.isoformat(),

        "near_term_end_date":
            near_end.isoformat(),

        "origins":
            cfg[
                "origins"
            ],

        "destinations":
            cfg[
                "destinations"
            ],

        "adults":
            int(
                cfg.get(
                    "adults",
                    1,
                )
            ),

        "stay_min_days":
            min(
                stays(cfg)
            ),

        "stay_max_days":
            max(
                stays(cfg)
            ),

        "history_days":
            int(
                cfg.get(
                    "history_days",
                    60,
                )
            ),

        "checked_this_run":
            run_success,

        "attempted_this_run":
            attempted,

        "near_checks_planned":
            sum(
                near_quota.values()
            ),

        "annual_checks_planned":
            sum(
                annual_quota.values()
            ),

        "total_checks_so_far":
            int(
                state.get(
                    "total_attempts",
                    0,
                )
            ),

        "total_combos_in_grid":
            total_grid,

        "route_near_quota":
            near_quota,

        "route_annual_quota":
            annual_quota,

        "deal_count":
            len(
                deals
            ),

        "deals":
            deals,
    }


# ============================================================
# SAVE JSON
# ============================================================

def save_json(
    path: Path,
    data: Any,
):

    temporary = (
        path.with_suffix(
            path.suffix
            +
            ".tmp"
        )
    )

    temporary.write_text(

        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),

        encoding="utf-8",
    )

    temporary.replace(
        path
    )


# ============================================================
# MAIN
# ============================================================

def main():

    cfg = load_config()

    history_path = (
        path_from_config(
            cfg,
            "history",
        )
    )

    latest_path = (
        path_from_config(
            cfg,
            "latest",
        )
    )

    state_path = (
        path_from_config(
            cfg,
            "state",
        )
    )

    timezone = ZoneInfo(
        cfg.get(
            "timezone",
            "Asia/Taipei",
        )
    )

    today = (
        dt.datetime.now(
            timezone
        ).date()
    )

    routes = [

        route_key(
            origin,
            destination,
        )

        for origin
        in cfg["origins"]

        for destination
        in cfg["destinations"]
    ]

    state = (
        load_state(
            cfg,
            state_path,
            today,
            routes,
        )
    )

    (
        batch,
        near_quota,
        annual_quota,
        total_grid,
        annual_end,
        near_end,
    ) = build_dual_track_batch(
        cfg,
        state,
        today,
    )

    print(
        "=== Flight Deal Watcher / 雙軌搜尋 ==="
    )

    print(
        f"全年：{today} -> {annual_end}"
    )

    print(
        f"近期：{today} -> {near_end}"
    )

    print(
        "停留："
        f"{min(stays(cfg))}"
        "～"
        f"{max(stays(cfg))}"
        " 天"
    )

    print(
        f"全年網格：{total_grid:,} 組"
    )

    print(
        "本輪：近期 "
        f"{sum(near_quota.values())}"
        " + 全年 "
        f"{sum(annual_quota.values())}"
        " = "
        f"{len(batch)} 組"
    )

    print(
        "各目的地配額："
    )

    for route in routes:

        print(
            "  "
            +
            route.replace(
                "|",
                " -> ",
            )
            +
            "：近期 "
            +
            str(
                near_quota[
                    route
                ]
            )
            +
            " / 全年 "
            +
            str(
                annual_quota[
                    route
                ]
            )
        )

    successful_rows = []

    delay = float(
        cfg.get(
            "request_delay_seconds",
            0.0,
        )
    )

    for index, task in enumerate(
        batch,
        1,
    ):

        print(
            f"[{index}/{len(batch)}] "
            f"{task['track']:6s} "
            f"{task['origin']}"
            "->"
            f"{task['destination']} "
            f"{task['departure_date']}"
            "~"
            f"{task['return_date']} "
            f"{task['stay_days']}d"
        )

        row = search_one(
            task,
            cfg,
        )

        state[
            "total_attempts"
        ] = (
            int(
                state.get(
                    "total_attempts",
                    0,
                )
            )
            +
            1
        )

        if row:

            successful_rows.append(
                row
            )

            print(
                "  OK "
                f"NT${row['price']:,}"
                " / options="
                f"{len(parse_options(row['flight_options_json']))}"
            )

        else:

            print(
                "  NO VALID RESULT"
            )

        if (
            delay > 0
            and
            index < len(batch)
        ):

            time.sleep(
                delay
            )

    append_history(
        history_path,
        successful_rows,
    )

    history = (
        load_history(
            history_path
        )
    )

    latest = (
        build_latest(
            cfg,
            history,
            today,
            annual_end,
            near_end,
            state,
            total_grid,
            near_quota,
            annual_quota,
            len(
                successful_rows
            ),
            len(batch),
        )
    )

    save_json(
        latest_path,
        latest,
    )

    save_json(
        state_path,
        state,
    )

    print(
        "=== 完成 ==="
    )

    print(
        f"本次嘗試：{len(batch)}"
    )

    print(
        "成功取得價格："
        f"{len(successful_rows)}"
    )

    print(
        "網站 deals："
        f"{len(latest['deals'])}"
    )


if __name__ == "__main__":

    main()