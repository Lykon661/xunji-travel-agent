"""Geographic ordering and checks against the actual daily schedule."""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def reference_opening(stop,day_date=None):
    from .opening import reference
    return reference(stop,day_date)


def coordinates(place):
    try:
        longitude, latitude = map(float, place["location"].split(","))
        if math.isfinite(longitude) and math.isfinite(latitude) and -180 <= longitude <= 180 and -90 <= latitude <= 90:
            return longitude, latitude
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def daily_route_scope(itinerary, day):
    hotel_confirmed = any(entry.get("city") == day.get("city") and entry.get("selected_by_user")
        and (entry.get("selected") or {}).get("name")
        and (not entry.get("context",{}).get("check_in") or
             entry["context"]["check_in"] <= day["date"] <= entry["context"]["check_out"])
        for entry in itinerary.get("hotels",[]))
    segments = [entry for entry in itinerary.get("transport",[]) if entry.get("selected")
                and entry.get("context",{}).get("date") == day["date"]]
    return {"hotel_location_confirmed":bool(hotel_confirmed),
            "first_leg_required":bool(hotel_confirmed or any(entry["context"].get("destination") == day.get("city") for entry in segments)),
            "last_leg_required":bool(hotel_confirmed or any(entry["context"].get("origin") == day.get("city") for entry in segments))}


def selected_route_resources(itinerary):
    return {**itinerary,"transport":[entry for entry in itinerary.get("transport",[]) if entry.get("selected")],
            "hotels":[entry for entry in itinerary.get("hotels",[]) if entry.get("selected_by_user")
                      and (entry.get("selected") or {}).get("name")]}


def scoped_day(itinerary, day):
    scope = daily_route_scope(itinerary,day)
    result = {**day,"activities":[dict(item) for item in day.get("activities",[])]}
    if not scope["first_leg_required"]:
        result["start_place"] = None
        first = next((item for item in result["activities"] if item.get("kind") == "visit"),None)
        if first:
            first["move_before"] = {}
    if not scope["last_leg_required"]:
        result.update(end_place=None,return_move={},return_start=None,return_end=None,tail_arrival=None)
    if not scope["hotel_location_confirmed"]:
        result["transfers"] = [item for item in day.get("transfers",[]) if not re.search(r"住宿|酒店",item.get("label",""))]
        result["gaps"] = [text for text in day.get("gaps",[]) if not re.search(r"住宿|酒店|出发地址|结束地址",text)]
        result["end_label"] = "结束当天安排"
        for entry in itinerary.get("transport",[]):
            selected,ctx = entry.get("selected") or {},entry.get("context") or {}
            if not selected or ctx.get("date") != day["date"]:
                continue
            if ctx.get("destination") == day.get("city"):
                result["start_place"] = selected.get("arrival_place")
                first = next((item for item in result["activities"] if item.get("kind") == "visit"),None)
                move = (first or {}).get("move_before") or {}
                if first and result["start_place"] and move.get("origin") and move["origin"] != result["start_place"]:
                    first["move_before"] = {}
            if ctx.get("origin") == day.get("city"):
                result["end_place"] = selected.get("departure_place")
                result["end_label"] = "前往出发车站或机场"
                move = result.get("return_move") or {}
                if result["end_place"] and move.get("destination") and move["destination"] != result["end_place"]:
                    result.update(return_move={},return_start=None,return_end=None,tail_arrival=None)
    return result


def transport_access_transfers(itinerary,day):
    """Attach only intercity access legs not already represented by the day's route."""
    represented = [activity.get("move_before") or {} for activity in day.get("activities",[])]
    represented += [day.get("return_move") or {}]+[item["move"] for item in day.get("transfers",[])]
    represented = [(day["city"],move) for move in represented]
    def same_endpoint(left,right,field):
        first,last = left.get(field+"_place") or {},right.get(field+"_place") or {}
        if first.get("id") and last.get("id"):
            return first["id"] == last["id"]
        a,b = coordinates(first),coordinates(last)
        if a and b:
            return a == b
        a,b = first.get("name") or left.get(field),last.get("name") or right.get(field)
        return bool(a and b and a.strip() == b.strip())
    result = []
    for entry in itinerary.get("transport",[]):
        selected,ctx = entry.get("selected") or {},entry.get("context",{})
        for timing,field,city_key,boundary,label in (
                ("departure","departure_transfer","origin","destination","出发接驳"),
                ("arrival","arrival_transfer","destination","origin","到达接驳")):
            move = selected.get(field) or {}
            if not all(move.get(endpoint) or (move.get(endpoint+"_place") or {}).get("name") for endpoint in ("origin","destination")):
                continue
            travel_date = ctx.get("date")
            value = selected.get(timing)
            if isinstance(value,str) and re.match(r"\d{4}-\d{2}-\d{2}T|\d{4}-\d{2}-\d{2} ",value):
                try:
                    moment = datetime.fromisoformat(value.replace("Z","+00:00"))
                    travel_date = (moment.astimezone(ZoneInfo("Asia/Shanghai")) if moment.tzinfo else moment).date().isoformat()
                except ValueError:
                    pass
            if travel_date != day["date"]:
                continue
            # The actual station leg supersedes the option's comparison leg, even after a stop/hotel changes.
            city = ctx.get(city_key)
            if any(city == other_city and ((city == day["city"] and same_endpoint(move,other,boundary)) or
                   (same_endpoint(move,other,"origin") and same_endpoint(move,other,"destination")))
                   for other_city,other in represented if other):
                continue
            result.append({"label":(city or "")+label,"move":move})
            represented.append((city,move))
    return result


def daily_movements(itinerary,day):
    day = scoped_day(itinerary,day)
    return ([activity.get("move_before") or {} for activity in day.get("activities",[])]+
            [day.get("return_move") or {}]+[item["move"] for item in day.get("transfers",[])]+
            [item["move"] for item in transport_access_transfers(itinerary,day)])


def distance(a, b):
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, value)))
