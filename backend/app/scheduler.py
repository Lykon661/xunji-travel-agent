import copy
import math
import re
from dataclasses import dataclass

from .actions import _route_days, optional_stop_issue
from .authority import USER_SOURCES
from .conflicts import FULL_DAY_MINUTES, _full_day, visit_minutes
from .constraints import active, walking_target
from .dining import is_dining
from .places import canonical_key, repeat_allowed, represented_ids, represented_stop_ids
from .planning_repairs import protected_stops
from .routing import coordinates, distance, reference_opening
from .rules import timestamp, walking_limit


MAX_ROUTE_PASSES = 3
MAX_DAY_STOPS = 12
MAX_LONG_LEG_TRIALS = 6
MAX_LONG_LEG_QUERIES = 16
NIGHT = re.compile(r"夜游|夜景|夜市|灯光")
PARK = re.compile(r"野生动物(?:园|世界)|欢乐世界|主题乐园|水上乐园")


def full_day(stop):
    return _full_day(stop) or bool(PARK.search(stop.get("name","")))


def clear_system_defaults(state):
    user = set()
    for entry in state.get("decision_history",[]):
        if entry.get("auto_settled") or (entry.get("source") or "legacy_unknown") not in USER_SOURCES:
            continue
        for action in entry.get("actions",[]):
            if action.get("type") in {"default_duration","retain_free_time"}:
                user.add((action["type"],action.get("date"),action.get("stop_id")))
    durations = {day:{identity:minutes for identity,minutes in values.items()
                      if ("default_duration",day,identity) in user}
                 for day,values in state.get("system_durations",{}).items()}
    return {"system_durations":{day:values for day,values in durations.items() if values},
            "system_free_time":{day:minutes for day,minutes in state.get("system_free_time",{}).items()
                                if ("retain_free_time",day,None) in user},
            "system_assumptions":[copy.deepcopy(item) for item in state.get("system_assumptions",[]) if
                item.get("current_action",{}).get("type") not in {"default_duration","retain_free_time"} or
                (item["current_action"]["type"],item["current_action"].get("date"),item["current_action"].get("stop_id")) in user]}


def user_duration(state, stop, day):
    req, identity = state["requirements"],stop["id"]
    explicit = next((item["minutes"] for item in active(req,"visit_duration",date=day,stop_id=identity)
                     if item.get("minutes") and (item.get("source") or "legacy_unknown") in USER_SOURCES),None)
    if explicit:
        return explicit
    if (stop.get("source_kind") == "user" or stop.get("product")) and stop.get("duration_minutes") and not stop.get("duration_is_default"):
        return stop["duration_minutes"]
    system = state.get("system_durations",{}).get(day,{}).get(identity)
    if system and any(action.get("type") == "default_duration" and action.get("date") == day and action.get("stop_id") == identity
                      and action.get("minutes") == system for entry in state.get("decision_history",[]) if not entry.get("auto_settled")
                      and (entry.get("source") or "legacy_unknown") in USER_SOURCES for action in entry.get("actions",[])):
        return system


def duration(state, stop, day, estimate=None):
    explicit = user_duration(state,stop,day)
    if explicit is not None:
        return explicit
    req, identity = state["requirements"],stop["id"]
    system = state.get("system_durations",{}).get(day,{}).get(identity)
    minutes = stop.get("duration_minutes") or system or estimate or visit_minutes(stop,req,day)
    return max(FULL_DAY_MINUTES,minutes) if full_day(stop) else minutes


def priority(state, identity, protected=None):
    stop = state["stops"][identity]
    if stop.get("source_kind") == "user" or identity in state.get("locks",[]):
        return 4
    if identity in (protected if protected is not None else protected_stops(state)):
        return 3
    value = state.get("selected_route",{}).get("feasibility",{}).get("priorities",{}).get(identity,stop.get("priority"))
    value = value if isinstance(value,(str,int,float)) else None
    return 2 if value not in {"optional","other","avoid","不建议","可选",0} else 1


