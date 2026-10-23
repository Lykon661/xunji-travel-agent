from datetime import date, timedelta
from itertools import groupby

from .dining import is_dining
from .routing import coordinates, daily_movements, distance


def medoid(points):
    return min(points,key=lambda point:sum(distance(point,other) for other in points)) if points else None


def area_values(value, anchor=None):
    if isinstance(value,str):
        return [{"name":name.strip(),"point":anchor,"estimated":True} for name in value.replace("；",";").split(";") if name.strip()]
    if isinstance(value,list):
        return [area for item in value for area in area_values(item,anchor)]
    if isinstance(value,dict) and isinstance(value.get("name"),str) and value["name"].strip():
        point = coordinates(value)
        return [{"name":value["name"].strip(),"point":point or anchor,"estimated":point is None}]
    return []


def group_center(days):
    return medoid([day["center"] for day in days if day["center"] is not None])


def group_gap(left, right):
    a,b = group_center(left),group_center(right)
    return distance(a,b) if a is not None and b is not None else 0


def existing_commutes(state):
    itinerary = state.get("itinerary") or {}
    result = {}
    for day in itinerary.get("days",[]):
        for move in daily_movements(itinerary,day):
            a,b = coordinates(move.get("origin_place")),coordinates(move.get("destination_place"))
            minutes = move.get("minutes")
            if a is not None and b is not None and isinstance(minutes,(int,float)):
                result[a,b] = max(minutes,result.get((a,b),0))
                result[b,a] = result[a,b]
    return result


def split_days(days, check_out, commutes):
    nights = (date.fromisoformat(check_out)-date.fromisoformat(days[0]["date"])).days
    if nights < 4 or sum(day["center"] is None for day in days) > len(days)/2:
        return [days]
    groups = []
    for day in days:
        if day["date"] < check_out:
            groups.append([day])
        elif groups:
            groups[-1].append(day)
    def far(left,right):
        a,b = group_center(left),group_center(right)
        return group_gap(left,right) > 8000+1e-6 or commutes.get((a,b),0) > 40
    while len(groups) > 1:
        adjacent = [(group_gap(groups[i],groups[i+1]),i) for i in range(len(groups)-1)
                    if not far(groups[i],groups[i+1])]
        if not adjacent:
            break
        _,index = min(adjacent)
        groups[index:index+2] = [groups[index]+groups[index+1]]
    while len(groups) > 1:
        short = next((i for i,group in enumerate(groups) if sum(day["date"] < check_out for day in group) < 2),None)
        if short is None:
            break
        neighbors = [i for i in (short-1,short+1) if 0 <= i < len(groups)]
        other = min(neighbors,key=lambda i:(group_gap(groups[short],groups[i]),i))
        first,last = sorted((short,other))
        groups[first:last+1] = [groups[first]+groups[last]]
    return groups


def plan_segments(state):
    route = sorted(enumerate(state["selected_route"]["days"],1),key=lambda item:(item[1]["date"],item[0]))
    days = []
    for index,day in route:
        stops = [state.get("stops",{})[identity] for identity in day.get("stop_ids",[]) if identity in state.get("stops",{})]
        stops = [stop for stop in stops if not is_dining(stop)]
        points = [point for stop in stops if (point:=coordinates(stop.get("map_place"))) is not None]
        days.append({**day,"index":index,"stops":stops,"center":medoid(points)})
    runs = []
    for day in days:
        if (not runs or runs[-1][-1]["city"] != day["city"] or
                date.fromisoformat(day["date"])-date.fromisoformat(runs[-1][-1]["date"]) != timedelta(days=1)):
            runs.append([])
        runs[-1].append(day)
    result,commutes = [],existing_commutes(state)
    for run in runs:
        check_out = min((date.fromisoformat(run[-1]["date"])+timedelta(days=1)).isoformat(),state["requirements"]["end_date"])
        if run[0]["date"] >= check_out:
            continue
        groups = split_days(run,check_out,commutes)
        for index,group in enumerate(groups):
            end = groups[index+1][0]["date"] if index+1 < len(groups) else check_out
            center = group_center(group)
            result.append({"city":group[0]["city"],"day_range":{"start":group[0]["index"],"end":group[-1]["index"]},
                           "dates":[day["date"] for day in group],"check_in":group[0]["date"],"check_out":end,
                           "nights":(date.fromisoformat(end)-date.fromisoformat(group[0]["date"])).days,
                           "center":f"{center[0]},{center[1]}" if center is not None else None,
                           "reason":"行程集中，住一处即可","areas":[],"_days":group,"_run":len(result)-index,
                           "_split":len(groups) > 1})
    return result


