from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re

from . import ablation
from .models import Candidate, Requirements
from .dining import is_dining, dining_wishes, dining_wish_issues
from .places import canonicalize_stops, canonical_key, repeat_allowed, represented_stop_ids
from .routing import daily_route_scope, daily_movements, transport_access_transfers, scoped_day
from .constraints import active, walking_target


def walking_limit(req, day_date=None):
    if day_date and day_date in req.get("walking_limit_exempt_dates",[]):
        return None
    # A "less walking" condition with an explicit distance tightens the daily cap.
    from .constraints import dated_walking_limit
    override = dated_walking_limit(req, day_date)
    if override is not None:
        return override
    caps = [item["meters"] for item in active(req,"fewer_walking",date=day_date) if item.get("meters") is not None and item["strength"] == "hard"]
    return min([req.get("max_walk_per_day",8000),*caps])


def ticket_claim_prices(claim):
    number = r"(\d+(?:\.\d+)?)"
    span = number+r"(?:\s*[-–—~～至]\s*"+number+r")?"
    matches = re.findall(span+r"\s*(?:元|[rR]|块)(?![A-Za-z])",str(claim or ""))
    matches += re.findall(r"[￥¥]\s*"+span,str(claim or ""))
    return [float(value) for pair in matches for value in pair if value and float(value) > 0]


def ticket_policy(item):
    if item.get("category") == "dining":
        return "not_applicable"
    claim = item.get("ticket_claim") or ""
    prices = ticket_claim_prices(claim)
    paid_claim = re.sub(r"不收费|无需付费|不收门票","",claim)
    if not any(value > 0 for value in prices) and not re.search(r"收费|付费",paid_claim) and (claim.strip() == "免费" or re.search(r"免门票|(?:无需|不需要)(?:门票|购票)|不收(?:费|门票)|免费(?:开放|入园|进入)|门票[：:\s]*(?:免费|0\s*元)",claim)):
        return "free"
    if prices or re.search(r"收费|付费|需(?:要)?购票|门票[：:\s]*\d+|票价[：:\s]*\d+",paid_claim):
        return "paid"
    return "unspecified"


def ticket_query_required(item):
    place = item.get("map_place") or {}
    if str(place.get("typecode") or "").startswith("05") or "餐饮" in str(place.get("type") or ""):
        return False
    return not is_dining(item) and ticket_policy(item) not in {"free","not_applicable"}


def ticket_price_quote(item, quote):
    quote = dict(quote or {})
    if quote.get("status") in {"error","paused","not_configured"} or quote.get("code") in {"flyai_network","flyai_timeout","flyai_rate_limit","rate_limit"}:
        return {**quote,"ticket_status":"unknown","price":0,"assumed_free":False,"price_unknown":True,
                "query_error":quote.get("query_error") or quote.get("note") or "","note":"","price_note":""}
    if not item and quote.get("ticket_status") in {"free","paid_unpriced"} and quote.get("price") == 0:
        return quote
    policy = ticket_policy(item)
    if quote.get("reported_free") or quote.get("free_poi_status") == "FREE":
        return {**quote,"ticket_status":"free","price":0,"status":"free","reported_free":True,
                "assumed_free":False,"price_unknown":False,"note":"","price_note":""}
    if quote.get("price") is not None and quote["price"] > 0 and not (quote.get("assumed_free") or quote.get("price_unknown")):
        return {**quote,"ticket_status":"paid_priced","note":quote.get("note") or "门票参考价，适用票种与最终结算待核对"}
    options = [option for option in quote.get("options",[]) if option.get("price") is not None and option["price"] > 0 and
               not (option.get("assumed_free") or option.get("price_unknown"))]
    if options:
        return {**quote,"ticket_status":"paid_priced","price":min(option["price"] for option in options),"status":"reference","price_basis":"starting",
                "assumed_free":False,"price_unknown":False,"note":"飞猪参考价，按最低票种起价估算，适用票种与最终结算待核对"}
    prices = ticket_claim_prices(item.get("ticket_claim"))
    if prices:
        return {**quote,"ticket_status":"paid_priced","price":min(prices),"status":"reference","price_basis":"guide_reference",
                "assumed_free":False,"price_unknown":False,"note":"攻略参考价，按攻略最低票价估算，适用票种与最终结算待核对"}
    if quote.get("reported_paid") or quote.get("free_poi_status") == "NOT_FREE" or policy == "paid":
        note = "飞猪标注收费，暂无在售票价，预算按0元计算" if quote.get("reported_paid") or quote.get("free_poi_status") == "NOT_FREE" else "收费，未查询到价格，预算按0元计算"
        return {**quote,"ticket_status":"paid_unpriced","price":0,"assumed_free":True,"price_unknown":True,
                "note":note,"price_note":note}
    if policy == "free":
        return {**quote,"ticket_status":"free","price":0,"assumed_free":False,"price_unknown":False,"note":"","price_note":""}
    return {**quote,"ticket_status":"unknown","price":0,"assumed_free":False,"price_unknown":True,"note":"","price_note":""}


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)