def fixed_dates(state):
    result = {identity:{stop["assigned_date"]} for identity,stop in state["stops"].items() if stop.get("assigned_date")}
    for item in state["requirements"].get("constraints",[]):
        scope = item.get("scope") or {}
        if item.get("revoke") or item.get("superseded_by") or not scope.get("dates") or (item.get("source") or "legacy_unknown") not in USER_SOURCES:
            continue
        identities = set(scope.get("stop_ids",[]))
        if item.get("kind") == "fixed_order":
            identities.update(identity for identity in (item.get("before"),item.get("after")) if identity)
        for identity in identities:
            result.setdefault(identity,set()).update(map(str,scope["dates"]))
    for plan in (state.get("old_itinerary") or {},state.get("itinerary") or {}):
        for day in plan.get("days",[]):
            for item in day.get("activities",[]):
                if (item.get("id") in state.get("locks",[]) or item.get("product") and item.get("selected_by_user") is not False
                        or item.get("appointment_time") and (item.get("appointment_time_source") or "legacy_unknown") in USER_SOURCES):
                    result.setdefault(item["id"],set()).add(day["date"])
    return result


def estimate_move(state, first, last):
    a,b = coordinates((first or {}).get("map_place")),coordinates((last or {}).get("map_place"))
    meters = math.ceil(distance(a,b)*1.25) if a and b else None
    mode = state["requirements"].get("local_transport","public")
    if meters == 0:
        minutes,walking,mode = 0,0,"walking"
    elif mode == "walking":
        minutes,walking = math.ceil(meters/80) if meters is not None else 45,meters if meters is not None else 3600
    elif mode == "public" and meters is not None and meters <= 1200:
        minutes,walking,mode = math.ceil(meters/80),meters,"walking"
    else:
        speed = {"public":300,"driving":500,"bicycling":250}.get(mode,300)
        minutes = math.ceil(meters/speed)+(12 if mode == "public" else 5) if meters is not None else 45
        walking = min(meters or 600,600) if mode == "public" else 0
        mode = "transit" if mode == "public" else mode
    return {"minutes":None,"estimate_minutes":minutes,"walking_meters":walking,"distance_meters":meters,
            "mode":mode,"note":"按直线距离换算的交通估计，实际路线待查询"}


def edge(state, window, first, last, moves, selected_move=None):
    if first is None or last is None:
        return {},0,0,0
    def stop(identity):
        if identity == "@start":
            return {"map_place":window.get("tail",{}).get("start_place")}
        if identity == "@end":
            return {"map_place":window.get("tail",{}).get("end_place")}
        return state["stops"][identity]
    guess = estimate_move(state,stop(first),stop(last))
    move = selected_move if selected_move is not None else moves.get((window["date"],first,last))
    if move is None:
        move = next((value for (_,a,b),value in moves.items() if a == first and b == last and value.get("minutes") is not None),guess)
    minutes = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",guess["estimate_minutes"])
    return move,minutes,move.get("walking_meters") if move.get("walking_meters") is not None else guess["walking_meters"],move.get("distance_meters") or guess.get("distance_meters") or 0


@dataclass
class Label:
    visits: tuple
    time: int
    lunch: dict | None = None
    walking: int = 0
    meters: int = 0
    night_penalty: int = 0
    transit_fallbacks: int = 0


