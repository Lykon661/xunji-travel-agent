"""Review bindings and submit gates are enforced in code, not by an LLM verdict."""
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone

from .models import ReviewReport
from .places import fingerprint, plan_fingerprint, occurrences
from .routing import coordinates, daily_route_scope, reference_opening, scoped_day
from .assumptions import agent_payload, user_decisions


POLICY_VERSION = 21


def evidence_ids(value):
    result = set()
    if isinstance(value,dict):
        for key,item in value.items():
            if key == "evidence_id" and isinstance(item,str) and item.startswith("query:"):
                result.add(item)
            elif key == "evidence_ids" and isinstance(item,list):
                result.update(identity for identity in item if isinstance(identity,str) and identity.startswith("query:"))
            elif key == "hotels" and isinstance(item,list):
                result.update(evidence_ids([entry for entry in item if entry.get("selected_by_user")]))
            elif key not in {"review", "raw", "options", "candidates"}:
                result.update(evidence_ids(item))
    elif isinstance(value,list):
        for item in value:
            result.update(evidence_ids(item))
    return result


def public_evidence(value):
    if isinstance(value,dict):
        return {key:public_evidence(item) for key,item in value.items()
                if str(key).casefold() not in {"api_key", "token", "access_token", "headers", "authorization", "diagnostic"}}
    if isinstance(value,list):
        return [public_evidence(item) for item in value]
    return value


def review_basis(state, itinerary, sources, queries):
    source_ids = state.get("selected_source_ids",[])
    return fingerprint({"policy_version":POLICY_VERSION,"plan_hash":plan_fingerprint(itinerary),
        "requirements":state["requirements"],"sources":[source for source in sources if source["id"] in source_ids],
        "source_ids":source_ids,"constraints":state.get("stop_constraints",{}),"locks":sorted(state.get("locks",[])),
        "decisions":decision_evidence(state),"queries":queries})


def decision_evidence(state):
    return [{key:value for key,value in entry.items() if key != "situation_hash"}
            for entry in state.get("decision_history",[])]


def review_payload(state, itinerary, sources, queries):
    itinerary = {**itinerary,"days":[scoped_day(itinerary,day) for day in itinerary.get("days",[])]}
    index = {row["evidence_id"]:public_evidence(row) for row in queries}
    for source in sources:
        if source["id"] in state.get("selected_source_ids",[]):
            # Review the actual quoted excerpts and image references without silently truncating the plan.
            index["source:"+source["id"]] = {key:source.get(key) for key in ("id", "title", "url", "extraction", "error")}
            quotes = [stop.get("evidence",{}).get("quote","") for stop in (source.get("extraction") or {}).get("stops",[])]
            index["source:"+source["id"]]["text_evidence"] = [quote for quote in quotes if quote and quote in source.get("text","")]
    index["requirements"] = state["requirements"]
    for item in state["requirements"].get("constraints",[]):
        if not item.get("superseded_by") and not item.get("revoke"):
            index["constraint:"+item["id"]] = item
    index["locks"] = state.get("locks",[])
    index["decisions"] = user_decisions(state.get("decision_history",[]))
    scope = review_scope(itinerary,queries)
    index["review_scope"] = scope
    for number,issue in enumerate(itinerary.get("issues",[])):
        index[f"rule:{number}"] = issue
    payload = {"plan":public_evidence(itinerary),"requirements":state["requirements"],
            "constraints":state.get("stop_constraints",{}),"locks":state.get("locks",[]),
            "decisions":state.get("decision_history",[]),"occurrences":occurrences(itinerary),
            "previous_plan":public_evidence(state.get("old_itinerary",{})),"change_scope":state.get("change",{}),
            "evidence_index":index,"review_scope":scope,"policy_version":POLICY_VERSION}
    payload = public_evidence(payload)
    index = payload["evidence_index"]
    payload["evidence_catalog"] = [
        {"evidence_id":identity,**{key:value[key] for key in ("provider","request","created_at","title","url") if key in value},
         "status":value.get("result",{}).get("status"),"code":value.get("result",{}).get("code")}
        for identity,value in index.items() if isinstance(value,dict)]
    payload["evidence_catalog"].extend({"evidence_id":identity} for identity,value in index.items() if not isinstance(value,dict))
    return compact_review_payload(public_evidence(agent_payload(state,payload)))


