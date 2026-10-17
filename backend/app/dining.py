"""Guide dining places are optional recommendations, separate from sightseeing."""
import re

from . import ablation
from .routing import coordinates, distance
from .places import canonical_key, repeat_allowed


def is_dining(stop):
    place = stop.get("map_place") or {}
    kind = str(place.get("type") or "")
    code = str(place.get("typecode") or "")
    name = stop.get("name", "")
    if stop.get("category") == "dining" or "餐饮" in kind or code.startswith("05"):
        return True
    if re.search(r"博物馆|展览馆|文化馆|小吃街|美食街", name):
        return False
    return bool(re.search(r"餐厅|餐馆|饭店|饭馆|小吃|煲仔饭|肠粉|鱼皮|甜品|糖水|咖啡|茶饮|奶茶|面馆|粥店|饼店|烘焙|酒楼|火锅|烧烤|芝麻糊|\b(?:coffee|cafe|café)\b", name, re.I))


def meal_recommendations(day, stops, excluded=(), used=None, requirements=None, max_options=2):
    visits = [item for item in day.get("activities", []) if item.get("kind") == "visit" and not is_dining(item)
              and coordinates(item.get("map_place"))]
    result = []
    # Ablation: no trip-wide restaurant table, so lunch, dinner and later days may repeat a venue.
    used = set() if used is None or ablation.disabled("dedupe") else used
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
            if identity in excluded or canonical_key(stop) in excluded_places or not is_dining(stop) or stop.get("city") != day["city"] or not point or not anchors:
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
            result.append({**{field: stop.get(field) for field in ("id", "name", "source_id", "source_title", "source_url", "evidence", "map_place", "opening_claim", "canonical_place_id", "source_references", "possible_duplicate_ids")},
                           "meal": meal, "nearby_activity": anchor["name"], "distance_meters": round(meters),
                           "note": "与该时段活动点直线距离不超过2公里；营业时间、道路通行和实际消费需核对"})
            if not ablation.disabled("dedupe"):
                used.add(key)
            if sum(item["meal"] == meal for item in result) >= max_options:
                break
    return result


def refresh_meals(itinerary, stops, excluded=(), dates=None, requirements=None):
    used = set()
    for day in itinerary.get("days",[]):
        if dates is not None and day["date"] not in dates:
            used.update(canonical_key(item,day["city"]) for item in day.get("meal_recommendations",[]))
    for day in itinerary.get("days",[]):
        if dates is None or day["date"] in dates:
            day["meal_recommendations"] = meal_recommendations(day,stops,excluded,used,requirements,max_options=1)
    # Give later meals a first recommendation before filling optional second choices.
    for day in itinerary.get("days",[]):
        if dates is None or day["date"] in dates:
            extras = meal_recommendations(day,stops,excluded,used,requirements,max_options=1)
            for item in extras:
                current = day["meal_recommendations"]
                if not any(canonical_key(row,day["city"]) == canonical_key(item,day["city"]) and row["meal"] == item["meal"] for row in current):
                    current.append(item)
