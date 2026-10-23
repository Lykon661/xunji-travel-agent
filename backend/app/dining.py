"""Guide dining places are optional recommendations, separate from sightseeing."""
import re

from . import ablation
from .routing import coordinates, distance
from .models import wish_stops
from .places import canonical_key, canonicalize_stops, repeat_allowed, represented_ids
from .stop_types import is_light_wish


def is_dining(stop):
    if is_light_wish(stop):
        return False
    place = stop.get("map_place") or {}
    kind = str(place.get("type") or "")
    code = str(place.get("typecode") or "")
    name = stop.get("name", "")
    if stop.get("category") == "dining" or "餐饮" in kind or code.startswith("05"):
        return True
    if re.search(r"博物馆|展览馆|文化馆|小吃街|美食街", name):
        return False
    if stop.get("source_kind") == "user" and BREAKFAST.search(name):
        return True
    return bool(re.search(r"餐厅|餐馆|饭店|饭馆|小吃|煲仔饭|肠粉|鱼皮|甜品|糖水|咖啡|茶饮|奶茶|面馆|粥店|饼店|烘焙|酒楼|火锅|烧烤|芝麻糊|\b(?:coffee|cafe|café)\b", name, re.I))


def dining_wishes(stops, requirements=None):
    wishes = wish_stops(requirements or {})
    pool = {**wishes,**stops}
    return {identity:stop for identity,stop in pool.items() if stop.get("source_kind") == "user" and is_dining(stop)
            and (requirements is None or "wish_places" not in requirements or identity in wishes)}


def visit_required_ids(identities, stops, requirements=None):
    return set(identities)-dining_wishes(stops,requirements).keys()


MEAL_FIELDS = ("id", "name", "source_id", "source_title", "source_url", "evidence", "map_place", "opening_claim",
               "canonical_place_id", "source_references", "possible_duplicate_ids")
BREAKFAST = re.compile(r"早餐|早茶|早点|豆浆|包子|油条|肠粉[（(]?早")


def _anchors(day, meal, strict=False):
    visits = [item for item in day.get("activities",[]) if item.get("kind") == "visit" and not is_dining(item)]
    if meal == "breakfast":
        return visits[:1]
    target = 720 if meal == "lunch" else 1080
    ranked = []
    for item in visits:
        try:
            start,end = [int(value[:2])*60+int(value[3:5]) for value in (item["start"],item["end"])]
        except (KeyError,ValueError,TypeError):
            continue
        ranked.append((max(start-target,target-end,0),item))
    ranked.sort(key=lambda pair:pair[0])
    nearby = [item for gap,item in ranked[:2] if gap <= 120]
    return nearby if nearby or strict else [item for _,item in ranked[:1]] or visits[:1]


def plan_meal_wishes(days, stops, requirements=None):
    """Choose restaurant days from spatial visit groups without consuming visit time."""
    result = {}
    for identity,stop in dining_wishes(stops,requirements).items():
        point = coordinates(stop.get("map_place"))
        options = []
        for index,day in enumerate(days):
            if day["city"] != stop["city"] or stop.get("assigned_date") not in {None,day["date"]}:
                continue
            visits = day.get("activities") or [stops[i] for i in day.get("stop_ids",[]) if i in stops and not is_dining(stops[i])]
            nearby = [(distance(point,other),position) for position,item in enumerate(visits)
                      if point and (other := coordinates(item.get("map_place")))]
            meters,position = min(nearby) if nearby else (float("inf"),0)
            meal = "breakfast" if BREAKFAST.search(stop["name"]) else "dinner" if position >= max(1,len(visits)/2) else "lunch"
            options.append(((meters,index),day,meal))
        if options:
            _,day,meal = min(options,key=lambda entry:entry[0])
            result[identity] = {"date":day["date"],"city":stop["city"],"name":stop["name"],"meal":meal,
                                "map_place":stop.get("map_place") if point else None}
    return result


