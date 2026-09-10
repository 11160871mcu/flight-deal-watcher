from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import time

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
    "track",
    "price_drop_percent",
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



def data_path(
    cfg: dict[str, Any],
    key: str,
) -> Path:

    path = ROOT / cfg["data"][key]

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path



def stay_days(
    cfg: dict[str, Any],
):

    s = cfg.get(
        "stay_duration",
        {},
    )

    return list(
        range(
            int(s.get("min_days",5)),
            int(s.get("max_days",15))+1,
            int(s.get("step_days",1)),
        )
    )



def route_key(
    origin,
    destination,
):

    return f"{origin}|{destination}"



# ============================================================
# SEARCH GRID
# ============================================================


def build_tasks(
    cfg,
    start,
    end,
    allowed_stays=None,
):

    result={}

    stays = (
        allowed_stays
        if allowed_stays
        else stay_days(cfg)
    )


    for origin in cfg["origins"]:

        for destination in cfg["destinations"]:

            key = route_key(
                origin,
                destination,
            )

            rows=[]


            total=(end-start).days


            for i in range(total+1):

                dep = (
                    start
                    +
                    dt.timedelta(days=i)
                )


                for stay in stays:

                    ret = (
                        dep
                        +
                        dt.timedelta(days=stay)
                    )


                    rows.append(
                        {
                            "origin":origin,
                            "destination":destination,
                            "departure_date":dep.isoformat(),
                            "return_date":ret.isoformat(),
                            "stay_days":stay,
                        }
                    )


            result[key]=rows


    return result



# ============================================================
# STATE
# ============================================================


def config_signature(cfg):

    raw=json.dumps(
        {
            "origin":cfg.get("origins"),
            "destination":cfg.get("destinations"),
            "stay":cfg.get("stay_duration"),
        },
        sort_keys=True,
    )


    return hashlib.sha256(
        raw.encode()
    ).hexdigest()[:20]



def new_state(
    cfg,
    routes,
):

    return {

        "version":8,

        "signature":
            config_signature(cfg),


        "urgent_cursor":
        {
            r:0
            for r in routes
        },


        "hot_cursor":
        {
            r:0
            for r in routes
        },


        "explore_cursor":
        {
            r:0
            for r in routes
        },


        "last_price_map":{},


        "total_attempts":0,

    }



def load_state(
    cfg,
    path,
    routes,
):

    fresh=new_state(
        cfg,
        routes,
    )


    if not path.exists():

        return fresh


    try:

        state=json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return fresh



    if (
        state.get("version")
        !=
        8
    ):

        return fresh


    return state



# ============================================================
# ADAPTIVE QUOTA
# ============================================================


def fair_quota(
    total,
    routes,
):

    base,extra=divmod(
        total,
        len(routes)
    )


    q={
        r:base
        for r in routes
    }


    for r in routes[:extra]:

        q[r]+=1


    return q
# ============================================================
# TASK PICKER
# 三層自適應搜尋
# ============================================================


def pick_tasks(
    pools,
    cursor_map,
    quotas,
    track,
    used,
):

    selected=[]


    for route,tasks in pools.items():

        if not tasks:
            continue


        cursor=int(
            cursor_map.get(
                route,
                0
            )
        )


        quota=quotas.get(
            route,
            0
        )


        count=0

        scanned=0


        while (
            count < quota
            and scanned < len(tasks)
        ):

            index=(
                cursor
                %
                len(tasks)
            )


            task=tasks[index]


            cursor += 1

            scanned += 1


            key=(
                task["origin"],
                task["destination"],
                task["departure_date"],
                task["return_date"],
                task["stay_days"],
            )


            if key in used:
                continue


            item=dict(task)

            item["track"]=track


            selected.append(
                item
            )


            used.add(key)


            count += 1



        cursor_map[route]=cursor



    return selected



# ============================================================
# THREE LEVEL SEARCH STRATEGY
# ============================================================


