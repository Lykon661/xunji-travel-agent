"""规划工作流的装配点：LangGraph 图定义、执行入口与全部人工确认 gate。

节点实现按职责拆在 workflow/ 与 nodes/ 的 mixin 中，Planner 只负责把它们组合起来；
所有 interrupt() 调用集中在本文件，便于审查“哪里会暂停等待用户”。
"""
import asyncio
import copy
import logging
import re
import traceback
from datetime import timedelta
from functools import cached_property
from pathlib import Path
from time import perf_counter
from weakref import WeakValueDictionary

from .adapters import Amap, Crawler, FlyAI, Models, ToolError
from .models import Candidate, Requirements, RequirementUpdate, DECISION_REQUIREMENT_FIELDS, DecisionResolution
from .rules import validate_candidate
from .store import Conflict, now, dump
from .routing import coordinates
from .agents import GuideAgent, ResourceAgent, RouteAgent, ReviewerAgent
from .agents.route import resolve_stop_id
from .agents.runtime import digest
from .decisions import apply_walking_decision, confirmation_blockers, walking_question
from .dining import is_dining
from .places import canonicalize_stops, plan_fingerprint
from .nodes.candidates import CandidateNodes
from .nodes.guides import GuideNodes
from .nodes.locations import LocationNodes
from .nodes.resources import ResourceNodes
from .nodes.review_loop import ReviewLoopNodes
from .nodes.scheduling import SchedulingNodes
from .workflow.state import NODE_STAGES, TripState
from .workflow.submit import SubmitWorkflow

__all__ = ["NODE_STAGES", "Planner", "TripState", "interrupt"]


def interrupt(value):
    from langgraph.types import interrupt as graph_interrupt
    return graph_interrupt(value)


def error_leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from error_leaves(child)
    else:
        yield error


