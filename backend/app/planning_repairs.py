from .dining import is_dining
from .places import canonical_key, represented_ids
from .routing import coordinates, distance


def change_dependencies(before, after=None, proposed=None, dates=None):
    from .actions import _actions, _plan, _route_days, _scope
    from .constraints import trip_dates
    from .models import REVIEW_DIMENSIONS
    from .rules import timestamp, walking_limit
    after = after or before
    plan = _plan(before)
    days = {day["date"]:day for day in plan.get("days",[])}
    routes = {day["date"]:day for day in _route_days(before)}
    available = set(days) | set(routes) or set(trip_dates(before["requirements"]))
    req, old_req = after["requirements"],before["requirements"]
    fields = {key for key in old_req.keys() | req.keys() if old_req.get(key) != req.get(key)} - {"constraints_basis"}
    local_fields = {"budget","max_walk_per_day","walking_limit_exempt_dates","earliest_departure","latest_arrival","latest_return",
                    "hotel_areas","constraints","required_stop_ids","excluded_stop_ids","repeat_exceptions","allow_taxi","local_transport"}
    full = bool(fields-local_fields or before.get("selected_source_ids",[]) != after.get("selected_source_ids",[]))
    replan, review, dimensions, resources, cities = set(dates or []),set(dates or []),set(),set(),set()
    parsed = _actions(proposed) if proposed else []
    def stop_dates(identity):
        result = {date for date,row in days.items() if any(item.get("id") == identity for item in row.get("activities",[]))}
        result |= {date for date,row in routes.items() if identity in row.get("stop_ids",[])}
        return result
    def city_dates(city):
        return {date for date,row in {**routes,**days}.items() if row.get("city") == city}
    def walk_dates(scope):
        review.update(scope)
        dimensions.update({"constraints","feasibility"})
        for date in scope:
            limit = walking_limit(req,date)
            if limit is not None and days.get(date,{}).get("walking_meters",0) > limit:
                replan.add(date)
    def window_dates(field):
        dimensions.update({"constraints","timing","feasibility"})
        value = req.get(field)
        if field == "latest_arrival":
            scope = {entry["context"]["date"] for entry in plan.get("transport",[]) if entry.get("selected")
                     and entry["context"].get("destination") != req.get("origin")}
        elif field == "latest_return":
            from .routing import daily_route_scope
            scope = {date for date,day in days.items() if daily_route_scope(plan,day)["hotel_location_confirmed"]}
            scope |= {entry["context"]["date"] for entry in plan.get("transport",[])
                      if entry.get("selected") and entry["context"].get("destination") == req.get("origin")}
        else:
            scope = set(available)
        review.update(scope)
        if not value:
            return
        bound = int(value[:2])*60+int(value[3:])
        for date in scope:
            arrivals = [entry for entry in plan.get("transport",[]) if entry.get("selected") and entry["context"]["date"] == date
                        and (entry["context"].get("destination") == req.get("origin")) == (field == "latest_return")]
            if field in {"latest_arrival","latest_return"} and any(
                    (arrival := timestamp(entry["selected"].get("arrival"),date)) and arrival > timestamp(value,date) for entry in arrivals):
                replan.add(date)
            if field == "latest_arrival":
                continue
            items = days.get(date,{}).get("activities",[])
            times = [int(clock.split(":")[0])*60+int(clock.split(":")[1]) for item in items
                     if (clock := item.get("start" if field == "earliest_departure" else "end"))]
            clock = min(times,default=None) if field == "earliest_departure" else max(times,default=None)
            if clock is not None and (clock < bound if field == "earliest_departure" else clock > bound):
                replan.add(date)
            if field == "latest_return":
                from .routing import daily_route_scope
                day = days.get(date,{})
                if day and daily_route_scope(plan,day)["hotel_location_confirmed"]:
                    returned = timestamp(day.get("return_end"),date)
                    finish = returned.hour*60+returned.minute if returned else (clock or 0)+(day.get("return_move",{}).get("minutes") or 0)
                    if finish > bound:
                        replan.add(date)
    for action in parsed:
        kind = action["type"]
        scope = set(_scope(action))
        identity = action.get("stop_id")
        if kind == "set_budget":
            dimensions.add("budget")
            continue
        if kind in {"set_walk_limit","exempt_walk_limit","restore_walk_limit"}:
            walk_dates(scope)
            continue
        if kind == "set_time_window" and action.get("field"):
            window_dates(action["field"])
            continue
        if kind == "set_time_window" and action.get("resource_id"):
            scope |= {entry["context"]["date"] for entry in plan.get("transport",[]) if entry["id"] == action["resource_id"]}
        if kind == "set_hotel_area":
            scope = city_dates(action["city"])
            cities.add(action["city"])
            resources.add("hotel")
        elif kind == "select_hub":
            scope = {entry["context"]["date"] for entry in plan.get("transport",[])
                     if not action.get("resource_id") or entry["id"] == action["resource_id"]}
        else:
            if identity:
                scope |= stop_dates(identity)
            if action.get("to"):
                scope.add(action["to"])
            if kind == "add_source" and not scope:
                scope = available
            if not scope and identity:
                scope = city_dates(before.get("stops",{}).get(identity,{}).get("city"))
            if not scope and kind in {"lower_count","accept_short","keep_short","grant_repeat"}:
                scope = available
        review.update(scope)
        if kind not in {"lower_count","accept_short","keep_short","grant_repeat","omit_unselected_hotel_legs"}:
            replan.update(scope)
    if not parsed:
        for field in fields:
            if field == "budget":
                dimensions.add("budget")
            elif field in {"max_walk_per_day","walking_limit_exempt_dates"}:
                scope = available if field == "max_walk_per_day" else set(old_req.get(field,[])) ^ set(req.get(field,[]))
                walk_dates(scope)
            elif field in {"earliest_departure","latest_return","latest_arrival"}:
                window_dates(field)
            elif field == "hotel_areas":
                for city in old_req.get(field,{}).keys() | req.get(field,{}).keys():
                    if old_req.get(field,{}).get(city) != req.get(field,{}).get(city):
                        cities.add(city)
                        replan.update(city_dates(city))
                resources.add("hotel")
            elif field in {"required_stop_ids","excluded_stop_ids"}:
                for identity in set(old_req.get(field,[])) ^ set(req.get(field,[])):
                    replan.update(stop_dates(identity) or city_dates(before.get("stops",{}).get(identity,{}).get("city")))
            elif field == "constraints":
                old = {item["id"]:item for item in old_req.get(field,[])}
                new = {item["id"]:item for item in req.get(field,[])}
                for identity in old.keys() | new.keys():
                    if old.get(identity) == new.get(identity):
                        continue
                    item = new.get(identity) or old[identity]
                    scope = item.get("scope",{})
                    affected = set(scope.get("dates") or available)
                    if scope.get("cities"):
                        affected &= set().union(*(city_dates(city) for city in scope["cities"]))
                    if scope.get("stop_ids"):
                        located = set().union(*(stop_dates(stop) for stop in scope["stop_ids"]))
                        if located:
                            affected &= located
                    if item["kind"] == "fewer_walking":
                        walk_dates(affected)
                    else:
                        replan.update(affected)
            elif field in {"allow_taxi","local_transport"}:
                replan.update(available)
            elif field == "repeat_exceptions":
                review.update(day for item in [*old_req.get(field,[]),*req.get(field,[])] for day in item.get("dates",[]))
    if full:
        replan.update(available)
    review.update(replan)
    if replan or dates or parsed and not dimensions:
        dimensions.update(REVIEW_DIMENSIONS)
    return {"actions":parsed,"requirement_fields":sorted(fields),"replan_dates":sorted(replan & available),
            "review_dates":sorted(review & available),"review_dimensions":sorted(dimensions),
            "resource_scopes":sorted(resources),"resource_cities":sorted(cities),"full":full}


