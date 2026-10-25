import copy
import json

from .constraints import trip_dates
from .routing import coordinates, reference_opening


def pick(value, fields):
    return {key:copy.deepcopy(value[key]) for key in fields if key in value}


def opening(stop, day_date):
    value = reference_opening(stop,day_date)
    return {key:item for key,item in pick(value,("status","windows","last_entry_minutes")).items()
            if item is not None and (key != "windows" or value["status"] == "open")}


def stop_summary(state, stop, day_date=None):
    identity = stop.get("id")
    req = state.get("requirements",{})
    value = pick(stop,("id","name","city","category","duration_minutes","priority","source_kind","assigned_date"))
    value.setdefault("source_kind","guide")
    point = coordinates(stop.get("map_place"))
    if point:
        value["coordinates"] = [round(number,5) for number in point]
    required = set(req.get("required_stop_ids",[])) | set(state.get("stop_constraints",{}).get("required",[]))
    for key,enabled in (("required",identity in required),("wish",stop.get("source_kind") == "user"),("locked",identity in state.get("locks",[]))):
        if enabled:
            value[key] = True
    if day_date:
        value["opening"] = opening(stop,day_date)
    else:
        groups = {}
        for day in trip_dates(req):
            result = opening(stop,day)
            key = json.dumps(result,sort_keys=True)
            if key not in groups:
                groups[key] = {"dates":[],**result}
            groups[key]["dates"].append(day)
        ordered = sorted(groups.values(),key=lambda item:-len(item["dates"]))
        if ordered:
            default = {key:item for key,item in ordered[0].items() if key != "dates"}
            value["opening"] = default if len(ordered) == 1 else {"default":default,"exceptions":ordered[1:]}
    return value


def movement(value):
    return pick(value or {},("mode","minutes","estimate_minutes","walking_meters","origin","destination","evidence_id"))


def activity_summary(state, item, day_date=None):
    stop = {**state.get("stops",{}).get(item.get("id"),{}),**item}
    value = stop_summary(state,stop,day_date) if item.get("kind") == "visit" else pick(item,("id","name"))
    value.update(pick(item,("kind","start","end","meal","appointment_time","appointment_time_source","visit_minutes")))
    if item.get("move_before"):
        value["move_before"] = movement(item["move_before"])
    if item.get("meal_break"):
        value["meal_break"] = pick(item["meal_break"],("id","kind","meal","name","start","end"))
    return value


def resource(entry):
    value = pick(entry,("id","label","city","context","selected_by_user","buffer_minutes"))
    selected = entry.get("selected")
    if selected:
        value["selected"] = pick(selected,("id","name","departure","arrival","price","currency","price_unit","evidence_id","constraint_issues"))
        for field in ("departure_transfer","arrival_transfer"):
            if selected.get(field):
                value["selected"][field] = movement(selected[field])
    return value


def plan_summary(state, plan):
    value = pick(plan,("budget","issues","information_unknown"))
    if "budget" in value:
        value["budget"] = pick(value["budget"],("known_subtotal","estimated_meals","estimated_resources","estimated_tickets","estimated_local_transport","known_plus_estimates","limit","status","unknown"))
    value["days"] = []
    for day in plan.get("days",[]):
        row = pick(day,("date","city","available_start","tail_deadline","walking_meters","travel_minutes","gaps"))
        row["activities"] = [activity_summary(state,item,day["date"]) for item in day.get("activities",[])]
        row["meal_recommendations"] = [pick(item,("id","name","meal","wish","source_kind")) for item in day.get("meal_recommendations",[])]
        row["return_move"] = movement(day.get("return_move"))
        value["days"].append(row)
    for group in ("transport","hotels"):
        value[group] = [resource(entry) for entry in plan.get(group,[]) if group != "hotels" or entry.get("selected_by_user")]
    return value


def _clean(value):
    if isinstance(value,list):
        return [_clean(item) for item in value]
    if not isinstance(value,dict):
        return value
    excluded = {"raw","map_candidates","source_references","source_url","url","evidence","text_evidence","opening_claim",
                "opentime2","open_time","opening_schedule","opening_reference","map_place","diagnostic","extraction","images"}
    return {key:_clean(item) for key,item in value.items() if key not in excluded}


def context_summary(payload):
    result = _clean(pick(payload,("user_requirements","user_decisions","system_assumptions","system_adjustments")))
    result["system_assumptions"] = [pick(item,("id","title","dates","stop_ids","current_action","reason"))
                                    for item in result.get("system_assumptions",[])]
    return result


