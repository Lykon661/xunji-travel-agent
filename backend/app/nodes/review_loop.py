"""规则检查、语义审查与规划失败处理。"""
import copy
import logging
from datetime import timedelta
from time import monotonic
from pydantic import ValidationError

from .. import ablation
from ..adapters import ToolError, default_ticket_price
from ..models import REVIEW_DIMENSIONS
from ..rules import check_itinerary, ticket_query_required, timestamp
from ..store import Conflict, now
from ..routing import reference_opening, selected_route_resources, scoped_day
from ..resource_planning import resource_changes, resource_context_issues, transport_issues
from ..decisions import confirmation_blockers, confirmation_issues, input_hash, route_structure
from ..unmet import collect
from ..places import canonicalize_itinerary, duplicate_issues, plan_fingerprint, verified_review_duplicates, canonical_key
from ..reviewing import bind_report, evidence_ids, review_basis, review_payload, incremental_review_payload, merge_incremental_review, review_context, review_context_state
from ..planning_recovery import previous_round, regression_reason, route_moves
from ..actions import execute
from ..constraints import public_only
from ..scheduler import estimate_move


logger = logging.getLogger(__name__)


class ReviewLoopNodes:
    def audit_state(self,state):
        return {**state,"locks":self.store.trip(state["trip_id"])["document"].get("locks",[])}

    def assert_review(self,state,report,itinerary):
        if not itinerary.get("days"):
            raise Conflict("路线尚无逐日草稿，不能确认或保存；请重新规划")
        state = self.audit_state(state)
        current = self.store.trip(state["trip_id"])
        doc = current["document"]
        if (state["requirements"] != doc["requirements"] or state.get("selected_source_ids",[]) != doc.get("selected_source_ids",[])
                or state.get("run_id") and current["active_run"] != state["run_id"]):
            raise Conflict("当前需求、攻略选择或规划任务已变化，旧审查不能用于保存")
        if hard := confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"],state.get("stops"),state):
            raise Conflict("路线仍有阻断："+"；".join(hard))

    def assert_document_review(self,trip):
        doc = trip["document"]
        state = {**doc,"trip_id":trip["id"]}
        self.assert_review(state,doc.get("review") or (doc.get("selected_route") or {}).get("review"),doc["itinerary"])

    def refresh_review_status(self,trip):
        doc = trip["document"]
        state = {**doc,"trip_id":trip["id"]}
        for candidate in doc.get("candidates",[]):
            itinerary = candidate.get("draft")
            if not itinerary:
                continue
            blockers = confirmation_blockers(itinerary,doc.get("stop_constraints"),doc["requirements"],state.get("stops"),state)
            blockers.extend(item.detail for item in collect({**state,"agent_unmet":doc.get("unmet",[])},itinerary)
                            if item.blocking)
            if not itinerary.get("days"):
                blockers.append("路线尚无逐日草稿，不能确认或保存；请重新规划")
            candidate["confirmation_blockers"] = list(dict.fromkeys(blockers))
            if candidate.get("review"):
                candidate["review"]["current"] = not bool(blockers)
        if doc.get("itinerary") and doc.get("review"):
            try:
                self.assert_document_review(trip)
                doc["review"]["current"] = True
            except Conflict:
                doc["review"]["current"] = False
        return trip

    async def audit(self,state,itinerary):
        self.emit(state,"reviewing","检验 Agent 正在检查攻略偏好、天气适配与餐饮推荐")
        state = self.audit_state(state)
        sources = self.selected_sources(state["trip_id"],state.get("selected_source_ids",[]))
        queries = self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary)))
        basis = review_basis(state,itinerary,sources,queries)
        if ablation.disabled("reviewer"):
            # Ablation: no independent model review; code blockers below still veto the plan.
            report = {"status":"pass","summary":"消融实验：未运行检验 Agent","findings":[],
                      "checks":[{"dimension":dimension,"result":"ok","note":"消融实验未审查"} for dimension in sorted(REVIEW_DIMENSIONS)]}
        else:
            previous = state.get("review_previous")
            payload = incremental_review_payload(state,itinerary,sources,queries,previous)
            dependency = state.get("review_change") or {}
            if payload is None and previous and dependency and not dependency.get("full"):
                dates = set(dependency.get("review_dates",dependency.get("replan_dates",[])))
                partial_plan = {**itinerary,"days":[day for day in itinerary.get("days",[]) if day["date"] in dates]}
                payload = {**review_payload(state,partial_plan,sources,queries),"review_dates":sorted(dates),
                           "review_mode":"changed_dates","review_dimensions":dependency.get("review_dimensions",[])}
            try:
                if payload:
                    self.emit(state,"reviewing","正在重审改动日期："+"、".join(payload["review_dates"])+"；其余日期沿用上轮提示")
                    partial = (await self.reviewer_agent.review(state,payload)).model_dump(mode="json")
                    report = merge_incremental_review(previous,partial,payload,itinerary)
                else:
                    report = (await self.reviewer_agent.review(state,review_payload(state,itinerary,sources,queries))).model_dump(mode="json")
            except (ToolError,ValidationError,KeyError,IndexError,ValueError) as exc:
                report = {"status":"pass_with_warnings","summary":"语义提示暂不可用："+str(exc),"findings":[],"unmet":[],
                          "checks":[{"dimension":dimension,"result":"not_applicable","note":"语义审查未完成；硬性要求仍由代码检查"}
                                    for dimension in sorted(REVIEW_DIMENSIONS)]}
                if payload and previous:
                    report = merge_incremental_review(previous,report,payload,itinerary)
        report["findings"] = [item for item in report.get("findings",[]) if item.get("dimension") in {"places","dining","sources"}]
        report["unmet"] = []
        for check in report.get("checks",[]):
            if check["dimension"] not in {"places","dining","sources"}:
                check.update(result="not_applicable",note="由代码负责，不属于语义审查")
        for finding in report.get("findings",[]):
            if finding.get("severity") == "blocker":
                finding["severity"] = "warning"
        for check in report.get("checks",[]):
            if check.get("result") == "blocker":
                check["result"] = "warning"
        if report["status"] not in {"pass","pass_with_warnings","review_unavailable"}:
            report["status"] = "pass_with_warnings"
        if report["status"] == "pass" and (report.get("findings") or any(day.get("gaps") for day in itinerary.get("days",[]))):
            report["status"] = "pass_with_warnings"
        config_store = getattr(self.models,"config_store",None)
        report["model"] = config_store.snapshot().public() if config_store else {}
        return bind_report(report,state,itinerary,basis,[row["evidence_id"] for row in queries])

    async def review_result(self,state):
        unchanged = state.get("change",{}).get("refresh_resources") and route_structure(state["itinerary"]) == route_structure(state.get("old_itinerary",{}))
        route = copy.deepcopy(state["selected_route"])
        if state.get("old_itinerary"):
            state["itinerary"]["change_summary"] = resource_changes(state["old_itinerary"],state["itinerary"])
        result = await self.assess_result(state,route,state["itinerary"])
        candidate = result["candidates"][0]
        if unchanged and not candidate["confirmation_blockers"] and not result.get("unmet") and route_structure(candidate["draft"]) == route_structure(state.get("old_itinerary",{})):
            from .coordinating import persisted
            trip = self.store.trip(state["trip_id"])
            doc = trip["document"]
            doc.update(persisted({**state,**result}))
            doc.update(itinerary=copy.deepcopy(candidate["draft"]),review=candidate["review"],selected_route=candidate,candidates=[candidate],
                       requirements=result["requirements"],stop_constraints=result["stop_constraints"],decision_history=state.get("decision_history",[]),
                       system_assumptions=copy.deepcopy(state.get("system_assumptions",doc.get("system_assumptions",[]))),
                       draft_revision=result["draft_revision"],pending=None,itinerary_outdated=False,
                       stage="completed" if doc.get("itinerary_saved_version") else "ready",workflow_step="itinerary",rollback=result.get("rollback",{}))
            if doc.get("confirmed_plan"):
                doc["confirmed_plan"].update(draft_revision=candidate["draft_revision"],plan_hash=candidate["plan_hash"],review_id=candidate["review"]["review_id"])
            self.store.update(state["trip_id"],doc)
            return {**result,"itinerary":candidate["draft"],"factual_refresh_completed":True}
        return {**result,"factual_refresh_completed":False}

    async def review_gate(self,state):
        candidates = state.get("candidates",[])
        if not candidates or not candidates[0].get("draft"):
            return {}
        candidate = candidates[0]
        report, itinerary = candidate.get("review"),candidate["draft"]
        audit_state = self.audit_state(state)
        queries = self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary)))
        basis = review_basis(audit_state,itinerary,self.selected_sources(state["trip_id"],state.get("selected_source_ids",[])),queries)
        if (report and report.get("review_basis_hash") == basis and report.get("plan_hash") == plan_fingerprint(itinerary)
                and report.get("draft_revision") == state.get("draft_revision",0)):
            # Keep the just-reviewed result for recovery; only a passed route is offered for confirmation.
            return {}
        return await self.assess_result(state,copy.deepcopy(candidate),copy.deepcopy(itinerary))

    @staticmethod
    def after_review_gate(state):
        from ..decisions import confirmation_issues
        candidate = (state.get("candidates") or [{}])[0]
        if candidate.get("draft") and confirmation_issues(candidate["draft"],state.get("stop_constraints"),
                state["requirements"],state.get("stops",{}),state):
            return "planning_failed"
        return "selection_gate"

    async def planning_failed(self,state):
        plan = self.coordinator_plan(state)
        report = state.get("review") or (state.get("candidates") or [{}])[0].get("review")
        return {"itinerary":plan,"unmet":[item.model_dump(mode="json") for item in collect(state,plan,report)],
                "coordinator_resume":"review_gate"}

    def refresh_duplicate_moves(self,state,original,dates):
        known = route_moves(original)
        original_days = {day["date"]:day for day in original.get("days",[])}
        amap = getattr(self,"amap",None)
        cache = getattr(amap,"cache",{})
        batch_var = getattr(amap,"batch_state",None)
        batch = batch_var.get() if batch_var is not None else None
        batch_routes = batch.get("route_results",{}) if isinstance(batch,dict) else {}
        mode = state["requirements"].get("local_transport","public")
        modes = ["transit","walking"] if mode == "public" else [mode]
        if state["requirements"].get("allow_taxi") and "driving" not in modes and not public_only(state["requirements"]):
            modes.append("driving")
        for day in state["itinerary"].get("days",[]):
            if day["date"] not in dates:
                continue
            old = original_days[day["date"]]
            old_visits = [item for item in old.get("activities",[]) if item.get("kind") == "visit"]
            def movement(first,last):
                if not first.get("name") or not last.get("name"):
                    return {}
                move = known.get((day["date"],day["city"],first["name"],last["name"]))
                if move is not None and move.get("minutes") is not None:
                    return copy.deepcopy(move)
                origin,destination = first.get("map_place") or {},last.get("map_place") or {}
                if origin.get("location") and destination.get("location"):
                    for allowed in modes:
                        key = ("route",state["trip_id"],day["city"],origin["location"],destination["location"],allowed)
                        cached = cache.get(key)
                        candidate = batch_routes.get(key)
                        if candidate is None and cached and monotonic()-cached[0] < 1800:
                            candidate = cached[1]
                        if candidate is None or candidate.get("minutes") is None:
                            continue
                        if (mode == "public" and allowed == "walking" and
                                (candidate["minutes"] > 25 or (candidate.get("walking_meters") or 0) > 1500)):
                            continue
                        return {**copy.deepcopy(candidate),"origin":first["name"],"destination":last["name"],
                                "origin_place":copy.deepcopy(origin),"destination_place":copy.deepcopy(destination)}
                return {**estimate_move(state,first,last),"origin":first["name"],"destination":last["name"],
                        "origin_place":copy.deepcopy(origin),"destination_place":copy.deepcopy(destination)}
            previous = {"name":day.get("start_place"),
                        "map_place":((old_visits[0].get("move_before") or {}).get("origin_place") if old_visits else None)}
            visits = [item for item in day.get("activities",[]) if item.get("kind") == "visit"]
            for item in visits:
                stop = {**item,"map_place":item.get("map_place") or state.get("stops",{}).get(item["id"],{}).get("map_place")}
                item["move_before"] = movement(previous,stop)
                previous = stop
            day["return_move"] = movement(previous,{"name":day.get("end_place"),
                "map_place":(old.get("return_move") or {}).get("destination_place")}) if visits else {}
            end = timestamp(visits[-1].get("end"),day["date"]) if visits else None
            move = day["return_move"]
            travel = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
            day["tail_arrival"] = end.hour*60+end.minute+(20+travel if move else 0) if end else day.get("available_start")

    async def repair_review_duplicates(self,state,report):
        generation = state.get("generation") or state.get("run_id")
        if not generation or state.get("duplicate_review_generation") == generation:
            return state,[]
        probe = copy.deepcopy(state)
        probe["duplicate_review_generation"] = generation
        original = copy.deepcopy(probe["itinerary"])
        actual_days = {day["date"]:[item["id"] for item in day["activities"] if item.get("kind") == "visit"]
                       for day in original["days"]}
        for route in [probe.get("selected_route") or {},*(probe.get("candidates") or [])]:
            for day in route.get("days",[]):
                if day["date"] in actual_days:
                    day["stop_ids"] = list(actual_days[day["date"]])
            if route.get("feasibility"):
                route["feasibility"].pop("spatial_unplaced",None)
        dates = set()
        for pair in verified_review_duplicates(probe,original,report.get("suspected_duplicates",[])):
            action = {"type":"skip","stop_id":pair["drop"],"date":pair["date"]}
            try:
                updates,_,_ = execute(probe,[action],"system")
            except ValueError as exc:
                logger.warning("Duplicate repair action rejected: %s; %s",action,exc)
                continue
            probe.update(updates)
            kept,dropped = probe["stops"][pair["keep"]],probe["stops"][pair["drop"]]
            references = [*(kept.get("source_references") or []),*(dropped.get("source_references") or [])]
            if dropped.get("source_id"):
                references.append({"stop_id":pair["drop"],"source_id":dropped["source_id"],"name":dropped["name"]})
            kept["source_references"] = [ref for index,ref in enumerate(references) if ref not in references[:index]]
            dropped.update(represented_by=pair["keep"],representation_reason="已被"+kept["name"]+"代表",
                           represented_canonical_id=canonical_key(dropped))
            dates.add(pair["date"])
        if not dates:
            return probe,[]
        after_skip = copy.deepcopy(probe)
        prior_blockers = {(issue["code"],issue.get("target")) for issue in confirmation_issues(
            probe["itinerary"],probe.get("stop_constraints"),probe["requirements"],probe["stops"],probe)}
        repair = {**probe,"old_itinerary":original,"old_requirements":copy.deepcopy(probe["requirements"]),
                  "repair_only":True,"duplicate_repair_dates":sorted(dates),"resource_retry_done":True,
                  "keep_current_plan":False,"revision_selected_ids":[],
                  "change":{"scopes":["activity"],"dates":sorted(dates),"cities":[]}}
        try:
            repair.update(await self.activities(repair))
            expected = {item["id"] for day in after_skip["itinerary"]["days"] if day["date"] in dates
                        for item in day["activities"] if item.get("kind") == "visit"}
            actual = {item["id"] for day in repair["itinerary"]["days"] if day["date"] in dates
                      for item in day["activities"] if item.get("kind") == "visit"}
            if not expected <= actual:
                raise ValueError("Local duplicate repair would remove other planned places")
            probe.update({key:repair[key] for key in ("itinerary","stops","system_assumptions","selected_route","candidates") if key in repair})
        except (ToolError,ValueError,KeyError) as exc:
            logger.warning("Duplicate repair kept the existing day without replacement: %s",exc)
            probe = copy.deepcopy(after_skip)
            self.refresh_duplicate_moves(probe,original,dates)
        await self.query_ticket_prices(probe,probe["itinerary"],dates,missing_only=True)
        checked = {**probe,"duplicate_repair_dates":sorted(dates)}
        probe.update(await self.check(checked))
        if any((issue["code"],issue.get("target")) not in prior_blockers for issue in confirmation_issues(
                probe["itinerary"],probe.get("stop_constraints"),probe["requirements"],probe["stops"],probe)):
            logger.warning("Duplicate replacement discarded because it would introduce a user requirement conflict")
            probe = copy.deepcopy(after_skip)
            self.refresh_duplicate_moves(probe,original,dates)
            probe.update(await self.check({**probe,"duplicate_repair_dates":sorted(dates)}))
        unchanged = {day["date"]:day for day in original["days"] if day["date"] not in dates}
        probe["itinerary"]["days"] = [copy.deepcopy(unchanged.get(day["date"],day)) for day in probe["itinerary"]["days"]]
        return probe,sorted(dates)

    async def assess_result(self,state,candidate,itinerary):
        if not itinerary.get("days"):
            from ..models import UnmetItem
            itinerary = self.coordinator_plan({**state,"candidates":[candidate]},itinerary)
            state = {**state,"unmet":[*state.get("unmet",[]),
                UnmetItem(kind="other",detail="本轮排程未返回逐日草稿，已保留上一版路线。").model_dump(mode="json")]}
        probe = {**state,"selected_route":copy.deepcopy(candidate),"itinerary":copy.deepcopy(itinerary)}
        probe["agent_unmet"] = [*state.get("agent_unmet",[]),*state.get("unmet",[])]
        probe.update(await self.check(probe))
        itinerary = probe["itinerary"]
        previous = state.get("review_previous")
        dependency = state.get("review_change")
        if previous and dependency and not dependency.get("full"):
            changed = set(dependency["replan_dates"])
            old_days = {day["date"]:day for day in previous["plan"].get("days",[]) if day["date"] not in changed}
            itinerary["days"] = [copy.deepcopy(old_days.get(day["date"],day)) for day in itinerary.get("days",[])]
        backup = state.get("round_backup") or previous_round({**state,"itinerary":state.get("old_itinerary"),"candidates":[]})
        reason = regression_reason(backup["itinerary"],itinerary) if backup else None
        batch_state = getattr(getattr(self,"amap",None),"batch_state",None)
        batch = batch_state.get() if batch_state is not None else None
        if backup and isinstance(batch,dict) and batch.get("failed") and not reason and (
                not any(item.get("kind") == "visit" for day in itinerary.get("days",[]) for item in day.get("activities",[]))
                or any(move.get("minutes") is None for move in route_moves(itinerary).values())):
            reason = "本轮地图查询暂停："+str(batch.get("error") or "未取得可用路线")
        rollback = {}
        if reason:
            rollback = {"reason":reason,"time":now(),"message":"本轮结果未采用，已恢复上一轮安排与依据。"}
            candidate = copy.deepcopy(backup["candidate"])
            probe.update(itinerary=copy.deepcopy(backup["itinerary"]),selected_route=candidate,review_previous=None)
            probe["itinerary"]["recovery_note"] = rollback["message"]+reason
            probe.update(await self.check(probe))
            itinerary = probe["itinerary"]
            if backup.get("requirements") != state["requirements"] or backup.get("selected_source_ids",[]) != state.get("selected_source_ids",[]):
                itinerary.setdefault("issues",[]).append({"code":"rollback_scope","target":"route","blocking":True,
                    "message":"恢复草稿与本次需求或攻略不同，请继续诊断后再确认。"})
        old = (state.get("candidates") or [{}])[0].get("draft") or state.get("old_itinerary") or {}
        revision = state.get("draft_revision",0) + (plan_fingerprint(old) != plan_fingerprint(itinerary))
        probe["draft_revision"] = revision
        report = await self.audit(probe,itinerary)
        before = copy.deepcopy(itinerary)
        sources = self.selected_sources(state["trip_id"],state.get("selected_source_ids",[]))
        previous_review = {"plan":before,"report":report,"context":review_context(probe,sources),
                           "context_state":review_context_state(probe),"sources":sources,
                           "queries":self.store.review_queries(state["trip_id"],list(evidence_ids(before)))}
        probe,duplicate_dates = await self.repair_review_duplicates(probe,report)
        if duplicate_dates:
            itinerary = probe["itinerary"]
            probe["draft_revision"] = state.get("draft_revision",0) + (plan_fingerprint(old) != plan_fingerprint(itinerary))
            probe["review_previous"] = previous_review
            probe["review_change"] = {"full":False,"replan_dates":duplicate_dates,"review_dates":duplicate_dates,
                                      "review_dimensions":["places","dining","sources"],"resource_scopes":[]}
            report = await self.audit(probe,itinerary)
        result = self.pack_candidate(probe,candidate,itinerary,report)
        return {**result,"itinerary":itinerary,"review":report,"rollback":rollback,"stops":probe["stops"],
                "weather":probe.get("weather",{}),"repair_round":state.get("repair_round",0),"coordinator_resume":"review_gate",
                "review_previous":None,"review_change":None,
                "system_assumptions":probe.get("system_assumptions",[]),
                "duplicate_review_generation":probe.get("duplicate_review_generation","")}

    def pack_candidate(self,state,candidate,itinerary,report):
        from ..models import UnmetItem
        candidate = copy.deepcopy(candidate)
        if not itinerary.get("days"):
            itinerary = self.coordinator_plan({**state,"candidates":[candidate]},itinerary)
            report = candidate.get("review") or report
            state = {**state,"unmet":[*state.get("unmet",[]),
                UnmetItem(kind="other",detail="本轮排程未返回逐日草稿，已保留上一版路线。").model_dump(mode="json")]}
        sources = self.selected_sources(state["trip_id"],state.get("selected_source_ids",[]))
        items = collect(state,itinerary,report)
        blockers = [item.detail for item in items if item.blocking]
        actual = {day["date"]:day for day in itinerary.get("days",[])}
        for day in candidate.get("days",[]):
            if day["date"] in actual:
                day["stop_ids"] = [item["id"] for item in actual[day["date"]].get("activities",[]) if item.get("kind") == "visit"]
        candidate.update(draft=copy.deepcopy(itinerary),review=copy.deepcopy(report),draft_revision=state.get("draft_revision",0),
            plan_hash=plan_fingerprint(itinerary),input_hash=input_hash(state["requirements"],state.get("selected_source_ids",[]),state["stops"],sources),
            confirmation_blockers=list(dict.fromkeys(blockers)),feasibility=self.feasibility(itinerary))
        return {"candidates":[candidate],"selected_route":{},"preview_ready":False,"draft_revision":state.get("draft_revision",0),
                "requirements":state["requirements"],"stop_constraints":state.get("stop_constraints",{}),
                "unmet":[item.model_dump(mode="json") for item in items],
                "previews":{candidate["id"]:{"itinerary":copy.deepcopy(itinerary),"queried_at":now()}}}

    @staticmethod
    def feasibility(itinerary):
        return {**({"meal_wishes":itinerary["meal_wish_plan"]} if itinerary.get("meal_wish_plan") else {}),
            "queried_at":now(),"budget":itinerary["budget"],"issues":itinerary["issues"],
            "information_unknown":itinerary.get("information_unknown",[]),
            "transport":[{"label":entry["label"],"selected":{key:value for key,value in (entry.get("selected") or {}).items() if key != "raw"},
                          "reason":entry.get("reason","")} for entry in itinerary.get("transport",[])],
            "hotels":[{"city":entry["city"],"selected_by_user":bool(entry.get("selected_by_user")),"name":(entry.get("selected") or {}).get("name"),
                       "area":entry.get("search_areas",[]),"commute_minutes":(entry.get("selected") or {}).get("commute_minutes"),
                       "selected":{key:value for key,value in (entry.get("selected") or {}).items() if key != "raw"},
                       "reason":entry.get("reason",""),"errors":entry.get("errors",[]),"query_status":entry.get("query_status")}
                      for entry in itinerary.get("hotels",[])],
            "days":[{"date":day["date"],"city":day["city"],"available_start":day.get("available_start"),"available_end":day.get("tail_deadline"),
                     "travel_minutes":day.get("travel_minutes"),"gaps":day.get("gaps",[])} for day in itinerary.get("days",[])]}

    async def check(self,state):
        self.emit(state,"checking","正在检查预算、日期和交通衔接")
        itinerary = canonicalize_itinerary(copy.deepcopy(state["itinerary"]))
        itinerary["days"] = [scoped_day(itinerary,day) for day in itinerary.get("days",[])]
        for day in itinerary.get("days",[]):
            if state.get("duplicate_repair_dates") and day["date"] not in state["duplicate_repair_dates"]:
                continue
            for activity in day.get("activities",[]):
                if activity.get("kind") == "visit" and not activity.get("product") and (activity.get("ticket_quote") or ticket_query_required(activity)):
                    activity["ticket_quote"] = default_ticket_price(activity.get("ticket_quote") or {"status":"not_queried"})
        for entry in itinerary.get("transport",[]):
            if entry.get("selected"):
                entry["selected"]["constraint_issues"] = transport_issues(entry["selected"],entry["context"],state["requirements"])
                departure = timestamp(entry["selected"].get("departure"),entry["context"]["date"])
                before = entry["selected"].get("departure_transfer") or {}
                earliest = timestamp(state["requirements"].get("earliest_departure"),entry["context"]["date"])
                if departure and earliest and before.get("minutes") is not None and departure-timedelta(minutes=entry.get("buffer_minutes",60)+before["minutes"]) < earliest:
                    entry["selected"]["constraint_issues"].append("计入接驳与进站后，需要早于允许时间出门")
        checked = check_itinerary(itinerary,state["requirements"],state.get("stops"))
        itinerary.update(checked)
        itinerary["issues"].extend(duplicate_issues(itinerary,state["requirements"]))
        for day in itinerary["days"]:
            if state.get("duplicate_repair_dates") and day["date"] not in state["duplicate_repair_dates"]:
                continue
            for activity in day["activities"]:
                if activity["kind"] != "visit":
                    continue
                start = timestamp(activity.get("start"),day["date"])
                end = timestamp(activity.get("end"),day["date"])
                opening = reference_opening({**activity,"map_place":(state.get("stops",{}).get(activity["id"],{}).get("map_place") or activity.get("map_place"))},day["date"])
                activity["opening_reference"] = opening
                activity["opening_window"] = None
                if opening and opening.get("closed"):
                    itinerary["issues"].append({"code":"closed_day","target":activity["id"],"message":activity["name"]+"在"+day["date"]+"的参考信息为闭馆，不能按普通营业时段安排","blocking":True})
                if opening and opening["windows"]:
                    activity["opening_window"] = next((window for window in opening["windows"] if start and end and
                        window[0] <= start.hour*60+start.minute and end.hour*60+end.minute <= window[1]),opening["windows"][0])
                hours = activity.get("opening_window")
                appointment = timestamp(activity.get("appointment_time"),day["date"])
                if hours and start and end and (start.hour*60+start.minute < hours[0] or end.hour*60+end.minute > hours[1]):
                    itinerary["issues"].append({"code":"opening_window","target":activity["id"],"message":activity["name"]+"的安排超出已取得的参考开放窗口，需要调整时间或明确取舍","blocking":True})
                last_entry = opening.get("last_entry_minutes") if opening else None
                if last_entry is not None and start and start.hour*60+start.minute > last_entry:
                    itinerary["issues"].append({"code":"last_entry","target":activity["id"],"message":activity["name"]+"的入场时刻晚于参考最晚入场时间，需要提前到达","blocking":True})
                if appointment and start and start != appointment:
                    itinerary["issues"].append({"code":"appointment_time","target":activity["id"],"message":activity["name"]+"无法在指定时刻开始，需要调整其他安排","blocking":True})
        old_ids = {a["id"] for d in state.get("old_itinerary",{}).get("days",[]) for a in d.get("activities",[])}
        old_ids |= {x["id"] for g in ("transport","hotels") for x in state.get("old_itinerary",{}).get(g,[])}
        new_ids = {a["id"] for d in itinerary.get("days",[]) for a in d.get("activities",[])}
        new_ids |= {x["id"] for g in ("transport","hotels") for x in itinerary.get(g,[])}
        for item_id in state["locks"]:
            if item_id in old_ids and item_id not in new_ids:
                itinerary["issues"].append({"code":"locked_missing","target":item_id,"message":"锁定项目无法纳入新行程，请解除锁定或调整需求","blocking":True})
        itinerary["issues"].extend(resource_context_issues(selected_route_resources(itinerary),state["requirements"]))
        if state["mode"] == "modify" and state.get("change",{}).get("scopes") == ["full"] and state["locks"]:
            for old_day in state.get("old_itinerary",{}).get("days",[]):
                if old_day["date"] < state["requirements"]["start_date"] or old_day["date"] > state["requirements"]["end_date"] or old_day["city"] not in state["requirements"]["cities"]:
                    if any(a["id"] in state["locks"] for a in old_day["activities"]):
                        itinerary["issues"].append({"code":"locked_date","target":old_day["date"],"message":"锁定活动与新日期或城市冲突，请调整","blocking":True})
        from ..decisions import confirmation_issues
        hints = [{**issue,"blocking":False} for issue in itinerary["issues"]]
        itinerary["issues"] = hints + confirmation_issues(itinerary,state.get("stop_constraints"),state["requirements"],state.get("stops"),state)
        if ablation.disabled("code_checks"):
            itinerary["issues"] = []
        if itinerary.get("personal_map") and plan_fingerprint(itinerary) != plan_fingerprint(state.get("old_itinerary") or state["itinerary"]):
            itinerary.pop("personal_map",None)
        return {"itinerary":itinerary}
