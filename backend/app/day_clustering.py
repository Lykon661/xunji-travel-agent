from fractions import Fraction
from itertools import product

from .conflicts import (DEFAULT_MOVE_MINUTES, FULL_DAY_MINUTES, LUNCH_MINUTES,
                        _minimum as daily_minimum, visit_minutes)
from .constraints import active, trip_dates
from .dining import is_dining, visit_required_ids, plan_meal_wishes
from .models import Candidate, CandidateDay, Requirements
from .places import canonical_key, repeat_allowed, represented_ids
from .routing import coordinates, daily_route_scope, distance, reference_opening
from .rules import merge_candidate_duplicates, validate_candidate
from .scheduler import fixed_dates, full_day as is_full_day
from .planning_repairs import protected_stops

ORDINARY_LOCATION_LIMIT = 48
MAX_LOCAL_PASSES = 20


def _minutes(stop, req, day, durations=None):
    estimate = stop.get("duration_minutes") or (durations or {}).get(stop.get("id"))
    return max(visit_minutes(stop,req,day,estimate),FULL_DAY_MINUTES if is_full_day(stop) else 0)


def _floor(req, day, city):
    return daily_minimum(req,day,city)


def _load(ids, stops, req, day, budget, durations=None):
    return sum(_minutes(stops[i],req,day,durations) for i in ids)+(budget["lunch"] if ids else 0)+max(0,len(ids)-1)*(DEFAULT_MOVE_MINUTES+20)


def _default_budget(req, day, city, old):
    start = sum(int(v)*m for v,m in zip((req.get("earliest_departure") or "09:00").split(":"),(60,1)))
    end = sum(int(v)*m for v,m in zip(req["latest_return"].split(":"),(60,1))) if req.get("latest_return") and daily_route_scope(old,{"date":day,"city":city})["hotel_location_confirmed"] else 1200
    return {"city":city,"start":start,"end":end,"lunch":LUNCH_MINUTES if start < 840 and end >= 750 else 0,
            "minimum":_floor(req,day,city),"load":0,"basis":"default_estimate"}


def spatial_errors(plan: dict, candidate: dict, stops: dict[str, dict], requirements: dict, durations=None) -> list[str]:
    errors, days = [],candidate.get("days",[])
    if plan.get("status") == "ready" and [(d["date"],d["city"]) for d in days] != [(d["date"],d["city"]) for d in plan["days"]]:
        errors.append("城市日期段必须与空间分日计划一致")
    visits = [(d["date"],i) for d in days for i in d.get("stop_ids",[]) if i in stops]
    covered = represented_ids([stops[i] for _,i in visits])
    keys = {canonical_key(stops[i]) for _,i in visits}
    for i in plan["required_ids"]:
        if i not in covered and (i not in stops or canonical_key(stops[i]) not in keys):
            errors.append(f"缺少必去或锁定地点：{i}")
    for i,date in plan["fixed_dates"].items():
        if not any(day == date and (j == i or canonical_key(stops[j]) == canonical_key(stops[i])) for day,j in visits):
            errors.append(f"固定地点 {i} 必须保留在 {date}")
    for day in days:
        date,city,ids = day["date"],day["city"],day.get("stop_ids",[])
        if any(i not in stops or stops[i].get("city") != city or is_dining(stops[i]) for i in ids):
            errors.append(f"{date} 引用了未知、跨城市或餐饮地点")
            continue
        for i in ids:
            if represented_ids([stops[i]]) & set(requirements.get("excluded_stop_ids",[])):
                errors.append(f"{date} 包含排除地点 {i}")
            if (i in plan["fixed_dates"] and plan["fixed_dates"][i] != date) or stops[i].get("assigned_date") not in {None,date}:
                errors.append(f"固定地点 {i} 不得移至 {date}")
            if (reference_opening(stops[i],date) or {}).get("closed") is True:
                errors.append(f"{date} 地点 {i} 明确闭馆")
        budget = plan["day_budgets"].get(date)
        if budget and budget.get("city",city) != city:
            budget = plan.get("default_day_budgets",{}).get(date,{}).get(city) or _default_budget(requirements,date,city,{})
        if budget and _load(ids,stops,requirements,date,budget,durations) > max(0,budget["end"]-budget["start"]):
            errors.append(f"{date} 超出每日估计时间容量")
        if len(ids) > 12 or len(ids) < _floor(requirements,date,city):
            errors.append(f"{date} 景点数量不满足每日要求")
        if len(ids) > 1 and any(is_full_day(stops[i]) for i in ids):
            errors.append(f"{date} 全天项目必须独占一天")
    return errors


