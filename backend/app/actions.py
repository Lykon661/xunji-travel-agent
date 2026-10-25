import copy
import math
import re

from . import authority
from .conflicts import _window, apply_option, day_demand, visit_minutes
from .constraints import active as active_constraints, merge, trip_dates
from .models import ConstraintDraft, DecisionOption, PlanAction, UnmetItem, WISH_PREFIX, wish_stop_id
from .places import canonical_key, represented_ids
from .rules import money, ticket_policy, walking_limit
from .routing import daily_route_scope, scoped_day


class AuthorityError(ValueError):
    def __init__(self, violations):
        self.violations = copy.deepcopy(violations)
        self.paths = [item["path"] for item in violations]
        super().__init__("Unauthorized user state changes: " + ", ".join(self.paths))


def _actions(actions):
    if not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 24:
        raise ValueError("Expected 1–24 actions")
    return [PlanAction.model_validate(item).model_dump(mode="json", exclude_unset=True, exclude_none=True) for item in actions]


def _plan(state):
    if isinstance(state.get("itinerary"), dict):
        return state["itinerary"]
    return state.get("selected_route", {}).get("draft") or state.get("old_itinerary") or {}


def _route_days(state):
    for route in [*(state.get("candidates") or [])[:1], state.get("selected_route") or {}]:
        if route.get("days"):
            return route["days"]
    return [{**day, "stop_ids":[item["id"] for item in day.get("activities", []) if item.get("kind") == "visit"]}
            for day in _plan(state).get("days", [])]


def _sources(state):
    sources = state.get("available_sources") or state.get("sources") or []
    return sources if isinstance(sources, dict) else {item["id"]:item for item in sources}


def _source_stops(state, action):
    source = _sources(state)[action["source_id"]]
    stops = (source.get("extraction") or {}).get("stops", source.get("stops", []))
    stops = stops.values() if isinstance(stops, dict) else stops
    return {item["id"]:{**copy.deepcopy(item), "source_id":source["id"], "source_title":source.get("title", "攻略"),
                         "source_url":source.get("url")} for item in stops
            if not action.get("stop_ids") or item["id"] in action["stop_ids"]}


def _protected(state, identity, date=None):
    req = state.get("requirements", {})
    if identity in set(state.get("locks", [])) | set(req.get("required_stop_ids", [])) | set(state.get("stop_constraints", {}).get("required", [])):
        return True
    stop = state.get("stops", {}).get(identity, {})
    if stop.get("source_kind") == "user" or stop.get("product") or identity.startswith(WISH_PREFIX):
        return True
    if stop.get("assigned_date") and stop.get("assigned_date_source", "legacy_unknown") in authority.USER_SOURCES:
        return True
    if any(item.get("source", "legacy_unknown") in authority.USER_SOURCES
           for kind in ("visit_duration", "time_anchor", "booking_prerequisite")
           for item in active_constraints(req, kind, date=date, stop_id=identity)):
        return True
    return any(item.get("id") == identity and item.get("appointment_time") and
               item.get("appointment_time_source", "legacy_unknown") in authority.USER_SOURCES
               for day in _plan(state).get("days", []) for item in day.get("activities", []))