def route_view(state, payload):
    fields = ("requirements","required_dates","trip_days","selected_source_ids","canonical_place_groups","weather","constraints",
              "city","date","day_adjustment","user_request","effective_max_walk_per_day","walking_target_per_day",
              "scheduled_stop_ids","time_window","available_minutes","locked_ids","change","reference_errors",
              "route_errors","previous_candidate","validation_error","reference_correction",
              "current_stop_ids","meal_wishes","remaining_tool_rounds","request","editable_dates","selected_stop_ids","previous_patch")
    value = _clean(pick(payload,fields))
    value.update(context_summary(payload))
    if payload.get("current_route"):
        value["current_route"] = pick(payload["current_route"],("id","title","reason","days","gaps"))
        value["current_plan"] = plan_summary(state,payload["current_route"].get("draft",{}))
    value["available_stops"] = {identity:{key:item for key,item in stop_summary(state,stop,payload.get("date")).items() if key != "id"}
                               for identity,stop in payload.get("available_stops",{}).items()}
    for field in ("current_activities","preserved_activities"):
        if field in payload:
            value[field] = [activity_summary(state,item,payload.get("date")) for item in payload[field]]
    if payload.get("spatial_plan"):
        value["spatial_plan"] = _clean(pick(payload["spatial_plan"],("status","days","day_budgets","fixed_dates","required_ids","unlocated_stop_ids","issues")))
    for field in ("guide_brief","guide_suggestions"):
        if payload.get(field):
            value[field] = _clean(pick(payload[field],("summary","groups","priorities","gaps","suggestions")))
    value["guides"] = [pick(item,("id","title")) for item in payload.get("guides",[])]
    value["route_evidence"] = [{**pick(edge,("origin_id","destination_id")),"movement":movement(edge.get("movement"))}
                               for edge in payload.get("route_evidence",[])]
    value["tool_observations"] = tool_observations(state,payload.get("tool_observations",[]))
    return value


def tool_observations(state, observations):
    values = []
    for item in observations:
        row = pick(item,("evidence_id","status","request"))
        result = item.get("result")
        if isinstance(result,dict):
            row["result"] = _clean(pick(result,("code","message","query_status","map_issue","mode","minutes","estimate_minutes",
                                                "walking_meters","origin","destination","available","forecasts","summary","tool_scope",
                                                "requested_mode","transit_queried","stop_ids","reason","gaps")))
            if result.get("walking_alternative"):
                row["result"]["walking_alternative"] = movement(result["walking_alternative"])
            if result.get("map_place"):
                row["result"]["place"] = pick(result["map_place"],("id","name","address","location"))
        else:
            row["result"] = _clean(result)
        values.append(row)
    return values


def coordinator_view(state, payload):
    fields = ("days","budget","unplaced_required_stop_ids","unplaced_wish_places","selected_source_ids","available_sources",
              "place_selection","hub_candidates","unmet","coordinator_messages","questions",
              "relevant_stops","batch","answers","note","situation")
    result = _clean(pick(payload,fields))
    result.update(context_summary(payload))
    result["coordinator_messages"] = [pick(item,("text","message","decision_index")) for item in payload.get("coordinator_messages",[])[-3:]]
    for item in result.get("unmet",[]):
        item["detail"] = item.get("detail","")[:300]
    for field in ("place_selection","hub_candidates"):
        if payload.get(field):
            result[field] = identity_candidates(payload[field])
    return result


def identity_candidates(value):
    if isinstance(value,list):
        return [identity_candidates(item) for item in value[:3]]
    if not isinstance(value,dict):
        return value
    result = pick(value,("id","stop_id","resource_id","name","address","city","field","role"))
    for key in ("candidates","map_candidates","places","items"):
        if key in value:
            result["candidates" if key == "map_candidates" else key] = identity_candidates(value[key])
    return result