def plan_day(state, window, identities, moves=None, preserved=None, order=None, max_leg_minutes=None):
    moves, preserved = moves or {},preserved or {}
    day, req = window["date"],state["requirements"]
    protected = protected_stops(state)
    ids = list(dict.fromkeys(identity for identity in identities if identity in state["stops"]))
    levels = {identity:priority(state,identity,protected) for identity in ids}
    openings = {identity:reference_opening(state["stops"][identity],day) for identity in ids}
    edges = {}
    def movement(first,last):
        if (first,last) not in edges:
            edges[first,last] = edge(state,window,first,last,moves)
        return edges[first,last]
    limits = [value for value in (walking_limit(req,day),walking_target(req,day)) if value is not None]
    limit = min(limits) if limits else math.inf
    base_walk = sum((item.get("move") or {}).get("walking_meters") or 0 for item in window.get("transfers",[]))
    variants = {}
    def movement_options(first,last):
        key = first,last
        if key not in variants:
            primary = movement(first,last)
            variants[key] = [(primary,False)]
            alternative = primary[0].get("transit_alternative")
            if (primary[0].get("mode") == "walking" and alternative and alternative.get("minutes") is not None
                    and alternative.get("mode") == "transit" and (alternative.get("walking_meters") or 0) < primary[2]):
                variants[key].append((edge(state,window,first,last,moves,alternative),True))
        return variants[key]
    def return_movement(last,walking):
        tail = "@end" if last and window.get("tail",{}).get("name") else None
        options = movement_options(last,tail)
        return next((path for path,_ in options if walking+path[2] <= limit),options[0][0])
    deadline = window["end"]-state.get("system_free_time",{}).get(day,0)
    predecessors = {identity:set() for identity in ids}
    for item in active(req,"fixed_order",date=day):
        if item.get("after") in predecessors:
            predecessors[item["after"]].add(item["before"])
    locked_order = [identity for identity in preserved if identity in ids]
    for first,last in zip(locked_order,locked_order[1:]):
        predecessors[last].add(first)
    def finish(label):
        last = label.visits[-1]["id"] if label.visits else None
        tail = "@end" if last and window.get("tail",{}).get("name") else None
        move,minutes,walking,meters = return_movement(last,base_walk+label.walking)
        end = label.time+(20+minutes if tail else 0)
        return end,move,walking,meters
    def score(label):
        counts = tuple(sum(levels[item["id"]] == level for item in label.visits) for level in (4,3,2,1))
        end,_,_,tail_meters = finish(label)
        return (*counts,-label.transit_fallbacks,-label.meters-tail_meters,-label.night_penalty,-end)
    def append_move(label,identity,path,fallback):
        used = {item["id"] for item in label.visits}
        if identity in used or not predecessors[identity] <= used:
            return []
        stop = state["stops"][identity]
        opening = openings[identity]
        if opening["closed"]:
            return []
        first = label.visits[-1]["id"] if label.visits else "@start" if window.get("tail",{}).get("start_name") else None
        move,travel,walking,meters = path
        if label.visits and max_leg_minutes is not None and travel > max_leg_minutes:
            return []
        current = label.time+(20 if label.visits else 0)
        old = preserved.get(identity,{})
        anchors = [item["at"] for kind in ("time_anchor","booking_prerequisite") for item in active(req,kind,date=day,stop_id=identity)
                   if item.get("at") and (item.get("source") or "legacy_unknown") in USER_SOURCES]
        if stop.get("appointment_time") and (stop.get("appointment_time_source") or "legacy_unknown") in USER_SOURCES:
            anchors.append(stop["appointment_time"])
        if old.get("start"):
            anchors.append(old["start"])
        fixed = [timestamp(at,day) for at in anchors]
        fixed = [item.hour*60+item.minute for item in fixed if item]
        if len(set(fixed)) > 1:
            return []
        at = fixed[0] if fixed else None
        minutes = duration(state,stop,day,window.get("durations",{}).get(identity))
        old_start,old_end = timestamp(old.get("start"),day),timestamp(old.get("end"),day)
        old_meal = old.get("meal_break")
        if old_start and old_end:
            minutes = round((old_end-old_start).total_seconds()/60)-(60 if old_meal else 0)
        extend_to_close = full_day(stop) and user_duration(state,stop,day) is None and not (old_start and old_end)
        visit_deadline = deadline
        if extend_to_close:
            minutes = FULL_DAY_MINUTES
            if window.get("tail",{}).get("name"):
                _,return_minutes,_,_ = return_movement(identity,base_walk+label.walking+walking)
                visit_deadline -= 20+return_minutes
        result = []
        for begin,end in opening["windows"]:
            for kind in ("none","before","after","within"):
                if label.lunch and kind != "none":
                    continue
                lunch = label.lunch
                ready = current
                if kind == "before":
                    meal_start = max(current,690)
                    if meal_start+60 > 840:
                        continue
                    lunch = {"start":meal_start,"end":meal_start+60,"within":None}
                    ready = meal_start+60
                earliest = max(ready+travel,begin)
                starts = [earliest]
                if at is not None:
                    starts = [at] if at >= earliest else []
                elif NIGHT.search(stop.get("name","")) and max(earliest,1080)+minutes <= min(end,deadline):
                    starts.append(max(earliest,1080))
                for start in dict.fromkeys(starts):
                    meal = lunch
                    visit_minutes = minutes
                    duration_minutes = minutes
                    if old_meal:
                        a,b = timestamp(old_meal.get("start"),day),timestamp(old_meal.get("end"),day)
                        if not a or not b or kind != "none" or label.lunch:
                            continue
                        meal = {"start":a.hour*60+a.minute,"end":b.hour*60+b.minute,"within":identity}
                        duration_minutes += meal["end"]-meal["start"]
                    elif kind == "within":
                        meal_deadline = min(end,visit_deadline) if extend_to_close else start+minutes
                        if minutes < 180 or not start <= 780 < meal_deadline or old:
                            continue
                        meal_start = max(start,720)
                        meal = {"start":meal_start,"end":meal_start+60,"within":identity}
                        duration_minutes += 60
                    if extend_to_close:
                        duration_minutes = min(end,visit_deadline)-start
                        visit_minutes = duration_minutes-(meal["end"]-meal["start"] if meal and meal["within"] == identity else 0)
                        if visit_minutes < FULL_DAY_MINUTES:
                            continue
                    finish_time = start+duration_minutes
                    current_time = finish_time
                    if kind == "after":
                        if not 690 <= finish_time <= 780:
                            continue
                        meal = {"start":finish_time,"end":finish_time+60,"within":None}
                        current_time += 60
                    if start < begin or finish_time > end or current_time > deadline or opening.get("last_entry_minutes") is not None and start > opening["last_entry_minutes"]:
                        continue
                    first_start = label.visits[0]["start_minutes"] if label.visits else start
                    if first_start < 780 and finish_time > 810 and not meal:
                        continue
                    if base_walk+label.walking+walking > limit:
                        continue
                    row = {"id":identity,"start_minutes":start,"end_minutes":finish_time,"duration_minutes":visit_minutes,
                           "opening_window":[begin,end],"opening_reference":opening,"move_before":move,
                           "appointment_time":old.get("appointment_time") or (anchors[0] if anchors and not old else None),
                           "appointment_time_source":old.get("appointment_time_source") or ("message" if anchors and not old else None)}
                    candidate = Label((*label.visits,row),current_time,meal,label.walking+walking,label.meters+meters,
                                      label.night_penalty+(max(0,1080-start) if NIGHT.search(stop.get("name","")) else 0),
                                      label.transit_fallbacks+int(fallback))
                    final,_,tail_walk,_ = finish(candidate)
                    if final <= deadline and base_walk+candidate.walking+tail_walk <= limit:
                        result.append(candidate)
        return result
    def append(label,identity):
        if any(item["id"] == identity for item in label.visits):
            return []
        first = label.visits[-1]["id"] if label.visits else "@start" if window.get("tail",{}).get("start_name") else None
        return [candidate for path,fallback in movement_options(first,identity)
                for candidate in append_move(label,identity,path,fallback)]
    initial = Label((),window["start"])
    best, frontier = initial,[initial]
    if order is not None:
        for identity in order:
            following = [next_label for label in frontier for next_label in append(label,identity)]
            if following:
                frontier = sorted(following,key=score,reverse=True)[:32]
        best = max(frontier,key=score)
    else:
        for depth in range(min(len(ids),MAX_DAY_STOPS)):
            following = [next_label for label in frontier for identity in ids for next_label in append(label,identity)]
            if not following:
                break
            unique = {}
            for label in following:
                key = (tuple(item["id"] for item in label.visits),label.time,label.walking,label.transit_fallbacks,
                       tuple(label.lunch.values()) if label.lunch else ())
                if key not in unique or score(label) > score(unique[key]):
                    unique[key] = label
            frontier = sorted(unique.values(),key=score,reverse=True)[:8000 if len(ids) <= 6 else 64]
            candidate = max(frontier,key=score)
            if score(candidate) > score(best):
                best = candidate
    final,return_move,tail_walk,_ = finish(best)
    selected = [item["id"] for item in best.visits]
    return {"date":day,"city":window["city"],"visits":list(best.visits),"stop_ids":selected,"lunch":best.lunch,
            "finish":final,"return_move":return_move,"walking_meters":base_walk+best.walking+tail_walk,
            "unplaced":[{"id":identity,"reason":"当天闭馆" if reference_opening(state["stops"][identity],day)["closed"] else
                         "开放窗口、停止入场、预约/锁定、午餐、步行上限或当天容量无法同时满足"} for identity in ids if identity not in selected]}


