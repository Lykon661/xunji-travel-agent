"""Geographic ordering and checks against the actual daily schedule."""
import math
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


def early_finish(day):
    start, deadline = day["available_start"], day["tail_deadline"]
    visits = [activity for activity in day["activities"] if activity["kind"] == "visit"]
    finish = max((int(activity["end"][:2])*60+int(activity["end"][3:5]) for activity in visits),default=start)
    occupied = max([finish]+[int(activity["end"][:2])*60+int(activity["end"][3:5]) for activity in day["activities"] if activity.get("meal") == "lunch"])
    move = day.get("return_move") or {}
    tail = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
    remaining = max(0,deadline-occupied-(20+tail if visits else 0))
    # This triggers one review, not a minimum visit duration or a requirement to fill the evening.
    if deadline-start >= 360 and finish < 17*60 and remaining >= 180:
        return {"last_visit_end":f"{finish//60:02d}:{finish%60:02d}","remaining_minutes":remaining,
                "scheduled_activities":day["activities"],"schedule_gaps":day.get("gaps",[])}
    return None


def opening_window(claim):
    match = re.search(r"(?<!\d)(\d{1,2}):([0-5]\d)\s*(?:-|—|–|~|～|至|到)\s*(\d{1,2}):([0-5]\d)",claim or "")
    if not match:
        return None
    first,minute,last,end_minute = map(int,match.groups())
    start,end = first*60+minute,last*60+end_minute
    return (start,end) if 0 <= first < 24 and 0 <= last < 24 and start < end else None


def _daily_opening_windows(claim):
    pattern = r"(?<!\d)(\d{1,2}):([0-5]\d)\s*(?:-|—|–|~|～|至|到)\s*(\d{1,2}):([0-5]\d)"
    remainder = re.sub(pattern,"",claim)
    remainder = re.sub(r"(?:周一|星期一)\s*(?:至|到|-|—|~|～)\s*(?:周日|周天|星期日|星期天)|每日|每天|营业时间|开放时间|开放时段|营业时段|[\s,，、;；()（）:：]","",remainder)
    windows = []
    for match in re.finditer(pattern,claim):
        first,minute,last,end_minute = map(int,match.groups())
        start,end = first*60+minute,last*60+end_minute
        if not (0 <= first < 24 and (0 <= last < 24 or last == 24 and end_minute == 0) and start < end):
            return None
        windows.append([start,end])
    return sorted(windows) if windows and not remainder else None


def _dated_opening_windows(claim,day_date):
    try:
        target = date.fromisoformat(day_date)
    except (ValueError,TypeError):
        return None
    period = r"^\s*(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?\s*(?:-|—|–|至|到|~|～)\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日?\s*[:：]?\s*"
    applicable = []
    for clause in re.split(r"[;；\n]",claim):
        match = re.match(period,clause)
        if not match:
            # Do not interpret a dated exception mixed with an unparsed general schedule.
            if clause.strip():
                return None
            continue
        year,month,day,last_year,last_month,last_day = match.groups()
        month,day,last_month,last_day = int(month),int(day),int(last_month or month),int(last_day)
        begin_year = int(year) if year else target.year-int(last_month < month and target.month <= last_month)
        finish_year = int(last_year) if last_year else begin_year+int(last_month < month)
        try:
            beginning,ending = date(begin_year,month,day),date(finish_year,last_month,last_day)
        except ValueError:
            return None
        if ending < beginning:
            return None
        if beginning <= target <= ending:
            text = clause[match.end():]
            text = re.sub(r"注[:：]营业时间与设施运营计划如有调整[，,]请以官网/现场公示为准[。.]?$","",text)
            if re.fullmatch(r"\s*(?:全天不开放|闭馆|闭园|休馆|不营业|暂停开放)\s*",text):
                applicable.append({"windows":[],"closed":True,"date_range":[beginning.isoformat(),ending.isoformat()]})
                continue
            windows = _daily_opening_windows(text)
            if not windows:
                return None
            applicable.append({"windows":windows,"date_range":[beginning.isoformat(),ending.isoformat()]})
    if len(applicable) != 1:
        return None
    return {**applicable[0],"applicable_date":day_date}