def build_search_batch(
    cfg,
    state,
    today,
):


    routes=[
        route_key(
            o,
            d
        )
        for o in cfg["origins"]
        for d in cfg["destinations"]
    ]


    # --------------------------------------------------------
    # 1. urgent
    # 未來30天
    # 全停留天數
    # 最高優先
    # --------------------------------------------------------

    urgent_end=(
        today
        +
        dt.timedelta(
            days=int(
                cfg.get(
                    "urgent_days",
                    30
                )
            )
        )
    )


    urgent_pool=build_tasks(
        cfg,
        today,
        urgent_end,
    )



    # --------------------------------------------------------
    # 2. hot
    # 未來180天
    # 只搜尋常見旅行長度
    # --------------------------------------------------------


    hot_end=(
        today
        +
        dt.timedelta(
            days=int(
                cfg.get(
                    "hot_days",
                    180
                )
            )
        )
    )


    hot_pool=build_tasks(
        cfg,
        today,
        hot_end,
        allowed_stays=[
            5,
            7,
            10,
            14,
        ],
    )



    # --------------------------------------------------------
    # 3. explore
    # 全年探索
    # --------------------------------------------------------


    explore_end=(
        today
        +
        relativedelta(
            months=int(
                cfg.get(
                    "explore_months_ahead",
                    12
                )
            )
        )
    )


    explore_pool=build_tasks(
        cfg,
        today,
        explore_end,
        allowed_stays=[
            7
        ],
    )



    used=set()



    urgent_quota=fair_quota(
        int(
            cfg.get(
                "urgent_checks_per_run",
                480
            )
        ),
        routes,
    )


    hot_quota=fair_quota(
        int(
            cfg.get(
                "hot_checks_per_run",
                400
            )
        ),
        routes,
    )


    explore_quota=fair_quota(
        int(
            cfg.get(
                "explore_checks_per_run",
                120
            )
        ),
        routes,
    )



    urgent=pick_tasks(
        urgent_pool,
        state["urgent_cursor"],
        urgent_quota,
        "urgent",
        used,
    )


    hot=pick_tasks(
        hot_pool,
        state["hot_cursor"],
        hot_quota,
        "hot",
        used,
    )


    explore=pick_tasks(
        explore_pool,
        state["explore_cursor"],
        explore_quota,
        "explore",
        used,
    )



    # 優先級排列
    #
    # urgent:
    #   最容易抓到突降
    #
    # hot:
    #   中期促銷
    #
    # explore:
    #   未來機會


    batch=[]


    batch.extend(
        urgent
    )


    batch.extend(
        hot
    )


    batch.extend(
        explore
    )


    return batch



# ============================================================
# PRICE PARSER
# ============================================================


def parse_price(
    value
):


    if value is None:

        return None



    if isinstance(
        value,
        (int,float)
    ):

        value=int(value)

        if value>=1000:

            return value


        return None



    text=str(
        value
    ).strip()



    if not text:

        return None



    upper=text.upper()



    # 防止 USD 被當 TWD

    if (
        "NT$"
        not in upper
        and
        "TWD"
        not in upper
    ):

        return None



    digits=re.sub(
        r"[^0-9]",
        "",
        text
    )



    if not digits:

        return None



    price=int(
        digits
    )


    if price < 1000:

        return None



    return price



# ============================================================
# PRICE DROP DETECTION
# ============================================================


def price_key(
    task
):

    return "|".join(
        [
            task["origin"],
            task["destination"],
            task["departure_date"],
            task["return_date"],
            str(task["stay_days"]),
        ]
    )



def calculate_drop(
    old_price,
    new_price,
):


    if not old_price:

        return 0



    if new_price >= old_price:

        return 0



    drop=(
        old_price-new_price
    ) / old_price * 100



    return round(
        drop,
        2
    )



def update_price_memory(
    state,
    task,
    price,
):


    key=price_key(
        task
    )


    old=state["last_price_map"].get(
        key
    )


    drop=calculate_drop(
        old,
        price
    )


    state["last_price_map"][key]=price


    return drop
# ============================================================
# FAST-FLIGHTS QUERY
# ============================================================


def fetch_candidates(
    task,
    cfg,
):


    kwargs={

        "flight_data":[

            FlightData(
                date=
                    task["departure_date"],

                from_airport=
                    task["origin"],

                to_airport=
                    task["destination"],
            ),


            FlightData(
                date=
                    task["return_date"],

                from_airport=
                    task["destination"],

                to_airport=
                    task["origin"],
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
                )
            ),

    }



    if cfg.get(
        "direct_only",
        True
    ):

        kwargs[
            "max_stops"
        ]=0



    search_filter=create_filter(
        **kwargs
    )



    try:

        result=get_flights_from_filter(
            search_filter
        )


        return result


    except Exception as e:

        print(
            "query failed:",
            task,
            e,
        )

        return None





