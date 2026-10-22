"""代码给出的取舍问题：一天排不下时找出冲突的几项与可行的放宽方式，景点不够时先在已导入的攻略里补查。

每个选项都对应一个结构化操作，用户点选后由代码直接执行，不经过模型理解。
"""
from datetime import date as Date, timedelta
import re

from .constraints import active, merge
from .models import ConstraintDraft
from .dining import is_dining
from .places import canonical_key, canonicalize_stops, local_name
from .routing import coordinates, early_finish
from .rules import timestamp

DEFAULT_VISIT_MINUTES = 90
DEFAULT_MOVE_MINUTES = 30
LUNCH_MINUTES = 60
MAX_OPTIONS = 4
# A guide place of at least this many minutes, or described as taking the whole day, fills its day alone.
FULL_DAY_MINUTES = 360
FULL_DAY = re.compile(r"一整天|整整一天|全天|整天|一天时间")
# A model question about the number of stops, as opposed to time, order or a place choice on the same day.
SHORTAGE = re.compile(r"每天至少|每日至少|(?:景点|地点)(?:数量|不足|不够)")


def _label_date(day):
    value = Date.fromisoformat(day)
    return f"{value.month}月{value.day}日"


def _visit_minutes(activity, day):
    start,end = timestamp(activity.get("start"),day),timestamp(activity.get("end"),day)
    if not start or not end:
        return None
    meal = activity.get("meal_break") if isinstance(activity.get("meal_break"),dict) else {}
    meal_start,meal_end = timestamp(meal.get("start"),day),timestamp(meal.get("end"),day)
    return round((end-start).total_seconds()/60-((meal_end-meal_start).total_seconds()/60 if meal_start and meal_end else 0))


def _days(state, itinerary):
    route = {day["date"]:day for day in (state.get("selected_route") or {}).get("days",[])}
    return [(day,route.get(day["date"],{})) for day in itinerary.get("days",[])]


def _scheduled(itinerary):
    return {activity["id"]:(day,activity) for day in itinerary.get("days",[])
            for activity in day.get("activities",[]) if activity.get("kind") == "visit"}


def _window(day):
    """Usable minutes of a day for visits, moves and lunch, after the trip back to the hotel or station."""
    start,deadline = day.get("available_start"),day.get("tail_deadline")
    if start is None or deadline is None:
        return None,False
    move = day.get("return_move") or {}
    tail = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
    return deadline-start-((20+(tail or 0)) if move else 0),start < 13*60 < deadline


def _mandatory(state, identity, day):
    """Why a stop cannot simply be dropped; empty when it is optional."""
    reasons = []
    if identity in state.get("locks",[]):
        reasons.append("已锁定")
    if identity in (state.get("stop_constraints") or {}).get("required",[]):
        reasons.append("必去")
    if identity in state.get("change",{}).get("user_selected_ids",[]):
        reasons.append("你选定")
    req = state.get("requirements",{})
    reasons += ["你要求游玩"+str(item["minutes"])+"分钟" for item in active(req,"visit_duration",date=day,stop_id=identity)]
    reasons += ["你要求"+(item["at"] or {"sunrise":"日出","sunset":"日落","appointment":"预约"}[item["anchor"]])+"到达"
                for item in active(req,"time_anchor",date=day,stop_id=identity) if item["anchor"]]
    return reasons