def protected_stops(state):
    from .actions import _protected
    stops = state.get("stops",{})
    identities = {identity for identity in stops if _protected(state,identity)}
    keys = {canonical_key(stops[identity]) for identity in identities}
    return identities | {identity for identity,stop in stops.items() if canonical_key(stop) in keys or represented_ids([stop]) & identities}


def identity_stop_ids(state):
    identities = protected_stops(state)
    route = state.get("selected_route") or (state.get("candidates") or [{}])[0]
    identities.update(identity for day in route.get("days",[]) for identity in day.get("stop_ids",[]))
    identities.update(item["id"] for day in (state.get("itinerary") or {}).get("days",[])
                      for item in day.get("activities",[]) if item.get("kind") == "visit")
    return identities


def unused_guide_stops(state, itinerary, city, date=None):
    stops = state.get("stops", {})
    used = {identity for day in (state.get("selected_route") or {}).get("days", []) for identity in day.get("stop_ids", [])}
    used |= {identity for route in state.get("candidates", [])[:1] for day in route.get("days", []) for identity in day.get("stop_ids", [])}
    used |= {item["id"] for day in itinerary.get("days", []) for item in day.get("activities", []) if item.get("kind") == "visit"}
    excluded = set(state.get("requirements", {}).get("excluded_stop_ids", [])) | set(state.get("stop_constraints", {}).get("excluded", []))
    keys = {canonical_key(stops[identity]) for identity in used | excluded if identity in stops}
    anchors = [coordinates(item.get("map_place")) for day in itinerary.get("days", []) if day.get("city") == city and (date is None or day["date"] == date)
               for item in day.get("activities", []) if item.get("kind") == "visit"]
    anchors = [point for point in anchors if point]
    result = []
    for identity, stop in stops.items():
        if (stop.get("city") != city or is_dining(stop) or stop.get("source_kind", "guide") in {"user", "flyai"}
                or represented_ids([stop]) & (used | excluded) or canonical_key(stop) in keys
                or date and stop.get("assigned_date") not in {None, date}
                or state.get("selected_source_ids") and stop.get("source_id") not in state["selected_source_ids"]
                   and not any(ref.get("source_id") in state["selected_source_ids"] for ref in stop.get("source_references", []))):
            continue
        point = coordinates(stop.get("map_place"))
        meters = min((distance(anchor, point) for anchor in anchors), default=None) if point else None
        result.append({**stop, "distance_meters":round(meters) if meters is not None else None})
    return sorted(result, key=lambda item:(item["distance_meters"] is None, item["distance_meters"] or 0, item["id"]))
