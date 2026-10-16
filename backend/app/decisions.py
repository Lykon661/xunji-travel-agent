from .agents.runtime import digest
from .routing import coordinates, daily_route_scope, reference_opening
from .rules import timestamp
from .resource_planning import resource_context_issues
from .places import duplicate_issues, represented_ids
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


def confirmation_blockers(itinerary, constraints=None, requirements=None):
    blockers = [issue["message"] for issue in itinerary.get("issues",[]) if issue.get("blocking")]
    if requirements:
        blockers.extend(issue["message"] for issue in resource_context_issues(itinerary,requirements))
    visits = represented_ids([activity for day in itinerary.get("days",[]) for activity in day.get("activities",[]) if activity["kind"] == "visit"])
    blockers.extend(issue["message"] for issue in duplicate_issues(itinerary,requirements))
    if not visits:
        blockers.append("路线尚未包含有来源的景点安排")
    if set((constraints or {}).get("required",[])) - visits:
        blockers.append("必去景点尚未全部纳入路线")
    if set((constraints or {}).get("excluded",[])) & visits:
        blockers.append("行程包含用户明确排除的地点或其攻略别名")
    for day in itinerary.get("days",[]):
        scope = daily_route_scope(itinerary,day)
        previous_visit = None
        for activity in day.get("activities",[]):
            if activity["kind"] != "visit":
                continue
            opening = reference_opening(activity,day["date"])
            if opening and opening.get("closed"):
                blockers.append(day["date"]+" "+activity["name"]+"的参考信息为闭馆")
            if not coordinates(activity.get("map_place")):
                blockers.append(f"{day['date']} {activity['name']}的位置尚未确定")
            move = activity.get("move_before") or {}
            if (previous_visit or scope["first_leg_required"] and (day.get("start_place") or move)) and move.get("minutes") is None:
                blockers.append(f"{day['date']} 前往{activity['name']}的实际交通耗时尚未取得")
            previous_visit = activity
        if scope["last_leg_required"] and day.get("activities") and day.get("return_move") and day["return_move"].get("minutes") is None:
            blockers.append(day["date"]+"结束路段的实际交通耗时尚未取得")
        for transfer in day.get("transfers",[]):
            if transfer["move"].get("minutes") is None:
                blockers.append(day["date"]+" "+transfer["label"]+"的实际耗时尚未取得")
    for entry in itinerary.get("transport",[]):
        selected = entry.get("selected") or {}
        day = entry.get("context",{}).get("date","")
        if not timestamp(selected.get("departure"),day) or not timestamp(selected.get("arrival"),day):
            blockers.append(entry["label"]+"的到离时间尚未取得")
        if not selected.get("departure_place") or not selected.get("arrival_place"):
            blockers.append(entry["label"]+"的出发或到达站点尚未确定")
    return list(dict.fromkeys(blockers))
