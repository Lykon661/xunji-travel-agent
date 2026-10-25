from . import ablation
from .agents.runtime import digest
from .routing import coordinates, daily_route_scope, reference_opening, route_issue_applies, scoped_day, selected_route_resources
from .rules import timestamp, walking_limit
from .resource_planning import resource_context_issues
from .places import duplicate_issues, represented_ids
from .dining import dining_wishes, dining_wish_issues, visit_required_ids
from datetime import date, timedelta
import re


def walking_question(question):
    text = question.get("title","")+question.get("question","")+question.get("why_needed","")
    return bool(re.search(r"(?:步行|走路).{0,30}(?:上限|超限|超额|限制)|(?:上限|超限|超额).{0,30}(?:步行|走路)",text))


def apply_walking_decision(req, question, answer_text, action="unchanged", keep=False):
    # A budget/price limit in the same answer is not a walking instruction.
    clauses = re.split(r"[，,。；;！!？?\n]|但是|不过|但|并且|而且|同时|以及|并|且",answer_text)
    walking_text = "；".join(clause for clause in clauses if re.search(r"步行|走路|步数|公里|千米|\d\s*米",clause)
        or walking_question(question) and not re.search(r"预算|费用|价格|票价|房价|车费|花费|消费|元|小时|分钟",clause))
    preserve = bool(re.search(r"(?:保持|保留|维持|继续按).{0,12}(?:规划|计划|行程|路线|方案|安排|景点|现状|全部|所有)|照旧|照原|不用改|不改|不删|别改|不要删",answer_text))
    strict = bool(re.search(r"不(?:要)?超(?:过|出)?|不接受.*超限|不允许.*超限|(?:上限|限制).{0,8}(?:保持|保留|不变)",walking_text))
    preserve &= not strict and not bool(re.search(r"不要(?:保持|保留|维持)|不再(?:保持|保留|维持)",answer_text))
    restore = strict or bool(re.search(r"恢复.*(?:步行|上限)|(?:仍|还是|重新|严格).*(?:遵守|执行).*(?:上限|限制)",walking_text))
    relax = bool(re.search(r"(?:取消|放宽|忽略).*(?:步行|走路|上限)|(?:接受|允许).*(?:超限|超出.*上限)",walking_text))
    if restore:
        action = "restore"
        preserve = keep = False
    elif action != "restore" and not strict and (walking_question(question) and preserve or relax):
        action = "relax"
    if action == "unchanged":
        return req,keep or preserve
    dates = question.get("affected_dates",[])
    if not dates or re.search(r"全程|所有天|每天|整趟",walking_text):
        if not req.get("start_date") or not req.get("end_date"):
            return req,keep or preserve
        start,end = date.fromisoformat(req["start_date"]),date.fromisoformat(req["end_date"])
        dates = [(start+timedelta(days=index)).isoformat() for index in range((end-start).days+1)]
    exempt = set(req.get("walking_limit_exempt_dates",[]))
    dates = [str(day) for day in dates if req["start_date"] <= str(day) <= req["end_date"]]
    exempt = exempt | set(dates) if action == "relax" else exempt-set(dates)
    return {**req,"walking_limit_exempt_dates":sorted(exempt)},keep or preserve


def input_hash(requirements, source_ids, stops, sources=None):
    return digest({"requirements":requirements,"sources":source_ids,
                   "source_content":[{key:source.get(key) for key in ("id","text","url","images","extraction")} for source in sources or []],
                   "stops":{key:{k:stop.get(k) for k in ("name","city","duration_minutes","source_id","source_title",
                                                         "evidence","opening_claim","ticket_claim","category","assigned_date")} for key,stop in stops.items()}})


def route_structure(itinerary):
    def move_signature(move):
        move = move or {}
        return {key:move.get(key) for key in ("mode","minutes","summary","origin","destination")}

    def resource_signature(entry):
        selected = entry.get("selected") or {}
        return {"entry_id":entry["id"],"context":entry.get("context"),
                "selected":{key:selected.get(key) for key in ("kind","name","supplier_id","departure","arrival",
                                                               "departure_place","arrival_place","address","bed_type","seat_class")}}

    return {"days":[{"date":day["date"],"city":day["city"],"start_place":day.get("start_place"),"end_place":day.get("end_place"),
                     "activities":[{**{key:activity.get(key) for key in ("id","start","end","kind","meal","meal_break","appointment_time")},
                                    "move":move_signature(activity.get("move_before"))} for activity in day["activities"]],
                     "return_move":move_signature(day.get("return_move")),
                     "transfers":[move_signature(item["move"]) for item in day.get("transfers",[])],
                     "return_start":day.get("return_start"),"return_end":day.get("return_end")}
                    for day in itinerary.get("days",[])],
            "meals":[{"date":day["date"],"items":[{key:item.get(key) for key in ("id","canonical_place_id","meal","name")}
                      for item in day.get("meal_recommendations",[])]} for day in itinerary.get("days",[])],
            "resources":{group:[resource_signature(entry) for entry in itinerary.get(group,[])] for group in ("transport","hotels")}}


