"""Deterministic constraints; missing supplier facts remain unknown."""
from datetime import date, timedelta

from .rules import resource_cost, timestamp


def resource_context_issues(itinerary, req):
    days = itinerary.get("days",[])
    expected, origin = {},req.get("origin")
    for index,day in enumerate(days):
        if index and day["city"] == days[index-1]["city"]:
            continue
        if origin and origin != day["city"] and not (origin == req.get("origin") and req.get("self_arranged_round_trip")):
            expected[(origin,day["city"])] = day["date"]
        origin = day["city"]
    if days and req.get("return_to_origin",True) and origin != req.get("origin") and not req.get("self_arranged_round_trip"):
        expected[(origin,req["origin"])] = req["end_date"]
    issues = []
    for group in ("transport","hotels"):
        for entry in itinerary.get(group,[]):
            if group == "hotels" and not entry.get("selected_by_user"):
                continue
            ctx, option = entry.get("context",{}),entry.get("selected") or {}
            if group == "transport":
                target = {**ctx,"date":expected.get((ctx.get("origin"),ctx.get("destination")))}
                fields = ("origin","destination","date")
                times = [timestamp(option.get(field),target.get("date") or req.get("start_date","")) for field in ("departure","arrival")]
                mismatch = not target["date"] or any(value and value.date().isoformat() != target["date"] for value in times)
            else:
                dates = [day["date"] for day in days if day["city"] == entry.get("city")]
                target = {"city":entry.get("city"),"check_in":dates[0] if dates else None,
                          "check_out":min((date.fromisoformat(dates[-1])+timedelta(days=1)).isoformat(),req["end_date"]) if dates else None}
                fields = ("city","check_in","check_out")
                mismatch = not dates or any(option.get(field) and option[field] != target[field] for field in ("check_in","check_out"))
            mismatch |= any(ctx.get(field) != target.get(field) for field in fields)
            quoted_context = option.get("context") or {}
            mismatch |= any(field in quoted_context and quoted_context[field] != target.get(field) for field in fields)
            if mismatch:
                issues.append({"code":"resource_context","target":entry["id"],"blocking":True,
                    "message":(entry.get("label") or entry.get("city") or "资源")+"的日期或城市与当前行程不一致，旧资源不能作为本次安排，请重新查询"})
    return issues


def same_resource(first, second):
    if first.get("kind") != second.get("kind") or first.get("context") != second.get("context"):
        return False
    if first.get("kind") == "hotel":
        fields = ("supplier_id","name","address","bed_type")
    else:
        fields = ("name","departure_place","arrival_place","seat_class")
        day = first.get("context",{}).get("date","")
        if any(not timestamp(first.get(field),day) or timestamp(first.get(field),day) != timestamp(second.get(field),day)
               for field in ("departure","arrival")):
            return False
        leg_fields = ("marketingTransportNo","transportNo","depDateTime","arrDateTime","depStationName","arrStationName","seatClassName","depTerm","arrTerm")
        if [[leg.get(field) for field in leg_fields] for leg in first.get("legs",[])] != [
                [leg.get(field) for field in leg_fields] for leg in second.get("legs",[])]:
            return False
    return all(first.get(field) == second.get(field) for field in fields)


def chosen_hotel(itinerary, city, day_date=None):
    for entry in itinerary.get("hotels",[]):
        context = entry.get("context",{})
        if entry.get("city") == city and entry.get("selected_by_user") and entry.get("selected") and (
            not day_date or not context.get("check_in") or context["check_in"] <= day_date <= context["check_out"]):
            return entry["selected"]
    return None


def recommended_hotel_areas(document, city):
    if not document.get("itinerary_outdated"):
        for entry in (document.get("itinerary") or {}).get("hotels", []):
            if entry.get("city") == city and entry.get("search_areas"):
                return list(dict.fromkeys(entry["search_areas"]))[:3]
    for candidate in document.get("candidates", [])[:1]:
        for entry in (candidate.get("feasibility") or {}).get("hotels", []):
            if entry.get("city") == city and entry.get("area"):
                return list(dict.fromkeys(entry["area"]))[:3]
    return []


def minute(value):
    if not value:
        return None
    hour, minutes = map(int, value.split(":"))
    return hour * 60 + minutes


def transport_issues(option, context, req):
    issues = []
    dep = timestamp(option.get("departure"), context["date"])
    arr = timestamp(option.get("arrival"), context["date"])
    if dep and dep.date().isoformat() != context["date"]:
        issues.append("出发日期不匹配")
    if dep and minute(req.get("earliest_departure")) is not None and dep.hour*60+dep.minute < minute(req["earliest_departure"]):
        issues.append("早于允许的出发时间")
    deadline = req.get("latest_return") if context.get("destination") == req.get("origin") else req.get("latest_arrival")
    if arr and deadline and arr.hour*60+arr.minute > minute(deadline):
        issues.append("晚于允许的到达时间")
    if dep and arr and arr <= dep:
        issues.append("到离时间顺序异常")
    if arr and arr.date().isoformat() != context["date"]:
        issues.append("跨日到达，需要调整城市停留日期")
    if dep and arr and req.get("max_transport_hours") and (arr-dep).total_seconds() > req["max_transport_hours"]*3600:
        issues.append("超过允许的城际耗时")
    if req.get("direct_only", True) and len(option.get("legs", [])) > 1:
        issues.append("不符合直达要求")
    return issues


def comparison(option, req):
    cost = resource_cost(option, req)
    return {"id":option["id"], "name":option["name"], "known_cost":float(cost) if cost is not None else None,
            "travel_minutes":option.get("door_minutes"), "walking_meters":option.get("walking_meters"),
            "commute_minutes":option.get("commute_minutes"), "issues":option.get("constraint_issues", []),
            "unknown":option.get("comparison_unknown", [])}


def resource_changes(old, new):
    changes = []
    for group in ("transport", "hotels"):
        previous = {item["id"]:item.get("selected") or {} for item in old.get(group, [])}
        for item in new.get(group, []):
            before, after = previous.get(item["id"], {}), item.get("selected") or {}
            fields = [("name", "选择"), ("price", "展示价"), ("departure", "出发"), ("arrival", "到达")]
            details = [f"{label}：{before.get(field, '待确认')} → {after.get(field, '待确认')}" for field,label in fields if before.get(field) != after.get(field)]
            if details:
                changes.append(item.get("label", item["id"]) + "；" + "；".join(details))
    previous_days = {day["date"]:day for day in old.get("days", [])}
    for day in new.get("days", []):
        before = previous_days.get(day["date"])
        if before and [(a["id"], a.get("start"), a.get("end")) for a in before.get("activities", [])] != [(a["id"], a.get("start"), a.get("end")) for a in day.get("activities", [])]:
            changes.append(day["date"] + "活动或时间已重排")
        if before and (before.get("weather") or {}).get("dayweather") != (day.get("weather") or {}).get("dayweather"):
            changes.append(day["date"] + "天气预报已更新")
    return changes
