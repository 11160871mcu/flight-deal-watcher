import os
import json
import csv
import yaml
import datetime
import itertools
from dateutil.relativedelta import relativedelta

from fast_flights import FlightData, Passengers, get_flights

CONFIG_PATH = "config.yaml"
HISTORY_CSV = "docs/data/history.csv"
LATEST_JSON = "docs/data/latest.json"
STATE_JSON = "docs/data/state.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dirs():
    os.makedirs("docs/data", exist_ok=True)


def rolling_date_grid(months_ahead=12):
    """
    每次執行都是用「今天」當起點往後推 months_ahead 個月，
    不是寫死的日期。所以今天執行是「今天 ~ 今天+12個月」，
    下個月執行就自動變成「下個月 ~ 下個月+12個月」，會自動滾動。
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
    origins = cfg.get("origins", ["TPE"])
    destinations = cfg.get("destinations", [])
    stay = cfg.get("stay_duration", {})

    min_days = stay.get("min_days", 5)
    max_days = stay.get("max_days", 15)
    step = stay.get("step_days", 2)

    tasks = []

    # 日期放最外層：同一天就把「所有目的地 x 所有停留天數」都排進去，
    # 這樣才會平均地涵蓋到全部目的地，而不是把第一個目的地一整年的
    # 日期組合查完才輪到下一個目的地（那樣要等好幾週才看得到其他航線）。
    for dep in rolling_date_grid(cfg.get("search_months_ahead", 12)):
        for origin, dest in itertools.product(origins, destinations):
            for days in range(min_days, max_days + 1, step):
                ret = dep + datetime.timedelta(days=days)

                tasks.append({
                    "origin": origin,
                    "destination": dest,
                    "departure": dep.strftime("%Y-%m-%d"),
                    "return": ret.strftime("%Y-%m-%d"),
                    "stay_days": days
                })

    return tasks


def load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)

    return {"next_index": 0}


def save_state(index):
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump({"next_index": index}, f, indent=2)


def _parse_price(raw_price):
    """price 欄位是像 'NT$9,597' 這種字串，統一轉成 int（新台幣）。"""
    if raw_price is None:
        return None
    if isinstance(raw_price, (int, float)):
        return int(raw_price)
    digits = "".join(ch for ch in str(raw_price) if ch.isdigit())
    return int(digits) if digits else None


def _safe_int(v):
    """
    history.csv 讀回來的每個欄位都是字串（例如 "12"、"Unknown"、""），
    新查到、還沒寫進 CSV 的資料則是原生 int/None。這個函式統一把兩種
    來源都轉成乾淨的 int，轉不了（像 "Unknown"、空字串）就回傳 None，
    避免 latest.json 裡同一個欄位有時是數字、有時是帶引號的字串。
    """
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def search_one(task):

    try:
        result = get_flights(
            flight_data=[
                # 去程
                FlightData(
                    date=task["departure"],
                    from_airport=task["origin"],
                    to_airport=task["destination"]
                ),
                # 回程（出發地/目的地對調，用 task["return"] 當日期）
                FlightData(
                    date=task["return"],
                    from_airport=task["destination"],
                    to_airport=task["origin"]
                )
            ],
            trip="round-trip",
            passengers=Passengers(adults=1),
            seat="economy",
            max_stops=0
        )

        if not result.flights:
            return None

        # 只挑有價格的候選，價格轉成數字再比較
        # （result.flights 是平面 list，每個 Flight 物件的 price
        #  就是這趟來回的總價，不是單程價）
        candidates = [
            f for f in result.flights
            if _parse_price(getattr(f, "price", None)) is not None
        ]

        if not candidates:
            return None

        best = min(candidates, key=lambda f: _parse_price(f.price))

        return {
            "origin": task["origin"],
            "destination": task["destination"],

            "departure_date": task["departure"],
            "return_date": task["return"],

            "price": _parse_price(best.price),
            "passengers": 1,

            # 這個免費資料來源偶爾（約 1/4 機率）因為 Google 隨機切換
            # 頁面樣式導致抓不到航空公司/時間，這時候會是 None，
            # 不代表程式錯誤，網站那邊會顯示「資料未提供」。
            "airline": getattr(best, "name", None),
            "departure": getattr(best, "departure", None),
            "arrival": getattr(best, "arrival", None),
            "duration": getattr(best, "duration", None),
            "stops": getattr(best, "stops", None),
            "is_best": getattr(best, "is_best", None),

            # 老實說：這個免費資料來源不會回傳托運行李資訊，
            # 所以這裡只能誠實標示「請洽訂票頁面確認」，
            # 不能假裝知道實際有沒有托運。
            "baggage": "此資料來源不提供托運行李資訊，請以訂票頁面實際顯示為準",

            "stay_days": task["stay_days"],

            "checked_at":
                datetime.datetime.now().isoformat()
        }

    except Exception as e:
        print("Search failed:", e)
        return None


def build_route_rankings(history, history_days=60):
    """
    把「每一筆查價紀錄」聚合成「每條航線（出發地+目的地）目前已知最便宜的組合」，
    這是網站首頁要顯示的資料（一個目的地一張卡片），跟 history.csv 的
    逐筆查價紀錄是分開的兩件事。
    """
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=history_days)

    by_route = {}

    for row in history:
        price = _parse_price(row.get("price"))
        if not price:
            continue

        key = (row.get("origin"), row.get("destination"))
        entry = dict(row)
        entry["price"] = price
        by_route.setdefault(key, []).append(entry)

    routes = []

    for (origin, destination), rows in by_route.items():
        # 「近期平均價」只用 history_days 天以內查到的資料算，
        # 如果最近都還沒查到資料（例如這條航線剛開始抽樣），
        # 就退而求其次用全部累積到的資料算平均。
        recent_rows = []
        for r in rows:
            try:
                checked_at = datetime.datetime.fromisoformat(r.get("checked_at", ""))
            except ValueError:
                checked_at = None
            if checked_at is None or checked_at >= cutoff:
                recent_rows.append(r)

        avg_pool = recent_rows if recent_rows else rows
        avg_price = sum(r["price"] for r in avg_pool) / len(avg_pool)

        best = min(rows, key=lambda r: r["price"])

        diff_percent = 0
        if avg_price:
            diff_percent = max(0, (avg_price - best["price"]) / avg_price * 100)

        routes.append({
            "origin": origin,
            "destination": destination,

            "best_price": best["price"],
            "best_departure_date": best.get("departure_date"),
            "best_return_date": best.get("return_date"),
            "best_stay_days": _safe_int(best.get("stay_days")),

            "airline": best.get("airline") or None,
            "dep_time": best.get("departure") or None,
            "arr_time": best.get("arrival") or None,
            "duration": best.get("duration") or None,
            "stops": _safe_int(best.get("stops")),
            "baggage": best.get("baggage") or "此資料來源不提供托運行李資訊，請以訂票頁面實際顯示為準",

            "recent_average_price": round(avg_price),
            "diff_percent": round(diff_percent, 1),

            "last_checked_at": best.get("checked_at"),
            "sample_size": len(rows),
        })

    routes.sort(key=lambda r: r["diff_percent"], reverse=True)

    for i, r in enumerate(routes):
        r["cheap_score"] = max(1, 100 - i)

    return routes


def main():
    print("=== Flight crawler start ===")
    ensure_dirs()
    cfg = load_config()

    tasks = generate_search_tasks(cfg)

    state = load_state()

    start = state["next_index"]

    batch = cfg.get(
        "max_checks_per_run",
        50
    )

    selected = tasks[start:start+batch]

    new_rows = []

    for task in selected:
        print(
            task["origin"],
            "->",
            task["destination"],
            task["departure"]
        )

        r = search_one(task)

        if r:
            new_rows.append(r)


    save_state(
        (start + batch) % len(tasks)
    )


    history = []

    if os.path.exists(HISTORY_CSV):

        with open(
            HISTORY_CSV,
            encoding="utf-8"
        ) as f:

            history.extend(
                csv.DictReader(f)
            )


    history.extend(new_rows)


    # 如果這次一組都沒查到（例如全部被限流失敗），就不要動 history.csv，
    # 也不要往下產生 latest.json，避免整支程式在空資料上崩潰。
    if not history:
        print("本次沒有任何成功的查詢結果，history.csv 維持原狀，也不會更新 latest.json")
        return


    with open(
        HISTORY_CSV,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=history[0].keys()
        )

        writer.writeheader()
        writer.writerows(history)


    history_days = cfg.get("history_days", 60)
    route_rankings = build_route_rankings(history, history_days)


    latest = {
        "generated_at":
            datetime.datetime.now().isoformat(),

        "adults": 1,
        "history_days": history_days,

        # 掃描進度：這次查了幾組、目前總共累積查過幾筆、
        # 全部候選組合共有幾組（對應 README 說要顯示的進度）
        "checked_this_run": len(new_rows),
        "total_checks_so_far": len(history),
        "total_combos_in_grid": len(tasks),

        "routes": route_rankings
    }


    with open(
        LATEST_JSON,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            latest,
            f,
            ensure_ascii=False,
            indent=2
        )


if __name__ == "__main__":
    main()