def _calendar_opening_windows(claim,day_date):
    """Read explicit month/weekday clauses; leave holidays and unknown conditions unresolved."""
    try:
        target = date.fromisoformat(day_date)
    except (ValueError,TypeError):
        return None
    claim = re.sub(r"星期|礼拜","周",claim).replace("周天","周日")
    claim = re.sub(r"[,，]\s*(?=周[一二三四五六日])","；",claim)
    weekdays = "一二三四五六日"
    month_range = r"(\d{1,2})月(?:\s*[-—–至到~～]\s*(\d{1,2})月)?"
    pattern = (r"(?:(?P<months>"+month_range+r"(?:\s*[,，、]\s*"+month_range+r")*)\s+)?"
               r"周(?P<first>[一二三四五六日])(?:\s*[-—–至到~～]\s*(?:周)?(?P<last>[一二三四五六日]))?\s*[,，:：]?\s*(?P<hours>.+)")
    applicable, default_windows = [],None
    for clause in re.split(r"[;；\n]",claim):
        if not clause.strip():
            continue
        match = re.fullmatch(pattern,clause.strip())
        if not match:
            daily = _daily_opening_windows(clause.strip())
            if daily and default_windows is None:
                default_windows = daily
                continue
            return None
        months = set(range(1,13))
        if match["months"]:
            months = set()
            for first,last in re.findall(month_range,match["months"]):
                first,last = int(first),int(last or first)
                if not 1 <= first <= last <= 12:
                    return None
                months.update(range(first,last+1))
        first,last = weekdays.index(match["first"]),weekdays.index(match["last"] or match["first"])
        if first > last:
            return None
        applies = target.month in months and first <= target.weekday() <= last
        text = match["hours"]
        if re.fullmatch(r"(?:全天不开放|闭馆|闭园|休馆|不营业|暂停开放)(?:[（(]法定节假日除外[）)])?",text):
            if applies:
                if "节假日" in text:
                    return None
                applicable.append({"windows":[],"closed":True})
            continue
        admission = re.search(r"\s*最晚(?:进入|入场|入馆|入园)\s*([0-2]?\d):([0-5]\d)$",text)
        last_entry = int(admission[1])*60+int(admission[2]) if admission else None
        hours = text[:admission.start()] if admission else text
        windows = _daily_opening_windows(re.sub(r"开放$","",hours.strip()))
        if not windows or last_entry is not None and not any(begin <= last_entry <= end for begin,end in windows):
            return None
        if applies:
            applicable.append({"windows":windows,"last_entry_minutes":last_entry})
    if not applicable and default_windows:
        applicable = [{"windows":default_windows}]
    return {**applicable[0],"applicable_date":day_date} if len(applicable) == 1 else None


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


def route_issue_applies(itinerary, issue):
    days = itinerary.get("days",[])
    target = issue.get("target")
    affected = [day for day in days if day["date"] in issue.get("dates",[]) or target == day["date"]
                or day["date"] in issue.get("message","") or any(target and target in {item.get("id"),item.get("name")}
                    for item in day.get("activities",[]))]
    match = re.match(r"^/days/(\d+)(?:/|$)",issue.get("field_path",""))
    if match and int(match[1]) < len(days):
        affected.append(days[int(match[1])])
    affected = affected or days
    text = issue.get("message","")
    if affected and re.search(r"住宿|酒店|出发地址|出发点|结束地址|起点地址",text):
        if all(not daily_route_scope(itinerary,day)["hotel_location_confirmed"] for day in affected):
            return False
    if affected and issue.get("code") == "tail_conflict":
        return any(daily_route_scope(itinerary,day)["last_leg_required"] for day in affected)
    return True


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


def geographic_order(ids, places, origin=None, destination=None):
    if len(ids) < 2 or len(ids) > 12 or any(coordinates(places.get(identity)) is None for identity in ids):
        return list(ids)
    points = [coordinates(places[identity]) for identity in ids]
    start, finish = coordinates(origin), coordinates(destination)
    # Bound the exact search to the existing 12-stop daily limit; never use distance as travel time.
    costs = {(1 << i, i):(distance(start, point) if start else 0, (i,)) for i, point in enumerate(points)}
    for mask in range(1, 1 << len(ids)):
        for last in range(len(ids)):
            if (mask, last) not in costs:
                continue
            cost, path = costs[mask, last]
            for following in range(len(ids)):
                if mask & (1 << following):
                    continue
                key = (mask | (1 << following), following)
                candidate = (cost + distance(points[last], points[following]), path + (following,))
                if key not in costs or candidate < costs[key]:
                    costs[key] = candidate
    full = (1 << len(ids))-1
    _, order = min((costs[full, i][0] + (distance(points[i], finish) if finish else 0), costs[full, i][1]) for i in range(len(ids)))
    return [ids[i] for i in order]