def resource_cost(option: dict | None, req: dict) -> Decimal | None:
    if not option or option.get("currency") != "CNY" or option.get("price") is None:
        return None
    price = money(option["price"])
    basis = option.get("price_basis")
    if basis == "person_segment" and option["kind"] in {"train","flight"}:
        # FlyAI adultPrice is not a children's quote.
        return price * req["people"] if not req.get("children",0) else None
    if basis == "room_night" and option["kind"] == "hotel":
        ctx = option["context"]
        nights = (date.fromisoformat(ctx["check_out"]) - date.fromisoformat(ctx["check_in"])).days
        # A single-night rate is not a quote for every night of a multi-night stay.
        if nights == 1:
            return price * req["rooms"]
    if basis == "segment_total":
        if option["kind"] == "hotel":
            ctx = option["context"]
            if str(option.get("quote_rooms")) == str(req["rooms"]) and option.get("check_in") == ctx["check_in"] and option.get("check_out") == ctx["check_out"]:
                return price
        elif str(option.get("quote_people")) == str(req["people"]):
            return price
    return None


def timestamp(value, day: str) -> datetime | None:
    if not isinstance(value,str):
        return None
    try:
        if len(value) in {5,8}:
            return datetime.combine(date.fromisoformat(day),time.fromisoformat(value))
        parsed = datetime.fromisoformat(value.replace("Z","+00:00"))
        if parsed.tzinfo:
            from zoneinfo import ZoneInfo
            parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        return parsed
    except (ValueError,TypeError):
        return None


def merge_candidate_duplicates(candidate: Candidate, req: Requirements, stops: dict, locked_ids=()) -> Candidate:
    """Normalize a newly generated proposal; submission gates still validate the exact displayed plan."""
    candidate = candidate.model_copy(deep=True)
    if ablation.disabled("dedupe"):
        return candidate
    stops = canonicalize_stops(stops)
    requirements = req.model_dump(mode="json")
    locked = set(locked_ids)
    suppressed = represented_stop_ids({"stops":stops,"requirements":requirements,"locks":list(locked)})
    for day in candidate.days:
        day.stop_ids = [identity for identity in day.stop_ids if identity not in suppressed]
    if isinstance(candidate.feasibility.get("spatial_unplaced"),list):
        candidate.feasibility["spatial_unplaced"] = [item for item in candidate.feasibility["spatial_unplaced"] if item.get("id") not in suppressed]
    groups = {}
    for day_index,day in enumerate(candidate.days):
        for position,identity in enumerate(day.stop_ids):
            stop = stops.get(identity)
            if not stop or stop.get("city") != day.city or is_dining(stop):
                continue
            if stop.get("assigned_date") and stop["assigned_date"] != day.date.isoformat():
                continue
            key = canonical_key(stop,day.city)
            protected = identity in locked or bool(stop.get("assigned_date") or stop.get("product") or stop.get("source_kind") == "flyai")
            allowed = repeat_allowed(stop,{"date":day.date.isoformat(),"city":day.city},"visit",requirements)
            groups.setdefault(key,[]).append((day_index,position,identity,protected,allowed))
    removed = {}
    for entries in groups.values():
        if len(entries) < 2:
            continue
        protected = [entry for entry in entries if entry[3]]
        keeper = protected[0] if protected else entries[0]
        kept_ids = set()
        for entry in entries:
            day_index,position,identity,is_protected,allowed = entry
            # Date-specific user authorization and adopted experiences must survive normalization.
            keep = entry == keeper or is_protected or allowed
            if keep and (day_index,identity) not in kept_ids:
                kept_ids.add((day_index,identity))
                continue
            removed.setdefault(day_index,set()).add(position)
    for day_index,positions in removed.items():
        day = candidate.days[day_index]
        names = list(dict.fromkeys(stops[identity].get("name") or identity
                                 for position,identity in enumerate(day.stop_ids) if position in positions))
        day.stop_ids = [identity for position,identity in enumerate(day.stop_ids) if position not in positions]
        note = "合并同一地点的重复攻略引用："+"、".join(names)+"；保留一次游览及全部攻略来源"
        day.adjustment = "；".join(value for value in (day.adjustment,note) if value)
    return candidate


