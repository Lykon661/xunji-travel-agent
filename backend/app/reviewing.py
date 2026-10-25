"""Review bindings and submit gates are enforced in code, not by an LLM verdict."""
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone

from .models import REVIEW_DIMENSIONS, ReviewReport
from .places import fingerprint, plan_fingerprint, occurrences
from .store import Conflict
from .routing import coordinates, daily_route_scope, reference_opening, route_issue_applies, scoped_day
from .constraints import active
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


def correct_transfer_arithmetic(report, payload):
    """Correct only a disproven transfer-buffer blocker; preserve separately evidenced constraints."""
    if active(payload.get("requirements",{}),"transfer_buffer"):
        return report
    scope = payload.get("review_scope") or review_scope(payload["plan"])
    corrected = report.model_copy(deep=True)
    corrections = []
    for finding in corrected.findings:
        if (finding.severity != "blocker" or finding.action != "repair"
                or not re.search(r"缓冲|余量|衔接|移动|交通|换乘",finding.message)
                or not re.search(r"不足|不够|不可靠|无法|冲突|赶不上|未充分",finding.message)
                or re.search(r"预约|班次|船班|登船|登机|检票|末班|停运|闭馆|开放|最晚入",finding.message)):
            continue
        path = canonical_review_path(payload["plan"],finding.field_path)
        activity = next((item for item in scope.get("activities",[]) if path == item["field_path"]
                         or path.startswith(item["field_path"]+"/")),None)
        transfer = (activity or {}).get("transfer_timing") or {}
        identity = transfer.get("evidence_id")
        if (transfer.get("within_transfer_window") is not True or not identity or identity not in finding.evidence_ids
                or not set(finding.evidence_ids) <= {identity,"requirements","review_scope"}):
            continue
        message = (f"{transfer.get('origin') or '前一项'}至{transfer['destination']}：前一项{transfer['previous_end']}结束，"
                   f"当前{activity['start']}开始，可用{transfer['available_minutes']:g}分钟，高德路线"
                   f"{transfer['route_minutes']:g}分钟，剩余{transfer['slack_minutes']:g}分钟；未证实存在额外的硬性缓冲要求。")
        finding.severity,finding.owner,finding.action = "warning","reviewer","evidence"
        finding.message = message
        finding.suggestion = "保留当前活动时段和高德路线；实时交通变化可作提醒，不因此重排行程。"
        corrections.append(message)
    if not corrections:
        return report
    blockers = [finding for finding in corrected.findings if finding.severity == "blocker"]
    if not blockers:
        if any(check.result == "blocker" and check.dimension not in {"timing","feasibility"} for check in corrected.checks):
            return report
        for check in corrected.checks:
            if check.result == "blocker":
                check.result,check.note = "warning","；".join(corrections)[:1000]
        corrected.status = "pass_with_warnings"
    corrected.summary = ("仍有已证实问题："+"；".join(finding.message for finding in blockers)+"；" if blockers else "")
    corrected.summary += "；".join(corrections)
    warnings = [finding.message for finding in corrected.findings if finding.severity == "warning" and finding.message not in corrections]
    if warnings:
        corrected.summary += "；其他提示："+"；".join(warnings)
    corrected.summary = corrected.summary[:2000]
    return type(report).model_validate(corrected.model_dump(mode="json"))