def review_context(state, sources):
    return fingerprint({**review_context_state(state),"sources":sources})


def review_context_state(state):
    return copy.deepcopy({key:state.get(key) for key in ("requirements","stop_constraints","locks","selected_source_ids")} |
                         {"decision_history":decision_evidence(state)})


def scoped_review_context(state, sources, previous):
    dependency = state.get("review_change")
    prior = previous.get("context_state")
    if not dependency or dependency.get("full") or not prior or previous.get("sources") != sources:
        return False
    if (previous["context"] != review_context(prior,sources) or
            previous["report"].get("review_basis_hash") != review_basis(prior,previous["plan"],sources,previous.get("queries",[]))):
        return False
    fields = {key for key in prior["requirements"].keys() | state["requirements"].keys()
              if prior["requirements"].get(key) != state["requirements"].get(key)} - {"constraints_basis"}
    if fields != set(dependency.get("requirement_fields",[])):
        return False
    if any(prior.get(key) != state.get(key) for key in ("locks","selected_source_ids")):
        return False
    if prior.get("stop_constraints") != state.get("stop_constraints"):
        if not fields & {"required_stop_ids","excluded_stop_ids"} or state.get("stop_constraints",{}) != {
                "required":state["requirements"].get("required_stop_ids",[]),"excluded":state["requirements"].get("excluded_stop_ids",[])}:
            return False
    history = decision_evidence(state)
    old_history = prior.get("decision_history",[])
    return history[:len(old_history)] == old_history


def incremental_review_payload(state, itinerary, sources, queries, previous):
    if (not previous or previous.get("context") != review_context(state,sources) and not scoped_review_context(state,sources,previous)
            or previous["report"].get("status") == "review_unavailable" or previous["report"].get("question")):
        return None
    old = previous["plan"]
    old_days = {day["date"]:day for day in old.get("days",[])}
    days = {day["date"]:day for day in itinerary.get("days",[])}
    if (not days or set(old_days) != set(days) or len(days) != len(itinerary["days"])
            or len(old_days) != len(old.get("days",[]))):
        return None
    dependency = state.get("review_change") or {}
    scoped = bool(dependency and not dependency.get("full"))
    budget_only = scoped and dependency.get("review_dimensions") == ["budget"]
    derived = {"days","issues","information_unknown","constraint_notes","change_summary"}
    if scoped:
        derived |= {"budget","personal_map"}
        if "hotel" in dependency.get("resource_scopes",[]):
            derived.add("hotels")
        if "transport" in dependency.get("resource_scopes",[]) or any(action["type"] == "select_hub" for action in dependency.get("actions",[])):
            derived.add("transport")
    if fingerprint({key:value for key,value in old.items() if key not in derived}) != fingerprint(
            {key:value for key,value in itinerary.items() if key not in derived}):
        return None
    changed = {day for day in days if fingerprint(days[day]) != fingerprint(old_days[day])}
    if scoped:
        changed |= set(dependency.get("review_dates",[]))
    if (not changed and not budget_only or changed == set(days) and not budget_only
            or changed - days.keys()):
        return None
    if any(item.get("severity") == "blocker" and not re.match(r"^/days/\d+(?:/|$)",item.get("field_path",""))
           and not (scoped and item.get("field_path","").startswith("/budget"))
           for item in previous["report"].get("findings",[])):
        return None
    unchanged_evidence = evidence_ids([day for key,day in days.items() if key not in changed])
    before = {row["evidence_id"]:row for row in previous.get("queries",[])}
    after = {row["evidence_id"]:row for row in queries}
    if any(before.get(key) != after.get(key) for key in unchanged_evidence):
        return None
    partial = {**itinerary,"days":[day for day in itinerary["days"] if day["date"] in changed]}
    required_evidence = evidence_ids(partial)
    selected_queries = [row for row in queries if row["evidence_id"] in required_evidence]
    partial_state = {**state,"old_itinerary":{**old,"days":[day for day in old["days"] if day["date"] in changed]}}
    payload = review_payload(partial_state,partial,sources,selected_queries)
    payload["review_mode"] = "changed_requirements" if budget_only else "changed_dates"
    if budget_only:
        payload["review_dimensions"] = ["budget"]
        payload["budget_items"] = [{"date":day["date"],"meal_estimate":day.get("meal_estimate"),
            "tickets":[{"id":item["id"],"name":item.get("name"),"ticket_quote":public_evidence(item.get("ticket_quote"))}
                       for item in day.get("activities",[]) if item.get("kind") == "visit"]}
            for day in itinerary["days"]]
    payload["review_dates"] = sorted(changed)
    payload["unchanged_days"] = [{"date":day["date"],"city":day["city"],
        "activities":[{key:item.get(key) for key in ("id","name","kind","start","end","canonical_place_id","alias_names","map_place")}
                      for item in day.get("activities",[]) if item.get("kind") == "visit"],
        "meals":[{key:item.get(key) for key in ("id","name","meal","canonical_place_id","map_place")}
                 for item in day.get("meal_recommendations",[])]}
        for day in itinerary["days"] if day["date"] not in changed]
    payload["occurrences"] = occurrences(itinerary)
    return payload


