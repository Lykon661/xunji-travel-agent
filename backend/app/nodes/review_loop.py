"""规则检查、独立审查、修复循环与规划失败处理。"""
import copy
import logging
import re
from datetime import timedelta

from .. import ablation
from ..adapters import ToolError, default_ticket_price
from ..models import REVIEW_DIMENSIONS, PlanningDecision
from ..rules import check_itinerary, enforce_locks, resource_cost, ticket_query_required, timestamp
from ..store import Conflict, now
from ..routing import reference_opening
from ..resource_planning import resource_changes, resource_context_issues, transport_issues
from ..agents.runtime import digest
from ..decisions import confirmation_blockers, input_hash, route_structure
from ..dining import is_dining
from ..constraints import station_buffer
from ..places import canonicalize_itinerary, canonical_key, duplicate_issues, repeat_allowed, plan_fingerprint
from ..reviewing import bind_report, evidence_ids, require_review, review_basis, review_blockers, review_payload, review_repair_dates
from ..planning_recovery import previous_round, regression_reason, route_moves


class ReviewLoopNodes:
    def audit_state(self,state):
        return {**state,"locks":self.store.trip(state["trip_id"])["document"].get("locks",[])}

    def assert_review(self,state,report,itinerary):
        state = self.audit_state(state)
        current = self.store.trip(state["trip_id"])
        doc = current["document"]
        if (state["requirements"] != doc["requirements"] or state.get("selected_source_ids",[]) != doc.get("selected_source_ids",[])
                or state.get("run_id") and current["active_run"] != state["run_id"]):
            raise Conflict("当前需求、攻略选择或规划任务已变化，旧审查不能用于保存")
        if hard := confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"]):
            raise Conflict("路线仍有阻断："+"；".join(hard))
        queries = self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary)))
        require_review(report,state,itinerary,self.selected_sources(state["trip_id"],state.get("selected_source_ids",[])),queries)

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
            blockers = confirmation_blockers(itinerary,doc.get("stop_constraints"),doc["requirements"])
            blockers.extend(review_blockers(candidate.get("review"),state,itinerary,
                self.selected_sources(trip["id"],doc.get("selected_source_ids",[])),
                self.store.review_queries(trip["id"],list(evidence_ids(itinerary)))))
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
        self.emit(state,"reviewing","检验 Agent 正在重审完整行程、全程地点与餐饮重复")
        state = self.audit_state(state)
        sources = self.selected_sources(state["trip_id"],state.get("selected_source_ids",[]))
        queries = self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary)))
        basis = review_basis(state,itinerary,sources,queries)
        if ablation.disabled("reviewer"):
            # Ablation: no independent model review; code blockers below still veto the plan.
            report = {"status":"pass","summary":"消融实验：未运行检验 Agent","findings":[],"question":None,
                      "checks":[{"dimension":dimension,"result":"ok","note":"消融实验未审查"} for dimension in sorted(REVIEW_DIMENSIONS)]}
        else:
            try:
                report = (await self.reviewer_agent.review(state,review_payload(state,itinerary,sources,queries))).model_dump(mode="json")
            except ToolError as exc:
                report = {"status":"review_unavailable","summary":"独立检验未完成："+str(exc),"checks":[],"findings":[],"question":None}
        hard = confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"])
        if hard:
            for message in hard:
                if any(item["message"] == message for item in report["findings"]):
                    continue
                factual = not message.startswith("必去景点") and any(word in message for word in ("尚未", "未取得", "未确定", "没有可用", "未选择"))
                issue = next((item for item in itinerary.get("issues",[]) if item.get("message") == message),{})
                target = issue.get("target")
                dates = [day["date"] for day in itinerary.get("days",[]) if day["date"] == target or day["date"] in message
                         or any(target and target in {item.get("id"),item.get("name")} for item in day.get("activities",[]))]
                report["findings"].append({"code":"code_constraint","severity":"blocker","owner":"resource" if "住宿" in message or "班次" in message else "route",
                    "action":"evidence" if factual else "repair","target":target or "route","dates":dates,"field_path":"/days",
                    "message":message,"evidence_ids":["requirements"],"suggestion":"保留用户约束，补齐依据或调整相关安排"})
            if report["status"] in {"pass", "pass_with_warnings"}:
                report["status"] = "needs_evidence" if all(item["action"] == "evidence" for item in report["findings"] if item["severity"] == "blocker") else "revise"
                report["summary"] = "代码检查仍有阻断，不能确认："+"；".join(hard)
        elif report["status"] == "pass" and (itinerary.get("recovery_note") or any(move.get("refresh_failed") for move in route_moves(itinerary).values()) or
                itinerary.get("information_unknown") or itinerary.get("budget",{}).get("unknown") or
                any(day.get("gaps") or not day.get("weather",{}).get("available") for day in itinerary.get("days",[]))):
            report["status"] = "pass_with_warnings"
            report["summary"] += "；费用、天气或其他资料缺口仍需按候选提示核对"
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
        if unchanged and not candidate["confirmation_blockers"] and not result.get("decision") and route_structure(candidate["draft"]) == route_structure(state.get("old_itinerary",{})):
            trip = self.store.trip(state["trip_id"])
            doc = trip["document"]
            doc.update(itinerary=copy.deepcopy(candidate["draft"]),review=candidate["review"],selected_route=candidate,candidates=[candidate],
                       requirements=result["requirements"],stop_constraints=result["stop_constraints"],decision_history=state.get("decision_history",[]),
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
        if state.get("decision"):
            return "decision_gate"
        candidate = (state.get("candidates") or [{}])[0]
        if candidate.get("draft") and (candidate.get("confirmation_blockers") or
                (candidate.get("review") or {}).get("status") not in {"pass","pass_with_warnings"}):
            return "planning_failed"
        return "selection_gate"

    async def planning_failed(self,state):
        candidate = (state.get("candidates") or [{}])[0]
        itinerary = candidate.get("draft") or state.get("itinerary") or {}
        report = candidate.get("review") or {}
        findings = [item for item in report.get("findings",[]) if item.get("severity") == "blocker"]
        issue = (findings or [item for item in itinerary.get("issues",[]) if item.get("blocking")] or [{}])[0]
        dates = review_repair_dates([issue],itinerary)
        question = None
        try:
            result = await self.route_agent.review({**state,"itinerary":itinerary,"selected_route":candidate},[issue] if issue else [])
            question = result.question.model_dump(mode="json")
        except Conflict:
            raise
        except Exception as exc:
            # A failed question-generation call still leaves an answerable interview and the saved draft.
            logging.getLogger(__name__).warning("Planning clarification fallback: %s",type(exc).__name__)
        if question is None:
            target = issue.get("target") or (dates[0] if dates else "当前行程")
            target = next((item["name"] for day in itinerary.get("days",[]) for item in day.get("activities",[])
                           if item.get("id") == target),target)
            detail = issue.get("message") or report.get("summary") or "当前安排还缺少可据以确定调整方式的信息"
            question = PlanningDecision(id="planning-clarification",title="明确这部分行程的调整优先级",
                question=(str(target)+"尚不能确定怎样调整。你最希望保留哪项活动或时刻，哪些安排可以调整？只会修改相关部分。")[:1500],
                options=[],recommendation="说明必须保留的活动、固定时刻及可调整项，系统按你的取舍继续局部修复。",
                why_needed=("已保留现有草稿。当前卡点："+detail+"；尚未确认的资料不会被当作已核实事实，也不会要求你代查接口。")[:1500],
                affected_dates=dates).model_dump(mode="json")
        else:
            question["affected_dates"] = dates
        self.emit(state,"interviewing","相关部分尚需明确取舍，已保留其他安排并转到需求追问")
        return {"decision":question,"decision_resume":"assess","selected_route":{},"preview_ready":False}

    async def assess_result(self,state,candidate,itinerary,decision=None):
        constraints = {**state.get("stop_constraints",{})}
        constraints["required"] = [identity for identity in constraints.get("required",[]) if not is_dining(state["stops"].get(identity,{}))]
        state = {**state,"stop_constraints":constraints,"requirements":{**state["requirements"],"required_stop_ids":constraints["required"]}}
        revision = state.get("draft_revision",0)+1
        preserved_dates = set()
        if state.get("keep_current_plan"):
            preserved_dates = set(state.get("assessment_dates") or [day["date"] for day in itinerary["days"]])
            if state.get("assessment_refresh"):
                preserved_dates = {day["date"] for day in itinerary["days"]}
        backup = state.get("round_backup") or previous_round({**state,"itinerary":state.get("old_itinerary"),"candidates":[]})
        rollback = {}
        pending_rollback_reason = None
        no_progress = False
        diagnosed = False
        resolved_decision = None
        quotes_refreshed = False
        state = {**state,"draft_revision":revision,"selected_route":candidate,"itinerary":itinerary}
        while True:
            state.update(await self.check(state))
            itinerary = state["itinerary"]
            reason = pending_rollback_reason or (regression_reason(backup["itinerary"],itinerary) if backup else None)
            batch_context = getattr(getattr(self,"amap",None),"batch_state",None)
            batch = batch_context.get() if batch_context is not None else None
            batch_error = batch.get("error") if isinstance(batch,dict) and batch.get("failed") else None
            missing_moves = any(move.get("minutes") is None for move in route_moves(itinerary).values())
            has_visits = any(activity.get("kind") == "visit" for day in itinerary.get("days",[]) for activity in day.get("activities",[]))
            if backup and isinstance(batch_error,ToolError) and not reason and (missing_moves or not has_visits):
                reason = "本轮地图服务查询已暂停："+str(batch_error)
            if reason:
                rollback = {"reason":reason,"time":now(),"message":"本轮结果未采用，已自动恢复上一轮景点、时段和交通依据；旧查询不代表本轮更新成功。"}
                itinerary = copy.deepcopy(backup["itinerary"])
                candidate = copy.deepcopy(backup["candidate"])
                state.update(itinerary=itinerary,selected_route=candidate,keep_current_plan=True,review_feedback=[])
                missing = {key for key,move in route_moves(itinerary).items() if move.get("minutes") is None}
                if missing and not batch_error:
                    # Restore the schedule, then reuse successful map-cache results for these exact old legs.
                    await self.refresh_day_routes(state,itinerary,missing_only=True)
                    recovered = sum(key in missing and move.get("minutes") is not None for key,move in route_moves(itinerary).items())
                    if recovered:
                        rollback["message"] = ("本轮路线调整未采用，已恢复上一轮景点和时段，并补回"+str(recovered)+
                            "条已取得的交通依据；其余问题仍需按审查提示处理。")
                itinerary["recovery_note"] = rollback["message"]+"原因："+reason
                self.emit(state,"reviewing",itinerary["recovery_note"])
                state.update(await self.check(state))
                itinerary = state["itinerary"]
                if backup.get("requirements") != state["requirements"] or backup.get("selected_source_ids",[]) != state.get("selected_source_ids",[]):
                    itinerary["issues"].append({"code":"rollback_scope","target":"route","blocking":True,
                        "message":"已恢复的上一轮行程与本次需求或攻略选择不同，请继续按当前条件修改后再确认"})
            if not quotes_refreshed and not decision and not state.get("decision") and not state.get("repair_only"):
                quotes_refreshed = True
                state.update(await self.refresh_expired_resources(state))
                if state["itinerary"] != itinerary:
                    state.update(await self.check(state))
                    itinerary = state["itinerary"]
            report = await self.audit(state,itinerary)
            repairable = [finding for finding in report["findings"] if finding["severity"] == "blocker" and finding["action"] in {"repair","evidence"}]
            unresolved_repairs = list(repairable)
            if preserved_dates:
                # Keeping one answered day must not suppress repairs on the rest of the trip.
                repairable = [{**finding,"dates":dates} for finding in repairable
                              if (dates := sorted(set(review_repair_dates([finding],itinerary))-preserved_dates))]
            if state.get("repair_only") and state.get("revision_selected_ids"):
                allowed_dates = set(state.get("assessment_dates",[]))
                repairable = [{**finding,"dates":dates} for finding in repairable
                              if (dates := sorted(set(review_repair_dates([finding],itinerary)) & allowed_dates))]
            failed_ids = {row["evidence_id"] for row in self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary)))
                          if row["result"].get("status") in {"error","unresolved","not_configured","paused","empty"}}
            repairable = [finding for finding in repairable if finding["action"] == "repair" or not set(finding["evidence_ids"]) & failed_ids]
            if report.get("question"):
                question_dates = set(report["question"].get("affected_dates",[])) or set(review_repair_dates(
                    [finding for finding in report["findings"] if finding["severity"] == "blocker" and finding["action"] == "decision"],itinerary))
                repairable = [{**finding,"dates":dates} for finding in repairable
                              if (dates := sorted(set(review_repair_dates([finding],itinerary))-question_dates))]
            stalled = no_progress or state.get("repair_round",0) >= 2 or bool(preserved_dates and unresolved_repairs and not repairable)
            final_attempt = False
            if (stalled and not diagnosed and unresolved_repairs and report["status"] != "review_unavailable" and not rollback
                    and not decision and not state.get("decision") and not report.get("question")):
                diagnosed = True
                self.emit(state,"repairing","正在判断剩余冲突能否继续自动修复，只有真实取舍才会询问你")
                resolution = await self.route_agent.resolve_review(state,unresolved_repairs)
                if resolution.status == "needs_decision":
                    resolved_decision = resolution.question.model_dump(mode="json")
                elif resolution.status == "repair" and repairable and state.get("repair_round",0) < 3:
                    repairable = [{**finding,"suggestion":finding.get("suggestion","")+"；本轮修复指令："+resolution.explanation} for finding in repairable]
                    final_attempt = True
            if (report["status"] == "review_unavailable" or not repairable or stalled and not final_attempt
                    or rollback or decision or state.get("decision") or resolved_decision):
                if (backup and not rollback and (state.get("repair_round",0) >= 2 or no_progress)
                        and not state.get("keep_current_plan") and not decision and not state.get("decision") and not report.get("question")):
                    old_findings = {(finding.get("code"),finding.get("target"),finding.get("field_path"))
                        for finding in (backup["candidate"].get("review") or {}).get("findings",[]) if finding.get("severity") == "blocker"}
                    new_findings = {(finding.get("code"),finding.get("target"),finding.get("field_path"))
                        for finding in report["findings"] if finding.get("severity") == "blocker"}
                    if len(new_findings-old_findings) >= 3:
                        pending_rollback_reason = "自动修复后仍新增至少 3 项阻断问题，恢复上一轮安排"
                        continue
                break
            before = digest([route_structure(itinerary),sorted(evidence_ids(itinerary))])
            if not backup:
                backup = previous_round({**state,"candidates":[{**candidate,"draft":itinerary}]})
            state["review_feedback"] = repairable
            self.emit(state,"repairing","正在自动反馈责任 Agent 修复，完成后会重新审查" if not final_attempt else "正在按新的修复指令完成最后一次自动调整")
            repair_state = {**state,"keep_current_plan":False,"change":{**state.get("change",{}),
                "dates":sorted(set(review_repair_dates(repairable,itinerary))-preserved_dates)}}
            state.update(await self.repair(repair_state))
            # Repair dispatch owns targeted ticket queries; do not refresh the whole trip here.
            no_progress = digest([route_structure(state["itinerary"]),sorted(evidence_ids(state["itinerary"]))]) == before
        actual = {day["date"]:day for day in itinerary["days"]}
        has_visits = any(a["kind"] == "visit" for day in itinerary["days"] for a in day["activities"])
        if has_visits:
            for day in candidate["days"]:
                day["stop_ids"] = [a["id"] for a in actual[day["date"]]["activities"] if a["kind"] == "visit"]
        candidate.update(draft=copy.deepcopy(itinerary),draft_revision=revision,plan_hash=plan_fingerprint(itinerary),
                         input_hash=input_hash(state["requirements"],state["selected_source_ids"],state["stops"],self.selected_sources(state["trip_id"],state["selected_source_ids"])),
                         review=report,confirmation_blockers=list(dict.fromkeys([
                             *confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"]),
                             *review_blockers(report,self.audit_state(state),itinerary,self.selected_sources(state["trip_id"],state["selected_source_ids"]),
                                 self.store.review_queries(state["trip_id"],list(evidence_ids(itinerary))))])))
        candidate["feasibility"] = self.feasibility(itinerary)
        decision = decision or state.get("decision") or resolved_decision or report.get("question")
        # Facts and tool failures stay visible gaps; only genuine user tradeoffs become questions.
        decision_findings = [finding for finding in report["findings"] if finding["action"] == "decision" and finding["severity"] == "blocker"]
        if not decision and decision_findings:
            decision = (await self.route_agent.review(state,decision_findings)).question.model_dump(mode="json")
        return {"candidates":[candidate],"previews":{candidate["id"]:{"itinerary":copy.deepcopy(itinerary),"queried_at":now()}},
                "draft_revision":revision,"decision":decision or {},"decision_resume":"assess","selected_route":{},"preview_ready":False,
                "requirements":state["requirements"],"stop_constraints":constraints,"repair_round":state.get("repair_round",0),
                "stops":state["stops"],"weather":state.get("weather",{}),"rollback":rollback,"place_selection":state.get("place_selection",{})}

    @staticmethod
    def feasibility(itinerary):
        return {"queried_at":now(),"budget":itinerary["budget"],"issues":itinerary["issues"],
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
        for day in itinerary.get("days",[]):
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
        checked = check_itinerary(itinerary,state["requirements"])
        itinerary.update(checked)
        itinerary["issues"].extend(duplicate_issues(itinerary,state["requirements"]))
        for day in itinerary["days"]:
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
        itinerary["issues"].extend(resource_context_issues(itinerary,state["requirements"]))
        if state["mode"] == "modify" and state.get("change",{}).get("scopes") == ["full"] and state["locks"]:
            for old_day in state.get("old_itinerary",{}).get("days",[]):
                if old_day["date"] < state["requirements"]["start_date"] or old_day["date"] > state["requirements"]["end_date"] or old_day["city"] not in state["requirements"]["cities"]:
                    if any(a["id"] in state["locks"] for a in old_day["activities"]):
                        itinerary["issues"].append({"code":"locked_date","target":old_day["date"],"message":"锁定活动与新日期或城市冲突，请调整","blocking":True})
        if ablation.disabled("code_checks"):
            itinerary["issues"] = []
        if itinerary.get("personal_map") and plan_fingerprint(itinerary) != plan_fingerprint(state.get("old_itinerary") or state["itinerary"]):
            itinerary.pop("personal_map",None)
        return {"itinerary":itinerary}

    async def repair(self,state):
        self.emit(state,"repairing","正在减少冲突并比较更合适的资源")
        itinerary = canonicalize_itinerary(copy.deepcopy(state["itinerary"]))
        issues = itinerary["issues"]
        changed_hotels, changed_transport = set(),set()
        protected = set(state["locks"]) | set(state.get("change",{}).get("user_selected_ids",[])) | set(state.get("stop_constraints",{}).get("required",[]))
        protected |= {entry["id"] for entry in itinerary.get("hotels",[]) if entry.get("selected_by_user")}
        protected_keys = {canonical_key(state["stops"][identity]) for identity in protected if identity in state.get("stops",{})}
        protected |= {activity["id"] for day in itinerary["days"] for activity in day["activities"] if activity["kind"] == "visit" and
                      canonical_key(activity,day["city"]) in protected_keys}
        seen, duplicate_dates = {},set()
        for day in itinerary["days"]:
            retained = []
            for activity in day["activities"]:
                if activity["kind"] == "visit":
                    key = canonical_key(activity,day["city"])
                    if key in seen and not repeat_allowed(activity,day,"visit",state["requirements"]):
                        scope_dates = state.get("change",{}).get("dates",[])
                        if (not scope_dates or day["date"] in scope_dates) and activity["id"] not in state.get("locks",[]) and activity["id"] not in state.get("change",{}).get("user_selected_ids",[]):
                            duplicate_dates.add(day["date"])
                            day["gaps"].append("已合并重复地点："+activity["name"]+"，保留首次安排与攻略来源")
                            continue
                    seen.setdefault(key,activity)
                retained.append(activity)
            day["activities"] = retained
        if duplicate_dates:
            await self.refresh_day_routes(state,itinerary,duplicate_dates)
            return {"itinerary":itinerary,"repair_round":state.get("repair_round",0)+1}
        time_conflict = any(issue["code"] in {"arrival_conflict","departure_conflict","tail_conflict","late_day","transport_time","resource_constraint"} for issue in issues)
        if time_conflict:
            cities = {day["city"] for day in state["selected_route"]["days"]}
            for entry in itinerary.get("transport",[]):
                if entry["id"] in protected or not (self.affected(state,"transport",entry["context"]["origin"]) or self.affected(state,"transport",entry["context"]["destination"])):
                    continue
                context = entry["context"]
                def usable_score(option):
                    if option.get("constraint_issues"):
                        return float("inf")
                    dep,arr = timestamp(option.get("departure"),context["date"]),timestamp(option.get("arrival"),context["date"])
                    if not dep or not arr or arr <= dep:
                        return float("inf")
                    return (arr.hour*60+arr.minute if context["destination"] in cities else 0)-(dep.hour*60+dep.minute if context["origin"] in cities else 0)
                alternatives = [option for option in entry.get("options",[]) if not option.get("constraint_issues")]
                if alternatives:
                    option = min(alternatives,key=usable_score)
                    selected = entry.get("selected")
                    if usable_score(option) != float("inf") and (selected is None or usable_score(option) < usable_score(selected)):
                        entry["selected"] = option
                        entry["buffer_minutes"] = station_buffer(state["requirements"],option["kind"],context["date"])
                        entry["reason"] = "时间检查后选择可用游玩时间更充足的班次，重新计算接驳"
                        changed_transport.update((context["origin"],context["destination"]))
        if any(issue["code"] == "walking_limit" for issue in issues):
            for entry in itinerary.get("hotels",[]):
                if not entry.get("selected_by_user") or entry["id"] in protected or not self.affected(state,"hotel",entry["city"]):
                    continue
                options = [option for option in entry.get("options",[]) if not option.get("constraint_issues") and option.get("commute_minutes") is not None]
                selected = entry.get("selected") or {}
                if options:
                    option = min(options,key=lambda item:(item.get("walking_meters",0),item["commute_minutes"]))
                    if option.get("walking_meters",0) < selected.get("walking_meters",float("inf")):
                        entry["selected"] = option; entry["reason"] = "步行检查后选择接驳负担更小的住宿"
                        changed_hotels.add(entry["city"])
        if any(i["code"] == "over_budget" for i in issues):
            for group in ("hotels","transport"):
                for entry in itinerary.get(group,[]):
                    if entry["id"] in protected:
                        continue
                    if group == "hotels" and not entry.get("selected_by_user"):
                        continue
                    if group == "hotels" and not self.affected(state,"hotel",entry["city"]):
                        continue
                    if group == "transport" and not (self.affected(state,"transport",entry["context"]["origin"]) or self.affected(state,"transport",entry["context"]["destination"])):
                        continue
                    costs = [(resource_cost(o,state["requirements"]),o) for o in entry.get("options",[]) if not o.get("constraint_issues")]
                    costs = [(c,o) for c,o in costs if c is not None]
                    if costs:
                        cost, option = min(costs,key=lambda pair:pair[0])
                        old_cost = resource_cost(entry.get("selected"),state["requirements"])
                        if old_cost is not None and cost < old_cost:
                            entry["selected"] = option; entry["reason"] = "预算检查后选择费用口径明确的较低报价"
                            if group == "hotels":
                                changed_hotels.add(entry["city"])
                            else:
                                changed_transport.update((entry["context"]["origin"],entry["context"]["destination"]))
        if changed_hotels or changed_transport:
            affected_cities = list(changed_hotels | changed_transport)
            scopes = (["hotel"] if changed_hotels else []) + (["transport"] if changed_transport else [])
            # Resource replacement must also update its dependent routes, including during automatic repair.
            reroute_state = {**state,"itinerary":itinerary,"old_itinerary":copy.deepcopy(itinerary),"mode":"modify",
                             "change":{**state.get("change",{}),"scopes":scopes,"cities":affected_cities,"preserve_cities":state.get("change",{}).get("preserve_cities",[])}}
            rerouted = await self.activities(reroute_state)
            itinerary = rerouted["itinerary"]
            return {"itinerary":itinerary,"repair_round":state.get("repair_round",0)+1}
        feedback = state.get("review_feedback",[])
        if feedback:
            affected_dates = review_repair_dates(feedback,itinerary)
            if state.get("change",{}).get("dates"):
                affected_dates = sorted(set(affected_dates or state["change"]["dates"]) & set(state["change"]["dates"]))
                if not affected_dates:
                    return {"itinerary":itinerary,"repair_round":state.get("repair_round",0)+1}
            resource_groups = {group for group in ("transport","hotels") for finding in feedback
                               if finding["owner"] == "resource" and (finding.get("field_path") == "/"+group or finding.get("field_path","").startswith("/"+group+"/"))}
            resource_feedback = bool(resource_groups)
            reroute = {**state,"itinerary":itinerary,"old_itinerary":copy.deepcopy(itinerary),"mode":"modify",
                       "repair_only":True,"assessment_dates":affected_dates,
                       "change":{**state.get("change",{}),"scopes":["activity",*(["transport"] if "transport" in resource_groups else []),*(["hotel"] if "hotels" in resource_groups else [])],
                                 "dates":affected_dates,"explanation":"按独立检验发现调整相关安排"}}
            ticket_ids,mutable,whole_days = set(),set(),set()
            for item in feedback:
                match = re.match(r"^/days/(\d+)/activities/(\d+)(?:/|$)",item.get("field_path",""))
                if match and int(match[1]) < len(itinerary["days"]):
                    day = itinerary["days"][int(match[1])]
                    if int(match[2]) < len(day["activities"]):
                        identity = day["activities"][int(match[2])]["id"]
                        mutable.add(identity)
                        if (state.get("repair_round",0) > 0 and not state.get("revision_selected_ids")
                                and item.get("owner") == "route" and item.get("action") == "repair"
                                and re.search(r"时间|时刻|预约|午餐|衔接|排程|重排",item.get("message",""))):
                            # If the target alone cannot fit, release only its immediate neighbours.
                            visits = [activity["id"] for activity in day["activities"] if activity["kind"] == "visit"]
                            if identity in visits:
                                index = visits.index(identity)
                                mutable.update(visits[max(0,index-1):index+2])
                        if item.get("owner") == "resource" and re.search(r"门票|票价|票种|票务",item.get("message","")):
                            ticket_ids.add("ticket:"+identity)
                        continue
                dates = review_repair_dates([item],itinerary)
                targets = {activity["id"] for day in itinerary["days"] if day["date"] in dates
                           for activity in day["activities"] if item.get("target") in {activity.get("id"),activity.get("name")}}
                if targets:
                    mutable.update(targets)
                else:
                    whole_days.update(dates)
            fixed = {activity["id"] for day in itinerary["days"] if day["date"] not in whole_days
                     for activity in day["activities"] if activity["kind"] == "visit" and activity["id"] not in mutable}
            reroute["locks"] = sorted(set(state.get("locks",[])) | fixed)
            if any(finding["action"] == "evidence" for finding in feedback):
                reroute["change"]["refresh_resources"] = True
            if "transport" in resource_groups:
                reroute.update(await self.transport(reroute))
            if "hotels" in resource_groups:
                reroute.update(await self.hotels(reroute))
            if ticket_ids:
                ticket_state = {**reroute,"change":{**reroute["change"],"resource_ids":sorted(ticket_ids),"refresh_resources":True}}
                await self.query_ticket_prices(ticket_state,itinerary,set(affected_dates))
                if all(item["action"] == "evidence" for item in feedback) and not resource_feedback:
                    return {"itinerary":itinerary,"repair_round":state.get("repair_round",0)+1}
            if any(finding["action"] == "repair" for finding in feedback):
                reroute["change"].pop("refresh_resources",None)
            reroute.update(await self.activities(reroute))
            return {"itinerary":reroute["itinerary"],"stops":reroute["stops"],"weather":reroute.get("weather",{}),
                    "decision":reroute.get("decision",{}),"place_selection":reroute.get("place_selection",{}),"repair_round":state.get("repair_round",0)+1}
        for issue in issues:
            if issue["code"] in {"arrival_conflict","departure_conflict","overlap","move_gap"} or issue.get("repair") == "drop":
                for day in itinerary["days"]:
                    if day["city"] in state.get("change",{}).get("preserve_cities",[]):
                        continue
                    if not (self.affected(state,"activity",day["city"],day["date"]) or self.affected(state,"hotel",day["city"]) or self.affected(state,"transport",day["city"])):
                        continue
                    day["activities"] = [a for a in day["activities"] if a["id"] != issue["target"] or a["id"] in protected]
                    day["gaps"].append(issue["message"] + "；未锁定的冲突活动已移除")
            if issue["code"] in {"late_day","tail_conflict","walking_limit"}:
                for day in itinerary["days"]:
                    if day["city"] in state.get("change",{}).get("preserve_cities",[]):
                        continue
                    if day["date"] == issue["target"]:
                        if not (self.affected(state,"activity",day["city"],day["date"]) or self.affected(state,"hotel",day["city"]) or self.affected(state,"transport",day["city"])):
                            continue
                        visits = [a for a in day["activities"] if a["kind"] == "visit" and a["id"] not in protected]
                        if visits:
                            day["activities"].remove(visits[-1]); day["gaps"].append("自动减少末项活动，仍需核对安排")
        if state.get("old_itinerary"):
            itinerary = enforce_locks(state["old_itinerary"],itinerary,state["locks"])
        await self.refresh_day_routes(state,itinerary,{day["date"] for day in itinerary["days"] if day["city"] not in state.get("change",{}).get("preserve_cities",[])})
        return {"itinerary":itinerary,"repair_round":state.get("repair_round",0)+1}