def build_spatial_plan(state: dict, stops: dict[str, dict], days=None, durations=None, priorities=None) -> dict:
    req, constraints = state["requirements"],state.get("stop_constraints") or {}
    dates,cities = trip_dates(req),req["cities"]
    required = set(constraints.get("required",[])) | set(req.get("required_stop_ids",[]))
    required = visit_required_ids(required,{**state.get("stops",{}),**stops},req)
    excluded = set(constraints.get("excluded",[])) | set(req.get("excluded_stop_ids",[]))
    locks,old = set(state.get("locks",[])),state.get("old_itinerary") or {}
    required |= {i for i,s in state.get("stops",stops).items() if i in locks and not is_dining(s)}
    preferred_required = set(required)
    all_stops = {**state.get("stops",{}),**stops}
    required |= {identity for identity in protected_stops({**state,"stops":all_stops}) & stops.keys()
                 if not is_dining(all_stops[identity])}
    allowed_dates = fixed_dates({**state,"stops":all_stops})
    fixed,groups,pool,issues = {},{},{},[]
    plan = {"version":1,"status":"partial",
            "days":[],"day_budgets":{},"default_day_budgets":{},"city_days":{},"required_ids":[],"fixed_dates":fixed,"visit_minutes":{},
            "unlocated_stop_ids":[],"reserve_stop_ids":[],"issues":issues}
    for i,stop in sorted(stops.items()):
        if is_dining(stop) or stop.get("city") not in cities or represented_ids([stop]) & excluded:
            continue
        refs = represented_ids([stop]) | {i}
        inherited_dates = [values for identity,values in allowed_dates.items() if identity in refs
                           and (identity == i or identity not in stops or identity not in locks)]
        if inherited_dates:
            allowed_dates[i] = set().union(*inherited_dates)
        locked = i in locks or bool((refs & locks)-stops.keys())
        recovered = sorted({d["date"] for d in old.get("days",[]) for a in d.get("activities",[])
                            if a.get("kind") == "visit" and (a.get("id") in refs or canonical_key(a,d.get("city","")) == canonical_key(stop))}) if locked else []
        direct = sorted({d["date"] for d in old.get("days",[]) for a in d.get("activities",[]) if a.get("kind") == "visit" and a.get("id") == i}) if locked else []
        recovered = direct or recovered
        if locked and not recovered and days:
            recovered = [day["date"] for day in days if i in day.get("stop_ids",[])]
        assigned = stop.get("assigned_date")
        if assigned and recovered and any(day != assigned for day in recovered):
            issues.append(f"固定地点 {i} 的采用日期 {assigned} 与锁定日期 {recovered} 冲突")
            plan["status"] = "conflict"
        if locked and len(recovered) != 1:
            issues.append(f"锁定地点 {i} 无法恢复唯一日期：{recovered}")
        allowed = allowed_dates.get(i,set())
        original = next((day["date"] for day in days or [] if i in day.get("stop_ids",[])),None)
        day = assigned or (recovered[0] if len(recovered) == 1 else None) or (next(iter(allowed)) if len(allowed) == 1 else None)
        if not coordinates(stop.get("map_place")) and original and not day:
            day = original
        if day:
            fixed[i] = day
        groups.setdefault(canonical_key(stop),[]).append((i,bool(day or locked),i in required or bool((refs & required)-stops.keys())))
    for entries in groups.values():
        entries.sort(key=lambda entry:(not entry[1],entry[0] not in preferred_required,not entry[2],entry[0]))
        protected = [i for i,p,_ in entries if p]
        keep = protected or [entries[0][0]]
        if len(protected) > 1 and not all(i in fixed and repeat_allowed(stops[i],{"date":fixed[i]},"visit",req) for i in protected):
            issues.append("同一地点的固定引用无法确定唯一访问实例："+"、".join(protected))
        pool.update({i:stops[i] for i in keep})
        if any(p or r for _,p,r in entries):
            plan["required_ids"].extend(keep)
    missing = required-represented_ids([stop for entries in groups.values() for i,_,_ in entries for stop in [stops[i]]])
    plan["required_ids"] = sorted(set(plan["required_ids"]) | missing)
    issues.extend("必去地点未进入可安排候选："+i for i in sorted(missing))
    if any(day not in dates for day in fixed.values()):
        issues.extend(f"固定地点 {i} 日期 {day} 超出旅行范围" for i,day in fixed.items() if day not in dates)
        plan["status"] = "conflict"
    weights = [sum(max((_minutes(s,req,d,durations) for d in dates),default=0) for s in pool.values() if s["city"] == city) for city in cities]
    weights = weights if sum(weights) else [1]*len(cities)
    minimum_days = [max(1,sum(is_full_day(pool[i]) for i in plan["required_ids"] if i in pool and pool[i]["city"] == city))
                    for city in cities]
    total,dp = sum(weights),{0:(0,())}
    for index,city in enumerate(cities):
        next_dp = {}
        for used,(cost,counts) in dp.items():
            for n in range(minimum_days[index],len(dates)-used-sum(minimum_days[index+1:])+1):
                segment = dates[used:used+n]
                if any((day in segment) != (pool[i]["city"] == city) for i,day in fixed.items() if i in pool):
                    continue
                value = cost+(n*total-len(dates)*weights[index])**2,(*counts,-n)
                if used+n not in next_dp or value < next_dp[used+n]:
                    next_dp[used+n] = value
        dp = next_dp
    if days:
        day_cities = [day["city"] for day in days]
        plan["city_days"] = {city:day_cities.count(city) for city in cities}
    elif len(dates) not in dp or plan["status"] == "conflict":
        plan["status"] = "conflict"
        issues.append("固定日期与城市连续日期段冲突："+"、".join(f"{i} {d}" for i,d in fixed.items()))
        return plan
    else:
        plan["city_days"] = dict(zip(cities,(-n for n in dp[len(dates)][1])))
        day_cities = [city for city,n in plan["city_days"].items() for _ in range(n)]
    budgets = plan["day_budgets"]
    for day,city in zip(dates,day_cities):
        plan["default_day_budgets"][day] = {c:_default_budget(req,day,c,old) for c in cities}
        budget = plan["default_day_budgets"][day][city]
        start,end = budget["start"],budget["end"]
        previous = next((d for d in old.get("days",[]) if d["date"] == day and d.get("city") == city),{}) if state.get("old_requirements") == req else {}
        start,end = (previous.get("available_start") if previous.get("available_start") is not None else start),(previous.get("tail_deadline") if previous.get("tail_deadline") is not None else end)
        budgets[day] = {"city":city,"start":start,"end":end,"lunch":LUNCH_MINUTES if start < 840 and end >= 750 else 0,
                        "minimum":_floor(req,day,city),"load":0,"basis":"previous_window" if any(previous.get(f) is not None for f in ("available_start","tail_deadline")) else "default_estimate"}
    points = {i:p for i,s in pool.items() if (p := coordinates(s.get("map_place"))) is not None}
    targets = {day:max(budgets[day]["minimum"],int(any(not item["scope"]["cities"] or city in item["scope"]["cities"]
                   for item in active(req,"daily_min_stops",date=day) if item["strength"] == "soft")))
               for day,city in zip(dates,day_cities)}
    plan["unlocated_stop_ids"] = sorted(set(pool)-points.keys())
    distances = {(i,j):round(distance(a,b)) for i,a in points.items() for j,b in points.items() if pool[i]["city"] == pool[j]["city"]}
    def medoid(ids):
        return min((i for i in ids if i in points),key=lambda i:(sum(distances[i,j] for j in ids if j in points),i),default=None)
    def objective(allocation):
        centers = [medoid(ids) for ids in allocation]
        return 4*sum(distances[i,c] for ids,c in zip(allocation,centers) for i in ids if i in points)+sum(
            distances[a,b] for k,(a,b) in enumerate(zip(centers,centers[1:])) if a and b and day_cities[k] == day_cities[k+1])
    def valid(k, ids, minimum=False):
        day = dates[k]
        return (len(ids) <= 12 and (not minimum or len(ids) >= budgets[day]["minimum"])
                and not (len(ids) > 1 and any(is_full_day(pool[i]) for i in ids))
                and all(pool[i]["city"] == day_cities[k] and fixed.get(i,day) == day
                        and (i not in allowed_dates or day in allowed_dates[i])
                        and (reference_opening(pool[i],day) or {}).get("closed") is not True for i in ids)
                and _load(ids,pool,req,day,budgets[day],durations) <= max(0,budgets[day]["end"]-budgets[day]["start"]))
    allocation = [[] for day in dates]
    used = set()
    for k,day in enumerate(dates):
        for i in sorted(fixed,key=lambda i:(not is_full_day(pool[i]),i)):
            if fixed[i] == day and valid(k,[*allocation[k],i]):
                allocation[k].append(i)
                used.add(i)
    seeds = {}
    for k,ids in enumerate(allocation):
        center = medoid(ids)
        choices = [i for i in points if i not in used and valid(k,[*ids,i])]
        centers = [medoid(v) for j,v in enumerate(allocation) if day_cities[j] == day_cities[k] and medoid(v)]+[c for j,c in seeds.items() if c and day_cities[j] == day_cities[k]]
        seeds[k] = center or min(choices,key=lambda i:(-min((distances[i,c] for c in centers),default=0),i),default=None)
    def place(i, only=None):
        choices = []
        base = objective(allocation)
        for k,ids in enumerate(allocation):
            if (only is not None and k != only) or not valid(k,[*ids,i]):
                continue
            if is_full_day(pool[i]) and i not in plan["required_ids"] and budgets[dates[k]]["minimum"] > 1:
                continue
            proposal = [values if j != k else [*ids,i] for j,values in enumerate(allocation)]
            penalty = 4*distances[i,seeds[k]] if i in points and seeds[k] and not medoid(ids) else 0
            budget = budgets[dates[k]]
            choices.append((len(ids) >= targets[dates[k]],objective(proposal)-base+penalty,
                            Fraction(_load([*ids,i],pool,req,dates[k],budget,durations),max(1,budget["end"]-budget["start"])),k))
        if choices:
            k = min(choices)[-1]
            allocation[k].append(i)
            used.add(i)
        return bool(choices)
    mandatory = [i for i in plan["required_ids"] if i in points and i not in used]
    mandatory.sort(key=lambda i:(not is_full_day(pool[i]),sum(valid(k,[*ids,i]) for k,ids in enumerate(allocation)),
                                 -max(_minutes(pool[i],req,d,durations) for d in dates),i))
    for i in mandatory:
        if is_full_day(pool[i]):
            empty = next((k for k,ids in enumerate(allocation) if not ids and valid(k,[i])),None)
            if empty is not None:
                place(i,empty)
        else:
            place(i)
    while True:
        gaps = [(len(options)-max(0,targets[dates[k]]-len(ids)),k,options)
                for k,ids in enumerate(allocation) if len(ids) < targets[dates[k]]
                if (options := [i for i in sorted(points) if i not in used and not is_full_day(pool[i]) and valid(k,[*ids,i])])]
        if not gaps:
            break
        _,k,options = min(gaps)
        place(min(options,key=lambda i:(objective([v if j != k else [*v,i] for j,v in enumerate(allocation)])+
              (4*distances[i,seeds[k]] if seeds[k] and not medoid(allocation[k]) else 0),i)),k)
    for i in sorted(points,key=lambda i:((priorities or {}).get(i) in {"optional","avoid"},i)):
        if i not in used and (not is_full_day(pool[i]) or any(budgets[d]["minimum"] <= 1 and not allocation[k] for k,d in enumerate(dates))):
            place(i)
    def accept(proposal, affected):
        return all(valid(k,proposal[k],True) and (not targets[dates[k]] or not allocation[k] or bool(proposal[k]))
                   for k in affected) and objective(proposal) < objective(allocation)
    for city in cities:
        segment = []
        for k in [j for j,c in enumerate(day_cities) if c == city]+[len(dates)]:
            if k < len(dates) and not any(i in fixed for i in allocation[k]):
                segment.append(k)
                continue
            remaining,order = list(segment),[]
            entrance = medoid(allocation[segment[0]-1]) if segment and segment[0] and day_cities[segment[0]-1] == city else None
            while remaining:
                j = min(remaining,key=lambda j:(medoid(allocation[j]) is None,distances[entrance,medoid(allocation[j])] if entrance and medoid(allocation[j]) else 0,medoid(allocation[j]) or "",j))
                order.append(j)
                remaining.remove(j)
                entrance = medoid(allocation[j])
            proposal = [list(ids) for ids in allocation]
            for target,source in zip(segment,order):
                proposal[target] = allocation[source]
            if accept(proposal,segment):
                allocation = proposal
            segment = []
    for _ in range(MAX_LOCAL_PASSES):
        best = None
        for k in range(len(dates)-1):
            if day_cities[k] != day_cities[k+1]:
                continue
            left,right = allocation[k:k+2]
            operations = [("move",i,"") for i in sorted(left) if i not in fixed]+[("move","",i) for i in sorted(right) if i not in fixed]
            operations += [("swap",i,j) for i,j in product(sorted(left),sorted(right)) if i not in fixed and j not in fixed]
            operations += [("day_swap","","")] if not any(i in fixed for i in left+right) else []
            for kind,i,j in operations:
                a,b = ([v for v in left if v != i]+([j] if j else []),[v for v in right if v != j]+([i] if i else [])) if kind != "day_swap" else (right,left)
                proposal = [*allocation[:k],a,b,*allocation[k+2:]]
                if accept(proposal,[k,k+1]):
                    rank = objective(proposal),{"move":0,"swap":1,"day_swap":2}[kind],k,i,j
                    if best is None or rank < best[0]:
                        best = rank,proposal
        if best is None:
            break
        allocation = best[1]
    for k,ids in enumerate(allocation):
        ordered,remaining = [],sorted(i for i in ids if i in points)
        while remaining:
            i = min(remaining,key=lambda i:(distances[ordered[-1],i] if ordered else 0,i))
            ordered.append(i)
            remaining.remove(i)
        note = "初始按已定位景点坐标集中安排，并按游玩时长预留午餐与移动时间；开放信息仍需临行核实。" if ordered else "保留用户固定日期，位置尚未核实，未据此判断远近。" if ids else "当日暂无已定位景点安排，需补齐可用地点。"
        if not ordered:
            note = "按候选地点、用户固定日期和每日可用时间形成初始分日建议。"+note
        soft = max((item["count"] for item in active(req,"daily_min_stops",date=dates[k]) if item["strength"] == "soft"
                    and (not item["scope"]["cities"] or day_cities[k] in item["scope"]["cities"])),default=0)
        note += f"当日安排{len(ids)}个景点，低于已接受不足的参考数量{soft}个。" if len(ids) < soft else ""
        plan["days"].append({"date":dates[k],"city":day_cities[k],"stop_ids":ordered+sorted(set(ids)-points.keys()),"adjustment":"空间分日："+note})
    try:
        candidate = Candidate(title="空间分日方案",reason="按坐标和可用时间形成初始分配",days=plan["days"])
        candidate = validate_candidate(merge_candidate_duplicates(candidate,Requirements(**req),stops,state.get("locks",[])),Requirements(**req),stops)
        plan["days"] = [d.model_dump(mode="json") for d in candidate.days]
    except ValueError as error:
        issues.append(str(error))
    chosen = {i for d in plan["days"] for i in d["stop_ids"]}
    plan["visit_minutes"] = {i:_minutes(stops[i],req,d["date"],durations) for d in plan["days"] for i in d["stop_ids"]}
    plan["reserve_stop_ids"] = sorted(points.keys()-chosen)
    for day in plan["days"]:
        budgets[day["date"]]["load"] = _load(day["stop_ids"],stops,req,day["date"],budgets[day["date"]],durations)
    issues.extend(spatial_errors(plan,{"days":plan["days"]},stops,{**req,"excluded_stop_ids":sorted(excluded)},durations))
    plan["status"] = "partial" if issues else "ready"
    plan["meal_wishes"] = plan_meal_wishes(plan["days"],{**state.get("stops",{}),**stops},req)
    issues.extend(f"{day['date']} {day['city']}："+day["adjustment"].removeprefix("空间分日：")
                  for day in plan["days"] if not day["stop_ids"] or "低于已接受不足的参考数量" in day["adjustment"])
    return plan