def _place_wishes(days, stops, used, requirements=None, preserved=(), meal_plan=None):
    """Allocate hard meals across the mutable days before any optional recommendations."""
    pool = canonicalize_stops({**wish_stops(requirements or {}),**stops})
    groups = {}
    for identity,stop in dining_wishes(pool,requirements).items():
        groups.setdefault(canonical_key(stop),[]).append(stop)
    occupied = {}
    preserved_ids = represented_ids(preserved)
    preserved_keys = {canonical_key(item,item.get("city","")) for item in preserved}
    already_used = set(used)
    for key,group in sorted(groups.items(),key=lambda entry:(not any(s.get("assigned_date") for s in entry[1]),entry[0])):
        used.add(key)
        if key in already_used or key in preserved_keys or any(stop["id"] in preserved_ids for stop in group):
            continue
        stop = next((s for s in group if s.get("assigned_date")),group[0])
        candidates = [day for day in days if day["city"] == stop["city"] and stop.get("assigned_date") in {None,day["date"]}]
        if not candidates:
            continue  # dining_wish_issues reports the incompatible city/date, never silently succeeds.
        point = coordinates(stop.get("map_place"))
        meals = ("breakfast",) if BREAKFAST.search(stop["name"]) else ("lunch","dinner")
        if not point:
            meals = ("lunch",)
        planned = (meal_plan or {}).get(stop["id"],{}).get("date")
        options = []
        for index,day in enumerate(candidates):
            for meal_index,meal in enumerate(meals):
                anchors = _anchors(day,meal)
                distances = [(distance(point,other),i,item) for i,item in enumerate(anchors)
                             if point and (other := coordinates(item.get("map_place")))]
                meters,_,anchor = min(distances) if distances else (float("inf"),0,anchors[0] if anchors else None)
                options.append((meters,day,meal,anchor,index,meal_index))
        nearby = [option for option in options if option[0] <= 2000]
        preferred = [option for option in nearby if option[1]["date"] == planned]
        choices = preferred or nearby or options
        def rank(option):
            meters,day,meal,_,index,meal_index = option
            occupation = occupied.get((day["date"],meal),0)
            if nearby:
                return occupation,meters,index,meal_index
            if not point:
                return day["date"] != planned if planned else False,occupation,index,meal_index
            return meters,occupation,index,meal_index
        meters,day,meal,anchor,_,_ = min(choices,key=rank)
        note = "未能在高德唯一定位，请自行确认地址" if not point else (
            f"与该时段活动点直线距离约{round(meters)}米"+("；距当天活动较远，需单独前往" if meters > 2000 else "")
            if meters != float("inf") else "当天活动缺少可用坐标，无法估计直线距离，需单独确认路线")
        if meal == "breakfast":
            note = "早餐参考08:00；"+note
        row = {**{field:stop.get(field) for field in MEAL_FIELDS},"source_kind":"user","wish":True,"category":"dining",
               "city":stop["city"],"meal":meal,"nearby_activity":anchor.get("name") if anchor else None,
               "distance_meters":round(meters) if meters != float("inf") else None,"note":note+"；营业时间、道路通行和实际消费需核对"}
        row["map_place"] = stop.get("map_place") if point else None
        day.setdefault("meal_recommendations",[]).append(row)
        occupied[day["date"],meal] = occupied.get((day["date"],meal),0)+1


def dining_wish_issues(itinerary, stops=None, requirements=None):
    rows = [(day,item) for day in itinerary.get("days",[]) for item in day.get("meal_recommendations",[])]
    pool = {item["id"]:{**item,"city":day["city"]} for day,item in rows if item.get("id")}
    pool.update(stops or {})
    wishes = dining_wishes(pool,requirements)
    issues = []
    for identity,stop in wishes.items():
        matches = [(day,item) for day,item in rows if identity in represented_ids([item]) or
                   canonical_key(item,day["city"]) == canonical_key(stop)]
        if len(matches) != 1:
            message = stop["name"]+("餐饮必去地点尚未安排进用餐" if not matches else "餐饮必去地点重复安排，必须恰好安排一次")
        elif (matches[0][0]["city"] != stop["city"] or stop.get("assigned_date") not in {None,matches[0][0]["date"]}
              or matches[0][1].get("meal") not in {"breakfast","lunch","dinner"}):
            message = stop["name"]+"餐饮必去地点的城市、指定日期或餐次不符合要求"
        else:
            continue
        if stop.get("assigned_date") and not any(day["date"] == stop["assigned_date"] and day["city"] == stop["city"] for day in itinerary.get("days",[])):
            message += "；指定日期不在该城市的行程日内"
        issues.append({"code":"dining_wish","target":identity,"blocking":True,"message":message})
    return issues


