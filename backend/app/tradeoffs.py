import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from uuid import uuid4

from . import actions, authority
from .constraints import active
from .decisions import confirmation_issues
from .dining import refresh_meals
from .models import DecisionBatch, DecisionQuestion, UnmetItem
from .places import canonical_key
from .planning_repairs import change_dependencies, protected_stops
from .routing import daily_route_scope, reference_opening
from .rules import check_itinerary, timestamp
from .scheduler import duration, fixed_dates, full_day, plan_trip, priority
from .unmet import collect, fingerprint


@dataclass
class Tradeoffs:
    batch: DecisionBatch | None = None
    automatic_actions: list = field(default_factory=list)
    affected_dates: list = field(default_factory=list)
    remaining: list = field(default_factory=list)


def _issues(state):
    return confirmation_issues(actions._plan(state),state.get("stop_constraints"),state["requirements"],state.get("stops",{}),state)


def _key(issue):
    return issue["code"],issue.get("target","")


def _clock(minutes):
    return f"{minutes//60:02d}:{minutes%60:02d}"


def _windows(state, dates):
    plan,req = actions._plan(state),state["requirements"]
    windows = {}
    for day in plan.get("days",[]):
        date = day["date"]
        if date not in dates:
            continue
        scope = daily_route_scope(plan,day)
        start = timestamp(req.get("earliest_departure") or "09:00",date)
        end = timestamp(req.get("latest_return") if scope["hotel_location_confirmed"] and req.get("latest_return") else "20:00",date)
        begin,finish = start.hour*60+start.minute,end.hour*60+end.minute
        tail = {"name":day.get("end_place") if scope["last_leg_required"] else None,
                "start_name":day.get("start_place") if scope["first_leg_required"] else None,
                "label":day.get("end_label") or "结束当天安排",
                "start_place":(day.get("activities") or [{}])[0].get("move_before",{}).get("origin_place"),
                "end_place":(day.get("return_move") or {}).get("destination_place")}
        for entry in plan.get("transport",[]):
            ctx,selected = entry.get("context",{}),entry.get("selected") or {}
            if ctx.get("date") != date or not selected:
                continue
            if ctx.get("destination") == day["city"]:
                arrival = timestamp(selected.get("arrival"),date)
                if arrival:
                    transfer = sum((item.get("move") or {}).get("minutes") if (item.get("move") or {}).get("minutes") is not None else 60
                                   for item in day.get("transfers",[]) if "放行李" in item.get("label","")) if scope["hotel_location_confirmed"] else 0
                    begin = max(begin,arrival.hour*60+arrival.minute+60+transfer)
                if not scope["hotel_location_confirmed"]:
                    tail.update(start_name=selected.get("arrival_place"),start_place=(selected.get("arrival_transfer") or {}).get("origin_place"))
            if ctx.get("origin") == day["city"]:
                departure = timestamp(selected.get("departure"),date)
                if departure:
                    finish = min(finish,departure.hour*60+departure.minute-entry.get("buffer_minutes",60))
                    tail.update(name=selected.get("departure_place"),end_place=(selected.get("departure_transfer") or {}).get("destination_place"),label="前往出发车站或机场")
        windows[date] = {"date":date,"city":day["city"],"start":begin,"end":finish,"tail":tail,
                         "transfers":copy.deepcopy(day.get("transfers",[])),
                         "durations":state.get("selected_route",{}).get("feasibility",{}).get("durations",{})}
    return windows


def _moves(plan):
    result = {}
    for day in plan.get("days",[]):
        previous = "@start" if day.get("start_place") else None
        for item in day.get("activities",[]):
            if item.get("kind") != "visit":
                continue
            if previous and item.get("move_before"):
                result[day["date"],previous,item["id"]] = copy.deepcopy(item["move_before"])
            previous = item["id"]
        if previous and day.get("end_place") and day.get("return_move"):
            result[day["date"],previous,"@end"] = copy.deepcopy(day["return_move"])
    return result


def _preserved(state, dates):
    result = {}
    for day in actions._plan(state).get("days",[]):
        if day["date"] not in dates:
            continue
        kept = {}
        for item in day.get("activities",[]):
            identity = item["id"]
            anchors = [entry["at"] for kind in ("time_anchor","booking_prerequisite")
                       for entry in active(state["requirements"],kind,date=day["date"],stop_id=identity) if entry.get("at")]
            appointment = item.get("appointment_time") and (not anchors or set(anchors) == {item.get("start")})
            if item.get("kind") == "visit" and (identity in state.get("locks",[]) or appointment):
                kept[identity] = copy.deepcopy(item)
        result[day["date"]] = kept
    return result