def day_demand(state, itinerary, plan_day, route_day, unplaced=True):
    """Each intended stop of a day with its estimated visit+move minutes and why it is mandatory.

    unplaced adds required stops that no day holds yet; it is off when measuring another day's spare time.
    """
    day = plan_day["date"]
    scheduled = {activity["id"]:activity for activity in plan_day.get("activities",[]) if activity.get("kind") == "visit"}
    anywhere = _scheduled(itinerary)
    intended = list(dict.fromkeys([*route_day.get("stop_ids",[]),*scheduled]))
    # A required stop dropped everywhere still needs a day: its assigned one, or any analysed day in its city.
    intended += [identity for identity in (state.get("stop_constraints") or {}).get("required",[]) if unplaced
                 and identity not in anywhere and identity not in intended
                 and state.get("stops",{}).get(identity,{}).get("assigned_date") in {day,None}
                 and state.get("stops",{}).get(identity,{}).get("city") == plan_day.get("city")]
    known = sorted(move["minutes"] for activity in scheduled.values() if (move := activity.get("move_before") or {}).get("minutes") is not None)
    typical = known[len(known)//2] if known else DEFAULT_MOVE_MINUTES
    items = []
    for identity in intended:
        stop = state.get("stops",{}).get(identity) or scheduled.get(identity)
        if not stop or is_dining(stop):
            continue
        fixed = next((item["minutes"] for item in active(state.get("requirements",{}),"visit_duration",date=day,stop_id=identity)),None)
        activity = scheduled.get(identity)
        visit = fixed or (activity and _visit_minutes(activity,day)) or stop.get("duration_minutes") or DEFAULT_VISIT_MINUTES
        move = ((activity or {}).get("move_before") or {}).get("minutes")
        items.append({"id":identity,"name":stop.get("name") or identity,"visit":visit,
                      "move":move if move is not None else typical,"reasons":_mandatory(state,identity,day)})
    return items


def _need(items, lunch):
    return sum(item["visit"]+item["move"] for item in items)+(LUNCH_MINUTES if lunch else 0)


def _slack(state, itinerary, date, city):
    for plan_day,route_day in _days(state,itinerary):
        if plan_day["date"] == date and plan_day.get("city") == city:
            window,lunch = _window(plan_day)
            if window is None:
                return None
            mandatory = [item for item in day_demand(state,itinerary,plan_day,route_day,unplaced=False) if item["reasons"]]
            return window-_need(mandatory,lunch)
    return None


def capacity_question(state, itinerary, day):
    """When a day's mandatory stops cannot fit, the smallest conflicting set and single changes that fit."""
    plan_day,route_day = next(((plan,route) for plan,route in _days(state,itinerary) if plan["date"] == day),(None,None))
    if not plan_day:
        return None
    window,lunch = _window(plan_day)
    if window is None:
        return None
    items = day_demand(state,itinerary,plan_day,route_day)
    mandatory = [item for item in items if item["reasons"]]
    over = _need(mandatory,lunch)-window
    if over <= 0:
        return None
    # Largest first gives the fewest items that already exceed the window: the conflict to show the user.
    conflict, total = [],LUNCH_MINUTES if lunch else 0
    for item in sorted(mandatory,key=lambda entry:entry["visit"]+entry["move"],reverse=True):
        conflict.append(item); total += item["visit"]+item["move"]
        if total > window:
            break
    options = []
    others = sorted({plan["date"] for plan,_ in _days(state,itinerary)
                     if plan["date"] != day and plan.get("city") == plan_day.get("city")})
    for item in conflict:
        if "已锁定" in item["reasons"]:
            continue
        cost = item["visit"]+item["move"]
        for other in others:
            slack = _slack(state,itinerary,other,plan_day.get("city"))
            if slack is not None and slack >= cost and not active(state.get("requirements",{}),"time_anchor",date=day,stop_id=item["id"]):
                options.append(("把"+item["name"]+"改到"+_label_date(other),{"type":"move","stop_id":item["id"],"date":day,"to":other}))
                break
        shorter = item["visit"]-over
        if shorter >= max(60,item["visit"]//2):
            options.append((item["name"]+"游玩时长缩短到"+str(shorter)+"分钟",
                            {"type":"shorten","stop_id":item["id"],"date":day,"minutes":shorter}))
        if "必去" in item["reasons"] and cost >= over:
            options.append(("这次不去"+item["name"],{"type":"exclude","stop_id":item["id"],"date":day}))
    rank = {"move":0,"shorten":1,"exclude":2}
    options = sorted(options,key=lambda option:rank[option[1]["type"]])[:MAX_OPTIONS]
    if not options:
        return None
    listed = "、".join(item["name"]+"（"+"，".join(item["reasons"])+"，约"+str(item["visit"])+"分钟）" for item in conflict)
    return {"id":"capacity-"+day,"title":_label_date(day)+"安排不下，需要取舍",
            "question":(_label_date(day)+"可用约"+str(window)+"分钟，以下几项加上路程"+("和午餐" if lunch else "")+"需要约"
                        +str(window+over)+"分钟，无法同时排下："+listed+"。请选一种调整，或直接说明你的安排。")[:1500],
            "options":[label for label,_ in options],
            "recommendation":options[0][0]+"：改动最少，其他安排保持不变。",
            "why_needed":("按每项游玩时长与路程估算，当天还差约"+str(over)+"分钟；这些项目是你明确要求或锁定的，不能由系统自行删减。")[:1500],
            "affected_dates":sorted({day,*(action["to"] for _,action in options if action["type"] == "move")})},{label:action for label,action in options}


def _trip_days(req):
    if not req.get("start_date") or not req.get("end_date"):
        return []
    start,end = Date.fromisoformat(str(req["start_date"])),Date.fromisoformat(str(req["end_date"]))
    return [(start+timedelta(days=offset)).isoformat() for offset in range((end-start).days+1)]


def _minimum(req, day, city):
    """The strictest hard "at least N stops a day" for this day and city; 0 when there is none.

    A soft minimum is one the user already accepted falling short of, so it never raises a question again.
    """
    return max((item["count"] for item in active(req,"daily_min_stops",date=day) if item["strength"] == "hard"
                and (not item["scope"]["cities"] or not city or city in item["scope"]["cities"])),default=0)


def _usable(state, stop, day=None):
    """A stop that can still be placed: not dining, not excluded, located or not yet tried, not tied to another day."""
    excluded = set((state.get("stop_constraints") or {}).get("excluded",[]))
    return (not is_dining(stop) and stop.get("id") not in excluded and (day is None or stop.get("assigned_date") in {None,day})
            and not (stop.get("map_issue") and not coordinates(stop.get("map_place"))))


def _supplements(state, cities, used, used_names, sources):
    """Stops in imported but unselected guides that the trip does not have yet, per guide."""
    selected = set(state.get("selected_source_ids",[]))
    found = []
    for source in sources():
        if source["id"] in selected:
            continue
        stops = {stop["id"]:{**stop,"source_id":source["id"],"source_title":source["title"],"source_url":source.get("url")}
                 for stop in (source.get("extraction") or {}).get("stops",[])
                 if stop.get("city") in cities and stop.get("category") != "dining" and stop.get("name") not in used_names
                 and canonical_key(stop,stop.get("city")) not in used}
        if stops:
            found.append((source,stops))
    return found


def pool_question(state, itinerary, day, sources):
    """A day short of stops (ends early, or below the user's daily minimum) with none left: look in unselected guides first.

    Works on the scheduled day when there is one, otherwise on the candidate route's day. sources() is called only once
    the selected guides have run out, so ordinary days never read the store.
    """
    plan_day = next((entry for entry in itinerary.get("days",[]) if entry["date"] == day),None)
    route_day = next((entry for entry in (state.get("selected_route") or {}).get("days",[]) if entry["date"] == day),None)
    if not plan_day and not route_day:
        return None
    city = (plan_day or route_day).get("city")
    stops = state.get("stops",{})
    if plan_day:
        placed = {identity:(activity,entry["city"]) for identity,(entry,activity) in _scheduled(itinerary).items()}
        count = sum(activity.get("kind") == "visit" for activity in plan_day.get("activities",[]))
    else:
        placed = {identity:(stops[identity],entry.get("city")) for entry in state["selected_route"]["days"]
                  for identity in entry.get("stop_ids",[]) if identity in stops}
        count = sum(identity in stops and not is_dining(stops[identity]) for identity in route_day.get("stop_ids",[]))
    req = state.get("requirements",{})
    minimum = _minimum(req,day,city)
    # Even an accepted (soft) daily minimum still means "not an empty day".
    floor = max(minimum,1 if active(req,"daily_min_stops",date=day) else 0)
    timed = (plan_day and plan_day.get("available_start") is not None and plan_day.get("tail_deadline") is not None
             and early_finish(plan_day))
    if not timed and count >= floor:
        return None
    used = {canonical_key(item,place_city) for item,place_city in placed.values()}
    used_names = {item.get("name") for item,_ in placed.values()}
    unused = [identity for identity,stop in stops.items() if stop.get("city") == city and identity not in placed
              and canonical_key(stop) not in used and _usable(state,{**stop,"id":identity},day)]
    # With time left and stops left, the scheduler can still fill the day; a full day below the minimum cannot take more.
    # Before scheduling there is no time to judge by, so the day counts as having room.
    room = bool(timed) if plan_day else True
    if unused and room:
        return None
    short = count < floor
    options = _moves(state,itinerary,day,city,count) if short and room and plan_day else []
    for source,found in (_supplements(state,{city},used,used_names,sources)[:MAX_OPTIONS-1-len(options)] if room else []):
        names = "、".join(list(dict.fromkeys(stop["name"] for stop in found.values()))[:3])
        options.append(("加入《"+source["title"][:30]+"》里的"+names,
                        {"type":"add_source","source_id":source["id"],"date":day,"stops":found}))
    options.append(("这天不安排景点，自由活动" if short and not count else "这天只安排"+str(count)+"个景点" if short
                    else "这天剩余时间自由安排",{"type":"keep_short","date":day}))
    if not room:
        detail = "这天的时间已经排满，放不下更多景点。"
    elif options[0][1]["type"] == "move":
        detail = "可以从景点较多的日子调一个过来，两天都会重新排时间。"
    elif len(options) > 1:
        detail = "已在你导入但未勾选的攻略里找到"+city+"的其他地点，加入后只调整这一天。"
    else:
        detail = "已导入的攻略里也没有"+city+"的其他地点；想多安排可以先在攻略页导入更多攻略，再重新规划这一天。"
    situation = ("目前只能安排"+str(count)+"个景点，少于你要求的每天至少"+str(minimum)+"个" if count < minimum
                 else "目前一个景点都没有安排" if short else "还剩约"+str(timed["remaining_minutes"])+"分钟")
    return {"id":"pool-"+day,"title":_label_date(day)+"可安排的景点不够",
            "question":(_label_date(day)+situation+"，"+("" if not room else "当前所选攻略里"+city+"的地点都已安排或与前几天重复。")+detail)[:1500],
            "options":[label for label,_ in options],
            "recommendation":options[0][0]+"：只用你导入的攻略补充，不编造地点。",
            "why_needed":"所选攻略的地点已经用完，补充新的攻略需要你同意。","affected_dates":[day]},{label:action for label,action in options}


def _full_day(stop):
    """The guide says this place takes the whole day: a long reference duration, or "一整天" in the clause that names it."""
    if (stop.get("duration_minutes") or 0) >= FULL_DAY_MINUTES:
        return True
    quote = str((stop.get("evidence") or {}).get("quote") or "")
    return any(stop.get("name") and stop["name"] in clause and FULL_DAY.search(clause) for clause in re.split(r"[。；;，,\n]",quote))


def _places(stops):
    """Group stops that are one real place: the same identity, the same name without the city prefix, or a shared alias."""
    groups = []
    for stop in stops:
        keys = {canonical_key(stop),*("name:"+local_name(name,stop.get("city"))
                                      for name in [stop.get("name"),*(stop.get("alias_names") or [])] if name)}
        group = {"keys":keys,"stops":[stop]}
        for other in [entry for entry in groups if entry["keys"] & keys]:
            groups.remove(other)
            group["keys"] |= other["keys"]; group["stops"] += other["stops"]
        groups.append(group)
    return groups


def _moves(state, itinerary, day, city, count):
    """Move one optional stop here from a same-city day that holds at least two more, so the days end up more even."""
    options = []
    for other in itinerary.get("days",[]):
        visits = [activity for activity in other.get("activities",[]) if activity.get("kind") == "visit"]
        if other["date"] == day or other.get("city") != city or len(visits) < count+2:
            continue
        movable = [activity for activity in visits if not _mandatory(state,activity["id"],other["date"])
                   and activity["id"] not in state.get("locks",[])]
        if movable:
            pick = min(movable,key=lambda activity:_visit_minutes(activity,other["date"]) or DEFAULT_VISIT_MINUTES)
            options.append(("把"+pick.get("name",pick["id"])+"从"+_label_date(other["date"])+"改到"+_label_date(day),
                            {"type":"move","stop_id":pick["id"],"date":other["date"],"to":day}))
    return options[:2]


def accepted_shortage(state, question):
    """A model question about too few stops on days where the user already accepted fewer and the guides have none left.

    Such a question would only ask again what the user answered, so it is settled without asking. Empty days and days
    with stops still unplaced are not covered: those are real choices and keep their question.
    """
    text = str(question.get("title",""))+str(question.get("question",""))
    dates = sorted({str(day) for day in question.get("affected_dates",[])})
    req = state.get("requirements",{})
    if not dates or not SHORTAGE.search(text):
        return False
    if any(_minimum(req,day,"") or not active(req,"daily_min_stops",date=day) for day in dates):
        return False
    candidate = (state.get("candidates") or [{}])[0]
    draft = {entry["date"]:entry for entry in (candidate.get("draft") or {}).get("days",[])}
    route = {entry["date"]:entry for entry in candidate.get("days",[])}
    if any(day in draft and not any(activity.get("kind") == "visit" for activity in draft[day].get("activities",[]))
           or day not in draft and not route.get(day,{}).get("stop_ids") for day in dates):
        return False
    stops = state.get("stops",{})
    visits = [{**activity,"city":entry.get("city")} for entry in draft.values() for activity in entry.get("activities",[])
              if activity.get("kind") == "visit"]
    visits += [{**stops[identity],"id":identity} for entry in route.values() for identity in entry.get("stop_ids",[]) if identity in stops]
    # A guide place counts as used when it shares an identity, a name or an alias with anything already planned.
    placed = set().union(*(group["keys"] for group in _places(visits)))
    cities = {(draft.get(day) or route.get(day) or {}).get("city") for day in dates}
    places = _places([{**stop,"id":identity} for identity,stop in stops.items()
                      if stop.get("city") in cities and _usable(state,{**stop,"id":identity})])
    return all(group["keys"] & placed for group in places)


def trip_pool_question(state, sources):
    """Before any route exists: the selected guides hold too few distinct places for the user's daily minimum.

    Each real place counts once across its names, and a place that takes the whole day fills that day alone. Asked once:
    every answer except adding a guide turns the minimum into a target, so later short days are noted instead of blocking.
    """
    req = state.get("requirements",{})
    days = _trip_days(req)
    needs = sorted(_minimum(req,day,"") for day in days)
    if not any(needs):
        return None
    places = _places([{**stop,"id":identity} for identity,stop in state.get("stops",{}).items()
                      if stop.get("city") in req.get("cities",[]) and _usable(state,{**stop,"id":identity})])
    whole = [group for group in places if any(_full_day(stop) for stop in group["stops"])][:len(days)]
    # Best case: whole-day places go to the days that ask for the fewest stops; the other places share the remaining days.
    rest, rest_days = len(places)-len(whole), len(days)-len(whole)
    if not any(need > 1 for need in needs[:len(whole)]) and rest >= sum(needs[len(whole):]):
        return None
    minimum = needs[-1]
    used = set().union(*(group["keys"] for group in places))
    used_names = {name for group in places for stop in group["stops"] for name in [stop.get("name"),*(stop.get("alias_names") or [])]}
    options = []
    for source,found in _supplements(state,set(req.get("cities",[])),used,used_names,sources)[:MAX_OPTIONS-2]:
        names = "、".join(list(dict.fromkeys(stop["name"] for stop in found.values()))[:3])
        options.append(("加入《"+source["title"][:30]+"》里的"+str(len(found))+"个地点（"+names+"）",
                        {"type":"add_source","source_id":source["id"],"date":None,"stops":found}))
    whole_names = "、".join(group["stops"][0]["name"] for group in whole)
    lower = rest//rest_days if rest_days else 0
    if 1 <= lower < minimum:
        options.append(("改为每天至少"+str(lower)+"个景点"+("，"+whole_names+"那天只安排它" if whole else ""),
                        {"type":"lower_count","count":lower}))
    options.append(("按现有攻略安排，景点不够的日子少于"+str(minimum)+"个也可以",{"type":"accept_short"}))
    detail = ("已在你导入但未勾选的攻略里找到其他地点。" if options[0][1]["type"] == "add_source"
              else "已导入的攻略里也没有更多地点；想多安排可以先在攻略页导入更多攻略。")
    return {"id":"pool-trip","title":"所选攻略的景点不够每天"+str(minimum)+"个",
            "question":("所选攻略去掉同一地点的不同叫法后共有"+str(len(places))+"个可安排的地点"
                        +("，其中"+whole_names+"需要一整天" if whole else "")+"；按你要求的每天至少"+str(minimum)+"个，"
                        +str(len(days))+"天排不满。"+detail+"选择后不再为景点数量追问，景点不够的日子只在方案里提示。")[:1500],
            "options":[label for label,_ in options],
            "recommendation":options[0][0]+"：只用你导入的攻略，不编造地点。",
            "why_needed":"所选攻略的地点不够，补充新的攻略或放宽每日数量需要你同意。","affected_dates":days},{label:action for label,action in options}


def code_decision(state, itinerary, dates, sources):
    """A code-built question for the first affected day that has a capacity conflict or runs out of stops.

    sources: a callable returning the trip's imported guides, read only when a day has run out of stops.
    """
    for day in sorted(set(map(str,dates))):
        found = capacity_question(state,itinerary,day) or pool_question(state,itinerary,day,sources)
        if found:
            return found
    return None


def _carve(req, dates, label):
    """Replace each daily minimum covering these dates with one for its remaining dates (or a revoke when none remain)."""
    drafts = []
    for item in active(req,"daily_min_stops"):
        covered = set(item["scope"]["dates"] or _trip_days(req))
        if not covered & set(dates):
            continue
        rest = sorted(covered-set(dates))
        fields = {key:item[key] for key in ConstraintDraft.model_fields if key in item}
        drafts.append(ConstraintDraft(**{**fields,"scope":{**item["scope"],"dates":rest},"revoke":not rest,"quote":label}).model_dump(mode="json"))
    return merge(req.get("constraints",[]),drafts,"decision")


def apply_option(state, action, label):
    """Turn a chosen option into requirement, stop and route changes; returns (updates, affected dates, summary)."""
    req = {**state.get("requirements",{})}
    constraints = {key:list(value) for key,value in (state.get("stop_constraints") or {}).items()}
    candidates = [dict(candidate,days=[dict(day,stop_ids=list(day["stop_ids"])) for day in candidate.get("days",[])])
                  for candidate in state.get("candidates",[])]
    days = {day["date"]:day for day in (candidates[0]["days"] if candidates else [])}
    stops = dict(state["stops"])
    updates, dates, keep = {}, [action["date"]] if action.get("date") else list(action.get("dates",[])), False
    kind, identity = action["type"],action.get("stop_id")
    name = stops.get(identity,{}).get("name",identity)
    if kind == "exclude":
        constraints["required"] = [item for item in constraints.get("required",[]) if item != identity]
        constraints["excluded"] = sorted({*constraints.get("excluded",[]),identity})
        if action["date"] in days:
            days[action["date"]]["stop_ids"] = [item for item in days[action["date"]]["stop_ids"] if item != identity]
        summary = "按你的选择，这次不安排"+name
    elif kind == "move":
        if action["date"] in days:
            days[action["date"]]["stop_ids"] = [item for item in days[action["date"]]["stop_ids"] if item != identity]
        if action["to"] in days and identity not in days[action["to"]]["stop_ids"]:
            days[action["to"]]["stop_ids"].append(identity)
            days[action["to"]]["adjustment"] = "按你的选择把"+name+"改到这一天"
        if stops.get(identity,{}).get("assigned_date"):
            stops[identity] = {**stops[identity],"assigned_date":action["to"]}
        dates.append(action["to"])
        summary = "按你的选择把"+name+"改到"+_label_date(action["to"])
    elif kind == "shorten":
        duration = ConstraintDraft(kind="visit_duration",scope={"dates":[action["date"]],"stop_ids":[identity]},
                                   minutes=action["minutes"],quote=label).model_dump(mode="json")
        req["constraints"] = merge(req.get("constraints",[]),[duration],"decision")
        summary = "按你的选择把"+name+"的游玩时长调整为"+str(action["minutes"])+"分钟"
    elif kind == "add_source":
        updates["selected_source_ids"] = [*state.get("selected_source_ids",[]),action["source_id"]]
        stops = canonicalize_stops({**stops,**action["stops"]})
        if action["date"] in days:
            days[action["date"]]["stop_ids"] += [item for item in action["stops"] if item not in days[action["date"]]["stop_ids"]]
            days[action["date"]]["adjustment"] = "按你的选择加入新攻略的地点"
        summary = "按你的选择加入攻略" + ("并补充"+_label_date(action["date"])+"的安排" if action["date"] else "，用新的地点重新安排")
    elif kind in {"lower_count","accept_short"}:
        # The user accepted the guides' limit: the minimum stays as a target, and short days are noted instead of blocking.
        drafts = [ConstraintDraft(**{**{key:item[key] for key in ConstraintDraft.model_fields if key in item},"strength":"soft",
                                     "count":action.get("count") or item["count"],"quote":label}).model_dump(mode="json")
                  for item in active(req,"daily_min_stops")]
        req["constraints"] = merge(req.get("constraints",[]),drafts,"decision")
        summary = ("按你的选择，改为每天至少"+str(action["count"])+"个景点" if kind == "lower_count"
                   else "按你的选择，用现有攻略安排")+"；景点不够的日子只在方案里提示"
    else:
        # Accepting fewer stops lifts the daily minimum on exactly those dates; other days keep it.
        req["constraints"] = _carve(req,dates,label)
        keep = kind == "keep_short"
        summary = "按你的选择，"+"、".join(map(_label_date,dates))+"不再补充景点"
    req["required_stop_ids"],req["excluded_stop_ids"] = sorted(constraints.get("required",[])),sorted(constraints.get("excluded",[]))
    updates.update(requirements=req,stop_constraints=constraints,candidates=candidates,stops=stops,keep_current_plan=keep)
    return updates,sorted(set(dates)),summary