# ============================================================
# FLIGHT RESULT PARSER
# ============================================================


def extract_price(
    flight
):


    candidates=[]



    for attr in [
        "price",
        "total_price",
        "price_amount",
    ]:

        if hasattr(
            flight,
            attr
        ):

            value=getattr(
                flight,
                attr
            )


            p=parse_price(
                value
            )


            if p:

                candidates.append(
                    p
                )



    if not candidates:

        return None



    return min(
        candidates
    )





def extract_options(
    result
):


    options=[]


    if result is None:

        return options



    flights=getattr(
        result,
        "flights",
        []
    )



    for f in flights:


        item={}



        for key in [
            "airline",
            "departure",
            "arrival",
            "duration",
            "stops",
        ]:


            if hasattr(
                f,
                key
            ):

                item[key]=str(
                    getattr(
                        f,
                        key
                    )
                )


        options.append(
            item
        )


    return options





# ============================================================
# HISTORY CSV
# ============================================================


def ensure_history(
    path
):


    if path.exists():

        return



    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:


        writer=csv.DictWriter(
            f,
            fieldnames=
                HISTORY_FIELDS,
        )


        writer.writeheader()





def append_history(
    path,
    row,
):


    with path.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as f:


        writer=csv.DictWriter(
            f,
            fieldnames=
                HISTORY_FIELDS,
        )


        writer.writerow(
            row
        )





# ============================================================
# RUN ONE TASK
# ============================================================


def process_task(
    task,
    cfg,
    state,
    history_path,
):


    result=fetch_candidates(
        task,
        cfg,
    )



    if result is None:

        return None



    price=extract_price(
        result
    )



    if price is None:

        return None



    options=extract_options(
        result
    )



    # 有價格但抓不到航空公司/時間等 metadata 時，依設定重試

    retry_count=int(
        cfg.get(
            "metadata_retry_count",
            1,
        )
    )

    retry_delay=float(
        cfg.get(
            "metadata_retry_delay_seconds",
            1.0,
        )
    )

    attempts=0

    while (
        not options
        and attempts < retry_count
    ):

        time.sleep(retry_delay)

        retry_result=fetch_candidates(
            task,
            cfg,
        )

        if retry_result is not None:

            retry_price=extract_price(
                retry_result
            )

            retry_options=extract_options(
                retry_result
            )

            if retry_price is not None:

                price=retry_price

            if retry_options:

                options=retry_options

        attempts += 1



    drop=update_price_memory(
        state,
        task,
        price,
    )



    now=dt.datetime.now(
        ZoneInfo(
            cfg.get(
                "timezone",
                "Asia/Taipei",
            )
        )
    ).isoformat()



    row={

        "origin":
            task["origin"],


        "destination":
            task["destination"],


        "departure_date":
            task["departure_date"],


        "return_date":
            task["return_date"],


        "stay_days":
            task["stay_days"],


        "price":
            price,


        "price_raw":
            price,


        "currency":
            "TWD",


        "passengers":
            cfg.get(
                "adults",
                1,
            ),


        "flight_options_json":
            json.dumps(
                options,
                ensure_ascii=False,
            ),


        "checked_at":
            now,


        "track":
            task.get(
                "track",
                "",
            ),


        "price_drop_percent":
            drop,

    }



    append_history(
        history_path,
        row,
    )



    return {

        **task,

        "price":
            price,


        "currency":
            "TWD",


        "flight_options":
            options,


        "checked_at":
            now,


        "price_drop_percent":
            drop,


        "alert":
            (
                "PRICE_DROP"
                if drop >=
                float(
                    cfg.get(
                        "price_drop_alert_percent",
                        20,
                    )
                )
                else ""
            ),

    }
# ============================================================
# CHEAP SCORE
# ============================================================


