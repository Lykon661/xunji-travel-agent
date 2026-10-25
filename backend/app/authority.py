import copy
import json


USER_SOURCES = frozenset({"form", "message", "interview", "decision", "user", "legacy_unknown"})


def _source(value):
    return value or "legacy_unknown"


def _user(value):
    return _source(value) in USER_SOURCES


def user_texts(state) -> list[str]:
    doc = state.get("document") or {}
    texts = []
    for container in (doc, state):
        texts.extend(container.get("user_inputs", []))
        texts.append(container.get("preferences_original", container.get("requirements", {}).get("preferences", "")))
        texts.append(container.get("message", ""))
        texts.append(container.get("revision_request", ""))
        texts.extend(item.get("content", "") for item in [*container.get("messages", []), *container.get("route_chat", [])]
                     if item.get("role") == "user")
        interview = container.get("interview") or {}
        for entry in [*interview.get("history", []), *container.get("decision_history", [])]:
            if entry.get("auto_settled") or not _user(entry.get("source")):
                continue
            texts.extend(entry.get("answers", {}).values())
            texts.append(entry.get("note", ""))
    return list(dict.fromkeys(text for text in texts if isinstance(text, str) and text.strip()))


def user_state(state) -> dict:
    state = {**(state.get("document") or {}), **state}
    req = copy.deepcopy(state.get("requirements") or {})
    req.pop("constraints_basis", None)
    req["constraints"] = [dict(item, source=_source(item.get("source")))
                          for item in req.get("constraints", []) if _user(item.get("source"))]
    provenance = state.get("stop_constraint_sources") or {}
    marks = {}
    for kind, field in (("required", "required_stop_ids"), ("excluded", "excluded_stop_ids")):
        sources = provenance.get(kind, {})
        req[field] = sorted(identity for identity in req.get(field, []) if _user(sources.get(identity)))
        marks[kind] = {identity: _source(sources.get(identity))
                       for identity in state.get("stop_constraints", {}).get(kind, []) if _user(sources.get(identity))}
    result = {"requirements": req, "stop_constraints": marks,
              "selected_source_ids": copy.deepcopy(state.get("selected_source_ids", [])),
              "reference_product_ids": copy.deepcopy(state.get("reference_product_ids", []))}
    plans = [state.get("old_itinerary") or {}, state.get("selected_route", {}).get("draft") or {},
             state.get("itinerary") or {}]
    plan = state["itinerary"] if isinstance(state.get("itinerary"),dict) else next((item for item in reversed(plans) if item), {})
    temporary = set(state.get("temporary_locks", []))
    locks = [item for item in state.get("locks", []) if
             (item.get("id") if isinstance(item, dict) else item) not in temporary
             and (not isinstance(item, dict) or _user(item.get("source")))]
    result["locks"] = {}
    for lock in locks:
        identity = lock.get("id") if isinstance(lock, dict) else lock
        entries = [{"date": day.get("date"), "city": day.get("city"), "activity": {
                    key: value for key, value in item.items() if key != "move_before"}}
                   for day in plan.get("days", []) for item in day.get("activities", []) if item.get("id") == identity]
        entries += [entry for group in ("transport", "hotels") for entry in plan.get(group, []) if entry.get("id") == identity]
        result["locks"][identity] = {"source": _source(lock.get("source")) if isinstance(lock, dict) else "legacy_unknown",
                                    "entries": entries}
    result["resources"] = {}
    for group in ("transport", "hotels"):
        result["resources"][group] = {}
        for entry in plan.get(group, []):
            selected = entry.get("selected")
            if not selected or entry.get("selected_by_user") is False:
                continue
            identity_fields = ("supplier_id", "kind", "name", "context", "departure", "arrival", "departure_place",
                               "arrival_place", "bed_type", "room_type", "room_name", "seat_class", "seat", "cabin", "legs")
            choice = {key: selected[key] for key in identity_fields if key in selected}
            if not selected.get("supplier_id") and not selected.get("name"):
                choice["id"] = selected.get("id")
            result["resources"][group][entry["id"]] = {"context": entry.get("context"), "selected": choice,
                "source": "user" if entry.get("selected_by_user") else "legacy_unknown"}
    result["products"] = {item["id"]: copy.deepcopy(item) for item in state.get("supplements", [])
                          if item.get("selected_by_user") is not False and _user(item.get("selected_by"))}
    result["products"].update({item["id"]:copy.deepcopy(item) for item in state.get("products",[])
                              if item.get("selected_by_user")})
    result["pois"], result["appointments"], result["hubs"], result["poi_decisions"], result["stop_assignments"] = {}, {}, {}, {}, {}
    from .constraints import trip_dates
    try:
        dates = trip_dates(req)
    except (TypeError,ValueError):
        dates = []
    dates = dates or [day["date"] for day in plan.get("days",[])]
    anchors = {}
    for constraint in req.get("constraints",[]):
        if (constraint.get("kind") not in {"time_anchor","booking_prerequisite"} or not constraint.get("at")
                or constraint.get("revoke") or constraint.get("superseded_by")
                or constraint.get("kind") == "time_anchor" and constraint.get("anchor") != "appointment"):
            continue
        scope = constraint.get("scope") or {}
        for day in scope.get("dates") or dates:
            for identity in scope.get("stop_ids",[]):
                anchors[str(day)+":"+identity] = {"at":constraint["at"],"source":_source(constraint.get("source"))}
    result["appointments"].update(anchors)
    for identity, stop in state.get("stops", {}).items():
        if stop.get("assigned_date") and _user(stop.get("assigned_date_source")):
            result["stop_assignments"][identity] = {"date":stop["assigned_date"], "source":_source(stop.get("assigned_date_source"))}
        if stop.get("map_selected_id") and _user(stop.get("map_selected_by")):
            result["pois"][identity] = {"id": stop["map_selected_id"], "source": _source(stop.get("map_selected_by")),
                                        "resolved_id":(stop.get("map_place") or {}).get("id") or stop["map_selected_id"]}
    for day in plan.get("days", []):
        for item in day.get("activities", []):
            if item.get("map_selected_id") and _user(item.get("map_selected_by")):
                result["pois"].setdefault(item["id"], {"id": item["map_selected_id"], "source": _source(item.get("map_selected_by")),
                                                       "resolved_id":(item.get("map_place") or {}).get("id") or item["map_selected_id"]})
            if item.get("product") and item.get("selected_by_user") is not False:
                result["products"][item["id"]] = {"product":copy.deepcopy(item["product"]), "date":day["date"],
                                                  "duration_minutes":item.get("duration_minutes")}
            if item.get("appointment_time") and _user(item.get("appointment_time_source")):
                result["appointments"].setdefault(day["date"] + ":" + item["id"], {
                    "at": item["appointment_time"], "source": _source(item.get("appointment_time_source"))})
    for entry in state.get("decision_history", []):
        if entry.get("auto_settled") or not _user(entry.get("source")):
            continue
        hub = entry.get("hub_selection")
        if hub:
            result["hubs"][json.dumps([hub.get("city"), hub.get("name")], ensure_ascii=False)] = copy.deepcopy(hub)
        poi = entry.get("place_selection")
        if poi and poi.get("poi_id"):
            result["poi_decisions"][poi["stop_id"]] = {"id": poi["poi_id"], "source": "user"}
    return copy.deepcopy(result)


def user_changes(before, after) -> list[dict]:
    changes = []
    def visit(left, right, path):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if key not in left or key not in right:
                    changes.append({"path": child, "before": copy.deepcopy(left.get(key)), "after": copy.deepcopy(right.get(key)),
                                    "operation": "add" if key not in left else "remove"})
                else:
                    visit(left[key], right[key], child)
        elif left != right:
            changes.append({"path": path, "before": copy.deepcopy(left), "after": copy.deepcopy(right), "operation": "replace"})
    visit(before, after, "")
    return changes


def authorize(changes, grants) -> tuple[bool, list[dict]]:
    violations = [copy.deepcopy(change) for change in changes if not any(
        grant.get("path") == change["path"] and "value" in grant and grant["value"] == change["after"]
        and (change.get("operation") != "remove" or grant.get("operation") == "remove") for grant in grants)]
    return not violations, violations


def assert_system_action(before_state, after_state):
    changes = user_changes(user_state(before_state), user_state(after_state))
    if changes:
        raise ValueError("System action changed user state: " + ", ".join(change["path"] for change in changes))