def route_metrics(days):
    minutes = [row["move_before"].get("minutes") if row["move_before"].get("minutes") is not None else
               row["move_before"].get("estimate_minutes",45) for day in days.values() for row in day["visits"][1:]]
    return max(minutes,default=0),sum(minutes)


def geographic_splits(state, days, first, second, fixed):
    dates = first,second
    combined = list(dict.fromkeys(identity for date in dates for identity in days[date]["stop_ids"]))
    points = {identity:point for identity in combined if (point := coordinates(state["stops"][identity].get("map_place")))}
    retained = {date:[identity for identity in days[date]["stop_ids"] if identity in fixed or identity not in points] for date in dates}
    movable = [identity for identity in combined if identity not in fixed and identity in points]
    anchors = {date:[points[identity] for identity in retained[date] if identity in points] for date in dates}
    def center(values):
        return tuple(sum(point[axis] for point in values)/len(values) for axis in (0,1)) if values else None
    centers = [center(anchors[date] or [points[identity] for identity in days[date]["stop_ids"] if identity in points]) for date in dates]
    if not all(centers) or not movable:
        return
    seeds = [tuple(centers)]
    if not anchors[first] and not anchors[second] and len(movable) > 1:
        a,b = max(((a,b) for index,a in enumerate(movable) for b in movable[index+1:]),
                  key=lambda pair:(distance(points[pair[0]],points[pair[1]]),pair))
        seeds.extend(((points[a],points[b]),(points[b],points[a])))
    elif not anchors[first]:
        seeds.append((points[max(movable,key=lambda identity:(distance(points[identity],centers[1]),identity))],centers[1]))
    elif not anchors[second]:
        seeds.append((centers[0],points[max(movable,key=lambda identity:(distance(points[identity],centers[0]),identity))]))
    seen = set()
    for left,right in seeds:
        def separation(identity):
            point = points[identity]
            a = min(distance(point,anchor) for anchor in anchors[first]) if anchors[first] else distance(point,left)
            b = min(distance(point,anchor) for anchor in anchors[second]) if anchors[second] else distance(point,right)
            return a-b,identity
        ordered = sorted(movable,key=separation)
        for count in range(len(ordered)+1):
            allocation = {first:[*retained[first],*ordered[:count]],second:[*retained[second],*ordered[count:]]}
            key = tuple(tuple(sorted(allocation[date])) for date in dates)
            if key not in seen:
                seen.add(key)
                yield allocation


