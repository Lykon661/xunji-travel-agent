"""动作执行及排程容量估算。"""
from datetime import date as Date
import re

from .constraints import active, merge, trip_dates
from .models import WISH_PREFIX, ConstraintDraft, wish_stop_id
from .dining import is_dining
from .stop_types import is_light_wish
from .places import canonicalize_stops
from .routing import scoped_day, daily_route_scope
from .rules import timestamp

DEFAULT_VISIT_MINUTES = 90
DEFAULT_MOVE_MINUTES = 30
LUNCH_MINUTES = 60
# A guide place of at least this many minutes, or described as taking the whole day, fills its day alone.
FULL_DAY_MINUTES = 360
FULL_DAY = re.compile(r"一整天|整整一天|全天|整天|一天时间")


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


def visit_minutes(stop: dict, req: dict, day: str | None = None,
                  scheduled_minutes: int | None = None) -> int:
    fixed = next((item["minutes"] for item in active(req,"visit_duration",date=day,stop_id=stop.get("id"))
                  if item.get("minutes")),None)
    if is_light_wish(stop):
        explicit = stop.get("duration_minutes") if not stop.get("duration_is_default") else None
        return fixed or explicit or scheduled_minutes or stop.get("duration_minutes") or 45
    return fixed or scheduled_minutes or stop.get("duration_minutes") or DEFAULT_VISIT_MINUTES


def day_demand(state, itinerary, plan_day, route_day, unplaced=True):
    """Each intended stop of a day with its estimated visit+move minutes and why it is mandatory.

    unplaced adds required stops that no day holds yet; it is off when measuring another day's spare time.
    """
    plan_day = scoped_day(itinerary,plan_day)
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
        activity = scheduled.get(identity)
        visit = visit_minutes({**stop,"id":identity},state.get("requirements",{}),day,activity and _visit_minutes(activity,day))
        move = ((activity or {}).get("move_before") or {}).get("minutes")
        if not items and not daily_route_scope(itinerary,plan_day)["first_leg_required"]:
            move = 0
        items.append({"id":identity,"name":stop.get("name") or identity,"visit":visit,
                      "move":move if move is not None else typical,"reasons":_mandatory(state,identity,day)})
    return items


def _minimum(req, day, city):
    """The strictest hard "at least N stops a day" for this day and city; 0 when there is none.

    A soft minimum is one the user already accepted falling short of, so it never raises a question again.
    """
    return max((item["count"] for item in active(req,"daily_min_stops",date=day) if item["strength"] == "hard"
                and (not item["scope"]["cities"] or not city or city in item["scope"]["cities"])),default=0)


def _full_day(stop):
    """The guide says this place takes the whole day: a long reference duration, or "一整天" in the clause that names it."""
    if (stop.get("duration_minutes") or 0) >= FULL_DAY_MINUTES:
        return True
    quote = str((stop.get("evidence") or {}).get("quote") or "")
    return any(stop.get("name") and stop["name"] in clause and FULL_DAY.search(clause) for clause in re.split(r"[。；;，,\n]",quote))


def _carve(req, dates, label):
    """Revoke each daily minimum on these dates while merge preserves its remaining dates."""
    drafts = []
    for item in active(req,"daily_min_stops"):
        covered = set(item["scope"]["dates"] or trip_dates(req))
        if not covered & set(dates):
            continue
        fields = {key:item[key] for key in ConstraintDraft.model_fields if key in item}
        drafts.append(ConstraintDraft(**{**fields,"scope":{**item["scope"],"dates":sorted(covered & set(dates))},"revoke":True,"quote":label}).model_dump(mode="json"))
    return merge(req.get("constraints",[]),drafts,"decision",trip_dates=trip_dates(req))


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
    if kind == "skip":
        if identity not in stops:
            raise ValueError("Unknown stop: " + str(identity))
        if identity in set(req.get("required_stop_ids",[])) | set(constraints.get("required",[])) | set(state.get("locks",[])) or stops[identity].get("source_kind") == "user":
            raise ValueError("Cannot skip a protected stop: " + identity)
        if action["date"] in days:
            days[action["date"]]["stop_ids"] = [item for item in days[action["date"]]["stop_ids"] if item != identity]
        summary = "本次未排入"+name
    elif kind == "default_duration":
        if identity not in stops or not isinstance(action.get("minutes"),int) or not 10 <= action["minutes"] <= 720:
            raise ValueError("Invalid system duration: " + str(identity))
        overrides = {day:dict(values) for day,values in state.get("system_durations",{}).items()}
        overrides.setdefault(action["date"],{})[identity] = action["minutes"]
        updates["system_durations"] = overrides
        summary = "系统参考游玩时长调整为"+str(action["minutes"])+"分钟："+name
    elif kind == "exclude":
        constraints["required"] = [item for item in constraints.get("required",[]) if item != identity]
        if identity.startswith(WISH_PREFIX):
            req["wish_places"] = [place for place in req.get("wish_places",[]) if wish_stop_id(place["city"],place["name"]) != identity]
            constraints["excluded"] = [item for item in constraints.get("excluded",[]) if item != identity]
            stops.pop(identity,None)
        else:
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
            if identity.startswith(WISH_PREFIX):
                req["wish_places"] = [{**place,"date":action["to"]} if wish_stop_id(place["city"],place["name"]) == identity else place
                                      for place in req.get("wish_places",[])]
        dates.append(action["to"])
        summary = "按你的选择把"+name+"改到"+_label_date(action["to"])
    elif kind == "shorten":
        duration = ConstraintDraft(kind="visit_duration",scope={"dates":[action["date"]],"stop_ids":[identity]},
                                   minutes=action["minutes"],quote=label).model_dump(mode="json")
        req["constraints"] = merge(req.get("constraints",[]),[duration],"decision",trip_dates=trip_dates(req))
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
        req["constraints"] = merge(req.get("constraints",[]),drafts,"decision",trip_dates=trip_dates(req))
        summary = ("按你的选择，改为每天至少"+str(action["count"])+"个景点" if kind == "lower_count"
                   else "按你的选择，用现有攻略安排")+"；景点不够的日子只在方案里提示"
    elif kind == "keep_short":
        # Accepting fewer stops lifts the daily minimum on exactly those dates; other days keep it.
        req["constraints"] = _carve(req,dates,label)
        keep = kind == "keep_short"
        summary = "按你的选择，"+"、".join(map(_label_date,dates))+"不再补充景点"
    else:
        raise ValueError("Unknown action type: " + str(kind))
    if kind not in {"skip","default_duration"}:
        req["required_stop_ids"],req["excluded_stop_ids"] = sorted(constraints.get("required",[])),sorted(constraints.get("excluded",[]))
    updates.update(requirements=req,stop_constraints=constraints,candidates=candidates,stops=stops,keep_current_plan=keep)
    return updates,sorted(set(dates)),summary