def validate_candidate(candidate: Candidate, req: Requirements, stops: dict) -> Candidate:
    stops = canonicalize_stops(stops)
    if ablation.disabled("code_checks"):
        # Ablation: accept the model's dates, cities and order; only drop IDs that would crash later nodes.
        for day in candidate.days:
            day.stop_ids = [stop_id for stop_id in dict.fromkeys(day.stop_ids)
                            if stop_id in stops and stops[stop_id].get("city") == day.city and not is_dining(stops[stop_id])]
        return candidate
    if not req.start_date or not req.end_date:
        raise ValueError("请先设定旅行起止日期")
    dates = [req.start_date + timedelta(days=i) for i in range((req.end_date-req.start_date).days+1)]
    if [d.date for d in candidate.days] != dates:
        raise ValueError(f"候选路线必须恰好为 {len(dates)} 天，并按顺序覆盖 {req.start_date} 至 {req.end_date} 的每个日期；不能多天、缺天、重复或越界")
    cities = [d.city for d in candidate.days]
    compressed = [city for i,city in enumerate(cities) if i == 0 or city != cities[i-1]]
    if compressed != req.cities:
        raise ValueError("候选路线改变了用户指定的城市顺序")
    seen, seen_places = set(),set()
    for day in candidate.days:
        if len(day.stop_ids) != len(set(day.stop_ids)):
            raise ValueError("同一天不能重复使用同一活动ID")
        for stop_id in day.stop_ids:
            if stop_id not in stops or stops[stop_id]["city"] != day.city:
                raise ValueError("候选路线引用了未知或不同城市的攻略活动")
            if is_dining(stops[stop_id]):
                raise ValueError("餐厅和咖啡店只能作为餐饮推荐，不能作为景点安排")
            if stops[stop_id].get("assigned_date") and stops[stop_id]["assigned_date"] != day.date.isoformat():
                raise ValueError("用户采用的体验商品不能移动到未经确认的日期")
            allowed_repeat = repeat_allowed(stops[stop_id],{"date":day.date.isoformat(),"city":day.city},"visit",req.model_dump(mode="json"))
            if stop_id in seen and not allowed_repeat:
                raise ValueError("候选路线重复使用同一攻略活动")
            identity = canonical_key(stops[stop_id],day.city)
            if identity in seen_places and not allowed_repeat:
                raise ValueError("不同攻略引用了同一高德地点，路线不能重复安排该景点")
            seen_places.add(identity)
            seen.add(stop_id)
        source_groups = {}
        for stop_id in day.stop_ids:
            stop = stops[stop_id]
            source_id = stop.get("source_id",stop_id.split(":")[0])
            source_groups.setdefault(source_id,[]).append(stop_id)
        if len(source_groups) > 1 and not day.adjustment:
            raise ValueError("组合不同攻略的每日路线时必须说明调整原因")
        for source_id,ids in source_groups.items():
            original = [key for key,value in stops.items() if value.get("source_id",key.split(":")[0]) == source_id and key in ids]
            if ids != original and not day.adjustment:
                raise ValueError("改变原攻略活动顺序时必须说明调整原因")
        if not day.stop_ids:
            candidate.gaps.append(f"{day.date} {day.city} 缺少可追溯的攻略活动")
    if not seen and not dining_wishes(stops,req.model_dump(mode="json")):
        raise ValueError("候选路线没有采用任何可追溯的攻略活动")
    return candidate