def long_leg_candidates(state, windows, days, moves, preserved, attempted):
    fixed = set(fixed_dates(state)) | {identity for entries in preserved.values() for identity in entries}
    endpoints = {(date,identity) for date,day in days.items() for a,b in zip(day["visits"],day["visits"][1:])
                 if (b.get("move_before") or {}).get("minutes") is not None and b["move_before"]["minutes"] > 45
                 for identity in (a["id"],b["id"]) if identity not in fixed}
    candidates = []
    cache,seen = {},set()
    def compute(date, ids, max_leg_minutes):
        key = date,tuple(ids),max_leg_minutes
        if key not in cache:
            cache[key] = plan_day(state,windows[date],ids,moves,preserved.get(date),max_leg_minutes=max_leg_minutes)
        return cache[key]
    def consider(allocation, kind):
        first,second = sorted(allocation)
        operation = first,second,tuple(sorted(allocation[first])),tuple(sorted(allocation[second]))
        if operation in attempted or operation in seen:
            return
        seen.add(operation)
        if sorted(identity for date in allocation for identity in allocation[date]) != sorted(
                identity for date in allocation for identity in days[date]["stop_ids"]):
            return
        for date,ids in allocation.items():
            minimum = max((item["count"] for item in active(state["requirements"],"daily_min_stops",date=date)
                           if item["strength"] == "hard" and (not item["scope"]["cities"] or windows[date]["city"] in item["scope"]["cities"])),default=0)
            if (not ids or len(ids) > MAX_DAY_STOPS or len(ids) < minimum
                    or len(ids) > 1 and any(full_day(state["stops"][item]) for item in ids)):
                return
        before = route_metrics({date:days[date] for date in allocation})
        trial = {date:compute(date,ids,before[0]-1) for date,ids in allocation.items()}
        if any(set(trial[date]["stop_ids"]) != set(ids) for date,ids in allocation.items()):
            return
        after = route_metrics(trial)
        if after[0] < before[0] and after[1] <= before[1]:
            candidates.append({"operation":operation,"days":trial,"rank":(*after,*operation),"kind":kind})
    for source,identity in sorted(endpoints):
        for target,other in sorted(days.items()):
            if target == source or windows[target]["city"] != windows[source]["city"]:
                continue
            for replacement in [None,*[item for item in other["stop_ids"] if item not in fixed]]:
                allocation = {source:[item for item in days[source]["stop_ids"] if item != identity]+([replacement] if replacement else []),
                              target:[item for item in other["stop_ids"] if item != replacement]+[identity]}
                consider(allocation,"swap" if replacement else "move")
    long_days = {date for date,day in days.items() if any((row.get("move_before") or {}).get("minutes") is not None
                 and row["move_before"]["minutes"] > 45 for row in day["visits"][1:])}
    pairs = {tuple(sorted((source,target))) for source in long_days for target in days
             if source != target and windows[source]["city"] == windows[target]["city"]}
    for first,second in sorted(pairs):
        for allocation in geographic_splits(state,days,first,second,fixed):
            consider(allocation,"repartition")
    return sorted(candidates,key=lambda item:item["rank"])