def meal_recommendations(day, stops, excluded=(), used=None, requirements=None, max_options=2, include_wishes=True):
    visits = [item for item in day.get("activities", []) if item.get("kind") == "visit" and not is_dining(item)
              and coordinates(item.get("map_place"))]
    result = []
    shared_used = used if used is not None else set()
    wishes = dining_wishes(stops,requirements)
    wish_keys = {canonical_key(stop) for stop in canonicalize_stops(wishes).values()}
    if include_wishes:
        temporary = {**day,"meal_recommendations":[]}
        _place_wishes([temporary],stops,shared_used,requirements)
        result.extend(temporary["meal_recommendations"])
    # Ablation: no trip-wide restaurant table, so lunch, dinner and later days may repeat a venue.
    used = set(shared_used) if ablation.disabled("dedupe") else shared_used
    excluded_places = {canonical_key(stops[identity]) for identity in excluded if identity in stops}
    for meal, target in (("lunch", 12 * 60), ("dinner", 18 * 60)):
        anchors = []
        for item in visits:
            try:
                start, end = [int(value[:2]) * 60 + int(value[3:5]) for value in (item["start"], item["end"])]
            except (KeyError, ValueError, TypeError):
                continue
            anchors.append((max(start - target, target - end, 0), item))
        anchors.sort(key=lambda pair: pair[0])
        # shortcut: within 2 km of a visit around the meal window; road access and opening hours still need checking.
        anchors = [item for gap, item in anchors[:2] if gap <= 120]
        options = []
        for identity, stop in stops.items():
            point = coordinates(stop.get("map_place"))
            if identity in wishes or canonical_key(stop) in wish_keys or identity in excluded or canonical_key(stop) in excluded_places or not is_dining(stop) or stop.get("city") != day["city"] or not point or not anchors:
                continue
            if stop.get("assigned_date") and stop["assigned_date"] != day["date"]:
                continue
            place = stop["map_place"]
            key = canonical_key(stop,day["city"])
            if key in used and not repeat_allowed(stop,day,"dining",requirements or {},meal):
                continue
            meters, anchor = min((distance(point, coordinates(item["map_place"])), index) for index, item in enumerate(anchors))
            if meters <= 2000:
                options.append((meters, identity, key, anchors[anchor]))
        options.sort(key=lambda option: (option[0], option[1]))
        for meters, identity, key, anchor in options:
            if key in used and not repeat_allowed(stops[identity],day,"dining",requirements or {},meal):
                continue
            if any(canonical_key(item,day["city"]) == key and item["meal"] == meal for item in result):
                continue
            stop = stops[identity]
            result.append({**{field: stop.get(field) for field in MEAL_FIELDS},
                           "meal": meal, "nearby_activity": anchor["name"], "distance_meters": round(meters),
                           "note": "与该时段活动点直线距离不超过2公里；营业时间、道路通行和实际消费需核对"})
            if not ablation.disabled("dedupe"):
                used.add(key)
            if sum(item["meal"] == meal for item in result) >= max_options:
                break
    return sorted(result,key=lambda item:({"breakfast":0,"lunch":1,"dinner":2}[item["meal"]],not item.get("wish",False)))


def refresh_meals(itinerary, stops, excluded=(), dates=None, requirements=None, meal_plan=None):
    used = set()
    preserved = []
    selected = []
    for day in itinerary.get("days",[]):
        if dates is not None and day["date"] not in dates:
            used.update(canonical_key(item,day["city"]) for item in day.get("meal_recommendations",[]))
            preserved.extend({**item,"city":day["city"]} for item in day.get("meal_recommendations",[]))
        else:
            day["meal_recommendations"] = []
            selected.append(day)
    _place_wishes(selected,stops,used,requirements,preserved,meal_plan or itinerary.get("meal_wish_plan"))
    for day in selected:
        day["meal_recommendations"].extend(meal_recommendations(day,stops,excluded,used,requirements,max_options=1,include_wishes=False))
    # Give later meals a first recommendation before filling optional second choices.
    for day in itinerary.get("days",[]):
        if dates is None or day["date"] in dates:
            extras = meal_recommendations(day,stops,excluded,used,requirements,max_options=1,include_wishes=False)
            for item in extras:
                current = day["meal_recommendations"]
                if not any(canonical_key(row,day["city"]) == canonical_key(item,day["city"]) and row["meal"] == item["meal"] for row in current):
                    current.append(item)
            day["meal_recommendations"].sort(key=lambda item:({"breakfast":0,"lunch":1,"dinner":2}[item["meal"]],not item.get("wish",False)))
    itinerary["meal_wish_plan"] = {item["id"]:{"date":day["date"],"city":day["city"],"name":item["name"],
                                                "meal":item["meal"],"map_place":item.get("map_place")}
                                   for day in itinerary.get("days",[]) for item in day.get("meal_recommendations",[]) if item.get("wish")}
