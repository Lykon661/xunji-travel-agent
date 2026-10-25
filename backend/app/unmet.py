import copy
import hashlib
import json

from .decisions import confirmation_issues, issue_record
from .models import UnmetItem
from .rules import check_itinerary
from .planning_repairs import identity_stop_ids
from .places import canonical_key, represented_ids


KINDS = {
    "stop_unplaced":"stop_unplaced", "dining_wish":"stop_unplaced", "pool_shortage":"pool_shortage",
    "over_budget":"budget", "walking_limit":"walking", "lunch_missing":"lunch", "place_identity":"place_identity",
    "hub_identity":"hub_identity", "opening_closed":"opening", "opening_conflict":"opening", "capacity":"capacity",
    "invalid_time":"time_conflict", "overlap":"time_conflict", "move_gap":"transfer", "arrival_conflict":"transfer",
    "departure_conflict":"transfer", "tail_conflict":"transfer", "day_end_conflict":"time_conflict", "late_day":"capacity",
    "transport_time":"transfer", "transfer_unverified":"transfer", "transport_unverified":"fact_unverified",
    "evidence_missing":"fact_unverified", "resource_missing":"fact_unverified", "fact_unverified":"fact_unverified",
    "ticket_match":"fact_unverified", "schedule_fill":"capacity", "duration":"capacity", "start_end_location":"transfer",
    "closed_day":"opening", "opening_hours":"opening", "opening_window":"opening", "appointment_conflict":"time_conflict",
    "appointment_time":"time_conflict",
    "local_transport_conflict":"transfer", "hotel_dates":"time_conflict", "hotel_gap":"fact_unverified",
}
CONSTRAINT_KINDS = {"fewer_walking":"walking", "fixed_order":"time_conflict", "time_anchor":"time_conflict",
                    "visit_duration":"capacity", "transfer_buffer":"transfer", "booking_prerequisite":"fact_unverified",
                    "daily_min_stops":"pool_shortage", "transit_rule":"transfer"}


def fingerprint(unmet):
    item = UnmetItem.model_validate(unmet).model_dump(mode="json")
    refs = sorted(set((ref["type"], ref.get("id") or ref.get("field")) for ref in item["refs"]))
    value = [item["kind"], refs, sorted(set(item["dates"]))]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _pointer(plan, path):
    value, date = plan, None
    try:
        for token in path.strip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
            if isinstance(value, dict) and value.get("date"):
                date = value["date"]
        return value if isinstance(value, dict) else {}, date
    except (KeyError, IndexError, TypeError, ValueError):
        return {}, date


def valid_refs(state, plan, refs):
    req = state.get("requirements",{})
    allowed = {
        "requirement":set(req),
        "constraint":{item["id"] for item in req.get("constraints",[]) if item.get("id") and not item.get("revoke") and not item.get("superseded_by")},
        "stop":set(state.get("stops",{})) | {item["id"] for day in plan.get("days",[]) for item in [*day.get("activities",[]),*day.get("meal_recommendations",[])] if item.get("id")},
        "day":{day["date"] for day in plan.get("days",[])},
        "resource":{item["id"] for group in ("transport","hotels") for item in plan.get(group,[]) if item.get("id")}}
    return all((ref.get("id") or ref.get("field")) in allowed.get(ref.get("type"),set()) for ref in refs)