def constraint_issues(itinerary: dict, req: dict):
    """Check the user constraints a finished plan can show: order, appointment times, visit lengths, walking and transit."""
    issues, notes, placed = [], [], {}
    for day in itinerary.get("days",[]):
        for index,activity in enumerate(day.get("activities",[])):
            if activity.get("kind") == "visit":
                placed.setdefault(activity["id"],(day["date"],index,activity))
    def violated(item, target, message, blocking=None, **extra):
        issues.append({"code":"user_constraint","constraint_id":item["id"],"target":target,
                       "message":message+"（用户要求："+item["quote"]+"）",
                       "blocking":item["strength"] == "hard" if blocking is None else blocking,**extra})
    def in_scope(item, *found):
        return all(entry[0] in item["scope"]["dates"] for entry in found) if item["scope"]["dates"] else True
    for item in active(req,"fixed_order"):
        first,second = placed.get(item["before"]),placed.get(item["after"])
        if first and second and in_scope(item,first,second) and first[:2] > second[:2]:
            violated(item,item["after"],second[2]["name"]+"被安排在"+first[2]["name"]+"之前")
    for kind in ("time_anchor","visit_duration","booking_prerequisite"):
        for item in active(req,kind):
            found = placed.get(item["scope"]["stop_ids"][0])
            if not found or not in_scope(item,found):
                continue
            day_date,_,activity = found
            start,end = timestamp(activity.get("start"),day_date),timestamp(activity.get("end"),day_date)
            if kind == "time_anchor" and item["anchor"] == "appointment" and item["at"] and start != timestamp(item["at"],day_date):
                violated(item,activity["id"],activity["name"]+"安排在"+str(activity.get("start"))+"开始，与预约时刻"+item["at"]+"不符")
            elif kind == "visit_duration" and start and end:
                meal = activity.get("meal_break") if isinstance(activity.get("meal_break"),dict) else {}
                meal_start,meal_end = timestamp(meal.get("start"),day_date),timestamp(meal.get("end"),day_date)
                length = (end-start).total_seconds()/60-((meal_end-meal_start).total_seconds()/60 if meal_start and meal_end else 0)
                if length < item["minutes"]:
                    violated(item,activity["id"],activity["name"]+"实际游玩约"+str(round(length))+"分钟，少于要求的"+str(item["minutes"])+"分钟")
            elif kind == "booking_prerequisite":
                notes.append(activity["name"]+"：按你的要求须先完成预约再前往，请出发前确认预约")
    for day in itinerary.get("days",[]):
        day = scoped_day(itinerary,day)
        visits = sum(activity.get("kind") == "visit" for activity in day.get("activities",[]))
        for item in active(req,"daily_min_stops",date=day["date"]):
            if (not item["scope"]["cities"] or day.get("city") in item["scope"]["cities"]) and visits < item["count"]:
                # An accepted (soft) minimum still rules out a day with no stop at all.
                violated(item,day["date"],day["date"]+("一个景点都没有安排" if not visits else "只安排了"+str(visits)+"个景点")
                         +"，少于每天至少"+str(item["count"])+"个",blocking=item["strength"] == "hard" or not visits)
        target,limit = walking_target(req,day["date"]),walking_limit(req,day["date"])
        walked = day.get("walking_meters") or 0
        if target is not None and limit is not None and target < limit and walked > target:
            item = next(entry for entry in active(req,"fewer_walking",date=day["date"]) if not entry["meters"] or entry["strength"] == "soft")
            # Without a number from the user this is a target, not a cap: it is reported but never blocks.
            violated(item,day["date"],day["date"]+"预计步行约"+str(walked)+"米，超过少走路的目标"+str(target)+"米",blocking=False)
        for item in active(req,"transit_rule",date=day["date"]):
            stops = set(item["scope"]["stop_ids"])
            visits = [activity for activity in day.get("activities",[])
                      if activity.get("kind") == "visit" and (not stops or activity["id"] in stops)]
            if item["rule"] == "public_only":
                moves = [(activity["id"],activity.get("move_before") or {}) for activity in visits]
                if not stops:
                    moves += [(day["date"],day.get("return_move") or {})]+[(day["date"],transfer["move"]) for transfer in day.get("transfers",[])]
                for target,move in moves:
                    if move.get("mode") in {"driving","bicycling"}:
                        violated(item,target,day["date"]+"有一段路线使用了"+("驾车" if move["mode"] == "driving" else "骑行")+"，不是公共交通或步行")
            else:
                for activity in visits:
                    if activity.get("move_before") and activity["move_before"].get("minutes") is None:
                        # The user authorised dropping a stop without a real route; the repair step removes it.
                        violated(item,activity["id"],activity["name"]+"没有查到可用的公共交通路线，按约定应取消而不是保留",repair="drop")
    return issues,notes