def simulate(state, proposed, actor="user", dates=None):
    from .nodes.scheduling import SchedulingNodes
    before = copy.deepcopy(state)
    work = copy.deepcopy(state)
    if proposed:
        updates,_,_ = actions.execute(work,proposed,actor)
        work.update(updates)
    authorized = authority.user_state(work)
    affected = set(dates if dates is not None else change_dependencies(before,work,proposed)["replan_dates"])
    plan = copy.deepcopy(actions._plan(work))
    routes = copy.deepcopy(actions._route_days(work))
    windows = _windows(work,affected)
    if windows:
        simulation = {**work,"change":{**work.get("change",{}),"fewer_activities":True},
                      "old_itinerary":copy.deepcopy(plan),"itinerary":copy.deepcopy(plan)}
        result = plan_trip(simulation,routes,windows,_moves(actions._plan(before)),_preserved(work,affected))
        old = {item["id"]:item for day in plan.get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"}
        renderer = SchedulingNodes()
        for index,day in enumerate(plan.get("days",[])):
            date = day["date"]
            if date not in windows:
                continue
            window = windows[date]
            route = next(row for row in routes if row["date"] == date)
            built = renderer.schedule_activity_day(simulation,route,result["days"][date],
                (window["start"],window["end"],[],window["transfers"],window["tail"]),day.get("weather",{}))
            for item_index,item in enumerate(built["activities"]):
                previous = old.get(item["id"],{})
                if item["id"] in work.get("locks",[]) and (item.get("start"),item.get("end")) == (previous.get("start"),previous.get("end")):
                    item = {**copy.deepcopy(previous),"move_before":item.get("move_before",{})}
                    built["activities"][item_index] = item
                if previous.get("ticket_quote"):
                    item["ticket_quote"] = copy.deepcopy(previous["ticket_quote"])
            plan["days"][index] = built
        for route in [work.get("selected_route") or {},*(work.get("candidates") or [])[:1]]:
            for day in route.get("days",[]):
                if day["date"] in result["days"]:
                    day["stop_ids"] = list(result["days"][day["date"]]["stop_ids"])
        refresh_meals(plan,work.get("stops",{}),work.get("stop_constraints",{}).get("excluded",[]),dates=affected,
                      requirements=work["requirements"],meal_plan=work.get("selected_route",{}).get("feasibility",{}).get("meal_wishes"))
    checked = check_itinerary(plan,work["requirements"],work.get("stops",{}))
    plan.update(budget=checked["budget"],issues=checked["issues"])
    work["itinerary"] = plan
    if authority.user_changes(authorized,authority.user_state(work)):
        raise ValueError("Simulation changed user state beyond the proposed actions")
    retained = {canonical_key(item,day.get("city","")) for day in plan.get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"}
    excluded = set(work["requirements"].get("excluded_stop_ids",[])) | set(work.get("stop_constraints",{}).get("excluded",[]))
    excluded_keys = {canonical_key(work["stops"][identity]) for identity in excluded if identity in work.get("stops",{})}
    for identity,(_,item) in _placements(actions._plan(before)).items():
        if (identity in protected_stops(before) and identity not in excluded and identity in work.get("stops",{})
                and canonical_key(item) not in retained | excluded_keys):
            raise ValueError("Simulation removed another protected stop")
    if actor == "system":
        authority.assert_system_action(before,work)
    return work,sorted(windows)


def _placements(plan):
    return {item["id"]:(day["date"],item) for day in plan.get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"}


def _metrics(state):
    plan = actions._plan(state)
    visits = _placements(plan)
    minutes = sum((timestamp(item["end"],day)-timestamp(item["start"],day)).total_seconds()/60 for day,item in visits.values())
    travel = 0
    estimated = False
    for day in plan.get("days",[]):
        for move in [*[item.get("move_before") or {} for item in day.get("activities",[])],day.get("return_move") or {}]:
            travel += move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
            estimated |= bool(move and move.get("minutes") is None)
    known = check_itinerary(plan,state["requirements"],state.get("stops",{}))["budget"]["known_subtotal"]
    return visits,int(minutes),sum(day.get("walking_meters",0) for day in plan.get("days",[])),known,travel,estimated


def _impact(before, after, dates):
    a,minutes,meters,known,travel,_ = _metrics(before)
    b,new_minutes,new_meters,new_known,new_travel,estimated = _metrics(after)
    return {"dates":dates,"removed_stop_ids":sorted(a.keys()-b.keys()),"added_stop_ids":sorted(b.keys()-a.keys()),
            "moved_stops":[{"stop_id":identity,"from":a[identity][0],"to":b[identity][0]} for identity in sorted(a.keys() & b.keys()) if a[identity][0] != b[identity][0]],
            "minutes":new_minutes-minutes,"meters":new_meters-meters,"amount":round(new_known-known,2),
            "known_before":known,"known_after":new_known,"budget_limit":after["requirements"].get("budget"),
            "travel_minutes":new_travel-travel,"estimated_transport":estimated}


def _safe(before, after, issue):
    old_records = {_key(item):item for item in _issues(before)}
    new_records = {_key(item):item for item in _issues(after)}
    old,new = set(old_records),set(new_records)
    if _key(issue) in new or new-old:
        return False
    if any(value > old_records[key].get("numbers",{}).get(name,0) for key,item in new_records.items()
           for name,value in item.get("numbers",{}).items() if name.startswith(("excess_","shortfall_"))):
        return False
    from .rules import constraint_issues
    old_violations = {(item.get("code"),item.get("target"),item.get("constraint_id")) for item in constraint_issues(actions._plan(before),before["requirements"])[0] if item.get("blocking")}
    new_violations = {(item.get("code"),item.get("target"),item.get("constraint_id")) for item in constraint_issues(actions._plan(after),after["requirements"])[0] if item.get("blocking")}
    return not (new_violations-old_violations)


def _exclude(state, identity, date):
    proposed = []
    if identity in state.get("locks",[]) or any(active(state["requirements"],kind,stop_id=identity) for kind in ("time_anchor","booking_prerequisite")):
        proposed.append({"type":"set_time_window","stop_id":identity,"unlock":True,"dates":[date]})
    proposed.append({"type":"exclude","stop_id":identity,"date":date})
    return proposed


def _place(state, identity, date):
    origin = next((day["date"] for day in actions._route_days(state) if identity in day.get("stop_ids",[])),None)
    if origin == date:
        return [{"type":"retain_free_time","date":date,"free_minutes":0}]
    return [{"type":"move","stop_id":identity,"date":origin,"to":date}] if origin else [{"type":"add_optional_stop","stop_id":identity,"date":date}]


def _offers(state, issue):
    code,identity = issue["code"],issue.get("target")
    plan,stops = actions._plan(state),state.get("stops",{})
    dates = issue.get("dates") or [day["date"] for day in plan.get("days",[])]
    if code == "stop_unplaced" and identity in stops:
        stop = stops[identity]
        fixed = fixed_dates(state).get(identity)
        choices = [day["date"] for day in plan.get("days",[]) if day["city"] == stop["city"] and
                   (not fixed or day["date"] in fixed) and not reference_opening(stop,day["date"])["closed"]]
        for day in choices:
            yield "安排到"+day,_place(state,identity,day)
        if stop.get("assigned_date"):
            origin = next((row["date"] for row in actions._route_days(state) if identity in row.get("stop_ids",[])),None)
            for row in plan.get("days",[]):
                day = row["date"]
                if row["city"] != stop["city"] or day == stop["assigned_date"] or reference_opening(stop,day)["closed"]:
                    continue
                seed = [] if origin else [{"type":"add_optional_stop","stop_id":identity,"date":stop["assigned_date"]}]
                yield "将指定日期改到"+day,[*seed,{"type":"move","stop_id":identity,"date":origin or stop["assigned_date"],"to":day}]
        for day in choices:
            current = duration(state,stop,day)
            minimum = 360 if full_day(stop) else 30
            for minutes in sorted({max(minimum,int(current*.75)//15*15),max(minimum,int(current*.5)//15*15)},reverse=True):
                if minutes < current:
                    yield "在"+day+"游玩"+str(minutes)+"分钟",[{"type":"shorten","stop_id":identity,"date":day,"minutes":minutes},*_place(state,identity,day)]
        day = (choices or dates)[0]
        excluded = _exclude(state,identity,day)
        yield "本次不去"+stop["name"],excluded
        used = {canonical_key(stops[i]) for row in actions._route_days(state) for i in row.get("stop_ids",[]) if i in stops}
        for other,candidate in sorted(stops.items(),key=lambda item:-priority(state,item[0])):
            if candidate.get("city") != stop["city"] or candidate.get("category") != stop.get("category") or canonical_key(candidate) in used or other == identity:
                continue
            target = next((row["date"] for row in plan.get("days",[]) if row["city"] == stop["city"] and not reference_opening(candidate,row["date"])["closed"]
                           and not actions.optional_stop_issue(state,other,row["date"])),None)
            if target:
                yield "改去"+candidate["name"],[*excluded,{"type":"add_optional_stop","stop_id":other,"date":target}]
    elif code == "over_budget":
        known = _metrics(state)[3]
        yield "预算提高到"+str(known)+"元",[{"type":"set_budget","amount":known}]
        paid = sorted(_placements(plan).items(),key=lambda item:-float((item[1][1].get("ticket_quote") or {}).get("price") or 0))
        cumulative = []
        for stop_id,(day,item) in paid:
            if not (item.get("ticket_quote") or {}).get("price"):
                continue
            cumulative += _exclude(state,stop_id,day)
            if len(cumulative) <= 24:
                yield "移出付费项目："+"、".join(stops.get(action.get("stop_id"),{}).get("name","") for action in cumulative if action["type"] == "exclude"),copy.deepcopy(cumulative)
        amount = min(10000000,max(known+1,math.ceil(known/100)*100))
        yield "预算提高到"+str(amount)+"元并保留余量",[{"type":"set_budget","amount":amount}]
    elif code in {"appointment_time","locked_time"} and identity in stops:
        day = dates[0]
        release = {"type":"set_time_window","stop_id":identity,"unlock":True,"dates":[day]}
        opening = reference_opening(stops[identity],day)
        for begin,end in opening["windows"] if not opening["closed"] else []:
            minutes = duration(state,stops[identity],day)
            for at in dict.fromkeys((begin,min(end-minutes,opening.get("last_entry_minutes") or end-minutes))):
                if begin <= at and at+minutes <= end:
                    yield "改为"+_clock(at)+"开始",[release,{"type":"set_time_window","stop_id":identity,"at":_clock(at),"dates":[day]},*_place(state,identity,day)]
        yield "取消锁定及预约时刻限制",[release,*_place(state,identity,day)]
        yield "本次不去"+stops[identity]["name"],_exclude(state,identity,day)
    elif code in {"transport_time","arrival_conflict","departure_conflict","tail_conflict","locked_time"}:
        for entry in plan.get("transport",[]):
            if identity not in {entry["id"],entry.get("context",{}).get("date")} and not set(dates) & {entry.get("context",{}).get("date")}:
                continue
            day = entry["context"]["date"]
            release = [{"type":"set_time_window","resource_id":entry["id"],"unlock":True,"dates":[day]}] if entry["id"] in state.get("locks",[]) else []
            for option in entry.get("options",[]):
                if option.get("id") and option != entry.get("selected"):
                    yield "改选"+str(option.get("name") or option["id"])+" "+str(option.get("departure","")),[*release,{"type":"set_time_window","resource_id":entry["id"],"value":option["id"],"dates":[day]}]
            selected = entry.get("selected") or {}
            proposed = []
            for field,clock in (("earliest_departure",selected.get("departure")),("latest_arrival",selected.get("arrival")),("latest_return",selected.get("arrival"))):
                moment = timestamp(clock,day)
                if state["requirements"].get(field) and moment:
                    proposed.append({"type":"set_time_window","field":field,"value":moment.strftime("%H:%M")})
            if proposed:
                yield "调整时间要求以保留当前班次",proposed


def _identity_questions(state, unmet):
    questions = []
    for raw in unmet:
        item = UnmetItem.model_validate(raw)
        if item.kind not in {"place_identity","hub_identity"} or not item.blocking:
            continue
        identity = next((ref.id for ref in item.refs if ref.type == "stop"),None)
        stop = state.get("stops",{}).get(identity,{})
        candidates = stop.get("map_candidates",[]) if identity else state.get("hub_candidates",[])
        options = []
        for place in candidates[:4]:
            if not place.get("id"):
                continue
            action = {"type":"select_poi","stop_id":identity,"poi_id":place["id"]} if identity else {
                "type":"select_hub","city":place["city"],"name":place.get("hub_name",place["name"]),"poi_id":place["id"],
                **({"resource_id":ref.id} if (ref := next((ref for ref in item.refs if ref.type == "resource"),None)) else {})}
            try:
                actions.execute(state,[action],"user")
            except (ValueError,KeyError,TypeError):
                continue
            options.append({"id":"poi-"+place["id"],"label":place.get("name") or place["id"],
                            "description":place.get("address") or "请选择实际地点","actions":[action],"impact":{}})
        if len(options) >= 2:
            questions.append({"id":"identity-"+fingerprint(item)[:16],"title":"确认"+stop.get("name","交通站点"),
                "question":"请选择实际地点。","why_needed":item.detail,"unmet_refs":[ref.model_dump(mode="json") for ref in item.refs],
                "affected_dates":list(map(str,item.dates)),"affected_stop_ids":[identity] if identity else [],
                "options":options,"recommended_option_id":options[0]["id"]})
    return questions


def prepare(state, itinerary, unmet=(), automatic=True):
    work = copy.deepcopy(state)
    work["itinerary"] = copy.deepcopy(itinerary)
    baseline = copy.deepcopy(work)
    result = Tradeoffs()
    if not itinerary.get("days"):
        result.remaining = [UnmetItem.model_validate(item) for item in unmet]
        questions = _identity_questions(work,unmet)[:6]
        if questions:
            result.batch = DecisionBatch(batch_id=uuid4().hex,draft_revision=state.get("draft_revision",0),
                stage_scope="early_identity",summary="请确认地点身份。",questions=questions)
        return result
    if automatic:
        for _ in range(24):
            issues = _issues(work)
            choices = []
            for issue in issues:
                identity = issue.get("target")
                if issue["code"] == "excluded_stop":
                    for day in actions._route_days(work):
                        for other in day.get("stop_ids",[]):
                            if other == identity or identity in work.get("stops",{}) and other in work["stops"] and canonical_key(work["stops"][other]) == canonical_key(work["stops"][identity]):
                                choices.append(([{"type":"skip","stop_id":other,"date":day["date"]}],issue))
                elif issue["code"] == "over_budget":
                    protected = protected_stops(work)
                    for identity,(day,item) in sorted(_placements(actions._plan(work)).items(),key=lambda pair:priority(work,pair[0])):
                        if identity not in protected and (item.get("ticket_quote") or {}).get("price",0) > 0:
                            choices.append(([{"type":"skip","stop_id":identity,"date":day}],issue))
                elif issue["code"] in {"appointment_time","locked_time"}:
                    affected = issue.get("dates") or [day["date"] for day in actions._route_days(work) if identity in day.get("stop_ids",[])]
                    choices.append(([{"type":"retain_free_time","date":day,"free_minutes":0} for day in affected],issue))
                elif issue["code"] == "stop_unplaced" and identity in work.get("stops",{}):
                    stop = work["stops"][identity]
                    fixed = fixed_dates(work).get(identity)
                    choices.extend((_place(work,identity,day["date"]),issue) for day in actions._route_days(work)
                        if day["city"] == stop["city"] and (not fixed or day["date"] in fixed)
                        and not reference_opening(stop,day["date"])["closed"])
            adopted = False
            for proposed,issue in choices:
                if not proposed or len(result.automatic_actions)+len(proposed) > 24:
                    continue
                try:
                    if actions.authority_of(work,proposed) != "system":
                        continue
                    trial,dates = simulate(baseline,[*result.automatic_actions,*proposed],"system")
                    if issue["code"] == "over_budget":
                        improved = _metrics(trial)[3] < _metrics(work)[3] and {_key(item) for item in _issues(trial)} <= {_key(item) for item in issues}
                    else:
                        improved = _safe(work,trial,issue)
                    if not improved:
                        continue
                except (ValueError,KeyError,TypeError):
                    continue
                work = trial
                result.automatic_actions.extend(proposed)
                result.affected_dates = sorted(set(result.affected_dates) | set(dates))
                adopted = True
                break
            if not adopted:
                break
    remaining = _issues(work)
    result.remaining = collect(work,actions._plan(work))
    questions = _identity_questions(work,[item for item in result.remaining if item.kind in {"place_identity","hub_identity"}])[:6]
    total = sum(max(len(option["actions"]) for option in question["options"]) for question in questions)
    handled = set()
    for issue in remaining:
        if issue["code"] in {"place_identity","excluded_stop"} or _key(issue) in handled or len(questions) >= 6:
            continue
        options,seen,solved_by_option = [],set(),{}
        try:
            offers = _offers(work,issue)
            for index,(label,proposed) in enumerate(offers):
                if index >= 40:
                    break
                try:
                    if actions.authority_of(work,proposed) != "user":
                        continue
                    trial,dates = simulate(work,proposed)
                    if not _safe(work,trial,issue):
                        continue
                    impact = _impact(work,trial,dates)
                    key = json.dumps({"user":authority.user_state(trial),"placements":{identity:date for identity,(date,_) in _placements(actions._plan(trial)).items()}},sort_keys=True,ensure_ascii=False,default=str)
                    if key in seen:
                        continue
                    seen.add(key)
                    removed = [work.get("stops",{}).get(identity,{}).get("name",identity) for identity in impact["removed_stop_ids"]]
                    description = "影响日期："+("、".join(dates) or "仅预算要求")+"；游玩时长变化"+str(impact["minutes"])+"分钟；已知费用变化"+str(impact["amount"])+"元；步行变化"+str(impact["meters"])+"米。"
                    if removed:
                        description += "移出："+"、".join(removed)+"。"
                    if impact["estimated_transport"]:
                        description += "新增路段按直线交通估计，选择后查询实际交通重算。"
                    changes = authority.user_changes(authority.user_state(work),authority.user_state(trial))
                    rank = (len(changes),len(removed),max(0,impact["travel_minutes"]),max(0,impact["amount"]))
                    option_id = "option-"+hashlib.sha256(json.dumps(proposed,sort_keys=True).encode()).hexdigest()[:16]
                    solved_by_option[option_id] = {_key(item) for item in remaining}-{_key(item) for item in _issues(trial)}
                    options.append((rank,{"id":option_id,
                        "label":label[:200],"description":description[:2000],"actions":proposed,"impact":impact}))
                except (ValueError,KeyError,TypeError,IndexError):
                    continue
                if len(options) >= 4 and issue["code"] != "stop_unplaced":
                    break
        except (ValueError,KeyError,TypeError,IndexError):
            continue
        if len(options) < 2:
            continue
        options.sort(key=lambda pair:pair[0])
        if issue["code"] == "stop_unplaced":
            placements = [pair for pair in options if pair[1]["label"].startswith(("安排到","将指定日期改到"))][:2]
            shortened = [pair for pair in options if any(action["type"] == "shorten" for action in pair[1]["actions"])][:1]
            omitted = [pair for pair in options if pair[1]["label"].startswith("本次不去")][:1]
            replacements = [pair for pair in options if pair[1]["label"].startswith("改去")]
            options = sorted([*placements,*shortened,*omitted,*replacements[:max(0,4-len(placements)-len(shortened)-len(omitted))]],key=lambda pair:pair[0])
        if len(options) < 2:
            continue
        raw_options = [option for _,option in options]
        needed = max(len(option["actions"]) for option in raw_options)
        if total+needed > 24:
            continue
        total += needed
        title = work.get("stops",{}).get(issue.get("target"),{}).get("name") or {
            "over_budget":"预算上限","appointment_time":"预约时刻","locked_time":"锁定安排",
            "transport_time":"班次安排","arrival_conflict":"到达衔接","departure_conflict":"出发衔接","tail_conflict":"返程衔接"}.get(issue["code"],"行程安排")
        question = DecisionQuestion(id="tradeoff-"+hashlib.sha256(str(_key(issue)).encode()).hexdigest()[:16],
            title=("行程取舍："+title)[:180],
            question="以下选项均已由代码模拟，请选择本次安排。",why_needed=issue["message"][:2000],
            unmet_refs=issue.get("refs",[]),affected_dates=sorted({day for option in raw_options for day in option["impact"]["dates"]}),
            affected_stop_ids=sorted({identity for option in raw_options for identity in [*option["impact"]["removed_stop_ids"],*option["impact"]["added_stop_ids"],
                *[item["stop_id"] for item in option["impact"]["moved_stops"]]]} | ({issue["target"]} if issue.get("target") in work.get("stops",{}) else set())),
            options=raw_options,recommended_option_id=raw_options[0]["id"])
        questions.append(question.model_dump(mode="json"))
        handled.update(set.intersection(*(solved_by_option[option["id"]] for option in raw_options)))
    if questions:
        result.batch = DecisionBatch(batch_id=uuid4().hex,draft_revision=state.get("draft_revision",0),stage_scope="draft_tradeoff",
            summary="请选择以下行程取舍；选项影响已由代码模拟。",questions=questions[:6])
    return result