async def segment_areas(segment, amap, trip_id, budget):
    stops = [stop for day in segment["_days"] for stop in day["stops"]]
    points = [point for stop in stops if (point:=coordinates(stop.get("map_place"))) is not None]
    center = coordinates({"location":segment["center"]})
    candidates = {}
    def collect(areas):
        for area in areas:
            previous = candidates.get(area["name"])
            if previous is None or previous["estimated"] and not area["estimated"]:
                candidates[area["name"]] = {**area,"source":"business_area"}
    for stop in stops:
        place = stop.get("map_place") or {}
        anchor = coordinates(place)
        for field in ("business_area","businessAreas","business_areas"):
            collect(area_values(place.get(field),anchor))
    anchors = list(dict.fromkeys([center]+sorted(points,key=lambda point:distance(center,point),reverse=True))) if center is not None else []
    for anchor in anchors[:2]:
        if len(candidates) >= 2 or budget[0] >= 12 or amap is None:
            break
        budget[0] += 1
        try:
            areas = await amap.business_areas(trip_id,f"{anchor[0]},{anchor[1]}")
            for area in areas:
                collect(area_values(area,anchor))
        except Exception:
            pass
    ordered = sorted(stops,key=lambda stop:distance(center,coordinates(stop.get("map_place")))
                     if center is not None and coordinates(stop.get("map_place")) is not None else float("inf"))
    for stop in ordered:
        if len(candidates) >= 2:
            break
        name = stop.get("name") or (stop.get("map_place") or {}).get("name")
        if name and name not in candidates:
            candidates[name] = {"name":name,"point":coordinates(stop.get("map_place")),"source":"poi_fallback","estimated":False}
    result = []
    for area in candidates.values():
        distances = [distance(area["point"],point)/1000 for point in points] if area["point"] is not None else []
        avg,max_km = (sum(distances)/len(distances),max(distances)) if distances else (None,None)
        reason = f"到本段景点平均约{avg:.1f}公里，最远约{max_km:.1f}公里" if distances else "靠近本段景点，距离待确认"
        if distances and area["estimated"]:
            reason += "（按景点或逆地理查询锚点估算）"
        result.append({"name":area["name"],"source":area["source"],"reason":reason,
                       "avg_km":round(avg,2) if avg is not None else None,"max_km":round(max_km,2) if max_km is not None else None})
    return sorted(result,key=lambda area:(area["avg_km"] is None,area["avg_km"] or 0,area["name"]))[:3]


async def build_hotel_area_segments(state, amap=None):
    segments,budget = plan_segments(state),[0]
    for segment in segments:
        segment["areas"] = await segment_areas(segment,amap,state.get("trip_id"),budget)
    for _,run in groupby(segments,key=lambda segment:segment["_run"]):
        run = list(run)
        for index,segment in enumerate(run):
            if segment["_split"]:
                other = run[index+1] if index+1 < len(run) else run[index-1]
                a,b = coordinates({"location":segment["center"]}),coordinates({"location":other["center"]})
                name = segment["areas"][0]["name"] if segment["areas"] else "本段景点"
                gap = f"，与相邻住宿段相距约{distance(a,b)/1000:.1f}公里" if a is not None and b is not None else ""
                segment["reason"] = f"第{segment['day_range']['start']}–{segment['day_range']['end']}天集中在{name}一带{gap}，建议分开住"
            for key in ("_days","_run","_split"):
                del segment[key]
    return segments