def collect(state, itinerary, review=None):
    from pydantic import ValidationError
    from .reviewing import canonical_review_path, pointer_exists
    plan = copy.deepcopy(itinerary)
    req, stops = state["requirements"],state.get("stops",{})
    checked = check_itinerary(plan,req,stops)
    plan["budget"] = checked["budget"]
    marks = {"required":sorted(set(req.get("required_stop_ids",[])) | set(state.get("stop_constraints",{}).get("required",[]))),
             "excluded":sorted(set(req.get("excluded_stop_ids",[])) | set(state.get("stop_constraints",{}).get("excluded",[])))}
    hard = confirmation_issues(plan,marks,req,stops,state)
    raw = [{**issue,"blocking":False} for issue in [*checked["issues"],*plan.get("issues",[])]]
    report = review.model_dump(mode="json") if hasattr(review,"model_dump") else review or {}
    dates = {day["date"] for day in plan.get("days",[])}
    scheduled = [(day,entry) for day in plan.get("days",[]) for entry in [*day.get("activities",[]),*day.get("meal_recommendations",[])]
                 if entry.get("kind") == "visit" or entry.get("meal") in {"breakfast","lunch","dinner"}]
    covered = represented_ids([entry for _,entry in scheduled])
    covered_keys = {canonical_key(entry,day.get("city","")) for day,entry in scheduled}
    ids = set(stops) | set(req) | dates | {item["id"] for item in req.get("constraints",[]) if item.get("id") and not item.get("revoke") and not item.get("superseded_by")} | {entry["id"] for group in ("transport","hotels") for entry in plan.get(group,[])}
    for finding in report.get("findings",[]):
        path = canonical_review_path(plan,finding.get("field_path",""))
        if not path or not pointer_exists(plan,path) or finding.get("target") and finding["target"] not in ids:
            continue
        if not set(finding.get("dates",[])) <= dates:
            continue
        obj,day_date = _pointer(plan,path)
        raw.append({**finding,"target":obj.get("id") or obj.get("date") or finding.get("target",""),
                    "dates":finding.get("dates") or ([day_date] if day_date else []),"blocking":False})
    items = {}
    def add(item):
        refs = sorted((ref.type,ref.id or ref.field) for ref in item.refs)
        key = fingerprint(item) if item.blocking else hashlib.sha256(json.dumps([refs,sorted(map(str,item.dates))],ensure_ascii=False).encode()).hexdigest()
        if key in items:
            previous = items[key]
            previous.blocking |= item.blocking
            previous.evidence_ids = sorted(set(previous.evidence_ids+item.evidence_ids))[:40]
            if item.detail and item.detail not in previous.detail:
                previous.detail = (previous.detail+"；"+item.detail)[:2000]
        else:
            items[key] = item
    for entry in [*state.get("agent_unmet",[]),*state.get("unmet",[]),*report.get("unmet",[])]:
        try:
            item = UnmetItem.model_validate(entry).model_copy(deep=True)
        except (ValidationError,ValueError,TypeError):
            continue
        if not valid_refs(state,plan,[ref.model_dump() for ref in item.refs]) or not set(map(str,item.dates)) <= dates:
            continue
        item.blocking = False
        if item.kind == "stop_unplaced":
            stop_refs = [ref for ref in item.refs if ref.type == "stop"]
            missing = [ref for ref in stop_refs if ref.id not in covered and
                       (ref.id not in stops or canonical_key(stops[ref.id]) not in covered_keys)]
            if stop_refs and not missing:
                continue
            item.refs = [ref for ref in item.refs if ref.type != "stop"]+missing
            item.dates = []
        add(item)
    for issue in [*raw,*hard]:
        record = issue_record(plan,issue,req,stops)
        if not valid_refs(state,plan,record["refs"]):
            continue
        kind = KINDS.get(record["code"],"other")
        refs = record["refs"] or [{"type":"requirement","id":"constraints"}]
        for start in range(0,len(refs),24):
            add(UnmetItem(kind=kind,refs=refs[start:start+24],dates=record["dates"][:30],numbers=record["numbers"],
                          detail=str(record["message"])[:2000],evidence_ids=issue.get("evidence_ids",[])[:40],blocking=record["blocking"]))
    relevant = identity_stop_ids({**state,"itinerary":plan})
    return [item for item in items.values() if item.kind != "place_identity" or
            any(ref.type == "stop" and ref.id in relevant for ref in item.refs)]