def reviewer_view(state, payload):
    fields = ("requirements","constraints","locks","decisions","occurrences","change_scope","policy_version","review_mode",
              "review_dates","review_dimensions","budget_items","unchanged_days","review_scope","field_path_index")
    result = _clean(pick(payload,fields))
    result["plan"] = plan_summary(state,payload.get("plan",{}))
    result["previous_plan"] = plan_summary(state,payload.get("previous_plan",{}))
    for key in ("plan","previous_plan"):
        originals = {day["date"]:day for day in payload.get(key,{}).get("days",[])}
        semantic_days = []
        for day in result[key].get("days",[]):
            row = pick(day,("date","city","meal_recommendations"))
            row["weather"] = _clean(originals[day["date"]].get("weather",{}))
            row["activities"] = [pick(item,("id","name","kind","meal","start","end","category","source_kind")) for item in day["activities"]]
            semantic_days.append(row)
        result[key] = {"days":semantic_days}
    result["guide_summary"] = _clean(pick(state.get("guide_brief",{}),("summary","groups","priorities")))
    current = state.get("itinerary") or payload.get("plan",{})
    result["current_places"] = [{"id":item["id"],"name":item.get("name"),"city":day.get("city"),"date":day["date"],
        "alias_names":list(item.get("alias_names") or [])[:8],
        "map_place":pick(item.get("map_place") or {},("id","name"))}
        for day in current.get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"]
    result.update(context_summary(payload))
    result["evidence_index"] = {identity:_clean(pick(item,("id","title","provider","created_at","status","code","message","kind","quote")))
                                if isinstance(item,dict) else _clean(item)
                                for identity,item in payload.get("evidence_index",{}).items()}
    result["evidence_catalog"] = [{"evidence_id":identity} for identity in result["evidence_index"]]
    return result


def model_view(role, state, payload):
    if role == "行程 Agent":
        return route_view(state,payload)
    if role == "主理人 Agent":
        return coordinator_view(state,payload)
    if role == "检验 Agent":
        return reviewer_view(state,payload)
    return payload


def fit_payload(payload, budget):
    result = copy.deepcopy(payload)
    size = lambda: len(json.dumps(result,ensure_ascii=False,separators=(",",":"),default=str))
    for field in ("coordinator_messages","system_adjustments","guides"):
        if size() <= budget:
            return result
        result.pop(field,None)
    for count in (6,4,2,0):
        if size() <= budget:
            return result
        for day in result.get("days",[]):
            if "unused_guide_stops" in day:
                day["unused_guide_stops"] = day["unused_guide_stops"][:count]
    for count in (8,4,2):
        if size() <= budget:
            return result
        if "tool_observations" in result:
            result["tool_observations"] = result["tool_observations"][-count:]
    if size() <= budget:
        return result
    for item in result.get("tool_observations",[]):
        item.pop("request",None)
        value = item.get("result")
        if isinstance(value,dict):
            item["result"] = pick(value,("code","query_status","mode","minutes","estimate_minutes","walking_meters",
                                         "origin","destination","available","stop_ids","place","walking_alternative"))
            for field in ("summary","message","reason"):
                if isinstance(value.get(field),str):
                    item["result"][field] = value[field][:200]
        elif isinstance(value,str):
            item["result"] = value[:200]
    for field in ("guide_brief","guide_suggestions","spatial_plan"):
        if size() <= budget:
            return result
        if field in result:
            result[field] = _grouping_ids(result[field])
    stops = result.get("available_stops",{})
    for status_only in (False,True):
        if size() <= budget:
            return result
        for stop in stops.values():
            value = stop.get("opening")
            if isinstance(value,dict):
                default = value.get("default",value)
                stop["opening"] = pick(default,("status",) if status_only else ("status","windows","last_entry_minutes"))
            if isinstance(stop.get("coordinates"),list):
                stop["coordinates"] = [round(number,3) for number in stop["coordinates"]]
            for field in ("category","priority") if status_only else ("category",):
                stop.pop(field,None)
            if stop.get("duration_minutes") is None:
                stop.pop("duration_minutes",None)
    if not stops:
        return result
    protected,grouped,recommended = _stop_priorities(payload)
    ordered = sorted(stops,key=lambda identity:(identity in grouped,identity in recommended))
    for identity in ordered:
        if size() <= budget:
            return result
        if identity in protected:
            continue
        del stops[identity]
        for field in ("guide_brief","guide_suggestions","spatial_plan","canonical_place_groups"):
            if field in result:
                result[field] = _without_stop(result[field],identity)
    return result


def _grouping_ids(value):
    if isinstance(value,list):
        return [_grouping_ids(item) for item in value if isinstance(item,(dict,list))]
    if not isinstance(value,dict):
        return {}
    fields = ("id","stop_id","stop_ids","city","date","dates","status","required_ids","excluded_ids",
              "fixed_dates","unlocated_stop_ids","priorities","day_budgets")
    result = pick(value,fields)
    for field in ("groups","suggestions","days"):
        if field in value:
            result[field] = _grouping_ids(value[field])
    return result


def _stop_priorities(payload):
    protected = set()
    for field in ("requirements","user_requirements"):
        protected.update(payload.get(field,{}).get("required_stop_ids",[]))
    protected.update(payload.get("constraints",{}).get("required",[]))
    for field in ("locked_ids","current_stop_ids","scheduled_stop_ids","selected_stop_ids"):
        protected.update(payload.get(field,[]))
    spatial = payload.get("spatial_plan",{})
    protected.update(spatial.get("required_ids",[]))
    protected.update(spatial.get("fixed_dates",{}))
    grouped = {identity for day in spatial.get("days",[]) for identity in day.get("stop_ids",[])}
    recommended = set()
    for field in ("guide_brief","guide_suggestions"):
        guide = payload.get(field,{})
        if isinstance(guide,dict):
            recommended.update(identity for group in guide.get("groups",[]) for identity in group.get("stop_ids",[]))
    for identity,stop in payload.get("available_stops",{}).items():
        if any(stop.get(field) for field in ("required","wish","locked","assigned_date")) or stop.get("source_kind") == "user":
            protected.add(identity)
        if stop.get("source_kind") == "guide" or stop.get("priority") == "recommended":
            recommended.add(identity)
    for identities in payload.get("canonical_place_groups",{}).values():
        if protected.intersection(identities):
            protected.update(identities)
    return protected,grouped,recommended


def _without_stop(value, identity):
    if isinstance(value,list):
        return [_without_stop(item,identity) for item in value
                if item != identity and not (isinstance(item,dict) and item.get("stop_id",item.get("id")) == identity)]
    if isinstance(value,dict):
        return {key:_without_stop(item,identity) for key,item in value.items() if key != identity}
    return value