def review_semantic_errors(report, payload):
    scope = payload.get("review_scope") or review_scope(payload["plan"])
    dates = {item["date"]:item for item in scope["days"]}
    calendar = {key:item["weekday"][-1] for item in scope["days"] for key in
                (item["date"],f"{int(item['date'][5:7])}月{int(item['date'][8:10])}日")}
    texts = [report.summary,*[item.note for item in report.checks],
             *[text for item in report.findings for text in (item.message,item.suggestion)]]
    errors = []
    if scope["budget_status"] == "incomplete" and report.status == "pass":
        errors.append("预算仍有未知费用，只能带明确提示通过，不能宣称费用已完整核算或预算已满足")
    for text in texts:
        for match in re.finditer(r"(\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日)\s*(?:为|是|[（(])?\s*(?:星期|周|礼拜)([一二三四五六日天])(?![至到])",text):
            if match[1] in calendar and match[2].replace("天","日") != calendar[match[1]]:
                errors.append(f"日期星期错误：{match[1]}应为星期{calendar[match[1]]}，不能据错误星期推断闭馆")
    for finding in report.findings:
        if finding.severity != "blocker":
            continue
        if not route_issue_applies(payload["plan"],finding.model_dump(mode="json")):
            errors.append("没有已选住宿时不检查住宿首末段或出发结束地址，即使有已选班次也仅检查其车站接驳")
        if re.search(r"(?:审查|审查结果|报告|检验结论).{0,12}(?:与阻断状态不一致|状态不一致|格式不符|结构不符)|为了.{0,8}(?:满足|符合).{0,8}(?:格式|结构)",finding.message):
            errors.append("审查报告自身的状态或结构矛盾应由检验Agent纠正，不能列为行程硬问题或要求路线Agent重排；未证实的普通核验需求保留warning")
        bound_path = canonical_review_path(payload["plan"],finding.field_path)
        if (bound_path.startswith("/budget") and any(item.get("ticket_assumed_free") for item in scope.get("activities",[]))
                and re.search(r"门票|票价|票务",finding.message)
                and re.search(r"未返回|未知|缺失|缺价|未取得|0元|漏计|不完整|缺口",finding.message)
                and not re.search(r"算术|加总错误|币种|重复计费|已售罄|无法预约|票种不符",finding.message)):
            errors.append("未查询到门票，预算按0元计算；不能据此认定预算漏计、不完整或阻断行程")
        activity = next((item for item in scope.get("activities",[]) if bound_path == item["field_path"]
                         or bound_path.startswith(item["field_path"]+"/")),None)
        if activity:
            if (activity["map_location_confirmed"] and re.search(r"飞猪|供应商|门票|票务|报价",finding.message)
                    and re.search(r"坐标.{0,15}(?:不同|不一致|差异|偏差|不匹配)|(?:不同|不一致|差异|偏差).{0,8}坐标",finding.message)
                    and not re.search(r"(?:名称|地址|展区).{0,8}(?:不符|不同|不一致|不匹配)|另一场馆|票种不符",finding.message)):
                errors.append(f"{activity['field_path']}已有高德定位；飞猪只查询票务，跨平台坐标差异不能阻断或要求用户确认场馆；"
                              "沿用高德POI和交通依据，票务按名称、地址、展区与别名匹配")
            if (activity.get("ticket_assumed_free") and re.search(r"门票|票价|票务",finding.message)
                    and re.search(r"未返回|未知|缺失|缺价|未取得|按零|免费|0元|漏计|不完整|缺口",finding.message)
                    and not re.search(r"已售罄|无法预约|无可预约|闭馆|票种不符",finding.message)):
                errors.append("未查询到门票，预算按0元计算；这项门票不列未知费用、不使预算不完整，也不构成漏计或阻断；assumed_free与供应商标记免费保持区分")
            opening = activity["opening_reference"] or {}
            if (activity["within_opening_window"] is True and activity["entry_before_last_admission"] is not False
                    and opening.get("evidence_id") in finding.evidence_ids
                    and set(finding.evidence_ids) <= {opening.get("evidence_id"),"requirements","review_scope"}
                    and re.search(r"开放|营业|闭馆|最晚(?:进入|入场|入馆|入园)|入馆缓冲",finding.message)
                    and re.search(r"超出|超过|早于|晚于|最晚|未明确入馆缓冲",finding.message)
                    and (not re.search(r"最晚|停止入",finding.message) or activity["entry_before_last_admission"] is True)):
                errors.append(f"{activity['field_path']}的{activity['start']}入场、{activity['end']}离场符合所引开放依据；"
                              "最晚入场只比较start，闭馆只比较end；未声明额外入馆缓冲或预约待核实不能编造成超时，请分开保留真实预约限制")
            if (activity["map_location_confirmed"] and activity["ticket_candidates_only"]
                    and re.search(r"门票|报价|票务|票种",finding.message)
                    and re.search(r"地点|位置|坐标|场馆|地址|匹配",finding.message)):
                errors.append(f"{activity['field_path']}已有地图定位，ticket_quote仍为place_confirmation的未采用候选；"
                              "供应商地点差异只提示票务待核对，不据此判定实际路线地点错误，不采用不匹配报价或改动地图定位")
        affected = review_repair_dates([finding.model_dump(mode="json")],payload["plan"])
        if (not active(payload.get("requirements",{}),"transfer_buffer")
                and re.search(r"进站|出站|值机|到达缓冲|班次衔接",finding.message)
                and re.search(r"不足|不够|冲突|赶不上|未留足",finding.message)):
            for day in affected:
                for connection in dates[day].get("transport_connections",[]):
                    valid = connection.get("within_station_buffer") if connection["direction"] == "departure" else connection.get("within_exit_buffer")
                    if (valid is True and connection.get("station") and connection["station"] in finding.message
                            and set(finding.evidence_ids) <= {"requirements","review_scope",*connection.get("evidence_ids",[])}):
                        errors.append("已选班次的进出站缓冲已计入review_scope，所引依据下衔接成立，不能重复扣除缓冲后阻断")
        missing_first = re.search(r"首站|首末段",finding.message) and all(not dates[day]["first_leg_required"] for day in affected)
        missing_last = re.search(r"首末段|末站|返程|返回住宿",finding.message) and all(not dates[day]["last_leg_required"] for day in affected)
        missing_hotel = re.search(r"住宿.{0,8}(?:缺口|未选|未定)|酒店.{0,8}(?:未选|未定)",finding.message) and all(
            not dates[day]["hotel_location_confirmed"] for day in affected)
        if affected and (missing_first or missing_last or missing_hotel):
            errors.append("未选定住宿且无已选城际班次的日期不检查首末段或住宿往返，不得据此阻断")
        if (finding.field_path.startswith("/budget") and scope["budget_status"] == "incomplete"
                and re.search(r"未知|未计入|待确认|无价",finding.message)
                and not re.search(r"算术|加总错误|币种|重复计费",finding.message)):
            errors.append("budget.status=incomplete已明确未知费用不在小计内；仅因缺价不能认定按零计费、超预算或阻断路线")
        explicit_booking = active(payload.get("requirements",{}),"booking_prerequisite",stop_id=activity.get("id") if activity else None)
        if (not explicit_booking and re.search(r"门票|票价|票务|预约",finding.message)
                and re.search(r"(?:票价|价格).{0,8}(?:null|无价|未确认|未知)|(?:门票|票价|预约).{0,8}(?:待确认|待核对|未完成|未取得)",finding.message,re.I)
                and not re.search(r"已售罄|无法预约|无可预约|停止售票|闭馆|不适用|日期错误|币种|票种不符",finding.message)):
            errors.append("普通票价或预约尚待确认只保留提示；阻断需引用已证实的不可预约、售罄、闭馆或明确用户前置约束")
    summary = re.sub(r"(?:未发现|没有|不存在|未见|无)(?:任何|明确的|实际的)?(?:开放|营业).{0,10}冲突|(?:开放|营业).{0,8}(?:无冲突|不冲突)","",report.summary)
    if re.search(r"(?<![未没无不])(?:存在|发现|出现|仍有|有).{0,8}(?:开放|营业).{0,12}(?:冲突|超出|越界)",summary) and not any(
            item.severity == "blocker" and re.search(r"开放|营业|闭馆|停止入[馆园场]",item.message) for item in report.findings):
        errors.append("摘要宣称开放时间冲突，但没有对应的具体阻断发现；应说明真实不等式或修正摘要")
    return list(dict.fromkeys(errors))


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