class Planner(SubmitWorkflow, GuideNodes, CandidateNodes, ResourceNodes, LocationNodes, SchedulingNodes, ReviewLoopNodes):
    def __init__(self, store, checkpointer):
        self.store = store
        self.models = Models()
        self.crawler = Crawler(store)
        self.flyai = FlyAI(store)
        self.amap = Amap(store)
        self.guide_agent = GuideAgent(self.models,store)
        self.route_agent = RouteAgent(self.models,store)
        self.resource_agent = ResourceAgent(self.models,store,self.flyai)
        self.reviewer_agent = ReviewerAgent(self.models,store)
        self.tasks: dict[str,asyncio.Task] = {}
        self.gates = WeakValueDictionary()
        self._checkpointer = checkpointer

    @cached_property
    def graph(self):
        started = perf_counter()
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(TripState)
        for name in ("coordinate","requirements_gate","guides_gate","prepare_guides","guide_analysis","decision_gate","confirm_route","review_result","review_gate","planning_failed","interview","interview_gate","discover","assess","selection_gate","revise_candidate","transport","hotels","activities","tickets","check","save"):
            graph.add_node(name,getattr(self,name))
        graph.add_edge(START,"coordinate")
        graph.add_conditional_edges("coordinate",self.after_coordinate,{"requirements":"requirements_gate","guides":"guides_gate","resources":"transport"})
        graph.add_edge("requirements_gate","coordinate")
        graph.add_edge("guides_gate","prepare_guides")
        graph.add_conditional_edges("prepare_guides",lambda s:"guide_analysis" if s.get("stops") else "selection_gate")
        graph.add_conditional_edges("guide_analysis",lambda s:"decision_gate" if s.get("decision") else "discover")
        graph.add_conditional_edges("decision_gate",lambda s:s["decision_resume"],{"guide_analysis":"guide_analysis","discover":"discover","assess":"assess","review_result":"review_result"})
        graph.add_edge("interview","interview_gate")
        graph.add_conditional_edges("interview_gate",lambda s:"discover" if s.get("interview",{}).get("confirmed") else "interview")
        graph.add_conditional_edges("discover",lambda s:"decision_gate" if s.get("decision") else "assess")
        graph.add_conditional_edges("assess",lambda s:"decision_gate" if s.get("decision") else "review_gate")
        graph.add_conditional_edges("review_gate",self.after_review_gate)
        graph.add_edge("planning_failed","decision_gate")
        graph.add_conditional_edges("selection_gate",lambda s:"revise" if s.get("route_edit") else "guides" if not s.get("selected_route") else "confirm",{"revise":"revise_candidate","guides":"guides_gate","confirm":"confirm_route"})
        graph.add_edge("confirm_route","save")
        graph.add_edge("revise_candidate","assess")
        graph.add_edge("transport","hotels")
        graph.add_edge("hotels","activities")
        graph.add_edge("activities","tickets")
        graph.add_edge("tickets","check")
        # 规则检查后一律交给独立审查；修复由 assess_result 在审查结论后按共享预算调用 repair()，
        # 不再经由 check -> repair 的图边（该边历史上恒不触发，已移除）。
        graph.add_edge("check","review_result")
        graph.add_conditional_edges("review_result",lambda s:END if s.get("factual_refresh_completed") else "decision_gate" if s.get("decision") else "review_gate")
        graph.add_edge("save",END)
        compiled = graph.compile(checkpointer=self._checkpointer)
        logging.getLogger("uvicorn.error").info("[startup] planning graph initialized on demand: %.3fs",perf_counter()-started)
        return compiled

    def gate(self, trip_id: str) -> asyncio.Lock:
        return self.gates.setdefault(trip_id,asyncio.Lock())

    def emit(self, state: TripState, stage: str, message: str):
        if self.store.trip(state["trip_id"])["active_run"] != state["run_id"]:
            raise Conflict("旧规划任务已失效，结果未覆盖当前草稿")
        self.store.event(state["run_id"],"progress",message,stage=stage)
        trip = self.store.trip(state["trip_id"])
        doc = trip["document"]; doc.update(stage=stage,progress_stage=stage,progress_message=message,pending=None)
        self.store.update(state["trip_id"],doc)

    async def decision_gate(self,state):
        question = state["decision"]
        history = state.get("decision_history",[])
        if history and walking_question(question) and not state.get("decision_reopened"):
            last = history[-1]
            previous = (last.get("questions") or [{}])[0]
            text = " ".join(last.get("answers",{}).values())+" "+last.get("note","")
            req,keep = apply_walking_decision(state["requirements"],previous,text)
            affected = question.get("affected_dates",[])
            if walking_question(previous) and keep and affected and set(affected) <= set(req.get("walking_limit_exempt_dates",[])):
                self.emit(state,"assessing","已按你上一条保持规划的指令处理步行限制，不再重复追问")
                return {"requirements":req,"decision":{},"decision_resume":"assess","keep_current_plan":True,
                        "assessment_dates":affected,"assessment_base":state["candidates"][0]["id"] if state.get("candidates") else ""}
        token = digest({"question":question,"history":state.get("decision_history",[]),"generation":state["generation"],"revision":state.get("draft_revision",0)})[:40]
        answer = interrupt({"kind":"decision","message":question["question"],"questions":[question],"round_id":token,
                            "round":len(state.get("decision_history",[]))+1,"summary":question["why_needed"],
                            "origin_step":"interview","resume_stage":state["decision_resume"],"draft_revision":state.get("draft_revision",0)})
        values = answer.get("answers",{})
        if answer.get("round_id") != token or answer.get("draft_revision") != state.get("draft_revision",0) or set(values) != {question["id"]} or not values[question["id"]].strip() or answer.get("confirm"):
            raise Conflict("问题或草稿已变化，请只回答当前这一题")
        selection = state.get("place_selection") or {}
        if selection.get("kind") == "hub" and selection.get("question_id") == question["id"]:
            poi_id = selection["options"].get(values[question["id"]].strip())
            if not poi_id:
                raise Conflict("请从列出的航站楼或进出站口中选择一个")
            summary = "已选择"+values[question["id"]]+"作为"+selection["name"]+"的接驳位置，重新查询接驳路线"
            history = [*history,{"questions":[question],"answers":values,"note":answer.get("note",""),"summary":summary,
                                 "hub_selection":{"city":selection["city"],"name":selection["name"],"poi_id":poi_id}}]
            # Re-run the full assessment so transfers, day start times and reviews all use the chosen hub.
            return {"decision_history":history,"decision":{},"place_selection":{},"decision_reopened":False,
                    "assessment_dates":[],"assessment_base":"","repair_only":False,"repair_round":0,"review_feedback":[],
                    "draft_revision":state.get("draft_revision",0)+1,"keep_current_plan":False,
                    "interview":{"summary":summary,"questions":[],"history":history,"confirmed":False}}
        poi_id = ((selection.get("options") or {}).get(values[question["id"]])
                  if selection.get("question_id") == question["id"] and not answer.get("note","").strip() else None)
        if poi_id:
            stops = copy.deepcopy(state["stops"])
            stop = stops[selection["stop_id"]]
            allowed = {item["id"] for item in stop.get("map_candidates",[])}
            if poi_id not in allowed:
                raise Conflict("地点候选已变化，请重新选择当前候选")
            async def selected_poi(candidates):
                return (poi_id,"用户在需求追问中明确选择该高德POI") if any(item.get("id") == poi_id for item in candidates) else (None,"已选POI不在当前高德候选中")
            try:
                place = await self.amap.place(state["trip_id"],stop["city"],stop["name"],selector=selected_poi)
                place = place if place and place.get("id") == poi_id and coordinates(place) else None
                stop["map_issue"] = "" if place else "已保留你选择的地点，但高德尚未返回该POI的可用坐标"
            except ToolError as exc:
                place = None
                stop["map_issue"] = str(exc)
            stop.update(map_place=place,map_selected_id=poi_id)
            summary = "已选择"+values[question["id"]]+("，继续使用高德定位规划" if place else "，将继续补查高德坐标")
            history = [*history,{"questions":[question],"answers":values,"note":answer.get("note",""),"summary":summary,
                "place_selection":{"stop_id":selection["stop_id"],"city":stop["city"],"name":stop["name"],"poi_id":poi_id,
                                   "preferences":state["requirements"].get("preferences","")}}]
            candidates,previews = copy.deepcopy(state.get("candidates",[])),copy.deepcopy(state.get("previews",{}))
            for candidate in candidates:
                draft = candidate.get("draft") or {}
                for day in draft.get("days",[]):
                    for activity in day.get("activities",[]):
                        if activity["id"] == selection["stop_id"]:
                            activity["map_place"] = copy.deepcopy(place)
                if draft.get("days"):
                    candidate["review"] = None
                    previews[candidate["id"]] = {"itinerary":copy.deepcopy(draft)}
            return {"stops":canonicalize_stops(stops),"candidates":candidates,"previews":previews,
                    "decision_history":history,"decision":{},"place_selection":{},"decision_reopened":False,
                    "assessment_dates":question.get("affected_dates",[]),"assessment_base":candidates[0]["id"] if candidates else "",
                    "repair_only":any(candidate.get("draft",{}).get("days") for candidate in candidates),
                    "repair_round":0,"review_feedback":[],"draft_revision":state.get("draft_revision",0)+1,
                    "keep_current_plan":False,"interview":{"summary":summary,"questions":[],"history":history,"confirmed":False}}
        instruction = (
            "将用户当前问题的明确回答转成约束更新。requirements只填用户在这次回答中明确授权改变的字段；"
            "推荐选项不是回答。required_ids与excluded_ids仅包含现有景点ID，代表这次明确设为必去或排除的景点。"
            "最新明确指令优先于默认规则与历史偏好，合并preferences时删除被本次指令推翻的旧限制。"
            "在步行超限取舍问题中，用户说保持规划、不要删点或接受超限，就是授权对受影响日期豁免步行上限："
            "walking_limit_action=relax，keep_current_plan=true，不要求用户再给一个数字，不重复要求削减。"
            "用户要求恢复步行限制则walking_limit_action=restore；无相关授权为unchanged。"
            "keep_current_plan仅在用户明确要求保留现有路线时为true。"
            "用户说你决定时在已有约束内做选择并说明。不要虚构地点、事实或改变未授权的日期、城市及人数。"
            '必须返回实际结果对象，六个字段直接放在根对象：{"requirements":{},"required_ids":[],"excluded_ids":[],"explanation":"概括用户本次回答","walking_limit_action":"unchanged","keep_current_plan":false}。'
            "这是字段形状示例，必须根据当前回答填写真实值；没有变更的requirements返回{}，没有增删景点时对应数组返回[]。"
            "requirements的键只能使用allowed_requirement_fields；required_ids和excluded_ids只能使用allowed_stop_ids中的完整ID，不能用景点名、POI ID或canonical_place_id。"
            "用户允许依据真实公共交通结果自动取舍时，把该条件合并到preferences，保留原偏好，不新增判断策略等自定义字段；"
            "尚未查询成立的条件不能提前变成无条件必去或排除，此时required_ids与excluded_ids返回[]。"
            "不要返回字段定义、JSON Schema、问题对象或properties包装，不要把完整原需求复制到requirements。")
        available = {identity:stop for identity,stop in state["stops"].items() if not is_dining(stop)}
        payload = {"requirements":state["requirements"],"question":question,"answer":values,"note":answer.get("note",""),
                   "available_stops":available,"constraints":state.get("stop_constraints",{}),
                   "allowed_requirement_fields":sorted(DECISION_REQUIREMENT_FIELDS),"allowed_stop_ids":sorted(available)}
        for attempt in range(2):
            resolution = await self.models.ask(DecisionResolution,instruction,payload)
            resolution.required_ids = [resolve_stop_id(identity,available) or identity for identity in resolution.required_ids]
            resolution.excluded_ids = [resolve_stop_id(identity,available) or identity for identity in resolution.excluded_ids]
            issues = []
            unknown = sorted(set(resolution.requirements)-DECISION_REQUIREMENT_FIELDS)
            if unknown:
                issues.append({"field":"requirements","unknown_fields":unknown})
            for field in ("required_ids","excluded_ids"):
                unknown = sorted(set(getattr(resolution,field))-set(available))
                if unknown:
                    issues.append({"field":field,"unknown_ids":unknown})
            overlap = sorted(set(resolution.required_ids) & set(resolution.excluded_ids))
            if overlap:
                issues.append({"field":"required_ids/excluded_ids","conflicting_ids":overlap})
            if not issues:
                break
            # Log only the rejected keys/IDs, never the user's answer or model field values.
            diagnostic = [{key:([str(value)[:120] for value in item[:5]] if isinstance(item,list) else item)
                           for key,item in issue.items()} for issue in issues]
            logging.getLogger(__name__).warning("Decision scope rejected: run=%s attempt=%s issues=%s",
                                               state.get("run_id"),attempt+1,dump(diagnostic))
            if attempt:
                detail = "；".join(issue["field"]+"："+"、".join(
                    issue.get("unknown_fields",issue.get("unknown_ids",issue.get("conflicting_ids",[])))) for issue in diagnostic)
                raise ToolError("decision_resolution","回答解析自动纠正一次后仍不符合允许范围（"+detail+"），原草稿已保留")
            self.emit(state,"decision","回答解析中出现越界字段或景点，正在按允许范围自动纠正一次")
            payload = {**payload,"previous_resolution":resolution.model_dump(mode="json"),"validation_errors":issues}
            instruction += ("上一条解析未通过范围校验，请按validation_errors纠正，保留本次用户的真实意图。"
                "只按原回答填写允许的变更；未知ID必须对照available_stops的完整键，不得猜测映射。"
                "不复制未获授权的原需求字段，不为消除错误而删除用户明确要求；条件式授权用preferences表达。")
        changes = dict(resolution.requirements)
        answer_text = " ".join(values.values())+" "+answer.get("note","")
        for permission in changes.get("repeat_exceptions",[]):
            if permission.get("place_id") not in state["stops"] or not permission.get("authorization") or permission["authorization"] not in answer_text:
                raise ToolError("decision_resolution","重复安排必须引用现有地点及本次用户明确回答，未采用推测授权")
        for field in ("budget","max_walk_per_day","max_transport_hours"):
            value = changes.get(field)
            if value is not None and value != state["requirements"].get(field):
                explicit = [float(number) for number in re.findall(r"\d+(?:\.\d+)?",answer_text)]
                try:
                    numeric = float(value)
                except (TypeError,ValueError) as exc:
                    raise ToolError("decision_resolution","回答中没有明确可用的新额度或上限，请补充具体数值") from exc
                if numeric not in explicit and not (field == "max_walk_per_day" and numeric/1000 in explicit and re.search(r"公里|千米|km",answer_text,re.I)):
                    changes.pop(field)
        req = Requirements.model_validate({**state["requirements"],**changes}).model_dump(mode="json")
        if "max_walk_per_day" in changes:
            req["walking_limit_exempt_dates"] = []
        action = "restore" if "max_walk_per_day" in changes else resolution.walking_limit_action
        req,keep = apply_walking_decision(req,question,answer_text,action,resolution.keep_current_plan)
        explanation = resolution.explanation.strip() or "已记录你的回答："+answer_text.strip()[:1000]
        if req.get("walking_limit_exempt_dates") != state["requirements"].get("walking_limit_exempt_dates",[]):
            explanation = "按你的最新指令更新步行限制；"+("保留当前规划，受影响日期不再因步行上限删点或追问" if keep else "已更新受影响日期的步行限制")
        constraints = state.get("stop_constraints",{})
        required = (set(constraints.get("required",[]))-set(resolution.excluded_ids)) | set(resolution.required_ids)
        required = {identity for identity in required if not is_dining(state["stops"][identity])}
        excluded = (set(constraints.get("excluded",[]))-set(resolution.required_ids)) | set(resolution.excluded_ids)
        req.update(required_stop_ids=sorted(required),excluded_stop_ids=sorted(excluded))
        history = [*history,{"questions":[question],"answers":values,"note":answer.get("note",""),"summary":explanation,
                             "walking_limit_exempt_dates":req.get("walking_limit_exempt_dates",[]),"keep_current_plan":keep}]
        dates = question.get("affected_dates",[])
        base = state.get("candidates",[{}])[0].get("id") if state.get("candidates") else ""
        resume = state["decision_resume"]
        global_changes = set(changes)-{"preferences","hotel_areas"}
        global_changes |= {"preferences"} if "preferences" in changes and re.search(r"全程|所有天|每天|整趟",answer_text) else set()
        if global_changes and resume in {"assess","review_result"}:
            dates = [day["date"] for day in state.get("candidates",[{}])[0].get("days",[])]
        elif "hotel_areas" in changes and state.get("candidates"):
            cities = {city for city in state["requirements"]["cities"] if req.get("hotel_areas",{}).get(city) != state["requirements"].get("hotel_areas",{}).get(city)}
            dates = sorted(set(dates) | {day["date"] for day in state["candidates"][0]["days"] if day["city"] in cities})
        candidates = copy.deepcopy(state.get("candidates",[]))
        if candidates:
            used = {identity for day in candidates[0]["days"] for identity in day["stop_ids"]}
            for identity in required-used:
                targets = [day for day in candidates[0]["days"] if (not dates or day["date"] in dates) and day["city"] == state["stops"][identity]["city"]]
                if targets:
                    targets[0]["stop_ids"].append(identity)
                    targets[0]["adjustment"] = "根据你回答的必去选择重新安排"
                else:
                    resume = "discover"
        return {"requirements":req,"candidates":candidates,"stop_constraints":{"required":sorted(required),"excluded":sorted(excluded)},"decision_history":history,
                "decision":{},"decision_reopened":False,"assessment_dates":dates,"assessment_base":base,"draft_revision":state.get("draft_revision",0)+1,
                "repair_round":0,"review_feedback":[],"place_selection":{},
                "keep_current_plan":keep,
                "decision_resume":"assess" if resume == "review_result" else resume,
                "interview":{"summary":explanation,"questions":[],"history":history,"confirmed":False}}

    async def confirm_route(self,state):
        itinerary = copy.deepcopy(state["selected_route"]["draft"])
        blockers = confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"])
        if blockers:
            raise Conflict("路线暂不能确认："+"；".join(blockers))
        if plan_fingerprint(itinerary) != state["selected_route"]["plan_hash"]:
            raise Conflict("待确认路线内容已变化，请刷新")
        self.assert_review(state,state["selected_route"].get("review"),itinerary)
        return {"itinerary":itinerary,"route_confirmed":True}

    async def execute(self, run_id: str, invocation):
        run = self.store.run(run_id)
        config = {"configurable":{"thread_id":run["thread_id"]},"recursion_limit":60}
        started = perf_counter()
        try:
            async with self.amap.batch():
                result = await self.graph.ainvoke(invocation,config)
            snapshot = await self.graph.aget_state(config)
            if self.store.trip(run["trip_id"])["active_run"] != run_id:
                return
            if snapshot.interrupts:
                pending = snapshot.interrupts[0].value
                trip = self.store.trip(run["trip_id"]); doc = trip["document"]
                doc.update(pending=pending,stage="waiting",requirements=result.get("requirements",doc["requirements"]),
                           selected_source_ids=result.get("selected_source_ids",[]),candidates=result.get("candidates",[]),warnings=result.get("warnings",[]),interview=result.get("interview") or None,
                           draft_revision=result.get("draft_revision",doc.get("draft_revision",0)),
                           stop_constraints=result.get("stop_constraints",{}),decision_history=result.get("decision_history",[]),rollback=result.get("rollback",{}))
                doc["workflow_step"] = {"requirements":"requirements","guides":"guides","sources":"guides","interview":"interview","interview_confirmation":"interview","decision":"interview","selection":"routes"}.get(pending.get("kind"),doc.get("workflow_step","interview"))
                if pending.get("kind") == "selection" and doc.get("route_chat") and doc["route_chat"][-1]["role"] == "user":
                    candidate = (result.get("candidates") or [{}])[0]
                    reply = result.get("revision_reply") or candidate.get("reason") or "行程已更新，请查看左侧路线。"
                    doc["route_chat"].append({"role":"assistant","content":reply,"time":now()})
                if pending.get("kind") in {"guides", "sources", "interview", "interview_confirmation", "selection"}:
                    doc.update(itinerary_outdated=bool(doc.get("itinerary")), selected_route={})
                self.store.update(run["trip_id"],doc)
                self.store.finish_run(run_id,"waiting",{"pending":pending})
                self.store.event(run_id,"waiting",pending["message"],pending=pending)
                logging.getLogger(__name__).info("Run %s waiting for %s after %.1fs",run_id,pending.get("kind"),perf_counter()-started)
            else:
                self.store.finish_run(run_id,"completed",{"version":self.store.trip(run["trip_id"])["version"]})
                self.store.event(run_id,"completed","已确认并保存这份每日行程")
                logging.getLogger(__name__).info("Run %s completed after %.1fs",run_id,perf_counter()-started)
        except asyncio.CancelledError:
            if self.store.trip(run["trip_id"])["active_run"] != run_id:
                raise
            self.store.finish_run(run_id,"recoverable",{"message":"服务停止，可以继续本次任务"})
            trip = self.store.trip(run["trip_id"]); doc = trip["document"]
            doc.setdefault("progress_stage", doc["stage"])
            doc["stage"] = "recoverable"
            self.store.update(run["trip_id"],doc)
            raise
        except Exception as exc:
            try:
                if self.store.trip(run["trip_id"])["active_run"] != run_id:
                    return
            except KeyError:
                return
            failures = list(error_leaves(exc))
            known = next((error for error in failures if isinstance(error,ToolError)),None)
            if known is None:
                known = next((error for error in failures if isinstance(error,Conflict)),None)
            if known is None and isinstance(exc,ValueError):
                known = exc
            message = str(known) if known is not None else "任务中断，已有资料已保留，请继续本次任务或查看 backend/data/server.log"
            code = known.code if isinstance(known,ToolError) else "run_failed"
            # Never include provider responses or secrets in browser-visible errors.
            for error in failures:
                frames = traceback.extract_tb(error.__traceback__)
                location = " -> ".join(f"{Path(frame.filename).name}:{frame.lineno}({frame.name})" for frame in frames)
                logging.getLogger(__name__).error("Run %s failed after %.1fs (%s; code=%s) at %s",run_id,perf_counter()-started,
                    type(error).__name__,getattr(error,"code","run_failed"),location or "no traceback")
            trip = self.store.trip(run["trip_id"]); doc = trip["document"]
            doc.setdefault("progress_stage", doc["stage"])
            doc["stage"] = "failed"
            # Keep completed interview rounds in sync with the checkpoint for retries.
            try:
                snapshot = await self.graph.aget_state(config)
                if snapshot.next:
                    doc["progress_stage"] = NODE_STAGES.get(snapshot.next[0],doc["progress_stage"])
                if snapshot.values:
                    doc.update(requirements=snapshot.values.get("requirements",doc["requirements"]),
                               interview=snapshot.values.get("interview") or doc.get("interview"),
                               candidates=snapshot.values.get("candidates",doc.get("candidates",[])))
            except Exception:
                logging.getLogger(__name__).error("Could not read checkpoint for failed run %s",run_id)
            backup = doc.get("round_backup")
            if backup and not isinstance(known,Conflict) and code != "planning_repair_incomplete":
                candidate = copy.deepcopy(backup["candidate"])
                candidate["draft"] = copy.deepcopy(backup["itinerary"])
                candidate["confirmation_blockers"] = ["本轮处理失败，已保留上一轮内容；请重新审查后再确认"]
                if candidate.get("review"):
                    candidate["review"]["current"] = False
                doc.update(candidates=[candidate],draft_revision=candidate.get("draft_revision",doc.get("draft_revision",0)),
                    rollback={"reason":message,"time":now(),"message":"本轮任务失败，已自动恢复上一轮候选内容；没有采用失败结果。"})
                message += "；上一轮候选已恢复"
            if code == "planning_repair_incomplete":
                candidate = (doc.get("candidates") or [{}])[0]
                doc.update(pending=None,workflow_step="routes",selected_route={},
                    draft_revision=candidate.get("draft_revision",doc.get("draft_revision",0)))
            self.store.update(run["trip_id"],doc)
            self.store.finish_run(run_id,"failed",{"code":code,"message":message,"stage":doc["progress_stage"]})
            self.store.event(run_id,"error",message,code=code,stage=doc["progress_stage"])

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def requirements_gate(self,state):
        missing = Requirements.model_validate(state["requirements"]).missing()
        answer = interrupt({"kind":"requirements","missing":missing,"message":"请补充：" + "、".join(missing)})
        req = Requirements.model_validate({**state["requirements"],**answer.get("requirements",{})})
        return {"requirements":req.model_dump(mode="json")}

    async def guides_gate(self, state):
        source_ids = state.get("selected_source_ids", [])
        if not source_ids:
            answer = interrupt({"kind":"guides","message":"请先勾选一篇或多篇参考攻略，再生成候选路线"})
            source_ids = answer.get("selected_source_ids", [])
        if not source_ids:
            raise ValueError("至少选择一篇参考攻略后才能生成候选路线")
        self.selected_sources(state["trip_id"], source_ids)
        return {"selected_source_ids":source_ids}

    async def interview_gate(self,state):
        interview = state["interview"]
        questions = interview["questions"][:1]
        answer = interrupt({"kind":"interview" if questions else "interview_confirmation",
            "message":"请回答当前这一题，提交后再继续追问" if questions else "请确认需求总结，确认后生成候选路线",
            "round_id":interview["round_id"],"round":interview["round"],"questions":questions,"summary":interview["summary"]})
        if answer.get("round_id") != interview["round_id"]:
            raise ValueError("追问轮次已改变，请刷新后回答")
        if questions and (answer.get("confirm") or set(answer.get("answers",{})) != {questions[0]["id"]} or not answer["answers"][questions[0]["id"]].strip()):
            raise ValueError("请只提交当前问题的回答")
        if not questions and answer.get("confirm"):
            return {"interview":{**interview,"confirmed":True}}
        history = [*interview["history"],{"questions":questions,"summary":interview["summary"],
                   "answers":answer.get("answers",{}),"note":answer.get("note","")}]
        update = await self.models.ask(RequirementUpdate,
            "根据用户当前这一题的回答补充旅行偏好，只允许返回preferences、transport、local_transport、bed_type、allow_taxi、earliest_departure、latest_arrival、latest_return、direct_only、max_transport_hours、max_walk_per_day、flight_cabin、train_seat、hotel_type、hotel_stars字段。"
            "local_transport表示市内出行：公交地铁为public，只步行为walking，自驾为driving，骑行为bicycling；明确允许打车才填allow_taxi=true，与跨城transport分开。时间为HH:MM。"
            "preferences合并保留历史明确偏好，明确修正的偏好用新值替换，不把推荐选项当成回答。"
            "不改变日期、城市、人数、房间数或预算。不明确的字段不填写。",
            {"current":state["requirements"],"questions":questions,"answers":answer.get("answers",{}),"note":answer.get("note","")})
        changes = {k:v for k,v in update.requirements.items() if k in {"preferences","transport","local_transport","bed_type","allow_taxi","earliest_departure","latest_arrival","latest_return","direct_only","max_transport_hours","max_walk_per_day","flight_cabin","train_seat","hotel_type","hotel_stars"}}
        if re.search(r"(?:不|不要|不想|不能|禁止|拒绝)(?:乘坐|坐)?(?:打车|出租车|网约车)",str(answer)):
            changes["allow_taxi"] = False
        req = Requirements.model_validate({**state["requirements"],**changes}).model_dump(mode="json")
        return {"requirements":req,"interview":{**interview,"history":history,"confirmed":False}}

    async def selection_gate(self,state):
        candidates = state.get("candidates",[])
        if not candidates:
            answer = interrupt({"kind":"sources","message":"所选攻略未形成满足日期和来源约束的路线，请调整选择或补充正文图片后重新生成"})
            return {"requirements":answer.get("requirements",state["requirements"]),"selected_source_ids":answer.get("selected_source_ids",state.get("selected_source_ids",[])),"selected_route":{}}
        blocked = bool(candidates[0].get("confirmation_blockers")) or not candidates[0].get("review") or candidates[0]["review"].get("status") not in {"pass","pass_with_warnings"}
        answer = interrupt({"kind":"selection","message":"尚未形成可确认路线，已保留未通过草稿；请修复冲突或补齐依据后重新审查" if blocked else
                            "完整候选已通过独立检验，请确认或修改安排"})
        if answer.get("route_edit"):
            return {"route_edit":answer["route_edit"],"selected_route":{}}
        if answer.get("retry_sources"):
            return {"selected_route":{},"requirements":answer.get("requirements",state["requirements"]),"selected_source_ids":answer.get("selected_source_ids",state.get("selected_source_ids",[]))}
        selected = next((c for c in candidates if c["id"] == answer.get("candidate_id")),None)
        if not selected:
            raise ValueError("所选路线不存在，请刷新后重新选择")
        validate_candidate(Candidate.model_validate(selected), Requirements.model_validate(state["requirements"]), state["stops"])
        if not selected.get("draft") or selected.get("confirmation_blockers"):
            raise Conflict("候选路线缺少完整地图依据或存在冲突，请修改或继续查询后再确认")
        self.assert_review(state,selected.get("review"),selected["draft"])
        if answer.get("draft_revision") != selected.get("draft_revision") or answer.get("plan_hash") != selected.get("plan_hash"):
            raise Conflict("待确认的完整路线版本已变化，请刷新后确认")
        return {"selected_route":selected,"itinerary":copy.deepcopy(selected["draft"]),"preview_ready":True}

    async def save(self,state):
        trip = self.store.trip(state["trip_id"]); doc = trip["document"]
        self.assert_review(state,(state.get("selected_route") or {}).get("review"),state["itinerary"])
        if doc.get("itinerary_generation") == state["generation"] or self.store.generation_version(state["trip_id"],state["generation"]) is not None:
            itinerary = doc.get("itinerary") or state["itinerary"]
            doc["stage"] = "completed" if doc.get("itinerary_saved_version") else "conflicts" if any(i["blocking"] for i in itinerary.get("issues",[])) else "incomplete" if itinerary.get("budget",{}).get("unknown") or itinerary.get("information_unknown") or any(d.get("gaps") for d in itinerary.get("days",[])) else "ready"
            self.store.update(state["trip_id"],doc)
            return {}
        self.emit(state,"saving","正在整理行程草稿和来源记录")
        trip = self.store.trip(state["trip_id"]); doc = trip["document"]
        itinerary = state["itinerary"]
        requirements = Requirements.model_validate(state["requirements"])
        dates = [(requirements.start_date + timedelta(days=i)).isoformat() for i in range((requirements.end_date-requirements.start_date).days+1)]
        if [day["date"] for day in itinerary["days"]] != dates:
            raise ValueError("最终行程日期与用户设置不一致，本次结果未保存；请调整冲突后重新生成")
        incomplete = bool(itinerary["budget"]["unknown"] or itinerary.get("information_unknown")) or any(d.get("gaps") for d in itinerary["days"])
        selected_route = copy.deepcopy(state["selected_route"])
        actual_days = {d["date"]:d for d in itinerary["days"]}
        for day in selected_route["days"]:
            if day["date"] in actual_days:
                day["stop_ids"] = [a["id"] for a in actual_days[day["date"]]["activities"] if a["kind"] == "visit"]
        doc.update(requirements=state["requirements"],candidates=state.get("candidates") or doc.get("candidates",[]),
            selected_route=selected_route,selected_source_ids=state.get("selected_source_ids",[]),itinerary=itinerary,itinerary_outdated=False,warnings=state.get("warnings",[]),pending=None,
            stage="conflicts" if any(i["blocking"] for i in itinerary["issues"]) else ("incomplete" if incomplete else "ready"),
            itinerary_saved_version=None,itinerary_generation=state["generation"],workflow_step="itinerary",
            changes=state.get("change",{}),repair_round=state.get("repair_round",0),
            sources_snapshot=self.store.sources(state["trip_id"]))
        doc.update(draft_revision=state.get("draft_revision",0),stop_constraints=state.get("stop_constraints",{}),
                   decision_history=state.get("decision_history",[]),interview=state.get("interview"),review=selected_route.get("review"))
        doc["messages"].append({"role":"assistant","content":state.get("change",{}).get("explanation") or "已生成行程，请查看来源、预算和待确认信息。","time":now()})
        if trip["version"] != state["base_version"]:
            raise Conflict("基础版本已改变，本次结果未覆盖当前行程")
        if state.get("route_confirmed"):
            doc.update(stage="completed",itinerary_saved_version=trip["version"]+1,
                       confirmed_plan={"draft_revision":selected_route["draft_revision"],"plan_hash":selected_route["plan_hash"],
                                       "review_id":selected_route["review"]["review_id"],"confirmed_at":now()})
            self.store.commit(state["trip_id"],state["base_version"],doc,generation=state["generation"],updated_at=trip["updated_at"])
        else:
            self.store.update(state["trip_id"],doc)
        return {}