def recluster_candidate(state, candidate, stops):
    days = [day.model_dump(mode="json") for day in candidate.days]
    selected = {identity for day in days for identity in day["stop_ids"]}
    all_stops = {**state.get("stops",{}),**stops}
    protected = protected_stops({**state,"stops":all_stops}) | {i for i,s in stops.items() if s.get("assigned_date")}
    selected.update(identity for identity in protected if identity in stops and not is_dining(stops[identity]))
    pool = {identity:stops[identity] for identity in selected if identity in stops}
    estimates = candidate.feasibility.get("durations",{})
    estimates = {identity:value for identity,value in estimates.items() if identity in stops and isinstance(value,int)
                 and not isinstance(value,bool) and 10 <= value <= 720} if isinstance(estimates,dict) else {}
    priorities = candidate.feasibility.get("priorities",{})
    priorities = {identity:value for identity,value in priorities.items() if identity in stops and isinstance(value,str)
                  and value in {"recommended","optional","avoid"}} if isinstance(priorities,dict) else {}
    plan = build_spatial_plan({**state,"stops":all_stops},pool,days=days,durations=estimates,priorities=priorities)
    assigned = {identity for day in plan["days"] for identity in day["stop_ids"]}
    covered = {canonical_key(stops[identity]) for identity in assigned}
    original = {identity:day["date"] for day in days for identity in day["stop_ids"]}
    fixed = fixed_dates({**state,"stops":stops})
    pending = []
    for identity in sorted(selected-assigned):
        stop = stops[identity]
        if canonical_key(stop) in covered and not repeat_allowed(stop,{"date":original.get(identity)},"visit",state["requirements"]):
            continue
        choices = [day for day in days if day["city"] == stop["city"] and (identity not in fixed or day["date"] in fixed[identity])]
        day = next((day["date"] for day in choices if day["date"] == original.get(identity)),None) or next(
            (day["date"] for day in choices),original.get(identity))
        if day:
            pending.append({"id":identity,"date":day})
    return candidate.model_copy(update={"days":[CandidateDay.model_validate(day) for day in plan["days"]],
        "feasibility":{"durations":estimates,"priorities":priorities,"spatial_unplaced":pending,
                       "meal_wishes":plan_meal_wishes(plan["days"],state.get("stops",stops),state["requirements"])}})