def _hub_options(state, action):
    selection = state.get("place_selection") or {}
    if selection.get("kind") == "hub" and (selection.get("city"), selection.get("name")) == (action["city"], action["name"]):
        return set(selection.get("options", {}).values())
    rows = state.get("hub_candidates", [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    return {item["id"] for item in rows if item.get("city") == action["city"] and item.get("hub_name", item.get("name")) == action["name"]}


def optional_stop_issue(state, identity, date, route_days=None):
    from .places import represented_stop_ids
    if identity in represented_stop_ids(state):
        return "Stop is already represented by another planned place"
    stops = state.get("stops",{})
    if identity not in stops:
        return "Unknown stop: "+str(identity)
    days = {day["date"]:day for day in (_route_days(state) if route_days is None else route_days)}
    if date not in days:
        return "Day has no route: "+str(date)
    stop,day = stops[identity],days[date]
    if stop.get("city") != day.get("city"):
        return "Stop city differs from route day"
    if len(day.get("stop_ids",[])) >= 12:
        return "Route day already contains 12 stops"
    excluded = set(state.get("requirements",{}).get("excluded_stop_ids",[])) | set(state.get("stop_constraints",{}).get("excluded",[]))
    if represented_ids([stop]) & excluded:
        return "Stop is excluded"
    key = canonical_key(stop)
    if any(key == canonical_key(stops[item]) for row in days.values() for item in row.get("stop_ids",[]) if item in stops):
        return "Stop or its canonical alias is already in the route"
    if stop.get("assigned_date") not in {None,date}:
        return "Stop is assigned to another date"
    selected = set(state.get("selected_source_ids",[]))
    if (stop.get("source_id") not in selected and stop.get("source_kind") != "user"
            and not any(ref.get("source_id") in selected for ref in stop.get("source_references",[]))):
        return "Stop is outside the existing selected pool"
    return None


def _validate(state, action):
    from .places import represented_stop_ids
    req, stops = state.get("requirements", {}), state.get("stops", {})
    kind, identity = action["type"], action.get("stop_id")
    if kind == "move" and identity in represented_stop_ids(state):
        raise ValueError("Stop is already represented by another planned place")
    dates = set(trip_dates(req)) or {day["date"] for day in _route_days(state)}
    for date in [*action.get("dates", []), *[action[key] for key in ("date", "to") if action.get(key)]]:
        if date not in dates:
            raise ValueError("Unknown day: " + date)
    if identity and identity not in stops:
        raise ValueError("Unknown stop: " + identity)
    days = {day["date"]:day for day in _route_days(state)}
    if kind in {"skip", "exclude", "move", "shorten", "default_duration", "reorder_day", "replace_optional_stop", "keep_short"}:
        for date in action.get("dates") or ([action["date"]] if action.get("date") else []):
            if date not in days:
                raise ValueError("Day has no route: " + date)
    if kind in {"skip", "move", "reorder_day", "replace_optional_stop"}:
        day = days[action["date"]]
        if identity and identity not in day.get("stop_ids", []):
            raise ValueError("Stop is not on the specified day: " + identity)
    excluded = set(req.get("excluded_stop_ids",[])) | set(state.get("stop_constraints",{}).get("excluded",[]))
    excluded_keys = {canonical_key(stops[item]) for item in excluded if item in stops}
    excluded_skip = kind == "skip" and (identity in excluded or identity in stops and canonical_key(stops[identity]) in excluded_keys)
    if kind in {"skip", "replace_optional_stop"} and _protected(state, identity, action["date"]) and not excluded_skip:
        raise ValueError("Cannot remove a protected stop: " + identity)
    if kind == "shorten" and action["minutes"] > visit_minutes(stops[identity], req, action["date"]):
        raise ValueError("shorten cannot increase visit duration")
    if kind == "move":
        if action["to"] not in days or action["date"] == action["to"] or days[action["to"]].get("city") != stops[identity].get("city"):
            raise ValueError("Move requires a different existing day in the stop's city")
    if kind == "set_time_window" and action.get("resource_id"):
        entry = next((item for group in ("transport","hotels") for item in _plan(state).get(group,[]) if item["id"] == action["resource_id"]),None)
        if entry is None or not action.get("unlock") and action.get("value") not in {item.get("id") for item in entry.get("options",[])}:
            raise ValueError("Unknown current resource or resource option")
    if kind == "add_source":
        if action["source_id"] not in _sources(state):
            raise ValueError("Unknown source: " + action["source_id"])
        selected = _source_stops(state, action)
        if not selected or set(action.get("stop_ids", [])) - selected.keys():
            raise ValueError("Unknown or empty source stop selection")
        if any(item.get("city") not in req.get("cities", []) for item in selected.values()):
            raise ValueError("Source stops are outside the trip cities")
        if action.get("date") and (action["date"] not in days or any(item.get("city") != days[action["date"]].get("city") for item in selected.values())):
            raise ValueError("Source stops do not belong to the selected route day")
    if kind == "set_hotel_area" and action["city"] not in req.get("cities", []):
        raise ValueError("Unknown city: " + action["city"])
    if kind == "select_poi" and action["poi_id"] not in {item.get("id") for item in stops[identity].get("map_candidates", [])}:
        raise ValueError("Unknown current POI candidate: " + action["poi_id"])
    if kind == "select_hub":
        if action["city"] not in {req.get("origin"), *req.get("cities", [])} or action["poi_id"] not in _hub_options(state, action):
            raise ValueError("Unknown current hub candidate: " + action["poi_id"])
        if action.get("resource_id") and action["resource_id"] not in {item["id"] for item in _plan(state).get("transport", [])}:
            raise ValueError("Unknown transport resource: " + action["resource_id"])
        if action.get("resource_id"):
            entry = next(item for item in _plan(state)["transport"] if item["id"] == action["resource_id"])
            selected, context = entry.get("selected") or {}, entry.get("context") or {}
            if (action["city"], action["name"]) not in {(context.get("origin"), selected.get("departure_place")),
                                                       (context.get("destination"), selected.get("arrival_place"))}:
                raise ValueError("Hub is not an endpoint of this transport resource")
    if kind == "reorder_day":
        ids = action["stop_ids"]
        original = days[action["date"]].get("stop_ids", [])
        if set(ids) != set(original) or set(ids) - stops.keys():
            raise ValueError("Reordering must preserve exactly the current stops")
        if any(ids.index(item) != original.index(item) for item in set(original) & set(state.get("locks", []))):
            raise ValueError("Cannot reorder a locked stop")
        for item in active_constraints(req, "fixed_order", date=action["date"]):
            if item["before"] in ids and item["after"] in ids and ids.index(item["before"]) > ids.index(item["after"]):
                raise ValueError("Reordering violates a fixed user order")
        anchors = {item["scope"]["stop_ids"][0]:item["at"] for item in active_constraints(req, "time_anchor", date=action["date"]) if item.get("at")}
        times = [anchors[item] for item in ids if item in anchors]
        if times != sorted(times):
            raise ValueError("Reordering violates user appointment order")
    if kind == "replace_optional_stop":
        replacement = action["replacement_stop_id"]
        if replacement not in stops:
            raise ValueError("Unknown replacement stop: " + replacement)
        candidate = stops[replacement]
        used = [stops[item] for day in _route_days(state) for item in day.get("stop_ids", []) if item in stops]
        excluded = set(req.get("excluded_stop_ids", [])) | set(state.get("stop_constraints", {}).get("excluded", []))
        if (replacement == identity or candidate.get("city") != days[action["date"]].get("city")
                or candidate.get("source_kind") in {"user", "flyai"} or candidate.get("product")
                or candidate.get("source_id") not in state.get("selected_source_ids", [])
                or represented_ids([candidate]) & excluded
                or any(canonical_key(candidate) == canonical_key(item) for item in used)):
            raise ValueError("Replacement must be an unused, allowed guide stop in the same city")
    if kind in {"add_optional_stop", "retain_free_time"} and action["date"] not in days:
        raise ValueError("Day has no route: " + action["date"])
    if kind == "add_optional_stop":
        if issue := optional_stop_issue(state,identity,action["date"]):
            raise ValueError("Added stop must be an unused, allowed stop in the existing selected pool: "+issue)
    if kind == "omit_unselected_hotel_legs":
        for date in action["dates"]:
            if date not in days or daily_route_scope(_plan(state), days[date])["hotel_location_confirmed"]:
                raise ValueError("Cannot omit legs of a user-selected hotel")


def _scope(action):
    return action.get("dates") or ([action["date"]] if action.get("date") else [])


def _draft(action):
    kind = action["type"]
    scope = {"dates":_scope(action)}
    fields = {"quote":action.get("quote") or kind}
    if kind == "set_walk_limit":
        fields.update(kind="fewer_walking", meters=action["meters"])
    elif kind == "shorten":
        fields.update(kind="visit_duration", minutes=action["minutes"])
        scope["stop_ids"] = [action["stop_id"]]
    else:
        fields.update(kind="time_anchor", anchor="appointment", at=action["at"])
        scope["stop_ids"] = [action["stop_id"]]
    return ConstraintDraft(scope=scope, **fields).model_dump(mode="json")


def _edit_routes(state, action):
    kind, identity, date = action["type"], action.get("stop_id"), action.get("date")
    routes = [*state.get("candidates", []), state.get("selected_route") or {}]
    for route in routes:
        for day in route.get("days", []):
            ids = day.get("stop_ids", [])
            if kind in {"skip", "exclude", "move"} and (kind == "exclude" or day["date"] == date):
                day["stop_ids"] = [item for item in ids if item != identity]
            if kind == "move" and day["date"] == action["to"] and identity not in day["stop_ids"]:
                day["stop_ids"].append(identity)
            if kind == "reorder_day" and day["date"] == date:
                day["stop_ids"] = list(action["stop_ids"])
            if kind == "replace_optional_stop" and day["date"] == date:
                day["stop_ids"] = [action["replacement_stop_id"] if item == identity else item for item in ids]
    plan = _plan(state)
    moved = None
    for day in plan.get("days", []):
        items = day.get("activities", [])
        if kind in {"skip", "exclude", "move", "replace_optional_stop"} and (kind == "exclude" or day["date"] == date):
            moved = next((item for item in items if item.get("id") == identity), moved)
            day["activities"] = [item for item in items if item.get("id") != identity]
        if kind == "reorder_day" and day["date"] == date:
            visits = {item["id"]:item for item in items if item.get("kind") == "visit"}
            ordered = iter(visits[item] for item in action["stop_ids"] if item in visits)
            day["activities"] = [next(ordered) if item.get("kind") == "visit" else item for item in items]
    if kind == "move" and moved:
        for day in plan.get("days", []):
            if day["date"] == action["to"]:
                day.setdefault("activities", []).append(moved)


def _intent(state, action, actor="user"):
    kind, identity = action["type"], action.get("stop_id")
    req = state.setdefault("requirements", {})
    if kind == "exclude":
        marks = state.setdefault("stop_constraints", {})
        required = set(req.get("required_stop_ids", [])) | set(marks.get("required", []))
        excluded = set(req.get("excluded_stop_ids", [])) | set(marks.get("excluded", []))
        req["required_stop_ids"] = sorted(required - {identity})
        req["excluded_stop_ids"] = sorted(excluded - {identity} if identity.startswith(WISH_PREFIX) else excluded | {identity})
        if identity.startswith(WISH_PREFIX):
            req["wish_places"] = [item for item in req.get("wish_places", []) if wish_stop_id(item["city"], item["name"]) != identity]
            state["stops"].pop(identity, None)
        marks.update(required=list(req["required_stop_ids"]), excluded=list(req["excluded_stop_ids"]))
        if not identity.startswith(WISH_PREFIX):
            state.setdefault("stop_constraint_sources", {}).setdefault("excluded", {})[identity] = "decision"
    elif kind == "move" and state["stops"][identity].get("assigned_date"):
        state["stops"][identity]["assigned_date"] = action["to"]
        if identity.startswith(WISH_PREFIX):
            req["wish_places"] = [{**item, "date":action["to"]} if wish_stop_id(item["city"], item["name"]) == identity else item
                                  for item in req.get("wish_places", [])]
    elif kind in {"shorten", "set_walk_limit"} or kind == "set_time_window" and action.get("stop_id") and action.get("at"):
        req["constraints"] = merge(req.get("constraints", []), [_draft(action)], "decision", trip_dates=trip_dates(req))
        if kind == "set_time_window":
            drafts = []
            for item in active_constraints(req,"booking_prerequisite",stop_id=identity):
                covered = set(item.get("scope",{}).get("dates") or trip_dates(req))
                dates = sorted(covered & set(action.get("dates") or covered))
                if item.get("at") and dates:
                    fields = {key:item[key] for key in ConstraintDraft.model_fields if key in item}
                    drafts.append({**fields,"at":action["at"],"scope":{**item["scope"],"dates":dates}})
            if drafts:
                req["constraints"] = merge(req["constraints"],drafts,"decision",trip_dates=trip_dates(req))
            state["stops"][identity]["appointment_time"] = action["at"]
            state["stops"][identity]["appointment_time_source"] = "decision"
            for day in _plan(state).get("days",[]):
                if not action.get("dates") or day["date"] in action["dates"]:
                    for item in day.get("activities",[]):
                        if item.get("id") == identity:
                            item.update(appointment_time=action["at"],appointment_time_source="decision")
        if kind == "set_walk_limit":
            req["walking_limit_exempt_dates"] = sorted(set(req.get("walking_limit_exempt_dates", [])) - set(action["dates"]))
    elif kind in {"lower_count", "accept_short", "keep_short"}:
        drafts = []
        for item in active_constraints(req, "daily_min_stops"):
            dates = sorted(set(item["scope"]["dates"] or trip_dates(req)) & set(_scope(action) or trip_dates(req)))
            if not dates:
                continue
            fields = {key:item[key] for key in ConstraintDraft.model_fields if key in item}
            drafts.append(ConstraintDraft(**{**fields, "scope":{**item["scope"], "dates":dates},
                "strength":"soft" if kind != "keep_short" else item["strength"], "count":action.get("count", item["count"]),
                "revoke":kind == "keep_short", "quote":action.get("quote") or kind}).model_dump(mode="json"))
        req["constraints"] = merge(req.get("constraints", []), drafts, "decision", trip_dates=trip_dates(req))
        state["keep_current_plan"] = kind == "keep_short"
    elif kind == "add_source":
        state["selected_source_ids"] = list(dict.fromkeys([*state.get("selected_source_ids", []), action["source_id"]]))
        state["stops"].update(_source_stops(state, action))
    elif kind == "set_budget":
        req["budget"] = action["amount"]
    elif kind in {"exempt_walk_limit", "restore_walk_limit"}:
        exempt = set(req.get("walking_limit_exempt_dates", []))
        req["walking_limit_exempt_dates"] = sorted(exempt | set(action["dates"]) if kind == "exempt_walk_limit" else exempt - set(action["dates"]))
    elif kind == "set_time_window":
        if action.get("unlock"):
            target = identity or action["resource_id"]
            state["locks"] = [item for item in state.get("locks",[]) if (item.get("id") if isinstance(item,dict) else item) != target]
            drafts = []
            for item in req.get("constraints",[]):
                covered = set(item.get("scope",{}).get("dates") or trip_dates(req))
                dates = sorted(covered & set(action.get("dates") or covered))
                if (item.get("kind") in {"time_anchor","booking_prerequisite"} and target in item.get("scope",{}).get("stop_ids",[])
                        and not item.get("revoke") and not item.get("superseded_by") and dates):
                    fields = {key:item[key] for key in ConstraintDraft.model_fields if key in item}
                    drafts.append({**fields,"revoke":True,"scope":{**item["scope"],"dates":dates}})
            if drafts:
                req["constraints"] = merge(req.get("constraints",[]),drafts,"decision",trip_dates=trip_dates(req))
            if target in state.get("stops",{}):
                state["stops"][target].pop("appointment_time",None)
                state["stops"][target].pop("appointment_time_source",None)
            for day in _plan(state).get("days",[]):
                for item in day.get("activities",[]):
                    if item.get("id") == target and (not action.get("dates") or day["date"] in action["dates"]):
                        item.pop("appointment_time",None)
                        item.pop("appointment_time_source",None)
        elif action.get("resource_id"):
            entry = next(item for group in ("transport","hotels") for item in _plan(state).get(group,[]) if item["id"] == action["resource_id"])
            entry["selected"] = copy.deepcopy(next(item for item in entry["options"] if item.get("id") == action["value"]))
            entry["selected_by_user"] = actor == "user"
            if entry["selected"].get("kind") in {"train","flight"}:
                from .constraints import station_buffer
                entry["buffer_minutes"] = station_buffer(req,entry["selected"]["kind"],entry["context"]["date"])
        else:
            req[action["field"]] = action["value"]
    elif kind == "set_hotel_area":
        req["hotel_areas"] = dict(req.get("hotel_areas", {}))
        if action["area"].strip():
            req["hotel_areas"][action["city"]] = action["area"].strip()
        else:
            req["hotel_areas"].pop(action["city"], None)
    elif kind == "select_poi":
        stop = state["stops"][identity]
        poi = next(item for item in stop["map_candidates"] if item["id"] == action["poi_id"])
        by = "user" if actor == "user" or stop.get("map_selected_id") and stop.get("map_selected_by") != "system" or _protected(state, identity) else "system"
        stop.update(map_place=copy.deepcopy(poi), map_selected_id=action["poi_id"], map_selected_by=by)
        if actor == "user":
            selection = {"stop_id":identity, "city":stop["city"], "name":stop["name"], "poi_id":action["poi_id"]}
            previous = next((item["place_selection"] for item in reversed(state.get("decision_history", []))
                             if item.get("place_selection", {}).get("stop_id") == identity
                             and not item.get("auto_settled") and item.get("source", "legacy_unknown") in authority.USER_SOURCES), None)
            if previous != selection:
                state.setdefault("decision_history", []).append({"source":"decision", "place_selection":selection})
        for day in _plan(state).get("days", []):
            for item in day.get("activities", []):
                if item.get("id") == identity:
                    item.update(map_place=copy.deepcopy(poi), map_selected_id=action["poi_id"], map_selected_by=by)
    elif kind == "select_hub":
        selection = {"city":action["city"], "name":action["name"], "poi_id":action["poi_id"]}
        previous = next((item["hub_selection"] for item in reversed(state.get("decision_history", []))
                         if item.get("hub_selection", {}).get("city") == action["city"] and item["hub_selection"].get("name") == action["name"]
                         and not item.get("auto_settled") and item.get("source", "legacy_unknown") in authority.USER_SOURCES), None)
        if previous != selection:
            state.setdefault("decision_history", []).append({"source":"decision", "hub_selection":selection})
    elif kind == "grant_repeat":
        permission = {"place_id":identity, "role":action["role"], "dates":action["dates"],
                      "meals":action.get("meals", []), "authorization":action.get("quote") or "明确选择再访"}
        if permission not in req.get("repeat_exceptions", []):
            if len(req.get("repeat_exceptions", [])) >= 20:
                raise ValueError("Repeat permission limit reached")
            req["repeat_exceptions"] = [*req.get("repeat_exceptions", []), permission]
    elif kind == "default_duration":
        state.setdefault("system_durations", {}).setdefault(action["date"], {})[identity] = action["minutes"]
    elif kind == "add_optional_stop":
        for route in [*state.get("candidates", []), state.get("selected_route") or {}]:
            for day in route.get("days", []):
                if day["date"] == action["date"] and identity not in day["stop_ids"]:
                    day["stop_ids"].append(identity)
        state.setdefault("system_free_time", {})[action["date"]] = 0
    elif kind == "retain_free_time":
        state.setdefault("system_free_time", {})[action["date"]] = action.get("free_minutes", 60)
    elif kind == "omit_unselected_hotel_legs":
        plan = _plan(state)
        plan["days"] = [scoped_day(plan, day) if day["date"] in action["dates"] else day for day in plan.get("days", [])]
    if kind in {"skip", "exclude", "move", "reorder_day", "replace_optional_stop"}:
        _edit_routes(state, action)


def _apply(state, action, actor):
    kind = action["type"]
    if kind in {"skip", "default_duration", "exclude", "move", "shorten", "add_source"}:
        legacy = {**action, "date":action.get("date")}
        if kind == "add_source":
            legacy["stops"] = _source_stops(state, action)
        if kind == "exclude":
            state["stop_constraints"] = {"required":sorted(set(state.get("stop_constraints", {}).get("required", [])) | set(state["requirements"].get("required_stop_ids", []))),
                                         "excluded":sorted(set(state.get("stop_constraints", {}).get("excluded", [])) | set(state["requirements"].get("excluded_stop_ids", [])))}
        updates, _, _ = apply_option(state, legacy, action.get("quote") or kind)
        if kind != "exclude":
            for field in ("required_stop_ids", "excluded_stop_ids"):
                if field in state["requirements"]:
                    updates["requirements"][field] = state["requirements"][field]
                else:
                    updates["requirements"].pop(field, None)
        state.update(updates)
        if kind == "exclude" and not action["stop_id"].startswith(WISH_PREFIX):
            state.setdefault("stop_constraint_sources", {}).setdefault("excluded", {})[action["stop_id"]] = "decision"
        if kind in {"skip", "exclude", "move"}:
            _edit_routes(state, action)
    else:
        _intent(state, action, actor)


def action_grants(state, actions):
    expected = copy.deepcopy(state)
    for action in _actions(actions):
        _validate(expected, action)
        _intent(expected, action)
    return [{"path":item["path"], "value":item["after"], "operation":item["operation"]}
            for item in authority.user_changes(authority.user_state(state), authority.user_state(expected))]


def authority_of(state, actions):
    candidate = copy.deepcopy(state)
    for action in _actions(actions):
        _validate(candidate, action)
        _apply(candidate, action, "system")
    return "user" if authority.user_changes(authority.user_state(state), authority.user_state(candidate)) else "system"


def _quote(quotes, index):
    if isinstance(quotes, dict):
        if "quotes" in quotes:
            return _quote(quotes["quotes"], index)
        return quotes.get(index, quotes.get(str(index), ""))
    for offset, item in enumerate(quotes or []):
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if isinstance(item, str) and offset == index:
            return item
        if isinstance(item, dict) and item.get("action_index") == index:
            return item.get("quote", "")
    return ""


def execute(state, actions, actor, quotes=None):
    if actor not in {"system", "user"}:
        raise ValueError("Unknown actor: " + str(actor))
    if quotes is not None and not isinstance(quotes, (list, tuple, dict)):
        raise ValueError("Quotes must be indexed user text fragments")
    parsed, candidate = _actions(actions), copy.deepcopy(state)
    texts = authority.user_texts(state)
    if isinstance(quotes, dict):
        texts += [quotes[key] for key in ("text", "answer_text", "original_text") if isinstance(quotes.get(key), str)]
    dates, summaries = set(), []
    for index, action in enumerate(parsed):
        _validate(candidate, action)
        before = authority.user_state(candidate)
        quoted = _quote(quotes, index) if quotes is not None else ""
        if quoted:
            action["quote"] = quoted
            PlanAction.model_validate(action)
        _apply(candidate, action, actor)
        changes = authority.user_changes(before, authority.user_state(candidate))
        if actor == "user" and quotes is not None and changes:
            compact = re.sub(r"[\s。，；！？,.;!?、]+", "", quoted)
            if not compact or not any(compact in re.sub(r"[\s。，；！？,.;!?、]+", "", text) for text in texts):
                raise ValueError("Missing original user quote for action " + str(index))
        dates.update(_scope(action))
        if action.get("to"):
            dates.add(action["to"])
        summaries.append(("系统调整：" if actor == "system" else "按你的选择：") + _summary(state, action))
    changes = authority.user_changes(authority.user_state(state), authority.user_state(candidate))
    grants = action_grants(state, parsed) if actor == "user" else []
    allowed, violations = authority.authorize(changes, grants)
    if not allowed:
        raise AuthorityError(violations)
    updates = {key:copy.deepcopy(value) for key, value in candidate.items() if key not in state or value != state[key]}
    return updates, sorted(dates), "；".join(summaries)


def _summary(state, action):
    kind = action["type"]
    name = state.get("stops", {}).get(action.get("stop_id"), {}).get("name", "该地点")
    if kind == "set_budget":
        return "总预算调整为" + str(action["amount"]) + "元"
    if kind == "set_walk_limit":
        return "、".join(action["dates"]) + "步行上限调整为" + str(action["meters"]) + "米"
    if kind in {"shorten", "default_duration"}:
        return name + "游玩时长调整为" + str(action["minutes"]) + "分钟"
    if kind in {"skip", "exclude"}:
        return "本次不安排" + name
    if kind == "move":
        return name + "改到" + action["to"]
    if kind == "set_hotel_area":
        return action["city"] + "住宿区域调整为" + (action["area"].strip() or "根据行程推荐")
    if kind == "select_poi":
        return "更新" + name + "的具体地点"
    if kind == "select_hub":
        return "更新" + action["name"] + "的接驳位置"
    if kind == "add_optional_stop":
        return "加入现有攻略中的" + name
    if kind == "retain_free_time":
        return "保留当日自由时间" if action.get("free_minutes", 60) else "取消系统预留的自由时间"
    if kind == "omit_unselected_hotel_legs":
        return "没有已选住宿，省略住宿首末段，保留车站接驳"
    return {"add_source":"加入所选参考攻略", "lower_count":"调整每日景点数量目标", "accept_short":"接受现有攻略的景点数量", "keep_short":"保留当日较少景点的安排",
            "exempt_walk_limit":"豁免所选日期的步行上限", "restore_walk_limit":"恢复所选日期的步行上限", "set_time_window":"更新指定时间要求",
            "grant_repeat":"允许在指定范围内再访" + name, "reorder_day":"调整当日普通景点顺序", "replace_optional_stop":"替换当日普通景点"}[kind]


def _minutes(state, date):
    route = next((day for day in _route_days(state) if day["date"] == date), None)
    if not route:
        return 0
    plan = copy.deepcopy(_plan(state))
    day = next((day for day in plan.get("days", []) if day["date"] == date), {"date":date, "city":route.get("city"), "activities":[]})
    day["activities"] = [item for item in day.get("activities", []) if item.get("id") in route.get("stop_ids", [])]
    demand = day_demand(state, plan, day, route, unplaced=False)
    for item in demand:
        override = state.get("system_durations", {}).get(date, {}).get(item["id"])
        stop = state["stops"][item["id"]]
        if override is not None and not (stop.get("source_kind") == "user" and stop.get("duration_minutes") and not stop.get("duration_is_default")):
            item["visit"] = visit_minutes(state["stops"][item["id"]], state["requirements"], date, override)
    return sum(item["visit"] + item["move"] for item in demand)


def _ticket_savings(before, after):
    def visits(state):
        items = {item["id"]:item for day in _plan(state).get("days", []) for item in day.get("activities", [])}
        return {identity:(items.get(identity, state.get("stops", {}).get(identity, {})), day["date"])
                for day in _route_days(state) for identity in day.get("stop_ids", [])}
    def cost(visit, req):
        item, date = visit
        if ticket_policy(item) in {"free", "not_applicable"}:
            return money(0)
        quote = item.get("ticket_quote") or {}
        if (quote.get("price") is None or quote.get("price_unknown") or quote.get("assumed_free") or quote.get("options")
                or quote.get("price_date") != date):
            return None
        return money(quote["price"]) * req["people"]
    old, new = visits(before), visits(after)
    removed = [cost(old[key], before["requirements"]) for key in old.keys() - new.keys()]
    added = [cost(new[key], after["requirements"]) for key in new.keys() - old.keys()]
    if any(value is None for value in added):
        return 0
    return max(0, float(sum(value for value in removed if value is not None) - sum(added)))


def _targets(state, actions):
    targets = set()
    for action in actions:
        kind, stop = action["type"], action.get("stop_id")
        dates = _scope(action) + ([action["to"]] if action.get("to") else [])
        targets.update(("day", date) for date in dates)
        if stop:
            targets.add(("stop", stop))
        if action.get("resource_id"):
            targets.add(("resource", action["resource_id"]))
        if kind == "select_hub":
            targets.add(("resource", "hub:" + action["city"] + ":" + action["name"]))
        fields = {"set_budget":["budget"], "set_walk_limit":["max_walk_per_day", "constraints", "walking_limit_exempt_dates"],
                  "exempt_walk_limit":["max_walk_per_day", "walking_limit_exempt_dates"], "restore_walk_limit":["max_walk_per_day", "walking_limit_exempt_dates"],
                  "shorten":["constraints"], "lower_count":["constraints"], "accept_short":["constraints"], "keep_short":["constraints"],
                  "exclude":["required_stop_ids", "excluded_stop_ids", "wish_places"], "set_time_window":[action.get("field") or "constraints"],
                  "set_hotel_area":["hotel_areas"], "grant_repeat":["repeat_exceptions"]}.get(kind, [])
        targets.update(("requirement", field) for field in fields)
        constraint_kind = {"set_walk_limit":"fewer_walking", "shorten":"visit_duration", "set_time_window":"time_anchor",
                           "lower_count":"daily_min_stops", "accept_short":"daily_min_stops", "keep_short":"daily_min_stops"}.get(kind)
        for constraint in state.get("requirements", {}).get("constraints", []):
            scope = constraint.get("scope") or {}
            if (stop in scope.get("stop_ids", []) or constraint_kind == constraint.get("kind")) and (
                    not dates or not scope.get("dates") or set(dates) & set(scope["dates"])) and constraint.get("id"):
                targets.add(("constraint", constraint["id"]))
    return targets


def validate_option(state, option):
    try:
        option = DecisionOption.model_validate(option).model_dump(mode="json", exclude_none=True)
        actions = _actions(option["actions"])
        candidate = copy.deepcopy(state)
        affected = set()
        for action in actions:
            _validate(candidate, action)
            _apply(candidate, action, "user")
            affected.update(_scope(action))
            if action.get("to"):
                affected.add(action["to"])
        before_req, after_req = state["requirements"], candidate["requirements"]
        minute_savings = {date:max(0, _minutes(state, date) - _minutes(candidate, date)) for date in affected}
        budget_gain = max(0, (after_req.get("budget") or 0) - (before_req.get("budget") or 0) + _ticket_savings(state, candidate))
        meter_gains = {date:math.inf if walking_limit(after_req, date) is None else
                      max(0, walking_limit(after_req, date) - (walking_limit(before_req, date) or 0)) for date in affected}
        problems = []
        targets = _targets(state, actions)
        impact = option.get("impact", {})
        if abs(impact.get("minutes") or 0) > sum(minute_savings.values()):
            problems.append("Claimed minutes exceed the recomputed reduction")
        if abs(impact.get("amount") or 0) > budget_gain:
            problems.append("Claimed budget amount exceeds the recomputed allowance")
        if abs(impact.get("meters") or 0) > sum(meter_gains.values()):
            problems.append("Claimed walking allowance exceeds the recomputed change")
        for raw in state.get("unmet", state.get("unmet_items", [])):
            unmet = UnmetItem.model_validate(raw).model_dump(mode="json")
            numbers, dates = unmet["numbers"], unmet["dates"]
            refs = {(item["type"], item.get("id") or item.get("field")) for item in unmet["refs"]}
            specific = {ref for ref in refs if ref[0] != "day"}
            if (specific or refs) and not (specific or refs) & targets:
                continue
            if dates and affected and not set(dates) & affected:
                continue
            if numbers.get("shortfall_minutes", 0) > sum(minute_savings.get(date, 0) for date in dates or affected):
                problems.append("Actions do not cover shortfall_minutes")
            if numbers.get("excess_amount", 0) > budget_gain:
                problems.append("Actions do not cover excess_amount")
            if numbers.get("excess_meters", 0) > min((meter_gains.get(date, 0) for date in dates or affected), default=0):
                problems.append("Actions do not cover excess_meters")
        for action in actions:
            if action["type"] == "move":
                target = action["to"]
                day = next((day for day in _plan(candidate).get("days", []) if day["date"] == target), {})
                if day.get("available_start") is not None and day.get("tail_deadline") is not None:
                    capacity, lunch = _window(day)
                    if lunch:
                        capacity -= 60
                    if _minutes(candidate, target) > capacity:
                        problems.append("Moved stop does not fit the destination day")
        return not problems, list(dict.fromkeys(problems))
    except (ValueError, KeyError, TypeError) as exc:
        return False, [str(exc)]