def issue_record(itinerary, issue, requirements=None, stops=None):
    requirements, stops = requirements or {}, stops or {}
    target = str(issue.get("target") or "")
    refs = [dict(ref) for ref in issue.get("refs", [])]
    dates = list(map(str, issue.get("dates") or ([issue["date"]] if issue.get("date") else [])))
    numbers = dict(issue.get("numbers") or {})
    code = issue.get("code", "other")
    if issue.get("constraint_id"):
        refs.append({"type":"constraint", "id":issue["constraint_id"]})
    if target in requirements:
        refs.append({"type":"requirement", "id":target})
    if target in stops:
        refs.append({"type":"stop", "id":target})
        if stops[target].get("assigned_date"):
            dates.append(str(stops[target]["assigned_date"]))
    for day in itinerary.get("days", []):
        if target == day["date"]:
            refs.append({"type":"day", "id":target})
            dates.append(day["date"])
        for item in [*day.get("activities", []), *day.get("meal_recommendations", [])]:
            if target and target in {item.get("id"), item.get("name")}:
                refs.append({"type":"stop", "id":item["id"]})
                dates.append(day["date"])
    for group in ("transport", "hotels"):
        for entry in itinerary.get(group, []):
            if target == entry.get("id"):
                refs.append({"type":"resource", "id":target})
                context = entry.get("context") or {}
                if context.get("date"):
                    dates.append(context["date"])
    if code == "walking_limit":
        refs.append({"type":"requirement", "id":"max_walk_per_day"})
        for day in itinerary.get("days", []):
            if day["date"] in dates:
                limit = walking_limit(requirements, day["date"])
                if limit is not None:
                    numbers.setdefault("excess_meters", max(0, day.get("walking_meters", 0)-limit))
    if code == "over_budget":
        refs.append({"type":"requirement", "id":"budget"})
        budget = itinerary.get("budget") or {}
        numbers.setdefault("excess_amount", max(0, budget.get("known_plus_estimates", 0)-(requirements.get("budget") or budget.get("limit", 0))))
    for key in ("shortfall_minutes", "excess_amount", "excess_meters", "shortfall_stops"):
        if key in issue:
            numbers[key] = issue[key]
    if "available_minutes" in issue and "required_minutes" in issue:
        numbers.setdefault("shortfall_minutes", max(0, issue["required_minutes"]-issue["available_minutes"]))
    unique = {(ref["type"], ref.get("id") or ref.get("field")):ref for ref in refs}
    return {**issue, "code":code, "message":issue.get("message", ""), "refs":[unique[key] for key in sorted(unique)],
            "dates":sorted(set(dates)), "numbers":numbers, "blocking":issue.get("blocking", True)}