def plan_trip(state, routes, windows, moves=None, preserved=None, exact_orders=None):
    moves, preserved = moves or {},preserved or {}
    dates = set(windows)
    protected, fixed = protected_stops(state),fixed_dates(state)
    allocation = {day["date"]:list(day.get("stop_ids",[])) for day in routes}
    stops,req = state["stops"],state["requirements"]
    suppressed = represented_stop_ids(state)
    spatial_pending = state.get("selected_route",{}).get("feasibility",{}).get("spatial_unplaced",[])
    intended_ids = {identity for ids in allocation.values() for identity in ids}
    for item in spatial_pending:
        if item["date"] in allocation and item["id"] in stops and item["id"] not in intended_ids:
            allocation[item["date"]].append(item["id"])
            intended_ids.add(item["id"])
    deferred = {item["id"] for item in spatial_pending if item["id"] not in protected and item["id"] not in fixed}
    excluded = set(req.get("excluded_stop_ids",[])) | set(state.get("stop_constraints",{}).get("excluded",[]))
    excluded_keys = {canonical_key(stops[identity]) for identity in excluded if identity in stops}
    declined = {(action.get("date"),action.get("stop_id")) for entry in state.get("decision_history",[]) if not entry.get("auto_settled")
                and (entry.get("source") or "legacy_unknown") in USER_SOURCES for action in entry.get("actions",[]) if action.get("type") == "skip"}
    declined_keys = {(day,canonical_key(stops[identity])) for day,identity in declined if identity in stops}
    results,used = {},set()
    for day in routes:
        day_date = day["date"]
        if day_date not in dates:
            used.update(canonical_key(stops[i]) for i in allocation[day_date] if i in stops)
    intended = {identity for ids in allocation.values() for identity in ids}
    intended_keys = {canonical_key(stops[i]) for i in intended if i in stops}
    for identity in sorted(protected,key=lambda identity:-priority(state,identity,protected)):
        if identity not in stops or canonical_key(stops[identity]) in intended_keys or is_dining(stops[identity]):
            continue
        choices = [day for day in routes if day["date"] in dates and day["city"] == stops[identity].get("city")
                   and (identity not in fixed or day["date"] in fixed[identity]) and not reference_opening(stops[identity],day["date"])["closed"]]
        if choices:
            allocation[choices[0]["date"]].append(identity)
            intended.add(identity)
            intended_keys.add(canonical_key(stops[identity]))
    for day_date in dates:
        allocation[day_date] = [identity for identity in allocation[day_date] if identity not in suppressed]
    cache = {}
    def compute(day_date,ids):
        key = day_date,tuple(ids)
        if key not in cache:
            cache[key] = plan_day(state,windows[day_date],ids,moves,preserved.get(day_date),
                                  exact_orders.get(day_date) if exact_orders is not None else None)
        return copy.deepcopy(cache[key])
    origins = {}
    for day in routes:
        day_date = day["date"]
        if day_date not in dates:
            continue
        ids = []
        for identity in allocation[day_date]:
            if identity not in stops or is_dining(stops[identity]) or represented_ids([stops[identity]]) & excluded or canonical_key(stops[identity]) in excluded_keys:
                continue
            key = canonical_key(stops[identity])
            if key in used and not repeat_allowed(stops[identity],day,"visit",req):
                continue
            if identity in fixed and day_date not in fixed[identity]:
                continue
            ids.append(identity)
            origins.setdefault(identity,day_date)
            used.add(key)
        allocation[day_date] = ids
        results[day_date] = compute(day_date,[identity for identity in ids if identity not in deferred or exact_orders is not None])
        if exact_orders is None:
            results[day_date]["unplaced"].extend({"id":identity,"reason":"空间分日未能容纳，继续尝试附近同城开放日期"}
                                                for identity in ids if identity in deferred)
    relocated = []
    if exact_orders is None:
        pending = [(day,entry) for day,result in results.items() for entry in result["unplaced"]]
        attempts = 0
        while pending and attempts < MAX_DAY_STOPS*len(dates):
            pending.sort(key=lambda item:-priority(state,item[1]["id"],protected))
            original,entry = pending.pop(0)
            attempts += 1
            identity = entry["id"]
            if identity in fixed or any(identity in result["stop_ids"] for result in results.values()):
                continue
            choices = []
            for target,result in results.items():
                if (target == original and identity not in deferred or windows[target]["city"] != stops[identity].get("city")
                        or reference_opening(stops[identity],target)["status"] != "open"):
                    continue
                nearest = min((estimate_move(state,stops[other],stops[identity])["estimate_minutes"]
                               for other in result["stop_ids"]),default=0)
                if nearest > 40 and identity not in protected:
                    continue
                trial = compute(target,[*result["stop_ids"],identity])
                retained = {i for i in result["stop_ids"] if identity not in protected or i in protected or i in fixed}
                if retained | {identity} <= set(trial["stop_ids"]):
                    choices.append((nearest,trial["finish"],target,trial))
            if choices:
                _,_,target,trial = min(choices,key=lambda value:(value[0],value[1],value[2]))
                displaced = set(results[target]["stop_ids"])-set(trial["stop_ids"])
                pending.extend((target,{"id":other,"reason":"为用户必去/想去地点优先预留容量"}) for other in sorted(displaced))
                results[target] = trial
                results[original]["unplaced"] = [item for item in results[original]["unplaced"] if item["id"] != identity]
                relocated.append({"id":identity,"from":original,"to":target})
    added = []
    occupied = {canonical_key(stops[i]) for result in results.values() for i in result["stop_ids"]}
    occupied.update(canonical_key(stops[i]) for day,ids in allocation.items() if day not in dates for i in ids if i in stops)
    addition_days = copy.deepcopy(_route_days(state))
    scheduled_ids = {identity for result in results.values() for identity in result["stop_ids"]}
    for row in addition_days:
        if row["date"] in results:
            retained = [identity for identity in row.get("stop_ids",[]) if identity in protected and identity not in scheduled_ids]
            row["stop_ids"] = list(dict.fromkeys([*results[row["date"]]["stop_ids"],*retained]))
    def spare_minutes(result,window):
        occupied = 0
        previous = None
        for visit in result["visits"]:
            occupied += visit["end_minutes"]-visit["start_minutes"]
            if previous is not None:
                occupied += 20
            move = visit.get("move_before") or {}
            occupied += move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
            previous = visit
        meal = result.get("lunch")
        if meal and not meal["within"]:
            occupied += meal["end"]-meal["start"]
        move = result.get("return_move") or {}
        if window.get("tail",{}).get("name") and previous:
            occupied += 20+(move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0))
        return window["end"]-window["start"]-state.get("system_free_time",{}).get(window["date"],0)-occupied
    for day_date,result in list(results.items()):
        window = windows[day_date]
        pace = req.get("preferences","")+" "+state.get("revision_request","")
        relaxed = bool(re.search(r"轻松|慢节奏|休闲|少安排|多留白",pace))
        allow_fill = exact_orders is None and not (state.get("change",{}).get("fewer_activities") or state.get("revision_selected_ids")
                     or state.get("change",{}).get("refresh_resources") or state.get("keep_current_plan") or relaxed)
        shopping_preference = bool(re.search(r"购物|逛街|买东西"," ".join(text for text in
            [req.get("preferences"),state.get("preferences_original"),state.get("message"),state.get("revision_request"),
             *state.get("user_inputs",[])] if isinstance(text,str))))
        def shopping(stop):
            place = stop.get("map_place") or {}
            codes = re.split(r"[|;,\s]+",str(place.get("typecode") or ""))
            category = str(stop.get("category") or "")+" "+str(place.get("type") or "")
            name = stop.get("name","")
            return (any(code.startswith("06") for code in codes)
                    or bool(re.search(r"购物|商场|百货|奥莱|奥特莱斯|shopping|mall",category,re.I))
                    or bool(re.search(r"购物中心|购物广场|商场|百货|奥莱|奥特莱斯",name))
                    or bool(re.search(r"广场|汇",name) and re.search(r"商业|商圈|零售",category)))
        filled_shopping = 0
        for _ in range(1 if state.get("duplicate_repair_dates") else MAX_DAY_STOPS):
            if not allow_fill or spare_minutes(result,window) < 90 or len(result["stop_ids"]) >= MAX_DAY_STOPS:
                break
            pool = []
            for identity,stop in stops.items():
                if (stop.get("city") != window["city"] or is_dining(stop) or canonical_key(stop) in occupied | excluded_keys
                        or identity in suppressed
                        or represented_ids([stop]) & excluded or (day_date,canonical_key(stop)) in declined_keys or identity in protected
                        or stop.get("assigned_date") not in {None,day_date} or not coordinates(stop.get("map_place"))
                        or optional_stop_issue(state,identity,day_date,addition_days)):
                    continue
                opening = reference_opening(stop,day_date)
                if opening["closed"]:
                    continue
                is_shopping = shopping(stop)
                if is_shopping and filled_shopping:
                    continue
                nearby = [estimate_move(state,stops[other],stop) for other in result["stop_ids"]]
                nearest = min(nearby,key=lambda move:(move["estimate_minutes"],move.get("distance_meters") or math.inf)) if nearby else {}
                travel = nearest.get("estimate_minutes",0)
                if travel > (40 if state.get("duplicate_repair_dates") else 50):
                    continue
                meters = nearest.get("distance_meters") or 0
                band = 0 if travel <= 15 else 1 if travel <= 30 else 2 if travel <= 40 else 3
                level = priority(state,identity,protected)-(1 if is_shopping and not shopping_preference else 0)
                evening = bool(NIGHT.search(stop.get("name","")) or opening["status"] == "open" and any(b >= 1200 for a,b in opening["windows"]))
                pool.append((-level,band,not evening if result["finish"] >= 960 else False,meters,identity,travel))
            placed = False
            for radius in (40,50):
                if radius == 50 and state.get("duplicate_repair_dates"):
                    break
                if radius == 50 and spare_minutes(result,window) < 150:
                    break
                for *_,identity,travel in sorted(pool):
                    if travel > radius or radius == 50 and travel <= 40:
                        continue
                    trial = compute(day_date,[*result["stop_ids"],identity])
                    if state.get("duplicate_repair_dates"):
                        adjacent = [visit.get("move_before") or {} for index,visit in enumerate(trial["visits"])
                                    if index and (visit["id"] == identity or trial["visits"][index-1]["id"] == identity)]
                        if any((move["minutes"] if move.get("minutes") is not None else move.get("estimate_minutes",0)) > 40 for move in adjacent):
                            continue
                    if set(result["stop_ids"]) | {identity} <= set(trial["stop_ids"]):
                        result = trial
                        results[day_date] = result
                        occupied.add(canonical_key(stops[identity]))
                        next(row for row in addition_days if row["date"] == day_date)["stop_ids"].append(identity)
                        if shopping(stops[identity]):
                            filled_shopping += 1
                        if identity in origins:
                            if origins[identity] != day_date:
                                relocated.append({"id":identity,"from":origins[identity],"to":day_date})
                        else:
                            added.append({"id":identity,"date":day_date})
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                break
    scheduled = {canonical_key(stops[i]) for result in results.values() for i in result["stop_ids"]}
    for day_date,result in results.items():
        result["unplaced"] = [{"id":identity,"reason":"当天闭馆" if reference_opening(stops[identity],day_date)["closed"] else
                               "开放窗口、停止入场、预约/锁定、午餐、步行上限或当天容量无法同时满足"}
                              for identity in allocation[day_date] if canonical_key(stops[identity]) not in scheduled]
    return {"days":results,"relocated":relocated,"added":added,"origins":origins}