def merge_incremental_review(previous, report, payload, itinerary):
    changed = set(payload["review_dates"])
    result = copy.deepcopy(report)
    current_ids = {item["id"] for day in itinerary.get("days",[]) for item in day.get("activities",[]) if item.get("kind") == "visit"}
    result["suspected_duplicates"] = [item for item in report.get("suspected_duplicates",[])
                                      if set(item.get("stop_ids",[])) <= current_ids][:6]
    old_days = previous["plan"]["days"]
    new_indices = {day["date"]:index for index,day in enumerate(itinerary["days"])}
    def rebind(finding, original_days):
        finding = copy.deepcopy(finding)
        match = re.match(r"^/days/(\d+)(/.*)?$",finding["field_path"])
        if match:
            date_key = original_days[int(match[1])]["date"]
            finding["field_path"] = f"/days/{new_indices[date_key]}"+(match[2] or "")
        return finding
    budget_only = payload.get("review_dimensions") == ["budget"]
    retained = [rebind(item,old_days) for item in previous["report"].get("findings",[])
                if not (item["field_path"].startswith("/budget") if budget_only
                        else set(review_repair_dates([item],previous["plan"])) & changed)]
    result["findings"] = [*retained,*[rebind(item,payload["plan"]["days"]) for item in report["findings"]]]
    inherited_blockers = any(item["severity"] == "blocker" for item in retained)
    ranking = {"not_applicable":0,"ok":1,"warning":2,"unknown":3,"blocker":4}
    old_checks = {item["dimension"]:item for item in previous["report"]["checks"]}
    for item in result["checks"]:
        earlier = old_checks.get(item["dimension"],{"dimension":item["dimension"],"result":"not_applicable","note":"没有可沿用的语义提示"})
        if budget_only:
            if item["dimension"] != "budget":
                item.clear()
                item.update(copy.deepcopy(earlier))
            continue
        if earlier["result"] == "blocker" and not inherited_blockers:
            continue
        if ranking[earlier["result"]] > ranking[item["result"]]:
            item.update(result=earlier["result"],note=earlier.get("note",""))
    blockers = [item for item in result["findings"] if item["severity"] == "blocker"]
    if blockers:
        result["status"] = "needs_evidence" if all(item["action"] == "evidence" for item in blockers) else "revise"
    else:
        result["status"] = "pass_with_warnings" if result["findings"] or any(
            item["result"] in {"warning","unknown"} for item in result["checks"]) else "pass"
    retained_unmet = [copy.deepcopy(item) for item in previous["report"].get("unmet",[])
                      if not (item["kind"] == "budget" if budget_only else set(item.get("dates",[])) & changed)]
    result["unmet"] = [*retained_unmet,*result.get("unmet",[])]
    result["summary"] = ("已重审"+("预算" if budget_only else "、".join(sorted(changed)))+"；其余日期沿用上轮结论。"+report["summary"])[:2000]
    return ReviewReport.model_validate(result).model_dump(mode="json")