def review_reference_errors(report, payload):
    errors = []
    days = payload["plan"].get("days",[])
    dates = {day["date"] for day in days}
    for index,finding in enumerate(report.findings):
        missing = [identity for identity in finding.evidence_ids if identity not in payload["evidence_index"]]
        if missing:
            errors.append({"code":"review_evidence","finding_index":index,"field":"evidence_ids","value":missing,
                           "message":"引用的依据不存在，只能使用evidence_index中的真实ID"})
        if not pointer_exists(payload["plan"],finding.field_path):
            errors.append({"code":"review_evidence","finding_index":index,"field":"field_path","value":finding.field_path,
                           "message":"该路径不在plan中；路径相对于plan，不加/plan前缀；缺失字段应指向现有父对象"})
        outside = [day.isoformat() for day in finding.dates if day.isoformat() not in dates]
        if outside:
            errors.append({"code":"review_scope","finding_index":index,"field":"dates","value":outside,
                           "message":"影响日期必须属于当前plan.days"})
        match = re.match(r"^/days/(0|[1-9][0-9]*)(?:/|$)",finding.field_path)
        if finding.dates and match and int(match[1]) < len(days):
            bound_date = days[int(match[1])]["date"]
            if bound_date not in {day.isoformat() for day in finding.dates}:
                errors.append({"code":"review_scope","finding_index":index,"field":"dates","value":[day.isoformat() for day in finding.dates],
                               "message":"影响日期应包含field_path所指日期 "+bound_date+"；跨日问题可以同时包含其他日期"})
    return errors


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


def review_blockers(report, state, itinerary, sources, queries):
    return []


def require_review(report, state, itinerary, sources, queries):
    if blockers := review_blockers(report,state,itinerary,sources,queries):
        raise Conflict("；".join(blockers))