def calculate_cheap_score(
    history_path,
    item,
    days=60,
    min_samples=20,
    tz="Asia/Taipei",
):

    if not history_path.exists():

        return None



    prices=[]


    # checked_at 是帶時區的 isoformat，cutoff 也要帶時區，
    # 否則 naive/aware 比較會直接丟例外

    cutoff=(
        dt.datetime.now(
            ZoneInfo(tz)
        )
        -
        dt.timedelta(
            days=days
        )
    )


    with history_path.open(
        "r",
        encoding="utf-8-sig",
    ) as f:


        reader=csv.DictReader(
            f
        )


        for row in reader:


            try:

                checked=dt.datetime.fromisoformat(
                    row["checked_at"]
                )


            except Exception:

                continue



            if checked < cutoff:

                continue



            if (
                row["origin"]
                ==
                item["origin"]

                and

                row["destination"]
                ==
                item["destination"]
            ):


                try:

                    prices.append(
                        int(
                            row["price"]
                        )
                    )

                except Exception:

                    pass



    if len(prices) < min_samples:

        return None



    current=int(
        item["price"]
    )


    better=sum(
        1
        for p in prices
        if current <= p
    )


    return round(
        better
        /
        len(prices)
        *
        100,
        1,
    )





# ============================================================
# LATEST JSON
# ============================================================


def write_latest(
    path,
    items,
    cfg,
    history_path,
):


    output=[]

    score_days=int(
        cfg.get(
            "history_days",
            60,
        )
    )

    min_samples=int(
        cfg.get(
            "min_score_samples",
            20,
        )
    )

    tz=cfg.get(
        "timezone",
        "Asia/Taipei",
    )


    for item in items:


        item=dict(item)



        item["cheap_score"]=calculate_cheap_score(
          history_path,
          item,
          days=score_days,
          min_samples=min_samples,
          tz=tz,
        )


        output.append(
            item
        )



    output.sort(
        key=lambda x:
            (
                -(x.get(
                    "cheap_score"
                ) or 0),

                x.get(
                    "price",
                    999999,
                )
            )
    )



    max_deals=int(
        cfg.get(
            "max_deals_to_publish",
            0,
        )
    )

    if max_deals > 0:

        output=output[:max_deals]



    path.write_text(
        json.dumps(
            {
                "updated_at":
                    dt.datetime.now(
                        ZoneInfo(
                            "Asia/Taipei"
                        )
                    ).isoformat(),

                "count":
                    len(output),

                "results":
                    output,

            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )





# ============================================================
# SAVE STATE
# ============================================================


def save_state(
    path,
    state,
):


    path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )





# ============================================================
# MAIN
# ============================================================


def main():


    cfg=load_config()


    history_path=data_path(
        cfg,
        "history",
    )


    latest_path=data_path(
        cfg,
        "latest",
    )


    state_path=data_path(
        cfg,
        "state",
    )



    ensure_history(
        history_path
    )



    today=dt.datetime.now(
        ZoneInfo(
            cfg.get(
                "timezone",
                "Asia/Taipei",
            )
        )
    ).date()



    routes=[
        route_key(
            o,
            d,
        )

        for o in cfg["origins"]

        for d in cfg["destinations"]
    ]



    state=load_state(
        cfg,
        state_path,
        routes,
    )



    print(
        "================================"
    )

    print(
        " Flight Deal Watcher"
    )

    print(
        " Adaptive Price Drop Detection"
    )

    print(
        "================================"
    )



    batch=build_search_batch(
        cfg,
        state,
        today,
    )



    print(
        "本輪查詢:",
        len(batch),
    )



    results=[]



    for index,task in enumerate(batch):


        print(
            f"[{index+1}/{len(batch)}]",
            task["origin"],
            "->",
            task["destination"],
            task["departure_date"],
            task["track"],
        )



        item=process_task(
            task,
            cfg,
            state,
            history_path,
        )


        if item:

            results.append(
                item
            )



        state["total_attempts"] += 1



        # 避免過快請求

        time.sleep(
            float(
                cfg.get(
                    "request_delay_seconds",
                    1,
                )
            )
        )



    write_latest(
        latest_path,
        results,
        cfg,
        history_path,
    )



    save_state(
        state_path,
        state,
    )



    print(
        "完成"
    )


    print(
        "成功:",
        len(results),
    )





if __name__=="__main__":

    main()