def review_scope(itinerary,queries=()):
    details = {}
    for row in sorted(queries,key=lambda item:item.get("created_at") or ""):
        raw = row.get("result",{}).get("raw") or {}
        if (row.get("provider") == "amap" and row.get("request",{}).get("tool") == "maps_search_detail"
                and isinstance(raw,dict) and raw.get("id") and row["request"].get("id") == raw["id"]):
            details[raw["id"]] = {**raw,"evidence_id":row["evidence_id"]}
    def minutes(value):
        return int(value[:2])*60+int(value[3:]) if isinstance(value,str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",value) else None
    activities = []
    for number,day in enumerate(itinerary.get("days",[])):
        day = scoped_day(itinerary,day)
        for index,activity in enumerate(day.get("activities",[])):
            if activity.get("kind") != "visit":
                continue
            place = activity.get("map_place") or {}
            # Old drafts may lack hours; use only detail evidence for that exact POI.
            opening_place = details.get(place.get("id"),place)
            opening = reference_opening({**activity,"map_place":opening_place},day["date"])
            start,end = minutes(activity.get("start")),minutes(activity.get("end"))
            previous = day["activities"][index-1] if index else None
            previous_end = minutes(previous.get("end")) if previous else None
            move = activity.get("move_before") or {}
            travel = move.get("minutes")
            gap = start-previous_end if start is not None and previous_end is not None else None
            known_move = (isinstance(travel,(int,float)) and not isinstance(travel,bool) and travel >= 0
                          and move.get("provider") == "amap" and str(move.get("evidence_id","")).startswith("query:"))
            last_entry = opening.get("last_entry_minutes") if opening else None
            quote = activity.get("ticket_quote") or {}
            activities.append({"field_path":f"/days/{number}/activities/{index}","id":activity.get("id"),
                "date":day["date"],"start":activity.get("start"),"end":activity.get("end"),"opening_reference":opening,
                "within_opening_window":any(begin <= start < end <= finish for begin,finish in opening["windows"])
                    if opening and start is not None and end is not None else None,
                "entry_before_last_admission":start <= last_entry if start is not None and last_entry is not None else None,
                "map_location_confirmed":coordinates(place) is not None and bool(place.get("evidence_id")),
                "transfer_timing":{"previous_end":previous.get("end") if previous else None,
                    "origin":move.get("origin"),"destination":move.get("destination") or activity.get("name"),
                    "available_minutes":gap,"route_minutes":travel,"evidence_id":move.get("evidence_id"),
                    "slack_minutes":gap-travel if gap is not None and known_move else None,
                    "within_transfer_window":gap >= travel if gap is not None and known_move else None},
                "ticket_quote_status":quote.get("status"),
                "ticket_assumed_free":bool(quote.get("assumed_free") or quote.get("price_unknown")),
                "ticket_candidates_only":quote.get("status") == "place_confirmation" and (quote.get("price") is None or quote.get("assumed_free"))
                    and not quote.get("options") and not activity.get("product")})
    def clock(value):
        return f"{value//60:02d}:{value%60:02d}" if isinstance(value,int) else None

    def connections(day):
        """Code-computed intercity timing so the reviewer does not re-derive station buffers."""
        day = scoped_day(itinerary,day)
        result = []
        visits = [item for item in day.get("activities",[]) if item.get("kind") == "visit"]
        for entry in itinerary.get("transport",[]):
            selected, ctx = entry.get("selected") or {},entry.get("context") or {}
            if ctx.get("date") != day["date"]:
                continue
            buffer = entry.get("buffer_minutes",60)
            if ctx.get("origin") == day["city"] and selected.get("departure"):
                departure = minutes(str(selected["departure"])[11:16])
                planned = minutes(day.get("return_end")) if (day.get("return_move") or {}).get("minutes") is not None else None
                latest = departure-buffer if departure is not None else None
                result.append({"direction":"departure","label":entry.get("label"),"station":selected.get("departure_place"),
                    "evidence_ids":sorted(evidence_ids([entry,day.get("return_move") or {}])),
                    "departure":clock(departure),"station_buffer_minutes":buffer,"latest_station_arrival":clock(latest),
                    "last_activity_end":visits[-1].get("end") if visits else None,
                    "route_minutes":(day.get("return_move") or {}).get("minutes"),"planned_station_arrival":clock(planned),
                    "slack_minutes":latest-planned if latest is not None and planned is not None else None,
                    "within_station_buffer":planned <= latest if latest is not None and planned is not None else None})
            if ctx.get("destination") == day["city"] and selected.get("arrival") and visits:
                arrival = minutes(str(selected["arrival"])[11:16])
                move = visits[0].get("move_before") or {}
                first = minutes(visits[0].get("start"))
                ready = arrival+60+move["minutes"] if arrival is not None and move.get("minutes") is not None else None
                result.append({"direction":"arrival","label":entry.get("label"),"station":selected.get("arrival_place"),
                    "evidence_ids":sorted(evidence_ids([entry,move])),
                    "arrival":clock(arrival),"exit_buffer_minutes":60,"route_minutes":move.get("minutes"),
                    "first_activity":visits[0].get("name"),"first_activity_start":visits[0].get("start"),
                    "slack_minutes":first-ready if first is not None and ready is not None else None,
                    "within_exit_buffer":first >= ready if first is not None and ready is not None else None})
        return result

    return {"days":[{"date":day["date"],"weekday":"星期"+"一二三四五六日"[date.fromisoformat(day["date"]).weekday()],
                     **daily_route_scope(itinerary,day),"transport_connections":connections(day)} for day in itinerary.get("days",[])],
            "activities":activities,
            "budget_status":itinerary.get("budget",{}).get("status"),
            "unknown_costs_are_excluded":bool(itinerary.get("budget",{}).get("unknown")),
            "coordinate_provider":"amap","ticket_coordinates_affect_route":False,
            "unpriced_tickets_or_unbooked_reservations_alone_are_blockers":False}


def compact_review_payload(payload):
    # Keep plan paths intact; only identical supporting material is replaced by JSON pointers.
    seen = {}

    def children(value):
        return value.items() if isinstance(value,dict) else enumerate(value) if isinstance(value,list) else ()

    def path_for(path,key):
        return path+"/"+str(key).replace("~","~0").replace("/","~1")

    def signature(value):
        text = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)
        return text if len(text) >= 300 else None

    def register(value,path):
        if key := signature(value):
            seen.setdefault(key,path)
        for key,item in children(value):
            register(item,path_for(path,key))

    def compact(value,path):
        key = signature(value)
        if key and key in seen:
            return {"$ref":seen[key]}
        if key:
            seen[key] = path
        if isinstance(value,dict):
            return {key:compact(item,path_for(path,key)) for key,item in value.items()}
        if isinstance(value,list):
            return [compact(item,path_for(path,index)) for index,item in enumerate(value)]
        return value

    register(payload["plan"],"#/plan")
    result = {}
    for key,value in payload.items():
        path = path_for("#",key)
        if key in {"plan","evidence_catalog","review_scope"}:
            result[key] = value
        elif key == "evidence_index":
            result[key] = {identity:compact(item,path_for(path,identity)) for identity,item in value.items()}
        else:
            result[key] = compact(value,path)
    return result