def check_itinerary(itinerary: dict, req: dict, stops: dict | None = None) -> dict:
    issues, lines, unknown, information_unknown, constraint_notes = [], [], [], [], []
    mobility = Requirements.model_validate(req).local_transport
    allowed_modes = {"walking","transit"} if mobility == "public" else {mobility}
    if req.get("allow_taxi"):
        allowed_modes.add("driving")
    known = Decimal("0")
    estimated_resources, estimated_tickets, estimated_local = Decimal("0"),Decimal("0"),Decimal("0")
    for group in ("transport","hotels"):
        for entry in itinerary.get(group,[]):
            if group == "hotels" and not entry.get("selected_by_user"):
                unknown.append((entry.get("city") or "住宿")+"酒店未选定，当前行程费用不含住宿")
                continue
            option = entry.get("selected")
            cost = resource_cost(option,req)
            estimate = None
            if cost is None and option and option.get("kind") == "hotel" and option.get("currency") == "CNY" and option.get("price") is not None and (
                option.get("price_basis") == "room_night" or option.get("reference_unit") == "room_night"):
                ctx = option["context"]
                nights = (date.fromisoformat(ctx["check_out"])-date.fromisoformat(ctx["check_in"])).days
                estimate = money(option["price"]) * req["rooms"] * nights
                estimated_resources += estimate
            elif cost is None and option and option.get("kind") in {"flight","train"} and option.get("price_basis") == "person_segment" and option.get("currency") == "CNY" and option.get("price") is not None and req.get("children",0):
                estimate = money(option["price"])*(req["people"]-req["children"])
                estimated_resources += estimate
                unknown.append((entry.get("label") or "交通")+"仅估算成人部分，儿童票价、行李与税费待核对")
            label = entry.get("label") or entry.get("city") or "资源"
            if cost is None:
                unknown.append(label + "报价或费用口径待确认")
            else:
                known += cost
            lines.append({"label":label,"amount":float(cost) if cost is not None else None,"estimated_amount":float(estimate) if estimate is not None else None,"kind":group,
                          "basis":option.get("price_basis") if option else "unknown"})
            if not option:
                issues.append({"code":"resource_missing","message":label + "尚未选定","target":entry["id"],"blocking":False})
            if option and (group != "hotels" or entry.get("selected_by_user")):
                for issue in option.get("constraint_issues",[]):
                    issues.append({"code":"resource_constraint","message":label+"："+issue,"target":entry["id"],"blocking":True})
    estimates = Decimal("0")
    for day in itinerary.get("days",[]):
        original_day = day
        day = scoped_day(itinerary,day)
        activities = day.get("activities",[])
        scope = daily_route_scope(itinerary,day)
        missing_routes = []
        previous = None
        for item in activities:
            start = timestamp(item.get("start"),day["date"])
            end = timestamp(item.get("end"),day["date"])
            if not start or not end or end <= start:
                issues.append({"code":"invalid_time","message":f"{day['date']} {item['name']}时间不完整","target":item["id"],"blocking":True})
            if previous and start and previous > start:
                issues.append({"code":"overlap","message":f"{day['date']} {item['name']}与前一项时间重叠","target":item["id"],"blocking":True})
            move = item.get("move_before") or {}
            travel = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes")
            if previous and start and travel is not None and (start-previous).total_seconds()/60 < travel:
                issues.append({"code":"move_gap","message":item["name"]+"前预留的移动时间不足","target":item["id"],"blocking":True})
            if scope["first_leg_required"] and not previous and start and day.get("available_start") is not None and travel is not None and start.hour*60+start.minute < day["available_start"]+travel:
                issues.append({"code":"move_gap","message":item["name"]+"前未留足从"+("住宿" if scope["hotel_location_confirmed"] else "到达车站或机场")+"出发的移动时间","target":item["id"],"blocking":True})
            previous = end
            if item.get("kind") == "visit" and not item.get("evidence"):
                issues.append({"code":"evidence_missing","message":item["name"]+"缺少攻略依据","target":item["id"],"blocking":True})
            if item.get("kind") == "visit":
                information_unknown.append(item["name"] + "当前开放时间待确认")
                ticket = ticket_price_quote(item,item.get("ticket_quote"))
                if ticket["ticket_status"] == "paid_unpriced":
                    constraint_notes.append(item["name"]+"："+ticket["note"])
                    lines.append({"label":day["date"]+" "+item["name"]+"门票", "amount":0, "estimated_amount":0,
                                  "kind":"tickets", "basis":"assumed_zero"})
                elif ticket["ticket_status"] == "paid_priced":
                    estimate = money(ticket["price"]) * req["people"]
                    estimated_tickets += estimate
                    lines.append({"label":day["date"]+" "+item["name"]+("门票攻略参考" if ticket.get("price_basis") == "guide_reference" else "门票参考"),
                                  "amount":None,"estimated_amount":float(estimate),"kind":"tickets","basis":ticket.get("price_basis") or "starting"})
                    constraint_notes.append(item["name"]+"："+(ticket.get("note") or "门票参考价，实际结算待核对"))
            if item.get("move_before",{}).get("minutes") is None and item.get("move_before"):
                missing_routes.append(item["name"])
        if missing_routes:
            information_unknown.append(day["date"]+"移动时间待确认："+"、".join(missing_routes))
        visits = [item for item in activities if item.get("kind") == "visit"]
        if any(item.get("start","") < "12:00" for item in visits) and any(item.get("end","") > "14:00" for item in visits):
            lunches = [(item,None) for item in activities if item.get("kind") == "rest" and item.get("meal") == "lunch"]
            lunches += [(item["meal_break"],item) for item in visits if isinstance(item.get("meal_break"),dict)]
            reserved = False
            for meal,owner in lunches:
                beginning,ending = timestamp(meal.get("start"),day["date"]),timestamp(meal.get("end"),day["date"])
                if (beginning and ending and (ending-beginning).total_seconds() >= 3600
                        and "11:30" <= meal.get("start","") < meal.get("end","") <= "14:00"
                        and (owner is None or owner.get("start","") <= meal["start"] < meal["end"] <= owner.get("end",""))):
                    reserved = True
                    break
            if not reserved:
                issues.append({"code":"lunch_missing","message":day["date"]+"上午至下午连续游览，未预留60分钟午餐，需要调整当天排程",
                               "target":day["date"],"blocking":True})
        if day.get("activities") and day["activities"][-1].get("end","") > "21:00":
            issues.append({"code":"late_day","message":day["date"]+"安排晚于 21:00，建议减少活动","target":day["date"],"blocking":False})
        meal = money(day.get("meal_estimate",0))
        estimates += meal
        day["transport_transfers"] = transport_access_transfers(itinerary,day)
        movements = daily_movements(itinerary,day)
        if any(move.get("walking_meters") is not None for move in [*movements,
                *[item.get("move_before") or {} for item in original_day.get("activities",[])],original_day.get("return_move") or {}]):
            day["walking_meters"] = sum(move.get("walking_meters") or 0 for move in movements)
        original_day.update(transport_transfers=day["transport_transfers"],walking_meters=day.get("walking_meters",0))
        limit = walking_limit(req,day["date"])
        if limit is not None and day.get("walking_meters",0) > limit:
            issues.append({"code":"walking_limit","message":day["date"]+"预计步行距离超过每日上限","target":day["date"],"blocking":True})
        elif limit is None:
            constraint_notes.append(day["date"]+"按你的最新指令保留规划，本日步行上限已豁免；路线记录的接驳步行约"+str(day.get("walking_meters",0))+"米")
        if any(move.get("mode") == "bicycling" for move in [a.get("move_before",{}) for a in activities]+[day.get("return_move",{})]):
            unknown.append(day["date"]+"骑行租赁或停车费用待确认")
        for transfer in day.get("transfers",[]):
            if transfer["move"].get("minutes") is None:
                information_unknown.append(day["date"]+" "+transfer["label"]+"耗时待确认")
        if scope["last_leg_required"] and day.get("return_move") and day["return_move"].get("minutes") is None:
            information_unknown.append(day["date"]+day.get("end_label","返回住宿")+"路线待确认")
        paid_moves = [move for move in movements if move.get("mode") in {"transit","driving"} or "transit" in move.get("allowed_modes",[])]
        if paid_moves:
            unknown.append(day["date"]+"市内交通费用为路线参考，实际乘车与结算待确认")
        for move in paid_moves:
            if move.get("cost") is not None:
                estimated_local += money(move["cost"]) * req["people"]
        if any(move.get("mode") and move["mode"] not in allowed_modes and move.get("minutes") != 0 for move in movements):
            issues.append({"code":"local_transport_conflict","message":day["date"]+"存在不符合市内交通偏好的路线","target":day["date"],"blocking":True})
        if scope["last_leg_required"] and day.get("tail_arrival") is not None and day.get("tail_deadline") is not None and day["tail_arrival"] > day["tail_deadline"]:
            issues.append({"code":"tail_conflict","message":day["date"]+day.get("end_label","返回住宿")+"的移动时间超出当天可用时间","target":day["date"],"blocking":True})
        elif not scope["last_leg_required"] and day.get("tail_deadline") is not None and any(
                (ending := timestamp(item.get("end"),day["date"])) and ending.hour*60+ending.minute > day["tail_deadline"] for item in activities):
            issues.append({"code":"day_end_conflict","message":day["date"]+"活动结束时间超出当天可用时间","target":day["date"],"blocking":True})
    for entry in itinerary.get("transport",[]):
        selected = entry.get("selected")
        ctx = entry["context"]
        departure = timestamp(selected.get("departure"),ctx["date"]) if selected else None
        arrival = timestamp(selected.get("arrival"),ctx["date"]) if selected else None
        if not departure or not arrival:
            information_unknown.append(entry["label"]+"到离时间待确认")
            continue
        if arrival <= departure:
            issues.append({"code":"transport_time","message":entry["label"]+"到离时刻无法衔接（跨日信息需确认）","target":entry["id"],"blocking":True})
        for day in itinerary.get("days",[]):
            for item in day.get("activities",[]):
                if item["kind"] != "visit":
                    continue
                start = timestamp(item["start"],day["date"]); end = timestamp(item["end"],day["date"])
                if day["city"] == ctx["destination"] and day["date"] == arrival.date().isoformat() and start and start < arrival + timedelta(minutes=60):
                    issues.append({"code":"arrival_conflict","message":item["name"]+"早于到达后进城缓冲结束","target":item["id"],"blocking":True})
                if day["city"] == ctx["origin"] and day["date"] == departure.date().isoformat() and end and end > departure-timedelta(minutes=entry.get("buffer_minutes",60)):
                    issues.append({"code":"departure_conflict","message":item["name"]+"与出发准备时间冲突","target":item["id"],"blocking":True})
    days = itinerary.get("days",[])
    adopted_hotels = [entry for entry in itinerary.get("hotels",[]) if entry.get("selected_by_user") and (entry.get("selected") or {}).get("name")]
    for entry in adopted_hotels:
        ctx = entry["context"]
        if ctx["check_out"] <= ctx["check_in"]:
            issues.append({"code":"hotel_dates","message":entry["city"]+"入住离店日期无效","target":entry["id"],"blocking":True})
    for day in days[:-1]:
        if not any(h["city"] == day["city"] for h in adopted_hotels):
            continue
        covered = any(h["city"] == day["city"] and h.get("selected") and h["context"]["check_in"] <= day["date"] < h["context"]["check_out"] for h in adopted_hotels)
        if not covered:
            issues.append({"code":"hotel_gap","message":day["date"]+"住宿日期未覆盖","target":day["date"],"blocking":True})
    total_estimate = known + estimates + estimated_resources + estimated_tickets + estimated_local
    if total_estimate > money(req["budget"]):
        issues.append({"code":"over_budget","message":f"已知费用加各项估算 {total_estimate} 元超过预算 {money(req['budget'])} 元","target":"budget","blocking":True})
    user_issues,user_notes = constraint_issues(itinerary,req)
    issues.extend(user_issues)
    issues.extend(dining_wish_issues(itinerary,stops,req))
    constraint_notes.extend(user_notes)
    unknown = list(dict.fromkeys(unknown))
    if ablation.disabled("code_checks"):
        issues = []
    return {"issues":issues,"constraint_notes":constraint_notes,"information_unknown":list(dict.fromkeys(information_unknown)),"budget":{"known_subtotal":float(known),"estimated_meals":float(estimates),
            "estimated_resources":float(estimated_resources),"estimated_tickets":float(estimated_tickets),"estimated_local_transport":float(estimated_local),
            "known_plus_estimates":float(total_estimate),"limit":req["budget"],"lines":lines,"unknown":unknown,
            "status":"over" if total_estimate > money(req["budget"]) else ("incomplete" if unknown else "within")}}