def confirmation_issues(itinerary, constraints=None, requirements=None, stops=None, state=None):
    if ablation.disabled("code_checks"):
        return []
    from .constraints import active, trip_dates
    from .models import wish_stops
    from .places import canonical_key
    from .rules import check_itinerary
    req, state = requirements or {}, state or {}
    pool = {**wish_stops(req), **(stops or {})}
    issues = []
    def add(code, message, target="", dates=(), refs=()):
        issues.append({"code":code,"message":message,"target":target,"dates":list(dates),"refs":list(refs),"blocking":True})
    days = itinerary.get("days",[])
    expected = set(trip_dates(req)) if req.get("start_date") and req.get("end_date") else set()
    actual = [day["date"] for day in days]
    rows = [(day,item) for day in days for item in [*day.get("activities",[]),*day.get("meal_recommendations",[])]
            if item.get("kind") == "visit" or item.get("meal") in {"breakfast","lunch","dinner"}]
    visits = represented_ids([item for _,item in rows])
    keys = {canonical_key(item,day.get("city","")) for day,item in rows}
    if not days or expected and (set(actual) != expected or len(actual) != len(expected)) or not rows:
        add("no_draft","路线尚无完整逐日草稿，不能确认或保存",refs=[{"type":"requirement","id":"start_date"}])
    required = set(req.get("required_stop_ids",[])) | set((constraints or {}).get("required",[])) | set(wish_stops(req))
    for entry in req.get("constraints",[]):
        if (entry.get("kind") in {"time_anchor","booking_prerequisite"} and entry.get("at")
                and not entry.get("revoke") and not entry.get("superseded_by")
                and entry.get("source","legacy_unknown") in {"form","message","interview","decision","user","legacy_unknown"}
                and (entry["kind"] == "booking_prerequisite" or entry.get("anchor") == "appointment")):
            required.update(entry.get("scope",{}).get("stop_ids",[]))
    for identity in sorted(required):
        if identity not in visits and (identity not in pool or canonical_key(pool[identity]) not in keys):
            add("stop_unplaced","用户必去或想去地点尚未纳入全程路线："+pool.get(identity,{}).get("name",identity),identity)
    excluded = set(req.get("excluded_stop_ids",[])) | set((constraints or {}).get("excluded",[]))
    for identity in sorted(excluded):
        if identity in visits or identity in pool and canonical_key(pool[identity]) in keys:
            add("excluded_stop","行程包含用户明确排除的地点",identity)
    if req:
        checked = check_itinerary(itinerary,req,pool)
        selected = [entry for entry in itinerary.get("transport",[]) if entry.get("selected")]
        for issue in checked["issues"]:
            if issue["code"] in {"arrival_conflict","departure_conflict","transport_time"}:
                issues.append({**issue,"blocking":True})
            elif issue["code"] == "tail_conflict" and any(entry.get("context",{}).get("date") == issue.get("target")
                    and any(day["date"] == issue["target"] and day.get("city") == entry["context"].get("origin") for day in days) for entry in selected):
                issues.append({**issue,"blocking":True})
        known = checked["budget"].get("known_subtotal",0)
        if req.get("budget") is not None and known > req["budget"]:
            add("over_budget",f"已知费用 {known} 元超过用户预算上限 {req['budget']} 元","budget")
        for entry in selected:
            from .resource_planning import transport_issues
            for message in transport_issues(entry["selected"],entry.get("context",{}),req):
                add("transport_time",(entry.get("label") or "已选班次")+"："+message,entry["id"])
    user_sources = {"form","message","interview","decision","user","legacy_unknown"}
    for day in days:
        for item in day.get("activities",[]):
            if item.get("kind") != "visit":
                continue
            start = timestamp(item.get("start"),day["date"])
            anchors = [entry for kind in ("time_anchor","booking_prerequisite")
                       for entry in active(req,kind,date=day["date"],stop_id=item["id"])
                       if entry.get("at") and entry.get("source","legacy_unknown") in user_sources
                       and (kind == "booking_prerequisite" or entry.get("anchor") == "appointment")]
            times = [entry["at"] for entry in anchors]
            if item.get("appointment_time") and item.get("appointment_time_source","legacy_unknown") in user_sources:
                times.append(item["appointment_time"])
            opening = reference_opening({**pool.get(item["id"],{}),**item},day["date"])
            stop = pool.get(item["id"],{})
            if (not coordinates(item.get("map_place") or stop.get("map_place")) and not stop.get("map_selected_id")
                    and len(stop.get("map_candidates",[])) >= 2):
                add("place_identity",item["name"]+"将进入路线，仍需在当前同名候选中确认地点身份",item["id"],[day["date"]])
            for at in times:
                moment = timestamp(at,day["date"])
                if moment and (start != moment or opening["closed"] or opening.get("last_entry_minutes") is not None
                        and moment.hour*60+moment.minute > opening["last_entry_minutes"]):
                    add("appointment_time",item["name"]+"与用户预约时刻或当日开放安排冲突",item["id"],[day["date"]])
    old = state.get("old_itinerary") or (state.get("review_previous") or {}).get("plan") or {}
    for identity in state.get("locks",[]):
        previous = [(day,item) for day in old.get("days",[]) for item in day.get("activities",[]) if item.get("id") == identity]
        current = [(day,item) for day in days for item in day.get("activities",[]) if item.get("id") == identity]
        conflict = bool(previous and not current)
        for group in ("transport","hotels"):
            before = next((entry for entry in old.get(group,[]) if entry.get("id") == identity),None)
            after = next((entry for entry in itinerary.get(group,[]) if entry.get("id") == identity),None)
            if before:
                conflict |= not after or before.get("context") != after.get("context") or any(
                    (before.get("selected") or {}).get(field) != (after.get("selected") or {}).get(field)
                    for field in ("departure","arrival"))
        if previous and current:
            first,before = previous[0]
            last,after = current[0]
            conflict |= (first["date"],before.get("start"),before.get("end")) != (last["date"],after.get("start"),after.get("end"))
        if current:
            day,item = current[0]
            opening = reference_opening({**pool.get(identity,{}),**item},day["date"])
            start,end = timestamp(item.get("start"),day["date"]),timestamp(item.get("end"),day["date"])
            conflict |= opening["closed"] or not start or not end
            if start and end and not opening.get("unknown"):
                begin,finish = start.hour*60+start.minute,end.hour*60+end.minute
                conflict |= not any(a <= begin < finish <= b for a,b in opening["windows"])
                conflict |= opening.get("last_entry_minutes") is not None and begin > opening["last_entry_minutes"]
        if conflict:
            add("locked_time","用户锁定项目与当前日期、时刻或开放时间冲突",identity)
    unique = {(item["code"],item.get("target"),tuple(item.get("dates",[]))):item for item in issues}
    return [issue_record(itinerary,item,req,pool) for item in unique.values()]


def confirmation_blockers(itinerary, constraints=None, requirements=None, stops=None, state=None):
    return list(dict.fromkeys(issue["message"] for issue in confirmation_issues(itinerary, constraints, requirements, stops, state)))