def review_repair_dates(findings, itinerary):
    days = itinerary.get("days",[])
    plan_dates = {day["date"] for day in days}
    dates = set()
    for finding in findings:
        affected = set(finding.get("dates",[])) & plan_dates
        match = re.match(r"^/days/(0|[1-9][0-9]*)(?:/|$)",finding.get("field_path",""))
        if match and int(match[1]) < len(days):
            affected.add(days[int(match[1])]["date"])
        if not affected:
            target = finding.get("target")
            affected.update(day["date"] for day in days if target == day["date"] or any(
                target in {item.get("id"),item.get("name")} for item in day.get("activities",[])))
        if not affected:
            affected.update(day["date"] for day in days if day["date"] in finding.get("message",""))
        if not affected:
            # Budget and resource findings without a local binding can affect the whole trip.
            return sorted(day["date"] for day in days)
        dates.update(affected)
    return sorted(dates)


def pointer_exists(value, path):
    if not isinstance(path,str) or path and not path.startswith("/"):
        return False
    if path == "":
        return True
    try:
        for part in path[1:].split("/"):
            if re.search(r"~(?![01])",part):
                return False
            part = part.replace("~1","/").replace("~0","~")
            if isinstance(value,list):
                if not re.fullmatch(r"0|[1-9][0-9]*",part):
                    return False
                value = value[int(part)]
            elif isinstance(value,dict):
                value = value[part]
            else:
                return False
        return True
    except (KeyError,IndexError,ValueError,TypeError):
        return False