def enforce_locks(old: dict, new: dict, locks: list[str]) -> dict:
    import copy
    new = copy.deepcopy(new)
    for group in ("transport","hotels"):
        locked = {item["id"]:item for item in old.get(group,[]) if item["id"] in locks}
        new[group] = [copy.deepcopy(locked.pop(item["id"])) if item["id"] in locked else item for item in new.get(group,[])]
        new[group].extend(copy.deepcopy(list(locked.values())))
    day_map = {d["date"]:d for d in new.get("days",[])}
    for old_day in old.get("days",[]):
        for item in old_day.get("activities",[]):
            if item["id"] not in locks:
                continue
            replacement = next((a for d in new.get("days",[]) for a in d["activities"] if a["id"] == item["id"]),None)
            for new_day in new.get("days",[]):
                new_day["activities"] = [a for a in new_day["activities"] if a["id"] != item["id"]]
            day = day_map.get(old_day["date"])
            if not day:
                day = {**copy.deepcopy(old_day),"activities":[]}
                new.setdefault("days",[]).append(day); day_map[day["date"]] = day
            kept = copy.deepcopy(item)
            if kept.get("kind") == "visit":
                if replacement and replacement.get("move_before"):
                    kept["move_before"] = copy.deepcopy(replacement["move_before"])
                else:
                    kept["move_before"] = {"minutes":None,"estimate_minutes":45,"note":"锁定活动保留原时间，移动路线需重新确认"}
            day["activities"].append(kept)
            day["activities"].sort(key=lambda a:a.get("start",""))
    return new