def canonical_review_path(plan, path):
    """Correct unambiguous pointer syntax without changing a finding's conclusion."""
    if pointer_exists(plan,path):
        return path
    original = path
    if not isinstance(path,str) or not path.startswith("/"):
        return original
    if path.startswith("/plan/"):
        path = path[len("/plan"):]
    parts = path[1:].split("/")
    if any(re.search(r"~(?![01])",part) for part in parts):
        return original
    value, prefix = plan,""
    for part in parts:
        key = part.replace("~1","/").replace("~0","~")
        if isinstance(value,dict):
            if key not in value:
                # Missing fields bind to their existing object; never hide an invalid array index.
                return prefix or original
            value = value[key]
        elif isinstance(value,list):
            if not re.fullmatch(r"0|[1-9][0-9]*",key) or int(key) >= len(value):
                return original
            value = value[int(key)]
        else:
            return original
        prefix += "/"+part
    return prefix


def review_field_index(plan):
    def escape(key):
        return str(key).replace("~","~0").replace("/","~1")

    result = [{"field_path":"/"+escape(key),"target":str(key)} for key in plan]
    for number,day in enumerate(plan.get("days",[])):
        prefix = f"/days/{number}"
        result.append({"field_path":prefix,"target":day.get("date"),"city":day.get("city")})
        for group in ("activities","meal_recommendations"):
            for index,item in enumerate(day.get(group,[])):
                result.append({"field_path":f"{prefix}/{group}/{index}","target":item.get("id"),
                               "name":item.get("name"),"date":day.get("date")})
    for group in ("transport","hotels"):
        for index,item in enumerate(plan.get(group,[])):
            result.append({"field_path":f"/{group}/{index}","target":item.get("id"),"name":item.get("label") or item.get("city")})
    return result


def quote_expiry(option):
    try:
        queried = datetime.fromisoformat(option.get("queried_at","").replace("Z","+00:00"))
        return queried+timedelta(minutes=10) if queried.tzinfo is not None else None
    except (ValueError,TypeError,AttributeError):
        return None


def bind_report(report, state, itinerary, basis, queried_ids):
    result = copy.deepcopy(report)
    current = datetime.now(timezone.utc)
    expiry = current+timedelta(minutes=10)
    for group in ("transport", "hotels"):
        for entry in itinerary.get(group,[]):
            if group == "hotels" and not entry.get("selected_by_user"):
                continue
            selected = entry.get("selected") or {}
            if selected:
                expiry = min(expiry,quote_expiry(selected) or current)
    result.update(review_id=fingerprint([state.get("run_id"),basis,current.isoformat()])[:32],
                  policy_version=POLICY_VERSION,run_id=state.get("run_id"),draft_revision=state.get("draft_revision",0),
                  plan_hash=plan_fingerprint(itinerary),review_basis_hash=basis,evidence_ids=sorted(queried_ids),
                  reviewed_at=current.isoformat(),expires_at=expiry.isoformat())
    return result
