import asyncio
import copy
import logging
import re
import traceback
from datetime import date, datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from time import perf_counter
from typing import TypedDict
from uuid import uuid4
from weakref import WeakValueDictionary

from .adapters import Amap, Crawler, FlyAI, Models, ToolError, default_ticket_price, image_data, map_link, navigation_link, remote_image
from .config import settings
from .models import (Candidate, CandidatePack, CandidateRevision, ChangeRequest, GuideExtraction, InterviewRound, OrderChoice, PlanningDecision,
                     Requirements, RequirementUpdate, ResourceChoice)
from .rules import check_itinerary, enforce_locks, merge_candidate_duplicates, resource_cost, ticket_policy, ticket_query_required, timestamp, validate_candidate, walking_limit
from .store import Conflict, now, dump
from .routing import coordinates, daily_route_scope, daily_movements, transport_access_transfers, distance, early_finish, geographic_order, reference_opening
from .resource_planning import chosen_hotel, comparison, resource_changes, resource_context_issues, same_resource, transport_issues
from .agents import GuideAgent, ResourceAgent, RouteAgent, ReviewerAgent
from .agents.route import resolve_stop_id
from .agents.runtime import digest
from .decisions import apply_walking_decision, confirmation_blockers, input_hash, route_structure, walking_question
from .models import DECISION_REQUIREMENT_FIELDS, DecisionResolution
from .dining import is_dining, meal_recommendations, refresh_meals
from .places import canonicalize_stops, canonicalize_itinerary, canonical_key, duplicate_issues, planning_stop_pool, repeat_allowed, represented_ids, plan_fingerprint
from .reviewing import (bind_report, evidence_ids, quote_expiry, require_review, review_basis, review_blockers, review_payload, review_repair_dates)
from .planning_recovery import previous_round, regression_reason, route_moves


NODE_STAGES = {"coordinate":"coordinating", "requirements_gate":"coordinating", "guides_gate":"parsing",
               "prepare_guides":"parsing", "interview":"interviewing", "interview_gate":"interviewing",
               "discover":"candidates", "assess":"assessing", "selection_gate":"candidates", "revise_candidate":"revising_candidate",
               "transport":"transport", "hotels":"hotels", "activities":"activities", "tickets":"tickets", "check":"checking",
               "repair":"repairing", "save":"saving"}
NODE_STAGES.update(guide_analysis="parsing",decision_gate="decision",confirm_route="saving",review_result="checking")
NODE_STAGES.update(review_result="reviewing",selection_gate="reviewing")
NODE_STAGES.update(review_gate="reviewing",planning_failed="interviewing")


def interrupt(value):
    from langgraph.types import interrupt as graph_interrupt
    return graph_interrupt(value)


def error_leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from error_leaves(child)
    else:
        yield error


class TripState(TypedDict, total=False):
    trip_id: str
    run_id: str
    generation: str
    base_version: int
    requirements: dict
    message: str
    change: dict
    mode: str
    warnings: list[str]
    candidates: list[dict]
    selected_route: dict
    selected_source_ids: list[str]
    source_selection_changed: bool
    interview: dict
    route_edit: dict
    itinerary: dict
    old_itinerary: dict
    old_requirements: dict
    stops: dict
    weather: dict
    resource_retry_done: bool
    repair_round: int
    locks: list[str]
    previews: dict
    preview_ready: bool
    assessment_dates: list[str]
    assessment_base: str
    revision_rejected: bool
    assessment: bool
    stop_constraints: dict
    guide_brief: dict
    decision: dict
    decision_resume: str
    decision_reopened: bool
    decision_history: list[dict]
    place_selection: dict
    draft_revision: int
    route_confirmed: bool
    factual_refresh_completed: bool
    assessment_refresh: bool
    keep_current_plan: bool
    revision_request: str
    revision_reply: str
    revision_selected_ids: list[str]
    review_feedback: list[dict]
    round_backup: dict
    rollback: dict
    repair_only: bool


class Planner:
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
        for name in ("coordinate","requirements_gate","guides_gate","prepare_guides","guide_analysis","decision_gate","confirm_route","review_result","review_gate","planning_failed","interview","interview_gate","discover","assess","selection_gate","revise_candidate","transport","hotels","activities","tickets","check","repair","save"):
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
        graph.add_conditional_edges("check",self.after_check,{"repair":"repair","save":"review_result"})
        graph.add_conditional_edges("review_result",lambda s:END if s.get("factual_refresh_completed") else "decision_gate" if s.get("decision") else "review_gate")
        graph.add_edge("repair","tickets")
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

    async def guide_analysis(self,state):
        self.emit(state,"parsing","攻略 Agent 正在整理景点与建议分组")
        result = await self.guide_agent.brief(state)
        prior = state.get("stop_constraints",{})
        required = set(prior.get("required",[])) | (set(result.required_ids)-set(prior.get("excluded",[]))) | set(state["requirements"].get("required_stop_ids",[]))
        required = {identity for identity in required if identity not in state["stops"] or not is_dining(state["stops"][identity])}
        excluded = set(prior.get("excluded",[])) | (set(result.excluded_ids)-required) | set(state["requirements"].get("excluded_stop_ids",[]))
        excluded -= required
        if not (required | excluded) <= set(state["stops"]):
            raise Conflict("必去或排除标记的攻略依据已变化，请在旅行需求中移除失效标记并重新规划")
        req = {**state["requirements"],"required_stop_ids":sorted(required),"excluded_stop_ids":sorted(excluded)}
        decision = result.question.model_dump(mode="json") if result.question else {}
        answered = state.get("decision_history",[])[-1:]
        previous_question = (state.get("guide_brief") or {}).get("question") or {}
        repeated = decision and previous_question.get("question") == decision["question"] and any(
            (any(item.get("answers",{}).values()) or item.get("note")) and any(
                old.get("question") == decision["question"] and set(old.get("affected_dates",[])) == set(decision.get("affected_dates",[]))
                for old in item.get("questions",[])) for item in answered)
        if repeated and not state.get("decision_reopened"):
            # The accepted answer is already in requirements/history; do not ask the same choice again.
            decision = {}
            result.status, result.question = "ready", None
            result.summary = "已沿用你对该问题的明确回答，继续按最新需求规划；可查事实由后续工具核实。"
        return {"requirements":req,"guide_brief":result.model_dump(mode="json"),"stop_constraints":{"required":sorted(required),"excluded":sorted(excluded)},
                "interview":{"summary":result.summary,"questions":[],"history":state.get("decision_history",[]),"confirmed":False},
                "decision":decision,"decision_resume":"guide_analysis"}

    async def agent_tool(self,state,request):
        if request.tool == "guide_feedback":
            self.emit(state,"parsing","路线 Agent 正在请求攻略 Agent 补充或替换景点")
            result = await self.guide_agent.feedback(state,request.request)
            if not set(result.stop_ids) <= set(state["stops"]):
                raise ToolError("guide_feedback","攻略 Agent 的替代景点没有所选攻略来源")
            self.emit(state,"activities" if state.get("assessment") else "candidates","路线 Agent 正在根据攻略反馈继续规划")
            return result.model_dump(mode="json")
        if request.city not in state["requirements"]["cities"]:
            raise ToolError("agent_tool_scope","Agent 请求的城市不在本次旅行中；allowed_cities="+dump(state["requirements"]["cities"]))
        if request.tool == "weather":
            return await self.amap.weather(state["trip_id"],request.city)
        stops = state["stops"]
        allowed = [identity for identity,stop in stops.items() if stop["city"] == request.city and not is_dining(stop)]
        if request.tool == "place":
            stop = stops.get(request.destination_id)
            if not stop or request.destination_id not in allowed:
                raise ToolError("agent_tool_scope","地点请求必须引用当前可选池的完整景点ID，不能使用名称或其他日期的景点；allowed_stop_ids="+dump(allowed))
            located = await self.locate_stops(state,{request.destination_id:stop})
            stops[request.destination_id].update(located[request.destination_id])
            if coordinates(located[request.destination_id].get("map_place")):
                stops[request.destination_id].pop("map_issue",None)
            return {**located[request.destination_id],
                    "query_status":"resolved" if coordinates(located[request.destination_id].get("map_place")) else "unresolved",
                    "tool_scope":"place仅匹配实际POI位置；本次没有查询营业时间、预约、门票或指定日期的开放状态"}
        if request.origin_id not in allowed or request.destination_id not in allowed:
            raise ToolError("agent_tool_scope","路线起终点必须引用当前可选池的完整景点ID；allowed_stop_ids="+dump(allowed))
        origin,destination = stops[request.origin_id],stops[request.destination_id]
        if any(stop["city"] != request.city for stop in (origin,destination)):
            raise ToolError("agent_tool_scope","市内路线不能跨城市")
        if request.mode != "auto":
            req = Requirements.model_validate(state["requirements"])
            allowed_modes = {"walking","transit"} if req.local_transport == "public" else {req.local_transport}
            if req.allow_taxi:
                allowed_modes.add("driving")
            if request.mode not in allowed_modes:
                raise ToolError("agent_tool_scope","查询方式超出用户允许范围；allowed_modes="+dump(sorted(allowed_modes)))
            missing = {identity:stops[identity] for identity in (request.origin_id,request.destination_id)
                       if not coordinates(stops[identity].get("map_place"))}
            if missing:
                located = await self.locate_stops(state,missing)
                for identity,stop in located.items():
                    stops[identity].update(stop)
                    if coordinates(stop.get("map_place")):
                        stops[identity].pop("map_issue",None)
            if not coordinates(origin.get("map_place")) or not coordinates(destination.get("map_place")):
                raise ToolError("agent_tool_place","起终点尚未唯一定位，无法按指定方式查询实际路线；已有来源与缺口保留")
            start = {**origin["map_place"],"name":origin["map_place"].get("name") or origin["name"]}
            end = {**destination["map_place"],"name":destination["map_place"].get("name") or destination["name"]}
            walking = {}
            if request.mode == "transit" and req.local_transport == "public":
                try:
                    walking = await self.amap.route(state["trip_id"],request.city,start,end,"walking")
                except ToolError as exc:
                    if exc.code in {"amap_limit","amap_rate_limit","amap_quota_limit","amap_usage_limit","amap_auth","amap_network","amap_failed"}:
                        raise
                if walking.get("minutes") is not None and 0 <= walking["minutes"] <= 15:
                    return {**walking,"requested_mode":"transit","query_status":"resolved","transit_queried":False,
                            "tool_scope":"按15分钟以内采用步行的规则返回真实步行依据；未查询公交，不代表已有公交路线，步行仍计入每日上限"}
            move = await self.amap.route(state["trip_id"],request.city,start,end,request.mode)
            if request.mode == "transit" and req.local_transport == "public" and move.get("minutes") is None:
                meters = walking.get("walking_meters") if walking.get("walking_meters") is not None else walking.get("distance_meters")
                if walking.get("minutes") is not None and walking["minutes"] <= 25 and meters is not None and 0 <= meters <= 1500:
                    # Keep distinct modes: a verified walk is an alternative, never a fabricated bus journey.
                    move = {**move,"walking_alternative":walking,
                            "note":"公交换乘耗时未取得，但已取得短距离步行依据；若用户明确要求本段必须公交/地铁，仍需保留该限制"}
            return {**move,"query_status":"resolved" if move.get("minutes") is not None else "unresolved",
                    "tool_scope":"主结果对应明确指定且已获允许的方式；walking_alternative如有则是另行核实的步行依据，不是公交结果或用户接受决定；原偏好不变"}
        move,_ = await self.movement(state,request.city,origin["name"],destination["name"],origin.get("map_place"),destination.get("map_place"))
        return {**move,"query_status":"resolved" if move.get("minutes") is not None else "unresolved",
                "tool_scope":"按用户允许的市内方式查询；实际采用方式以mode为准，request备注不改变交通方式"}

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

    def active(self, trip_id: str):
        trip = self.store.trip(trip_id)
        if trip["active_run"] and self.store.run(trip["active_run"])["status"] == "running":
            raise Conflict("这趟旅行正在处理，请等待本次运行结束")
        return trip

    async def delete_trip(self, trip_id: str, base: int):
        async with self.gate(trip_id):
            trip = self.active(trip_id)
            if trip["version"] != base:
                raise Conflict("行程已更新，请刷新后重新删除")
            task = self.tasks.get(trip["active_run"])
            if task and not task.done():
                raise Conflict("这趟旅行正在处理，请等待本次运行结束后再删除")
            for thread in self.store.trip_threads(trip_id):
                await self.graph.checkpointer.adelete_thread(thread)
            self.store.delete_trip(trip_id,base)
            self.clear_trip_cache(trip_id)

    def clear_trip_cache(self,trip_id):
        prefix = dump([trip_id])[:-1]+","
        for adapter in (self.amap,self.flyai):
            cache = getattr(adapter,"cache",{})
            for key in list(cache):
                if (isinstance(key,tuple) and trip_id in key or isinstance(key,str) and key.startswith(prefix)):
                    cache.pop(key,None)

    async def submit(self, trip_id: str, base: int, message: str = "", requirements: dict | None = None,
                     retry: bool = False, candidate_id: str | None = None,
                     selected_source_ids: list[str] | None = None,
                     interview_response: dict | None = None, route_edit: dict | None = None,
                     resume: bool = False, refresh_resources: bool = False,
                     resource_choice: dict | None = None, supplement_choice: dict | None = None,
                     resource_ids: list[str] | None = None, updated_at: str | None = None,
                     hotel_area_choice: dict | None = None, candidate_revision: int | None = None,
                     candidate_hash: str | None = None) -> str:
        from langgraph.types import Command

        async with self.gate(trip_id):
            trip = self.active(trip_id)
            if trip["version"] != base:
                raise Conflict("行程已经更新，请刷新后重新提交")
            if updated_at is not None and trip["updated_at"] != updated_at:
                raise Conflict("旅行内容已更新，请刷新后重新提交")
            doc = trip["document"]
            doc["requirements"] = Requirements.model_validate(doc["requirements"]).model_dump(mode="json")
            backup = previous_round(doc)
            if hotel_area_choice:
                city,area = hotel_area_choice["city"].strip(),hotel_area_choice["area"].strip()
                if city not in doc["requirements"]["cities"] or doc.get("pending") or doc.get("itinerary_outdated") or not doc.get("itinerary") or not doc.get("selected_route"):
                    raise Conflict("请先确认每日行程，再修改对应城市的住宿区域")
                if "hotel:"+city in doc.get("locks",[]):
                    raise Conflict("该城市住宿已锁定，请先解锁")
                areas = {**doc["requirements"].get("hotel_areas",{})}
                if area:
                    areas[city] = area
                else:
                    areas.pop(city,None)
                requirements = {"hotel_areas":areas}
            source_ids = list(dict.fromkeys(selected_source_ids if selected_source_ids is not None else doc.get("selected_source_ids", [])))
            self.selected_sources(trip_id, source_ids)
            selection_changed = source_ids != doc.get("selected_source_ids", [])
            if selection_changed:
                for key in list(self.amap.cache):
                    if isinstance(key,tuple) and trip_id in key:
                        self.amap.cache.pop(key,None)
            merged = {**doc["requirements"],**(requirements or {})}
            req = Requirements.model_validate(merged).model_dump(mode="json")
            available_ids = set(self.source_stops(trip_id,source_ids))
            if not set(req.get("required_stop_ids",[])+req.get("excluded_stop_ids",[])) <= available_ids:
                raise Conflict("景点约束必须来自当前勾选的攻略，请先调整必去或排除项")
            config = {"configurable":{"thread_id":trip["active_thread"]},"recursion_limit":60}
            snapshot = await self.graph.aget_state(config) if trip["active_thread"] else None
            pending = doc.get("pending") or {}
            if resource_choice or supplement_choice:
                choice_input = resource_choice or supplement_choice
                if choice_input["updated_at"] != trip["updated_at"] or pending or doc.get("itinerary_outdated") or not doc.get("itinerary") or not doc.get("selected_route"):
                    raise Conflict("当前行程已变化或尚未完成，请刷新后再采用备选")
                if req != doc["requirements"] or selection_changed or retry or resume or refresh_resources or message:
                    raise Conflict("采用备选需要使用当前行程与原需求")
                if resource_choice:
                    entry = next((entry for group in ("transport","hotels") for entry in doc["itinerary"].get(group,[]) if entry["id"] == resource_choice["entry_id"]),None)
                    if not entry or entry["id"] in doc.get("locks",[]):
                        raise Conflict("资源不存在或已锁定，请先解锁")
                    option = next((item for item in entry["options"] if item["id"] == resource_choice["option_id"]),None)
                    if not option:
                        raise Conflict("备选资源已变化，请重新查询")
                    quoted = timestamp(option.get("queried_at"),now()[:10])
                    current_time = timestamp(now(),now()[:10])
                    if not quoted or (current_time-quoted).total_seconds() >= 600:
                        raise Conflict("该备选报价已超过10分钟，请先更新报价与路线")
                    if option.get("constraint_issues"):
                        raise Conflict("该备选不符合硬性条件："+"；".join(option["constraint_issues"]))
                else:
                    product = next((item for item in doc.get("products",[]) if item["id"] == supplement_choice["product_id"]),None)
                    if not product or product.get("reference_only"):
                        raise Conflict("该商品不存在或仅适合作为独立预订参考")
                    supplier_city = (product.get("raw") or {}).get("cityName")
                    if supplier_city and supplier_city.rstrip("市") != supplement_choice["city"].rstrip("市"):
                        raise Conflict("供应商返回的商品城市与采用日期的城市不一致")
                    if not product.get("map_place") or supplement_choice.get("place_name"):
                        product["map_place"] = None
                        resource = {**product,"name":supplement_choice.get("place_name") or product["name"]}
                        async with self.amap.batch():
                            place = await self.amap.match_resource(trip_id,supplement_choice["city"],resource)
                        if place and (not place.get("cityname") or place["cityname"].rstrip("市") == supplement_choice["city"].rstrip("市")):
                            product.update(map_place=place,city=supplement_choice["city"],match_basis="你确认的体验地点，经高德匹配")
                    self.validate_supplement(doc,supplement_choice)
            if refresh_resources:
                if resume or retry or message or pending or not doc.get("itinerary") or not doc.get("selected_route") or selection_changed or req != doc["requirements"]:
                    raise Conflict("更新报价需要使用当前已保存的行程和原需求，请先保存修改或完成当前任务")
                validate_candidate(Candidate.model_validate(doc["selected_route"]),Requirements.model_validate(req),self.source_stops(trip_id,source_ids))
                valid_ids = {entry["id"] for group in ("transport","hotels") for entry in doc["itinerary"].get(group,[])}
                valid_ids.update("ticket:"+activity["id"] for day in doc["itinerary"].get("days",[]) for activity in day["activities"] if activity["kind"] == "visit")
                if resource_ids and (not set(resource_ids) <= valid_ids or {identity.removeprefix("ticket:") for identity in resource_ids} & set(doc.get("locks",[]))):
                    raise Conflict("要更新的资源已变化或已锁定，请先刷新或解锁")
            elif resource_ids:
                raise Conflict("指定资源只能用于更新报价")
            if resume:
                if doc["stage"] not in {"failed", "recoverable"} or not snapshot or not snapshot.next:
                    raise Conflict("本次任务的恢复记录不可用，请重新生成候选路线")
                if message or selection_changed or req != Requirements.model_validate(snapshot.values.get("requirements",{})).model_dump(mode="json") or source_ids != snapshot.values.get("selected_source_ids", []):
                    raise Conflict("需求或参考攻略已变化，无法继续原任务；请恢复原输入，或重新开始规划")
                thread, invocation = trip["active_thread"], None
            elif route_edit is not None:
                if pending.get("kind") != "selection" or not snapshot or not snapshot.next:
                    raise Conflict("当前没有可修改的候选路线，请先生成路线")
                candidate = next((c for c in doc.get("candidates",[]) if c["id"] == route_edit["candidate_id"]),None)
                if not candidate or selection_changed or snapshot.values.get("selected_source_ids") != source_ids:
                    raise Conflict("候选路线或参考攻略已变化，请刷新后重试")
                if not route_edit["content"].strip():
                    raise Conflict("请填写需要修改的内容")
                if not set(route_edit["dates"]) <= {d["date"] for d in candidate["days"]}:
                    raise Conflict("只能修改当前候选路线内的日期")
                route_edit["dates"] = list(dict.fromkeys(route_edit["dates"]))
                selected_ids = set(route_edit.get("selected_stop_ids",[]))
                selectable = {activity["id"]:day["date"] for day in candidate.get("draft",{}).get("days",[])
                              for activity in day["activities"] if activity["kind"] == "visit"}
                if not selected_ids <= set(selectable):
                    raise Conflict("选中的行程项已变化，请重新选择")
                if selected_ids and {selectable[identity] for identity in selected_ids} != set(route_edit["dates"]):
                    raise Conflict("调整日期必须与选中的行程项一致")
                route_edit["selected_stop_ids"] = sorted(selected_ids)
                thread, invocation = trip["active_thread"], Command(resume={"route_edit":route_edit})
                message = route_edit["content"]
            elif interview_response is not None:
                if pending.get("kind") not in {"interview","interview_confirmation","decision"} or not snapshot or not snapshot.next:
                    raise Conflict("当前没有等待回答的追问，请刷新后重试")
                if selection_changed or req != Requirements.model_validate(doc["requirements"]).model_dump(mode="json") or interview_response["round_id"] != pending.get("round_id"):
                    raise Conflict("攻略、需求或追问轮次已变化，请重新开始或刷新")
                # Older checkpoints can contain a batch; only the first unanswered question is active.
                questions = pending.get("questions",[])[:1]
                answers = interview_response.get("answers",{})
                if pending.get("kind") == "decision" and interview_response.get("draft_revision") != pending.get("draft_revision"):
                    raise Conflict("路线草稿已变化，请刷新后回答当前问题")
                if questions and (interview_response.get("confirm") or set(answers) != {q["id"] for q in questions} or any(not value.strip() for value in answers.values())):
                    raise Conflict("请只回答当前这一题；推荐选项需要你主动选择")
                if not questions and interview_response.get("confirm") and interview_response.get("note","").strip():
                    raise Conflict("请先提交补充内容，更新需求总结后再确认")
                if not questions and answers:
                    raise Conflict("当前等待确认需求总结，请通过补充内容提交修正")
                if not questions and not interview_response.get("confirm") and not interview_response.get("note","").strip():
                    raise Conflict("请确认需求，或填写需要补充修改的内容")
                thread, invocation = trip["active_thread"], Command(resume=interview_response)
                message = "\n".join([f"{q['title']}：{answers.get(q['id'],'')}" for q in questions] + [interview_response.get("note","")]).strip() or "确认旅行需求，生成候选路线"
            elif candidate_id is not None:
                if pending.get("kind") != "selection":
                    raise Conflict("当前没有等待选择的路线")
                if candidate_id not in {c["id"] for c in doc.get("candidates",[])}:
                    raise Conflict("候选路线已改变，请刷新后重新选择")
                if not source_ids or not snapshot or snapshot.values.get("selected_source_ids") != source_ids:
                    raise Conflict("参考攻略选择已变化，请先重新生成候选路线")
                candidate = Candidate.model_validate(next(c for c in doc["candidates"] if c["id"] == candidate_id))
                if not candidate.draft or candidate.confirmation_blockers:
                    raise Conflict("这份路线的关键依据尚未齐全，请继续查询或局部修改")
                if candidate_revision != candidate.draft_revision or candidate_hash != candidate.plan_hash:
                    raise Conflict("完整路线草稿已变化，请刷新后再确认")
                if candidate.input_hash != input_hash(req,source_ids,self.source_stops(trip_id,source_ids),self.selected_sources(trip_id,source_ids)):
                    raise Conflict("需求或攻略内容已变化，请重新规划")
                self.assert_review({**doc,"trip_id":trip_id,"requirements":req},candidate.review,candidate.draft)
                try:
                    validate_candidate(candidate, Requirements.model_validate(req), self.source_stops(trip_id, source_ids))
                except ValueError as exc:
                    raise Conflict("候选路线与当前日期或所选攻略不一致，请重新生成：" + str(exc)) from exc
                thread, invocation = trip["active_thread"], Command(resume={"candidate_id":candidate_id,"draft_revision":candidate_revision,"plan_hash":candidate_hash})
            elif pending and snapshot and snapshot.next and not selection_changed and not (retry and pending.get("kind") in {"selection","interview","interview_confirmation","decision"}) and not (pending.get("kind") in {"interview","interview_confirmation","decision"} and req != doc["requirements"]):
                if pending.get("kind") in {"interview","interview_confirmation","decision"}:
                    raise Conflict("请在需求追问界面回答或确认；修改攻略后可重新开始")
                if pending.get("kind") == "selection" and not retry:
                    raise Conflict("请先选择候选路线；若要更改条件，请重新生成候选路线")
                if pending.get("kind") == "requirements" and message:
                    update = await self.models.ask(RequirementUpdate,
                        "从用户补充信息中提取需求，只填写明确提供的字段，不推测年份、房间数或人数。",{"current":req,"message":message})
                    req = Requirements.model_validate({**req,**update.requirements}).model_dump(mode="json")
                thread = trip["active_thread"]
                invocation = Command(resume={"requirements":req,"selected_source_ids":source_ids,"retry_sources":True})
            elif retry and snapshot and snapshot.next and not pending and not selection_changed and req == Requirements.model_validate(snapshot.values.get("requirements",{})).model_dump(mode="json"):
                thread, invocation = trip["active_thread"], None
            else:
                thread = f"{trip_id}:{uuid4().hex}"
                old = copy.deepcopy(doc.get("itinerary") or (snapshot.values.get("old_itinerary") if snapshot else {}) or {})
                invocation = {"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":req,"message":message,
                    "mode":"modify" if old and not retry else "new", "warnings":[], "candidates":[],
                    "selected_route":doc.get("selected_route") or {}, "selected_source_ids":source_ids,"old_itinerary":old,"itinerary":copy.deepcopy(old),
                    "stops":self.source_stops(trip_id, source_ids),"change":{},"source_selection_changed":selection_changed,"repair_round":0,"locks":doc.get("locks",[]),
                    "interview":{},"route_edit":{},
                    "stop_constraints":{"required":req.get("required_stop_ids",[]),"excluded":req.get("excluded_stop_ids",[])},
                    "decision_history":doc.get("decision_history",[]) if not selection_changed else [],
                    "draft_revision":doc.get("draft_revision",0),"decision":{},"route_confirmed":False,
                    "previews":{},"preview_ready":False,
                    "old_requirements":copy.deepcopy(doc["requirements"])}
                if refresh_resources:
                    invocation["change"] = {"scopes":["transport","hotel","activity"],"refresh_resources":True,"explanation":"按当前配置重新查询报价和地图路线，保留活动选择"}
                    if resource_ids:
                        invocation["change"]["resource_ids"] = resource_ids
                        targeted_tickets = {identity.removeprefix("ticket:") for identity in resource_ids if identity.startswith("ticket:")}
                        if len(targeted_tickets) == len(resource_ids):
                            invocation["change"].update(scopes=["activity"],dates=[day["date"] for day in old["days"] if any(activity["id"] in targeted_tickets for activity in day["activities"])])
                        else:
                            scopes,cities = {"activity"},set()
                            for group,scope in (("hotels","hotel"),("transport","transport")):
                                for entry in old.get(group,[]):
                                    if entry["id"] in resource_ids:
                                        scopes.add(scope)
                                        cities.update([entry["city"]] if group == "hotels" else [entry["context"]["origin"],entry["context"]["destination"]])
                            invocation["change"].update(scopes=sorted(scopes),cities=sorted(cities))
                if hotel_area_choice:
                    invocation["change"] = {"scopes":["hotel","activity"],"cities":[city],"refresh_resources":True,
                        "explanation":f"{city}住宿区域改为{area or '根据每日行程推荐'}，重查住宿并更新相关接驳"}
                if resource_choice:
                    group = "hotel" if entry.get("city") else "transport"
                    cities = [entry["city"]] if group == "hotel" else [entry["context"]["origin"],entry["context"]["destination"]]
                    changed_entry = next(item for item in invocation["itinerary"]["hotels" if group == "hotel" else "transport"] if item["id"] == entry["id"])
                    changed_entry["selected"] = copy.deepcopy(option)
                    if group == "hotel":
                        changed_entry["selected_by_user"] = True
                    changed_entry["reason"] = "你选择的备选；本次查询报价，实际库存需在预订页确认"
                    if group == "transport":
                        changed_entry["buffer_minutes"] = 120 if option["kind"] == "flight" else 60
                    invocation["change"] = {"scopes":[group],"cities":cities,"resource_choice":resource_choice,"user_selected_ids":[entry["id"]],"explanation":"采用你选择的备选，并重算相关日期"}
                if supplement_choice:
                    product = self.validate_supplement(doc,supplement_choice)
                    stop_id = "product:"+product["id"]
                    place = product["map_place"]
                    stop = {"id":stop_id,"city":supplement_choice["city"],"name":place["name"],"day":1,
                        "assigned_date":supplement_choice["date"],"duration_minutes":supplement_choice["duration_minutes"],
                        "source_id":stop_id,"source_kind":"flyai","source_title":product["name"],"source_url":product.get("booking_url"),
                        "evidence":{"kind":"text","quote":"飞猪查询商品："+product["name"]},"map_place":place,"product":product}
                    invocation["stops"][stop_id] = stop
                    doc.setdefault("supplements",[]).append(stop)
                    selected_route = copy.deepcopy(invocation["selected_route"])
                    day = next(day for day in selected_route["days"] if day["date"] == supplement_choice["date"])
                    day["stop_ids"].append(stop_id)
                    day["adjustment"] = "采用你确认的飞猪体验商品，重新计算当天时间"
                    invocation["selected_route"] = selected_route
                    invocation["change"] = {"scopes":["activity"],"cities":[stop["city"]],"dates":[stop["assigned_date"]],
                        "resource_choice":{"supplement":True},"add_stop_ids":[stop_id],"user_selected_ids":[stop_id],"explanation":"采用你确认的体验，并重算当天安排；套餐价格单独列为待核对"}
            if message:
                doc["messages"].append({"role":"user","content":message,"time":now()})
            if invocation is not None and candidate_id is None and route_edit is None and interview_response is None and not hotel_area_choice and (retry or selection_changed or req != doc["requirements"]):
                doc["candidates"] = []
                doc["selected_route"] = {}
                doc["interview"] = {}
            doc["round_backup"] = backup or {}
            doc["rollback"] = {}
            doc["requirements"] = req
            doc["selected_source_ids"] = source_ids
            doc["progress_stage"] = (doc.get("progress_stage") or NODE_STAGES.get(snapshot.next[0], "queued")
                                     if thread == trip["active_thread"] and snapshot and snapshot.next else "queued")
            if refresh_resources:
                doc["progress_stage"] = "transport"
            if candidate_id or refresh_resources or resource_choice or supplement_choice or hotel_area_choice:
                doc["workflow_step"] = "itinerary"
            elif route_edit or interview_response:
                doc["workflow_step"] = "interview"
            elif not resume:
                doc["workflow_step"] = "interview"
            if route_edit and not route_edit.get("recheck"):
                selected_names = [activity["name"] for day in candidate.get("draft",{}).get("days",[]) for activity in day["activities"]
                                  if activity["id"] in route_edit.get("selected_stop_ids",[])]
                doc.setdefault("route_chat",[]).append({"role":"user","content":route_edit["content"].strip(),"dates":route_edit["dates"],
                                                       "selected_names":selected_names,"time":now()})
            run_id = self.store.start_run(trip_id,base,thread,{"request":message,"requirements":req})
            try:
                if isinstance(invocation,dict):
                    invocation["run_id"] = run_id
                    invocation.update(round_backup=copy.deepcopy(backup or {}),rollback={})
                elif snapshot:
                    # IDs are runtime metadata, updated outside nodes before resuming their durable checkpoint.
                    metadata = {"run_id":run_id,"base_version":base,"requirements":req,"selected_source_ids":source_ids,"locks":doc.get("locks",[]),
                                "round_backup":copy.deepcopy(backup or {}),"rollback":{}}
                    if resume and "planning_failed" in snapshot.next:
                        candidate = (snapshot.values.get("candidates") or [{}])[0]
                        dates = review_repair_dates([finding for finding in (candidate.get("review") or {}).get("findings",[])
                            if finding.get("severity") == "blocker"],candidate.get("draft",{}))
                        previews = copy.deepcopy(snapshot.values.get("previews",{}))
                        if candidate.get("draft",{}).get("days"):
                            previews[candidate["id"]] = {"itinerary":copy.deepcopy(candidate["draft"])}
                        metadata.update(repair_round=0,keep_current_plan=False,assessment_refresh=False,decision={},repair_only=True,
                            previews=previews,review_feedback=[],revision_selected_ids=[],
                            assessment_base=candidate.get("id",""),assessment_dates=dates)
                        await self.graph.aupdate_state(config,metadata,as_node="revise_candidate")
                    else:
                        await self.graph.aupdate_state(config,metadata)
                doc["stage"] = "queued"; doc["pending"] = None
                self.store.update(trip_id,doc)
                self.store.event(run_id,"progress","任务已提交",stage="queued")
            except Exception:
                self.store.finish_run(run_id,"failed",{"message":"任务提交未完成，请重试"})
                raise
            task = asyncio.create_task(self.execute(run_id,invocation),name=run_id)
            self.tasks[run_id] = task
            task.add_done_callback(lambda _:self.tasks.pop(run_id,None))
            return run_id

    async def back_to_step(self,trip_id: str,base: int,updated_at: str,step: str) -> str | None:
        async with self.gate(trip_id):
            trip = self.active(trip_id)
            if trip["version"] != base or trip["updated_at"] != updated_at:
                raise Conflict("旅行已变化，请刷新后再返回上一步")
            if step not in {"requirements","guides","interview","routes","itinerary"}:
                raise Conflict("不支持返回该规划步骤")
            doc = trip["document"]
            req = Requirements.model_validate(doc["requirements"]).model_dump(mode="json")
            source_ids = doc.get("selected_source_ids",[])
            stops = self.source_stops(trip_id,source_ids) if step in {"interview","routes"} else {}
            candidates = copy.deepcopy(doc.get("candidates",[]))
            interview = copy.deepcopy(doc.get("interview") or {})
            pending = doc.get("pending") or {}
            resume_node = None
            if step == "routes":
                for candidate in candidates:
                    try:
                        validate_candidate(Candidate.model_validate(candidate),Requirements.model_validate(req),stops)
                    except ValueError as exc:
                        raise Conflict("候选路线已不符合当前需求，请重新生成") from exc
                candidates = candidates[:1]
                resume_node = "assess" if candidates else None
            elif step == "interview" and interview.get("summary") and source_ids and stops and not Requirements.model_validate(req).missing():
                interview.setdefault("history",[])
                interview.update(questions=copy.deepcopy(pending.get("questions",[])[:1]) if pending.get("kind") == "interview" else [],
                                 round=len(interview["history"])+1,round_id=uuid4().hex,confirmed=False)
                candidates = []
                resume_node = "guide_analysis" if pending.get("kind") == "decision" and pending.get("questions") else "interview"
            threads = self.store.trip_threads(trip_id)
            snapshot = await self.graph.aget_state({"configurable":{"thread_id":trip["active_thread"]}}) if trip["active_thread"] and resume_node else None
            thread = f"{trip_id}:{uuid4().hex}"
            state = {"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":req,"message":"",
                     "mode":"new","change":{},"selected_source_ids":source_ids,"stops":stops,"locks":[],
                     "interview":interview,"candidates":candidates,"selected_route":{},"old_itinerary":{},
                     "itinerary":{},"old_requirements":copy.deepcopy(req),"warnings":[],
                     "previews":{c["id"]:{"itinerary":copy.deepcopy(c.get("draft",{})),"queried_at":now()} for c in candidates},
                     "preview_ready":False,"route_edit":{},"repair_round":0,"decision":{},"route_confirmed":False,
                     "draft_revision":candidates[0].get("draft_revision",0) if candidates else 0,
                     "stop_constraints":doc.get("stop_constraints",{}),"decision_history":doc.get("decision_history",[]),
                     "guide_brief":copy.deepcopy(snapshot.values.get("guide_brief",{})) if snapshot else {}}
            if resume_node == "guide_analysis":
                state.update(decision=copy.deepcopy(pending["questions"][0]),
                             decision_resume="guide_analysis" if pending.get("resume_stage") == "guide_analysis" else "discover")
            if resume_node:
                await self.graph.aupdate_state({"configurable":{"thread_id":thread}},state,as_node=resume_node)
            try:
                reset = self.store.back_to_step(trip_id,base,updated_at,step)
            except Exception:
                if resume_node:
                    await self.graph.checkpointer.adelete_thread(thread)
                raise
            for previous in threads:
                try:
                    await self.graph.checkpointer.adelete_thread(previous)
                except Exception:
                    logging.getLogger(__name__).warning("Inactive workflow checkpoint cleanup failed for %s",previous)
            self.clear_trip_cache(trip_id)
            if not resume_node:
                return None
            run_id = self.store.start_run(trip_id,base,thread,{"request":"返回规划步骤","step":step})
            try:
                doc = reset["document"]
                await self.graph.aupdate_state({"configurable":{"thread_id":thread}},
                    {"run_id":run_id,"requirements":doc["requirements"],"stop_constraints":doc.get("stop_constraints",{}),
                     "stops":self.source_stops(trip_id,doc.get("selected_source_ids",[]))},as_node=resume_node)
                doc.update(stage="queued",progress_stage="candidates" if step == "routes" else "interviewing",
                           candidates=candidates,interview=interview)
                self.store.update(trip_id,doc)
                self.store.event(run_id,"progress","正在返回候选选择" if step == "routes" else "正在返回需求确认",stage=doc["progress_stage"])
            except Exception:
                self.store.finish_run(run_id,"failed",{"message":"返回步骤未完成，请重试"})
                raise
            task = asyncio.create_task(self.execute(run_id,None),name=run_id)
            self.tasks[run_id] = task
            task.add_done_callback(lambda _:self.tasks.pop(run_id,None))
            return run_id

    async def reopen_decision(self,trip_id,base,updated_at,index):
        async with self.gate(trip_id):
            trip = self.active(trip_id)
            if trip["version"] != base or trip["updated_at"] != updated_at:
                raise Conflict("旅行已变化，请刷新后再修改回答")
            doc = trip["document"]
            history = doc.get("decision_history",[])
            if index >= len(history) or not history[index].get("questions"):
                raise Conflict("该回答记录不可用")
            candidate = copy.deepcopy((doc.get("candidates") or [doc.get("selected_route")])[0])
            if not candidate or not candidate.get("draft"):
                raise Conflict("请先完成路线草稿，再修改已有回答")
            question = copy.deepcopy(history[index]["questions"][0])
            question.setdefault("why_needed",history[index].get("summary") or "修改这项选择会影响路线安排")
            question["affected_dates"] = question.get("affected_dates") or [day["date"] for day in candidate["days"]]
            thread = f"{trip_id}:{uuid4().hex}"
            state = {"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":doc["requirements"],
                     "selected_source_ids":doc.get("selected_source_ids",[]),"stops":self.source_stops(trip_id,doc.get("selected_source_ids",[])),
                     "mode":"modify","change":{},"message":"修改已有回答","locks":doc.get("locks",[]),"warnings":[],
                     "candidates":[candidate],"selected_route":{},"old_itinerary":copy.deepcopy(doc.get("itinerary") or {}),
                     "old_requirements":doc["requirements"],"itinerary":copy.deepcopy(candidate["draft"]),
                     "previews":{candidate["id"]:{"itinerary":copy.deepcopy(candidate["draft"]),"queried_at":now()}},
                     "decision":question,"decision_reopened":True,"decision_resume":"assess","decision_history":history,"stop_constraints":doc.get("stop_constraints",{}),
                     "draft_revision":doc.get("draft_revision",0)+1,"route_confirmed":False,"route_edit":{},"repair_round":0}
            run_id = self.store.start_run(trip_id,base,thread,{"request":"重新回答已有选择","history_index":index})
            state["run_id"] = run_id
            try:
                await self.graph.aupdate_state({"configurable":{"thread_id":thread}},state,as_node="guide_analysis")
                doc.update(stage="queued",pending=None,workflow_step="interview")
                self.store.update(trip_id,doc)
            except Exception:
                self.store.finish_run(run_id,"failed",{"message":"问题恢复失败，原记录已保留"})
                raise
            task = asyncio.create_task(self.execute(run_id,None),name=run_id)
            self.tasks[run_id] = task
            task.add_done_callback(lambda _:self.tasks.pop(run_id,None))
            return run_id

    async def execute(self, run_id: str, invocation):
        run = self.store.run(run_id)
        config = {"configurable":{"thread_id":run["thread_id"]},"recursion_limit":60}
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
            else:
                self.store.finish_run(run_id,"completed",{"version":self.store.trip(run["trip_id"])["version"]})
                self.store.event(run_id,"completed","已确认并保存这份每日行程")
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
                logging.getLogger(__name__).error("Run %s failed (%s; code=%s) at %s",run_id,
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

    def selected_sources(self, trip_id: str, source_ids: list[str]) -> list[dict]:
        available = {source["id"]:source for source in self.store.sources(trip_id)}
        if any(identity not in available for identity in source_ids):
            raise Conflict("所选参考攻略不属于当前旅行或已经不存在，请重新选择")
        return [available[identity] for identity in source_ids]

    def source_stops(self, trip_id: str, source_ids: list[str]) -> dict:
        stops = {stop["id"]:{**stop,"source_id":source["id"],"source_title":source["title"],"source_url":source.get("url")}
                 for source in self.selected_sources(trip_id, source_ids) for stop in (source.get("extraction") or {}).get("stops",[])}
        stops.update({stop["id"]:stop for stop in self.store.trip(trip_id)["document"].get("supplements",[])})
        return stops

    @staticmethod
    def validate_supplement(doc, choice):
        product = next((item for item in doc.get("products",[]) if item["id"] == choice["product_id"]),None)
        if not product or not product.get("map_place") or not coordinates(product["map_place"]):
            raise Conflict("商品尚未匹配到唯一高德地点，不能直接加入每日行程")
        if product.get("reference_only"):
            raise Conflict("交通、住宿及套餐商品仅作为独立参考，请在对应资源或预订页面核对")
        day = next((day for day in doc["selected_route"]["days"] if day["date"] == choice.get("date")),None)
        if not day or day["city"] != choice["city"] or product.get("city") != choice["city"]:
            raise Conflict("商品城市或采用日期与当前行程不一致")
        if any(stop["id"] == "product:"+product["id"] for stop in doc.get("supplements",[])):
            raise Conflict("该商品已经采用")
        if len(day["stop_ids"]) >= 12:
            raise Conflict("当天已达到12项活动上限，请先减少活动")
        return product

    async def coordinate(self, state: TripState) -> dict:
        if state.get("change",{}).get("refresh_resources"):
            self.emit(state,"transport","正在按当前行程更新报价与路线")
        else:
            self.emit(state,"coordinating","正在理解旅行需求")
        req, change = state["requirements"],state.get("change",{})
        history = state.get("decision_history",[])
        if history:
            last = history[-1]
            question = (last.get("questions") or [{}])[0]
            if walking_question(question):
                req,_ = apply_walking_decision(req,question," ".join(last.get("answers",{}).values())+" "+last.get("note",""))
        if state.get("message") and not change:
            if state["mode"] == "modify":
                available = self.source_stops(state["trip_id"], state.get("selected_source_ids", []))
                update = await self.models.ask(ChangeRequest,
                    "识别用户修改：只填写明确的需求变化。scopes可同时包括酒店、活动、交通；日期或城市顺序变化必须full。"
                    "cities和dates指定受影响范围，未指定时为空。减少走路或活动用fewer_activities；酒店变便宜用cheaper_hotel。"
                    "用户明确要求保留的城市填preserve_cities。第二天等相对日期转换成当前行程中对应的准确日期，不扩大到其他天。"
                    "新增活动只可使用已有攻略stop_id；无相应资料时使用full重新检索，不编造ID。"
                    "市内local_transport：公交地铁为public，只步行为walking，自驾为driving，骑行为bicycling；明确允许打车才填allow_taxi=true，跨城transport单独填写。",
                    {"requirements":req,"message":state["message"],"current":state.get("old_itinerary"),"available_stops":available})
                change = update.model_dump(mode="json")
                before = Requirements.model_validate(state.get("old_requirements") or req).model_dump(mode="json")
                req = Requirements.model_validate({**req,**change["requirements"]}).model_dump(mode="json")
                if any(before.get(k) != req.get(k) for k in ("cities","origin","start_date","end_date","people","rooms","return_to_origin","children","earliest_departure","latest_arrival","latest_return","direct_only","max_transport_hours")):
                    change["scopes"] = ["full"]
                if not change["scopes"]:
                    change["scopes"] = ["activity"]
                if any(city not in req["cities"] for city in change["cities"]):
                    raise ValueError("修改涉及未知城市，请明确新的目的地城市列表")
            else:
                update = await self.models.ask(RequirementUpdate,
                    "提取用户明确的旅行需求。不要推测年份、人数、房间数或日期。预算是整趟旅行总预算。保留用户目的地顺序。"
                    "市内local_transport：公交地铁为public，只步行为walking，自驾为driving，骑行为bicycling；明确允许打车才填allow_taxi=true，跨城transport单独填写。",
                    {"current":req,"message":state["message"]})
                req = Requirements.model_validate({**req,**update.requirements}).model_dump(mode="json")
                change = {"explanation":update.explanation}
        if re.search(r"(?:不|不要|不想|不能|禁止|拒绝)(?:乘坐|坐)?(?:打车|出租车|网约车)",state.get("message","")):
            req = {**req,"allow_taxi":False}
        req = Requirements.model_validate(req).model_dump(mode="json")
        previous_limit = state.get("old_requirements",state["requirements"]).get("max_walk_per_day",8000)
        if req["max_walk_per_day"] != previous_limit:
            req["walking_limit_exempt_dates"] = []
            if history:
                history = [*history,{"questions":[],"answers":{},"note":state.get("message","") or "已更新每日步行上限",
                                    "summary":"按最新指令更新每日步行上限，替代此前的豁免"}]
        before_exempt = req.get("walking_limit_exempt_dates",[])
        req,_ = apply_walking_decision(req,{},state.get("message",""))
        if state.get("message") and req.get("walking_limit_exempt_dates",[]) != before_exempt:
            history = [*history,{"questions":[],"answers":{},"note":state["message"],"summary":"按最新指令更新步行限制"}]
        removed = set(change.get("remove_stop_ids",[]))
        if removed:
            if not removed <= set(state.get("stops",{})):
                raise Conflict("要删除的景点不在当前所选攻略中")
            req["required_stop_ids"] = [identity for identity in req.get("required_stop_ids",[]) if identity not in removed]
            req["excluded_stop_ids"] = sorted(set(req.get("excluded_stop_ids",[])) | removed)
        if state["mode"] == "modify" and not change.get("scopes"):
            before = Requirements.model_validate(state.get("old_requirements") or req).model_dump(mode="json")
            changed = any(before.get(k) != req.get(k) for k in ("cities","origin","start_date","end_date","people","rooms","return_to_origin","children","earliest_departure","latest_arrival","latest_return","direct_only","max_transport_hours"))
            change = {**change,"scopes":["full"] if changed else ["hotel","activity"]}
        if state["mode"] == "modify" and req.get("local_transport") != state.get("old_requirements",{}).get("local_transport","public"):
            # Local mobility applies to every affected connection, even when the model chooses a narrow scope.
            scopes = ["full"] if "full" in change.get("scopes",[]) else list(dict.fromkeys([*change.get("scopes",[]),"activity"]))
            change = {**change,"scopes":scopes,"cities":[],"dates":[]}
        if state["mode"] == "modify" and state.get("source_selection_changed"):
            change = {**change,"scopes":["full"]}
        dates = Requirements.model_validate(req)
        for old_day in state.get("old_itinerary", {}).get("days", []):
            for activity in old_day.get("activities", []):
                if activity["id"] not in state.get("locks", []):
                    continue
                if dates.start_date and dates.end_date and not (dates.start_date.isoformat() <= old_day["date"] <= dates.end_date.isoformat()):
                    raise ValueError("新旅行日期与已锁定活动冲突，请先解锁超出日期范围的活动；不会扩展旅行天数")
                if activity.get("kind") == "visit" and activity.get("source_kind") != "flyai" and state.get("selected_source_ids") and activity.get("source_id", activity["id"].split(":")[0]) not in state["selected_source_ids"]:
                    raise ValueError("已锁定活动来自未选择的攻略，请先解锁该活动或重新选择对应参考攻略")
        return {"requirements":req,"change":change,"message":"","decision_history":history,"stop_constraints":{"required":req.get("required_stop_ids",[]),"excluded":req.get("excluded_stop_ids",[])}}

    def after_coordinate(self,state):
        if Requirements.model_validate(state["requirements"]).missing():
            return "requirements"
        if state["mode"] == "modify" and "full" not in state.get("change",{}).get("scopes",[]) and state.get("selected_route") and state.get("selected_source_ids"):
            validate_candidate(Candidate.model_validate(state["selected_route"]), Requirements.model_validate(state["requirements"]), self.source_stops(state["trip_id"], state["selected_source_ids"]))
            return "resources"
        return "guides"

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

    async def parse_source(self, source: dict, cities: list[str]) -> dict:
        images, indices, gaps = [], [], []
        for index,image in enumerate(source.get("images",[])[:9],1):
            try:
                if image.startswith("local:"):
                    filename = Path(image.removeprefix("local:"))
                    resolved = (settings.data / "uploads" / filename).resolve()
                    resolved.relative_to((settings.data / "uploads").resolve())
                    raw = resolved.read_bytes()
                    images.append(image_data(raw))
                else:
                    images.append(await remote_image(image))
                indices.append(index)
            except (OSError,ValueError,ToolError) as exc:
                gaps.append(f"图片 {index} 未解析：{str(exc) if isinstance(exc,ToolError) else '文件不可读取'}")
        if len(source.get("images",[])) > 9:
            gaps.append("图片超过九张，剩余图片未解析")
        if not source.get("text") and not images:
            raise ToolError("source_empty","攻略正文和图片都不可读取，请补充资料")
        extraction = await self.guide_agent.extract(
            "忠实提取这篇攻略的每日完整路线。只提取正文或图片实际出现的地点，保留原顺序；不要填充景点。"
            "每个stop必须给出原文证据或图片序号，文本quote必须逐字引用提供的正文且包含地点名称。"
            "停留时长、开放时间、票价、天数未写则为null。先按攻略标题、每日段落和交通转场判断地点真实所属城市，再筛选用户目的地。"
            "destination_cities是本次规划范围，不是所有地点的默认城市；混合城市攻略不得把其他城市、区县的地点改标成目的地城市。"
            "仅提取有依据属于目的地的地点，city使用对应destination_cities原值；明确属于范围外或所属城市不明的地点不放入stops，"
            "在gaps中列出地点、原文区域线索和未纳入原因，分别标明超出目的地范围或城市待确认，不称为地图定位失败。"
            "餐厅、咖啡店、甜品店、小吃店和酒楼只有明确推荐时才提取，必须category=dining，即使它出现在原路线中也不是景点。"
            "其他游览活动category=sight。不要生成用餐占位点。"
            "ticket_claim忠实保留攻略的门票原文，包含全部票种、各自价格与免费条件；文字来源逐字摘录。"
            "未写门票不代表收费或免费，不列为必需补全的gap。"
            "攻略没有路线时stops为空并说明gap，热度不代表有效。",
            {"title":source["title"],"text":source.get("text","")[:30000],"city_hint":source.get("city_hint"),
              "destination_cities":cities,"image_indices":indices},images,indices)
        valid = []
        previous_ids = {(stop.get("city"),stop.get("name"),digest(stop.get("evidence"))):stop["id"]
                        for stop in (source.get("extraction") or {}).get("stops",[]) if stop.get("id")}
        for index,stop in enumerate(extraction.stops):
            evidence = stop.evidence
            if evidence.kind == "text" and (evidence.quote not in source.get("text","") or stop.name not in evidence.quote):
                gaps.append(stop.name + "的文字证据无法匹配，未采用")
                continue
            if evidence.kind == "image" and evidence.image_index not in indices:
                gaps.append(stop.name + "引用了未解析图片，未采用")
                continue
            stop.id = previous_ids.get((stop.city,stop.name,digest(evidence.model_dump(mode="json")))) or f"{source['id']}:{digest({'city':stop.city,'name':stop.name,'evidence':evidence.model_dump(mode='json')})[:16]}"
            if stop.ticket_claim and evidence.kind == "text" and stop.ticket_claim not in source.get("text",""):
                stop.ticket_claim = None
            if stop.duration_minutes is not None and evidence.kind == "text":
                import re
                minutes = [int(n) for n in re.findall(r"(\d+)\s*分钟",evidence.quote)]
                minutes += [round(float(n)*60) for n in re.findall(r"(\d+(?:\.\d+)?)\s*小时",evidence.quote)]
                if stop.duration_minutes not in minutes:
                    stop.duration_minutes = None
                    gaps.append(stop.name+"停留时长未取得对应文字证据，采用明确标注的系统估算")
            valid.append(stop)
        extraction.stops = valid
        extraction.gaps.extend(gaps)
        source.update(extraction=extraction.model_dump(mode="json"),extraction_cities=cities,extraction_version=4,error=None)
        return source

    async def prepare_guides(self,state):
        self.emit(state,"discovering","正在读取你选择的参考攻略；规划过程不会自动采集帖子")
        req = Requirements.model_validate(state["requirements"])
        warnings = list(state.get("warnings",[]))
        sources = self.selected_sources(state["trip_id"], state.get("selected_source_ids", []))
        if not sources:
            raise ValueError("请先选择参考攻略，再生成候选路线")
        # Account protection: planning uses saved sources; remote collection requires a manual search.
        for source in sources:
            if source.get("kind") == "url" and not source.get("text") and not source.get("images"):
                source["error"] = "仅保存了链接，规划不再自动采集；请导入正文或图片"
                self.store.add_source(state["trip_id"],source)
                warnings.append(source["error"])
        chosen = {source["id"]:source for source in sources}
        for source in chosen.values():
            if source.get("extraction") and source.get("extraction_cities") == req.cities and source.get("extraction_version") == 4:
                continue
            self.emit(state,"parsing","正在解析攻略：" + source["title"])
            try:
                source = await self.parse_source(source,req.cities)
            except ToolError as exc:
                source["error"] = str(exc); warnings.append(source["title"]+"："+str(exc))
            self.store.add_source(state["trip_id"],source)
        stops = self.source_stops(state["trip_id"], state["selected_source_ids"])
        allowed_source_ids = set(chosen)
        stops = {k:v for k,v in stops.items() if (v["source_id"] in allowed_source_ids or v.get("source_kind") == "flyai") and v["city"] in req.cities
                 and (not v.get("assigned_date") or req.start_date.isoformat() <= v["assigned_date"] <= req.end_date.isoformat())}
        stops = {identity:{**stop,"category":"dining" if is_dining(stop) else "sight"} for identity,stop in stops.items()}
        dining_ids = {identity for identity,stop in stops.items() if is_dining(stop)}
        requirements = req.model_dump(mode="json")
        requirements["required_stop_ids"] = [identity for identity in req.required_stop_ids if identity not in dining_ids]
        return {"candidates":[],"stops":stops,"requirements":requirements,
                "stop_constraints":{"required":requirements["required_stop_ids"],"excluded":requirements["excluded_stop_ids"]},
                "selected_route":{},"interview":{},"decision":{},"warnings":list(dict.fromkeys(warnings))}

    async def interview(self,state):
        self.emit(state,"interviewing","正在根据你的回答确定下一个问题")
        previous = state.get("interview",{})
        sources = self.selected_sources(state["trip_id"],state["selected_source_ids"])
        result = await self.models.ask(InterviewRound,
            "像 grilling 访谈一样，一次只问一个问题。questions最多只能有1项，禁止提前列出后续问题。"
            "收到当前问题的回答后，再依据最新需求和历史回答决定下一问；依赖未回答问题的分支必须等待回答后再分析。"
            "每题只询问一个旅行决策，题干直接写清这个问题，禁止在一题中列多个子问题或混问多个选择。title简短，question不超过200字。"
            "只询问尚未明确且会影响路线的选择，不重复已提供的日期、人数、预算、交通或历史答案。"
            "围绕所选攻略具体地点、必去与可舍弃项、节奏与体力、同行限制、兴趣取舍追问，避免泛泛问卷。"
            "每题给出推荐答案和理由，可提供2到4个选项，但推荐不是用户回答，不得自动接受。允许用户写自己的答案或明确让你决定。"
            "攻略中的可查事实由你阅读，不要求用户代查；缺少事实标明待确认，不虚构票价开放时间。"
            "summary总结已知需求、用户选择和资料缺口。偏好已足够明确时questions为空，进入用户确认，不自行生成路线。"
            "用户提出修正时重新分析受影响分支。需求表中的起止日期、城市顺序等硬约束不能擅自改动。",
            {"requirements":state["requirements"],"history":previous.get("history",[]),
             "guides":[{"id":s["id"],"title":s["title"],"guide":s.get("extraction")} for s in sources],"available_stops":state["stops"]})
        return {"interview":{**result.model_dump(mode="json"),"round":previous.get("round",0)+1,
                "round_id":uuid4().hex,"history":previous.get("history",[]),"confirmed":False}}

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

    async def discover(self,state):
        chosen = {s["id"]:s for s in self.selected_sources(state["trip_id"],state["selected_source_ids"])}
        stale_geography = any(source.get("extraction_version") in {1,2,3} for source in chosen.values())
        prepared = await self.prepare_guides(state) if "stops" not in state or stale_geography else {}
        stops = prepared.get("stops",state.get("stops",{}))
        if prepared:
            for identity,stop in stops.items():
                previous = state.get("stops",{}).get(identity,{})
                if (previous.get("city"),previous.get("name")) == (stop.get("city"),stop.get("name")):
                    stop.update({key:copy.deepcopy(previous[key]) for key in
                        ("map_place","map_candidates","map_selected_id","map_issue","query_status") if key in previous})
        warnings = list(prepared.get("warnings",state.get("warnings",[])))
        req = Requirements.model_validate(state["requirements"])
        if prepared:
            chosen = {s["id"]:s for s in self.selected_sources(state["trip_id"],state["selected_source_ids"])}
        if not stops:
            return {"candidates":[],"stops":stops,"selected_route":{},"warnings":warnings}
        self.emit(state,"candidates","正在比较攻略路线，整理候选方案")
        sights = {identity:stop for identity,stop in stops.items() if not is_dining(stop)}
        if not sights:
            return {"candidates":[],"stops":stops,"selected_route":{},"warnings":list(dict.fromkeys([
                *warnings,"所选攻略只有餐饮推荐，尚无可安排的游览景点；请补充包含景点的攻略"]))}
        priority = set(state.get("stop_constraints",{}).get("required",[])) & set(sights)
        excluded = set(state.get("stop_constraints",{}).get("excluded",[]))
        eligible = {identity:stop for identity,stop in sights.items() if not represented_ids([stop]) & excluded}
        if not eligible:
            return {"candidates":[],"stops":stops,"selected_route":{},"warnings":list(dict.fromkeys([
                *warnings,"所选攻略的游览景点已全部排除，请补充攻略或调整排除项"]))}
        priority &= set(eligible)
        # Locate required places and a few anchors first; actual chosen days locate all their visits later.
        city_anchors = [next((identity for identity,stop in eligible.items() if stop["city"] == city),None) for city in req.cities]
        grouped = [identity for group in state.get("guide_brief",{}).get("groups",[]) for identity in group.get("stop_ids",[])[:2]
                   if identity in eligible]
        anchors = [identity for identity in dict.fromkeys([*city_anchors,*grouped,*eligible]) if identity and identity not in priority]
        pool = list(dict.fromkeys([*sorted(priority),*list(dict.fromkeys(anchors))[:24]]))
        located = await self.locate_stops(state,{identity:stops[identity] for identity in pool if not coordinates(stops[identity].get("map_place"))})
        stops = canonicalize_stops({identity:located.get(identity,stop) for identity,stop in stops.items()})
        if clarification := self.place_clarification(stops,priority):
            return {"stops":stops,"selected_route":{},"decision_resume":"discover",**clarification}
        weather = await self.weather_context(state)
        parsed_guides = [{"id":s["id"],"title":s["title"],"likes":s.get("likes",0),"favorites":s.get("favorites",0),
                         "published_at":s.get("published_at"),"guide":{key:value for key,value in s["extraction"].items() if key != "stops"}}
                        for s in chosen.values() if s.get("extraction")]
        dates = [(req.start_date + timedelta(days=i)).isoformat() for i in range((req.end_date-req.start_date).days+1)]
        available,place_groups = planning_stop_pool({identity:stop for identity,stop in stops.items()
                                                   if not is_dining(stop) and not represented_ids([stop]) & excluded},
                                                  priority,state.get("locks",[]))
        instruction = (
            "根据用户确认的访谈需求和勾选的攻略形成一条最匹配的整趟候选路线。不得使用未选择攻略或自行补景点。"
            "guides提供攻略整体说明、季节、提醒与缺口；景点明细及各攻略证据统一在available_stops和source_references中，不重复附攻略stops。"
            "每条路线的days必须与required_dates逐项完全一致，每个日期恰好一天，不得添加、缺少、重复或改变日期。"
            "trip_days是用户旅行天数，参考攻略原有天数不是旅行天数。例如用户3天、攻略5天时，必须删减或合并为3天，绝不能生成5天。"
            "路线标题和说明也必须与用户天数一致，城市按用户顺序连续停留。"
            "活动只引用available_stops中的景点ID，一项不可重复。餐厅和咖啡店只作区域餐饮推荐，不加入stop_ids。用户确认的flyai来源体验只可放在assigned_date。结合map_place实际坐标把相近地点安排在同一天，减少跨区域往返折返；"
            "canonical_place_groups中多个ID是同一实际地点的攻略引用，代表项的source_references保留全部来源，不必逐个安排。"
            "除用户明确再访授权外，全程每个实际地点只安排一次；合并后某天缺少景点时可从现有攻略池补充，不得虚构地点。"
            "按用户节奏统筹每天上午和下午的游览量；完整游览日不能只分配一两个短活动，而把其他日期塞满。"
            "完整游览日需扣除约60分钟午餐后再分配活动，附近餐饮推荐不等于已经预留了用餐时间。"
            "优先按合理游玩时长、区域和开放说法分配现有攻略活动，不按固定景点数量平均分天；大型园区可单独一天但必须实际预留全天游玩时间。"
            "用户要求半天、留白或交通限制可以安排短日，在当天adjustment中说明依据；不得虚增游玩时长、餐厅或未选攻略景点凑时长。"
            "可重排攻略顺序，删减、合并、重排写adjustment并说明原因。地点未定位时不可虚构地理关系。"
            "weather只用于预报覆盖的对应日期；雨雪或恶劣天气优先已有攻略中的室内活动，保留必去项并说明风险；无预报不可编造天气。"
            "根据季节、交通方式和体力偏好取舍；所选攻略未覆盖某城市时保留空活动并写gap。"
            "尚未查询交通和酒店，不写实时报价、确定的到离时间。仅有一套有依据方案时只返回一套。")
        payload = {"requirements":state["requirements"],"required_dates":dates,"trip_days":len(dates),
                   "selected_source_ids":state["selected_source_ids"],"guides":parsed_guides,"available_stops":available,
                   "canonical_place_groups":place_groups,
                   "interview":state.get("interview",{}),"weather":weather,
                   "guide_brief":state.get("guide_brief",{}),"constraints":state.get("stop_constraints",{}),"decision_history":state.get("decision_history",[])}
        candidates = []
        for attempt in range(2):
            turn = await self.route_agent.plan(state,instruction,payload,lambda request:self.agent_tool({**state,"stops":stops},request))
            if turn.status == "needs_decision":
                return {"stops":stops,"weather":weather,"decision":turn.question.model_dump(mode="json"),"decision_resume":"discover"}
            try:
                for day in turn.candidate.days:
                    day.stop_ids = [resolve_stop_id(identity,available,day.city) or identity for identity in day.stop_ids]
                normalized = merge_candidate_duplicates(turn.candidate,req,stops,state.get("locks",[]))
                if any(before.stop_ids != after.stop_ids for before,after in zip(turn.candidate.days,normalized.days)):
                    self.emit(state,"candidates","已合并候选中的重复地点引用，保留全部攻略来源并继续校验")
                candidate = validate_candidate(normalized,req,stops)
                used = represented_ids([stops[identity] for day in candidate.days for identity in day.stop_ids])
                if set(state.get("stop_constraints",{}).get("required",[])) - used:
                    raise ValueError("路线遗漏用户明确必去的景点，应提出取舍问题而不是删除")
                if used & set(state.get("stop_constraints",{}).get("excluded",[])):
                    raise ValueError("路线包含用户明确排除的景点")
                candidate.id = uuid4().hex
                candidate.feasibility = {}
                candidates.append(candidate.model_dump(mode="json"))
                break
            except ValueError as exc:
                if attempt:
                    raise ToolError("route_constraints","路线仍不满足用户约束，未采用："+str(exc)) from exc
                payload = {**payload,"previous_candidate":turn.candidate.model_dump(mode="json"),"validation_error":str(exc)}
        if not candidates:
            warnings.append("未形成符合日期和来源约束的候选路线，请补充攻略后重试")
        return {"candidates":candidates,"stops":stops,"weather":weather,"selected_route":{},"warnings":list(dict.fromkeys(warnings)),"assessment_dates":[],"assessment_base":"","revision_rejected":False,"decision":{}}

    async def refresh_expired_resources(self,state):
        itinerary = copy.deepcopy(state["itinerary"])
        current = datetime.now(timezone.utc)
        expired = []
        for group in ("transport","hotels"):
            for entry in itinerary.get(group,[]):
                selected = entry.get("selected")
                if not selected or group == "hotels" and not entry.get("selected_by_user"):
                    continue
                expires = quote_expiry(selected)
                if expires is None or expires <= current:
                    expired.append(entry["id"])
        if not expired:
            return {}
        self.emit(state,"assessing","正在重查已过期的原班次与已采用住宿，保留景点和游览时段")
        probe = {**state,"itinerary":itinerary,"old_itinerary":copy.deepcopy(itinerary),"mode":"modify",
                 "locks":[identity for identity in state.get("locks",[]) if identity not in expired],
                 "change":{"scopes":["transport","hotel"],"resource_ids":expired,"refresh_resources":True,
                           "refresh_selected":True,"cities":[],"dates":[]}}
        if any(entry["id"] in expired for entry in itinerary.get("transport",[])):
            probe.update(await self.transport(probe))
        if any(entry["id"] in expired for entry in itinerary.get("hotels",[])):
            probe.update(await self.hotels(probe))
        # Quote refresh cannot replace a resource or silently move the original schedule.
        for group in ("transport","hotels"):
            previous = {entry["id"]:entry.get("selected") or {} for entry in itinerary.get(group,[])}
            for entry in probe["itinerary"].get(group,[]):
                before, selected = previous.get(entry["id"],{}),entry.get("selected")
                if selected and same_resource(before,selected):
                    entry["selected"] = {**copy.deepcopy(before),**selected}
        return {"itinerary":probe["itinerary"],"warnings":probe.get("warnings",state.get("warnings",[]))}

    async def assess(self,state):
        if state.get("revision_rejected"):
            previous = state.get("candidates",[])
            if previous and previous[0].get("draft"):
                candidate = copy.deepcopy(previous[0])
                result = await self.assess_result(state,candidate,candidate["draft"])
                return {**result,"revision_rejected":False}
            return {"revision_rejected":False,"selected_route":{}}
        candidates, previews = [],{}
        warnings = list(state.get("warnings",[]))
        for original in state.get("candidates",[])[:1]:
            self.emit(state,"assessing","正在查证候选的到离时间、住宿位置与每日可用时间")
            candidate = copy.deepcopy(original)
            probe = {**state,"selected_route":candidate,"itinerary":{"days":[],"transport":[],"hotels":[]},
                     "old_itinerary":copy.deepcopy(state.get("old_itinerary",{})),"mode":"new","change":{},"locks":state.get("locks",[]),"repair_round":0,
                     "resource_retry_done":True,"assessment":True}
            previous = state.get("previews",{}).get(state.get("assessment_base"))
            if not previous and (state.get("revision_selected_ids") or state.get("assessment_refresh")) and candidate.get("draft",{}).get("days"):
                previous = {"itinerary":candidate["draft"]}
            if previous and state.get("assessment_dates"):
                probe.update(itinerary=copy.deepcopy(previous["itinerary"]),old_itinerary=copy.deepcopy(previous["itinerary"]),mode="modify",
                             change={"scopes":["activity"],"dates":state["assessment_dates"],"cities":[],"refresh_resources":bool(state.get("assessment_refresh"))})
            preserved = (previous or {}).get("itinerary") or candidate.get("draft")
            if state.get("revision_selected_ids") and preserved:
                fixed_ids = {activity["id"] for day in preserved.get("days",[]) if day["date"] in state.get("assessment_dates",[])
                             for activity in day["activities"] if activity["kind"] == "visit" and activity["id"] not in state["revision_selected_ids"]}
                probe["locks"] = sorted(set(probe["locks"]) | fixed_ids)
            if state.get("repair_only") and preserved and preserved.get("days"):
                self.emit(state,"assessing","继续上次失败环节：保留原行程与查询依据，先重新检查，不重新生成路线或查询其他项目")
                resumed = copy.deepcopy(preserved)
                # A recovery note describes its original run, not a new failure on every resume.
                resumed.pop("recovery_note",None)
                probe.update(itinerary=resumed,old_itinerary=copy.deepcopy(resumed),mode="modify")
                probe.update(await self.check(probe))
            elif state.get("assessment_refresh") and preserved and preserved.get("days"):
                self.emit(state,"assessing","保留现有景点、时段和成功路段，更新过期报价并补齐地图依据后重新审查")
                probe["itinerary"] = copy.deepcopy(preserved)
                probe["keep_current_plan"] = True
                probe.update(await self.refresh_expired_resources(probe))
                await self.refresh_day_routes(probe,probe["itinerary"],set(state.get("assessment_dates",[])) or None,missing_only=True)
                probe.update(await self.check(probe))
            elif state.get("keep_current_plan") and preserved and preserved.get("days"):
                self.emit(state,"assessing","按你的最新指令保留路线与游览时段，更新约束检查")
                probe["itinerary"] = copy.deepcopy(preserved)
                await self.refresh_day_routes(probe,probe["itinerary"],refresh_routes=False)
                probe.update(await self.check(probe))
            else:
                for operation in (self.transport,self.hotels,self.activities,self.tickets,self.check):
                    probe.update(await operation(probe))
                    if probe.get("decision"):
                        question = probe["decision"]
                        actual = {day["date"]:[item["id"] for item in day["activities"] if item["kind"] == "visit"]
                                  for day in probe["itinerary"].get("days",[])}
                        for day in candidate["days"]:
                            if day["date"] in actual:
                                day["stop_ids"] = actual[day["date"]]
                        candidate.update(draft=copy.deepcopy(probe["itinerary"]),review=None,
                                         confirmation_blockers=["请先回答当前取舍问题，再检查和审查行程"])
                        return {"candidates":[candidate],"previews":{candidate["id"]:{"itinerary":copy.deepcopy(probe["itinerary"]),"queried_at":now()}},
                                "selected_route":{},"preview_ready":False,"stops":probe["stops"],"weather":probe.get("weather",{}),
                                "warnings":list(dict.fromkeys([*warnings,*probe.get("warnings",[])])),
                                "decision":question,"decision_resume":"assess","assessment_base":candidate["id"],
                                "place_selection":probe.get("place_selection",{}),
                                "assessment_dates":question.get("affected_dates",[])}
            itinerary = probe["itinerary"]
            warnings.extend(probe.get("warnings",[]))
            adopted = {day["date"]:[activity["id"] for activity in day["activities"] if activity["kind"] == "visit"] for day in itinerary["days"]}
            if any(adopted.values()):
                for day in candidate["days"]:
                    if previous and state.get("assessment_dates") and day["date"] not in state["assessment_dates"]:
                        continue
                    if adopted[day["date"]] != day["stop_ids"]:
                        day["stop_ids"] = adopted[day["date"]]
                        day["adjustment"] = (day.get("adjustment","")+"；根据实际交通与地图耗时调整活动").strip("；")
            candidate["feasibility"] = {"queried_at":now(),"budget":itinerary["budget"],
                "issues":itinerary["issues"],"information_unknown":itinerary.get("information_unknown",[]),
                "transport":[{"label":entry["label"],"selected":{k:v for k,v in (entry.get("selected") or {}).items() if k != "raw"},"reason":entry["reason"]} for entry in itinerary["transport"]],
                 "hotels":[{"city":entry["city"],"selected_by_user":bool(entry.get("selected_by_user")),"name":(entry.get("selected") or {}).get("name"),"area":entry.get("search_areas",[]),"commute_minutes":(entry.get("selected") or {}).get("commute_minutes"),
                            "selected":{k:v for k,v in (entry.get("selected") or {}).items() if k != "raw"},"reason":entry["reason"],"errors":entry.get("errors",[]),"query_status":entry.get("query_status")} for entry in itinerary["hotels"]],
                "days":[{"date":day["date"],"city":day["city"],"available_start":day["available_start"],"available_end":day["tail_deadline"],
                         "travel_minutes":day.get("travel_minutes"),"gaps":day["gaps"]} for day in itinerary["days"]]}
            candidate["gaps"] = list(dict.fromkeys([*candidate.get("gaps",[]),*[gap for day in itinerary["days"] for gap in day["gaps"]]]))
            result = await self.assess_result(probe,candidate,itinerary,probe.get("decision"))
            result["warnings"] = list(dict.fromkeys(warnings))
            result.update(stops=probe["stops"],weather=probe.get("weather",{}))
            result.update(assessment_dates=[],assessment_base="",assessment_refresh=False,keep_current_plan=False,repair_only=False)
            return result
        self.emit(state,"assessing","候选资源查证完成，已保留费用与时间缺口")
        return {"candidates":candidates,"previews":previews,"preview_ready":False,"selected_route":{},"warnings":list(dict.fromkeys(warnings)),"assessment_dates":[],"assessment_base":""}

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

    async def revise_candidate(self,state):
        self.emit(state,"revising_candidate","正在修改指定日期的候选安排，保留其他日期")
        edit = state["route_edit"]
        original = next((c for c in state["candidates"] if c["id"] == edit["candidate_id"]),None)
        if not original:
            raise ValueError("要修改的候选路线已不存在")
        original = copy.deepcopy(original)
        for day in original["days"]:
            day["stop_ids"] = [identity for identity in day["stop_ids"] if not is_dining(state["stops"][identity])]
        required = [identity for identity in state.get("stop_constraints",{}).get("required",[]) if not is_dining(state["stops"][identity])]
        requirements = {**state["requirements"],"required_stop_ids":required}
        constraints = {**state.get("stop_constraints",{}),"required":required}
        state = {**state,"requirements":requirements,"stop_constraints":constraints}
        if edit.get("recheck"):
            cities = {day["city"] for day in original["days"] if day["date"] in edit["dates"]}
            for key,value in list(self.amap.cache.items()):
                if isinstance(key,tuple) and len(key) == 3 and key[0] == state["trip_id"] and key[1] in cities and value is None:
                    self.amap.cache.pop(key,None)
                    self.amap.cache.pop(("place_issue",*key),None)
                    self.amap.cache.pop(("place_candidates",*key),None)
            revised = copy.deepcopy(original)
            revised["id"] = uuid4().hex
            return {"candidates":[revised],"route_edit":{},"selected_route":{},"assessment_dates":edit["dates"],
                    "assessment_base":original["id"],"assessment_refresh":True,"revision_rejected":False,
                    "requirements":requirements,"stop_constraints":constraints,"revision_request":"","revision_reply":"","revision_selected_ids":[],
                    "repair_round":0,"keep_current_plan":False}
        target_dates = set(edit["dates"])
        fixed_ids = {identity for day in original["days"] if day["date"] not in target_dates for identity in day["stop_ids"]}
        available = {k:v for k,v in state["stops"].items() if k not in fixed_ids and not is_dining(v)}
        payload = {"requirements":state["requirements"],"interview":state.get("interview",{}),"current_route":original,
                   "editable_dates":edit["dates"],"selected_stop_ids":edit.get("selected_stop_ids",[]),
                   "request":edit["content"],"available_stops":available,"weather":state.get("weather",{})}
        instruction = ("仅局部修改editable_dates中的候选路线安排，days只返回这些日期，每个日期恰好一次，城市不变。"
            "依据用户要求删减、替换或调整活动顺序，活动ID只能来自available_stops，不重复使用。餐厅和咖啡店不能作为景点或加入stop_ids。"
            "利用map_place坐标减少绕路折返，天气只用weather中对应日期的预报，不虚构位置、天气或交通耗时。"
            "其他日期已固定，不能移走其活动。调整原因写adjustment，explanation说明修改与未能满足的要求。"
            "selected_stop_ids非空时，只修改这些用户选中的行程项，未选中的现有景点不能删除或改变相对顺序。替换只能使用已有攻略景点。"
            "若要求的地点不在所选攻略中，保留有依据的安排并说明缺口，不虚构地点或报价。"
            "若要求改旅行日期或城市顺序，解释需在旅行需求表修改并重新规划，本次仍保持原日期城市。")
        for attempt in range(2):
            patch = await self.route_agent.ask(state,CandidateRevision,instruction,payload)
            try:
                revised = self.apply_candidate_revision(original,patch,edit["dates"],state["requirements"],state["stops"],edit.get("selected_stop_ids"))
                break
            except ValueError as exc:
                if attempt:
                    return {"route_edit":{},"selected_route":{},"revision_rejected":True,
                        "revision_selected_ids":[],"revision_reply":"这次调整没有满足日期、选择范围或攻略来源约束，已保留原路线："+str(exc),"warnings":list(dict.fromkeys([
                        *state.get("warnings",[]),"局部修改不符合日期或来源约束，原候选路线已保留："+str(exc)]))}
                payload = {**payload,"previous_patch":patch.model_dump(mode="json"),"validation_error":str(exc)}
        revised["id"] = uuid4().hex
        return {"candidates":[revised],"route_edit":{},"selected_route":{},"assessment_dates":edit["dates"],"assessment_base":original["id"],"assessment_refresh":False,"revision_rejected":False,
                "requirements":requirements,"stop_constraints":constraints,"revision_request":edit["content"],"revision_reply":patch.explanation,
                "revision_selected_ids":edit.get("selected_stop_ids",[]),"repair_round":0,"keep_current_plan":False,
                "review_feedback":[finding for finding in (original.get("review") or {}).get("findings",[]) if finding.get("severity") == "blocker"]}

    def apply_candidate_revision(self,original,patch,dates,requirements,stops,selected_stop_ids=None):
        targets = set(dates)
        replacements = {day.date.isoformat():day.model_dump(mode="json") for day in patch.days}
        if set(replacements) != targets or len(patch.days) != len(targets):
            raise ValueError("局部修改必须恰好覆盖指定日期")
        revised = copy.deepcopy(original)
        for index,day in enumerate(original["days"]):
            if day["date"] in targets:
                replacement = replacements[day["date"]]
                if replacement["city"] != day["city"]:
                    raise ValueError("局部修改不能改变当天城市")
                if selected_stop_ids:
                    fixed = [identity for identity in day["stop_ids"] if identity not in selected_stop_ids]
                    preserved = [identity for identity in replacement["stop_ids"] if identity in fixed]
                    if preserved != fixed:
                        raise ValueError("未选中的行程项不能删除或改变相对顺序")
                revised["days"][index] = replacement
        revised["reason"] = patch.explanation
        revised["feasibility"] = {}
        # Empty-day gaps belong to the current route; regenerate them after a patch.
        revised["gaps"] = [gap for gap in revised.get("gaps",[]) if not any(gap.startswith(day+" ") for day in targets)]
        candidate = validate_candidate(Candidate.model_validate(revised),Requirements.model_validate(requirements),stops)
        candidate.gaps = list(dict.fromkeys(candidate.gaps))
        return candidate.model_dump(mode="json")

    def affected(self,state,scope,city=None,day=None):
        if state["mode"] != "modify" or "full" in state.get("change",{}).get("scopes",[]):
            return True
        change = state["change"]
        if city in change.get("preserve_cities",[]):
            return False
        if scope not in change.get("scopes",[]):
            return False
        if change.get("cities") and city not in change["cities"]:
            return False
        if day and change.get("dates") and day not in change["dates"]:
            return False
        return True

    def route_stops(self,state,city,day_date=None):
        return [state["stops"][identity] for day in state["selected_route"]["days"] if day["city"] == city and (not day_date or day["date"] == day_date)
                for identity in day["stop_ids"] if identity in state["stops"]]

    async def enrich_transport(self,state,ctx,options):
        req = state["requirements"]
        for option in options:
            option["constraint_issues"] = transport_issues(option,ctx,req)
        allowed = [option for option in options if not option["constraint_issues"]]
        # Price and duration shortlists retain distinct useful alternatives without mapping every hit.
        priced = sorted(allowed,key=lambda item:(item.get("price") is None,item.get("price") or 0))[:3]
        timed = sorted(allowed,key=lambda item:str(item.get("arrival") or "99"))[:3]
        shortlist = list({item["id"]:item for item in [*priced,*timed]}.values())
        for option in shortlist:
            departure = timestamp(option.get("departure"),ctx["date"])
            arrival = timestamp(option.get("arrival"),ctx["date"])
            origin_stops = self.route_stops(state,ctx["origin"],ctx["date"])
            arrival_stops = self.route_stops(state,ctx["destination"],ctx["date"])
            origin_hotel = chosen_hotel(state["itinerary"],ctx["origin"],ctx["date"])
            arrival_hotel = chosen_hotel(state["itinerary"],ctx["destination"],ctx["date"])
            source_name = req.get("origin_address") if ctx["origin"] == req["origin"] else origin_stops[-1]["name"] if origin_stops else (origin_hotel or {}).get("name")
            target_name = req.get("origin_address") if ctx["destination"] == req["origin"] else (arrival_hotel or {}).get("name") or (arrival_stops[0]["name"] if arrival_stops else None)
            source_place = None if ctx["origin"] == req["origin"] else (origin_hotel or {}).get("map_place") if not origin_stops else origin_stops[-1].get("map_place")
            target_place = None if ctx["destination"] == req["origin"] else (arrival_hotel or {}).get("map_place") or (arrival_stops[0].get("map_place") if arrival_stops else None)
            before,_ = await self.movement(state,ctx["origin"],source_name,option.get("departure_place"),origin_place=source_place)
            after,_ = await self.movement(state,ctx["destination"],option.get("arrival_place"),target_name,destination_place=target_place)
            known_time = departure is not None and arrival is not None and arrival > departure
            duration = int((arrival-departure).total_seconds()/60) if known_time else None
            buffer = 120 if option["kind"] == "flight" else 60
            unknown = []
            if not source_name:
                unknown.append("出发城市具体起终点未提供，完整门到门耗时待确认")
            if before.get("minutes") is None or after.get("minutes") is None or duration is None:
                unknown.append("部分班次或接驳耗时待确认")
            option.update(departure_transfer=before,arrival_transfer=after,
                door_minutes=duration+buffer+60+before["minutes"]+after["minutes"] if duration is not None and before.get("minutes") is not None and after.get("minutes") is not None else None,
                walking_meters=(before.get("walking_meters") or 0)+(after.get("walking_meters") or 0),comparison_unknown=unknown)
            if len(option.get("legs",[])) > 1:
                option["comparison_unknown"].append("中转最短衔接、行李与税费需要核对供应商规则")
            if departure and before.get("minutes") is not None and req.get("earliest_departure"):
                earliest = timestamp(req["earliest_departure"],ctx["date"])
                if departure-timedelta(minutes=buffer+before["minutes"]) < earliest:
                    option["constraint_issues"].append("计入接驳与进站后，需要早于允许时间出门")
        return shortlist + [item for item in options if item["constraint_issues"]][:3]

    def hotel_areas(self,state,city):
        specified = state.get("requirements",{}).get("hotel_areas",{}).get(city)
        if specified:
            return [specified]
        stops = self.route_stops(state,city)
        located = [stop for stop in stops if coordinates(stop.get("map_place"))]
        if not located:
            return list(dict.fromkeys(stop["name"] for stop in stops))[:3] or [None]
        central = min(located,key=lambda stop:sum(distance(coordinates(stop["map_place"]),coordinates(other["map_place"])) for other in located))
        selected = [central]
        for stop in sorted(located,key=lambda stop:distance(coordinates(central["map_place"]),coordinates(stop["map_place"])),reverse=True):
            if len(selected) >= 3:
                break
            if all(distance(coordinates(stop["map_place"]),coordinates(other["map_place"])) > 1500 for other in selected):
                selected.append(stop)
        return [stop["map_place"]["name"] for stop in selected]

    async def enrich_hotels(self,state,city,options):
        unique = list({str(item.get("supplier_id") or item["name"]):item for item in options}.values())
        shortlist = sorted(unique,key=lambda item:(item.get("price") is None,item.get("price") or 0))[:6]
        for option in shortlist:
            unknown, total, walking = [],0,0
            exceeds_limit = False
            try:
                place = await self.amap.match_resource(state["trip_id"],city,option)
            except ToolError as exc:
                place = None; unknown.append(str(exc))
            option["map_place"] = place
            if place:
                for day in state["selected_route"]["days"]:
                    if day["city"] != city:
                        continue
                    stops = self.route_stops(state,city,day["date"])
                    anchors, departures = [],[]
                    for entry in state["itinerary"].get("transport",[]):
                        if entry["context"]["date"] != day["date"]:
                            continue
                        resource = entry.get("selected") or {}
                        if entry["context"]["destination"] == city and resource.get("arrival_place"):
                            anchors.append(({"name":resource["arrival_place"]},False))
                        if entry["context"]["origin"] == city and resource.get("departure_place"):
                            departures.append({"name":resource["departure_place"]})
                    if stops:
                        anchors.append((stops[0],True))
                        if not departures:
                            anchors.append((stops[-1],False))
                    else:
                        anchors.extend((station,True) for station in departures)
                    daily_walk = 0
                    for anchor,from_hotel in anchors:
                        if from_hotel:
                            move,_ = await self.movement(state,city,option["name"],anchor["name"],place,anchor.get("map_place"))
                        else:
                            move,_ = await self.movement(state,city,anchor["name"],option["name"],anchor.get("map_place"),place)
                        if move.get("minutes") is None:
                            unknown.append(day["date"]+"部分住宿接驳待确认")
                        else:
                            total += move["minutes"]
                        daily_walk += move.get("walking_meters") or 0
                    walking = max(walking,daily_walk)
                    limit = walking_limit(state["requirements"],day["date"])
                    exceeds_limit |= limit is not None and daily_walk > limit
            else:
                unknown.append("酒店未匹配到唯一地点，通勤无法比较")
            option.update(commute_minutes=total if place and not unknown else None,walking_meters=walking,
                comparison_unknown=list(dict.fromkeys(unknown)),constraint_issues=["住宿接驳步行已超过每日上限"] if exceeds_limit else [])
        return shortlist

    def segments(self,state):
        days = state["selected_route"]["days"]
        req = state["requirements"]
        city_starts = [day for index,day in enumerate(days) if index == 0 or day["city"] != days[index-1]["city"]]
        segments = []
        origin = req["origin"]
        for day in city_starts:
            if origin != day["city"]:
                ctx = {"origin":origin,"destination":day["city"],"date":day["date"]}
                segments.append({"id":f"transport:{origin}:{day['city']}","label":origin+" → "+day["city"],"context":ctx})
            origin = day["city"]
        if req["return_to_origin"] and origin != req["origin"]:
            ctx = {"origin":origin,"destination":req["origin"],"date":req["end_date"]}
            segments.append({"id":f"transport:{origin}:{req['origin']}","label":origin+" → "+req["origin"],"context":ctx})
        return segments

    async def transport(self,state):
        self.emit(state,"transport","正在查询跨城市交通")
        if state.get("change",{}).get("resource_choice"):
            return {"itinerary":copy.deepcopy(state["itinerary"])}
        old = {s["id"]:s for s in state.get("old_itinerary",{}).get("transport",[])}
        result, warnings = [],list(state.get("warnings",[]))
        req = state["requirements"]
        for segment in self.segments(state):
            ctx = segment["context"]
            refresh = self.affected(state,"transport",ctx["destination"]) or self.affected(state,"transport",ctx["origin"])
            if state.get("change",{}).get("resource_ids"):
                refresh &= segment["id"] in state["change"]["resource_ids"]
            if segment["id"] in old and (segment["id"] in state["locks"] or not refresh):
                result.append(copy.deepcopy(old[segment["id"]])); continue
            options, failures, query_ids = [],[],[]
            for kind in (["train","flight"] if req["transport"] == "any" else [req["transport"]]):
                deadline = req.get("latest_return") if ctx["destination"] == req["origin"] else req.get("latest_arrival")
                for sort in (2,3,4):
                    flags = {"origin":ctx["origin"],"destination":ctx["destination"],"dep-date":ctx["date"],"sort-type":sort,
                        "journey-type":1 if req.get("direct_only",True) else None,
                        "dep-hour-start":req["earliest_departure"][:2] if req.get("earliest_departure") else None,
                        "arr-hour-end":deadline[:2] if deadline else None,"total-duration-hour":req.get("max_transport_hours"),
                        "seat-class-name":req.get("flight_cabin" if kind == "flight" else "train_seat") or None}
                    try:
                        response = await self.resource_agent.search(state,kind,ctx,flags,fresh=bool(state.get("change",{}).get("refresh_resources")))
                        if response.get("evidence_id"):
                            query_ids.append(response["evidence_id"])
                        previous_ids = {option["id"] for option in options}
                        options.extend(response["options"])
                        if response.get("message"):
                            failures.append(response["message"])
                        if not response["options"] or not ({option["id"] for option in response["options"]}-previous_ids):
                            break
                    except ToolError as exc:
                        failures.append(str(exc))
                        if getattr(exc,"evidence_id",None):
                            query_ids.append(exc.evidence_id)
                        break
            options = list({option["id"]:option for option in options}.values())
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(segment["id"],{}).get("selected") or {}
                options = [option for option in options if same_resource(chosen,option)]
                for option in options:
                    option["constraint_issues"] = transport_issues(option,ctx,req)
                selected = next((option for option in options if not option.get("constraint_issues")),None)
                reason = "保留原班次、站点与时刻，仅更新本次报价依据"
                if selected is None:
                    failures.append("原班次未在本次查询中得到核实，旧报价不会延长有效期，请调整或重新查询")
            else:
                options = await self.enrich_transport(state,ctx,options)
                selected, reason = await self.resource_agent.choose(state,options,segment["label"] + "，" + state.get("change",{}).get("explanation",""))
            if selected is None and segment["id"] in old and old[segment["id"]].get("selected") and old[segment["id"]].get("context") == ctx:
                kept = copy.deepcopy(old[segment["id"]]); kept["errors"] = failures+["本次未获得替代方案，保留原交通；原报价未重新确认"]
                kept["evidence_ids"] = list(dict.fromkeys([*kept.get("evidence_ids",[]),*query_ids]))
                result.append(kept)
            else:
                result.append({**segment,"options":options,"selected":selected,"reason":reason,"evidence_ids":query_ids,
                               "buffer_minutes":120 if selected and selected["kind"] == "flight" else 60,"errors":list(dict.fromkeys(failures)),
                               "comparison":[comparison(option,req) for option in options]})
            warnings.extend(segment["label"]+"："+failure for failure in failures)
        itinerary = copy.deepcopy(state.get("itinerary") or {}); itinerary["transport"] = result
        return {"itinerary":itinerary,"warnings":list(dict.fromkeys(warnings))}

    async def hotels(self,state):
        self.emit(state,"hotels","正在匹配住宿日期与活动区域")
        if state.get("change",{}).get("resource_choice"):
            return {"itinerary":copy.deepcopy(state["itinerary"])}
        days, req = state["selected_route"]["days"],state["requirements"]
        old = {h["id"]:h for h in state.get("old_itinerary",{}).get("hotels",[])}
        result, warnings = [],list(state.get("warnings",[]))
        for city in req["cities"]:
            dates = [d["date"] for d in days if d["city"] == city]
            if not dates:
                continue
            check_in, check_out = dates[0],(date.fromisoformat(dates[-1])+timedelta(days=1)).isoformat()
            if check_out > req["end_date"]:
                check_out = req["end_date"]
            if check_in >= check_out:
                continue
            entry_id = "hotel:" + city
            refresh = self.affected(state,"hotel",city) or self.affected(state,"transport",city)
            if state.get("change",{}).get("resource_ids"):
                refresh &= entry_id in state["change"]["resource_ids"]
            if entry_id in old and (entry_id in state["locks"] or not refresh):
                result.append(copy.deepcopy(old[entry_id])); continue
            ctx = {"city":city,"check_in":check_in,"check_out":check_out}
            nights = (date.fromisoformat(check_out)-date.fromisoformat(check_in)).days
            total_nights = max(1,(date.fromisoformat(req["end_date"])-date.fromisoformat(req["start_date"])).days)
            known_transport = sum(float(cost) for entry in state["itinerary"].get("transport",[]) if (cost:=resource_cost(entry.get("selected"),req)) is not None)
            remaining = req["budget"]-known_transport-(total_nights+1)*req["people"]*100
            cap = max(1,remaining/total_nights/req["rooms"])
            if state.get("change",{}).get("cheaper_hotel") and entry_id in old:
                prior = old[entry_id].get("selected") or {}
                if prior.get("price_basis") == "room_night" and prior.get("price"):
                    cap = min(cap,prior["price"]*0.85)
            areas = self.hotel_areas(state,city)
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(entry_id,{}).get("selected") or {}
                areas = [chosen.get("search_area") or (areas[0] if areas else None)]
            errors,options,statuses,query_ids = [],[],[],[]
            for area in areas:
                flags = {"dest-name":city,"check-in-date":check_in,"check-out-date":check_out,"hotel-bed-types":req["bed_type"],
                         "poi-name":area,"max-price":round(cap),"hotel-types":req.get("hotel_type") or None,"hotel-stars":req.get("hotel_stars") or None,
                         "sort":"price_asc" if state.get("change",{}).get("cheaper_hotel") else "distance_asc"}
                if state.get("change",{}).get("refresh_selected"):
                    flags.update({"key-words":chosen.get("name"),"max-price":None})
                try:
                    response = await self.resource_agent.search(state,"hotel",ctx,flags,fresh=bool(state.get("change",{}).get("refresh_resources")))
                    if response.get("evidence_id"):
                        query_ids.append(response["evidence_id"])
                    statuses.append(response.get("status","ok"))
                    returned = response["options"] if state.get("change",{}).get("refresh_selected") else response["options"][:3]
                    options.extend({**option,"search_area":area} for option in returned)
                    if response.get("message"):
                        errors.append(response["message"])
                except ToolError as exc:
                    if getattr(exc,"evidence_id",None):
                        query_ids.append(exc.evidence_id)
                    statuses.append("error"); errors.append(str(exc)); break
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(entry_id,{}).get("selected") or {}
                options = [option for option in options if same_resource(chosen,option)]
            if old.get(entry_id,{}).get("selected_by_user") and not state.get("change",{}).get("refresh_selected"):
                chosen = old[entry_id].get("selected") or {}
                adopted = [option for option in options if same_resource(chosen,option)]
                enriched = {option["id"]:option for option in await self.enrich_hotels(state,city,adopted)}
                options = [enriched.get(option["id"],option) for option in options]
            prior = old.get(entry_id,{})
            preserve_adopted = bool(prior.get("selected_by_user") and prior.get("selected") and
                                    not state.get("change",{}).get("cheaper_hotel"))
            if state.get("change",{}).get("refresh_selected"):
                selected = next((option for option in options if not option.get("constraint_issues")),None)
                reason = "保留你已采用的酒店、房型与日期，仅更新本次报价依据"
                if selected is None:
                    errors.append("原酒店及房型未在本次查询中得到核实，旧报价不会延长有效期，请调整或重新查询")
            elif preserve_adopted:
                selected = next((option for option in options if same_resource(prior["selected"],option) and not option.get("constraint_issues")),None)
                reason = "保留你已采用的住宿及房型，更新本次查询信息"
                if selected is None:
                    errors.append("原住宿未取得身份、房型和日期一致且满足约束的新结果，保留旧安排供处理，不能视为本次已核实")
            else:
                selected, reason = await self.resource_agent.choose(state,options,f"{city}住宿{nights}晚，{req['rooms']}间，整段费用口径必须明确。"+state.get("change",{}).get("explanation",""))
            if selected is None and entry_id in old and old[entry_id].get("selected") and old[entry_id].get("context") == ctx:
                kept = copy.deepcopy(old[entry_id]); kept["errors"] = errors+["本次未获得替代方案，保留原住宿；原报价未重新确认"]
                rejected = next((option for option in options if same_resource(prior["selected"],option) and option.get("constraint_issues")),None)
                if rejected:
                    kept["selected"]["constraint_issues"] = list(rejected["constraint_issues"])
                old_req = state.get("old_requirements") or req
                if any(old_req.get(field) != req.get(field) for field in ("bed_type","hotel_type","hotel_stars")):
                    kept["selected"]["constraint_issues"] = list(dict.fromkeys([
                        *kept["selected"].get("constraint_issues",[]),"原住宿尚未核实满足本次修改后的床型或酒店要求，请重新采用符合条件的房型"]))
                kept["evidence_ids"] = list(dict.fromkeys([*kept.get("evidence_ids",[]),*query_ids]))
                result.append(kept)
            else:
                confirmed = bool(prior.get("selected_by_user") and selected and prior.get("selected") and same_resource(prior["selected"],selected))
                result.append({"id":entry_id,"city":city,"label":city+"住宿","context":ctx,"options":options,
                               "evidence_ids":query_ids,
                               "selected":selected,"selected_by_user":confirmed,"reason":reason,"errors":list(dict.fromkeys(errors)),"search_areas":[area for area in areas if area],
                               "query_status":"ok" if options else next((status for status in statuses if status not in {"ok","empty"}),"empty"),
                               "comparison":[comparison(option,req) for option in options],"budget_basis":"扣除已知城际费用与餐饮估算后分配；未知门票及儿童价尚未扣除"})
            warnings.extend(city+"："+error for error in errors)
        itinerary = copy.deepcopy(state["itinerary"]); itinerary["hotels"] = result
        return {"itinerary":itinerary,"warnings":list(dict.fromkeys(warnings))}

    @staticmethod
    def place_clarification(stops,identities,days=()):
        for identity in sorted(identities):
            stop = stops.get(identity,{})
            candidates = list({item["id"]:item for item in stop.get("map_candidates",[]) if item.get("id")}.values())
            if coordinates(stop.get("map_place")) or stop.get("map_selected_id") or len(candidates) < 2:
                continue
            options = {f"{index+1}. {item['name']}（{item.get('address') or '地址未返回'}）":item["id"] for index,item in enumerate(candidates[:4])}
            question_id = "place-"+digest([identity,candidates])[:24]
            dates = [day["date"] for day in days if identity in day.get("stop_ids",[])]
            question = PlanningDecision(id=question_id,title="请选择具体地点",
                question="“"+stop["name"]+"”返回了多个高德候选，现有资料还不能唯一对应。你指的是哪一个？",
                options=[*options,"以上都不是，我补充地点线索"],recommendation="按你要去的场馆、分店或地址选择；也可补充名称、地址等线索。",
                why_needed=(stop.get("map_issue") or "候选地点存在歧义")[:1500],affected_dates=dates).model_dump(mode="json")
            return {"decision":question,"place_selection":{"question_id":question_id,"stop_id":identity,"options":options}}
        return None

    async def locate_stops(self,state,stops):
        limit = asyncio.Semaphore(3)
        # Keep verified AMap POIs through reassessment instead of rematching their guide aliases.
        saved = {}
        for plan in (state.get("old_itinerary",{}),state.get("selected_route",{}).get("draft",{}),state.get("itinerary",{})):
            for day in plan.get("days",[]):
                for item in day.get("activities",[]):
                    place = item.get("map_place") or {}
                    if coordinates(place) and str(place.get("evidence_id","")).startswith("query:"):
                        saved[(item["id"],day["city"],item["name"])] = place
        preserved,confirmed_choices = {},{}
        preferences = state.get("requirements",{}).get("preferences","")
        for identity,stop in stops.items():
            confirmed = next((entry["place_selection"] for entry in reversed(state.get("decision_history",[]))
                if entry.get("place_selection",{}).get("stop_id") == identity),{})
            if ((confirmed.get("city"),confirmed.get("name")) == (stop["city"],stop["name"])
                    and confirmed.get("preferences","") == preferences and confirmed.get("poi_id")):
                confirmed_choices[identity] = confirmed["poi_id"]
            place = stop.get("map_place") or {}
            if not (coordinates(place) and str(place.get("evidence_id","")).startswith("query:")):
                place = saved.get((identity,stop["city"],stop["name"]))
            selected = confirmed_choices.get(identity) or stop.get("map_selected_id")
            if selected and (place or {}).get("id") != selected:
                place = None
            if coordinates(place):
                preserved[identity] = copy.deepcopy(place)
        keys = [identity for identity in stops if identity not in preserved]
        candidate_sets,selection_results,failures = {},{},{}
        lookup_locks = {(stops[identity]["city"],stops[identity]["name"]):asyncio.Lock() for identity in keys}
        async def locate(identity):
            async with limit:
                stop = stops[identity]
                key = (stop["city"],stop["name"])
                try:
                    async def select(candidates):
                        candidate_sets[identity] = [{field:row.get(field) for field in ("id","name","address","cityname","adname","type","typecode","location")} for row in candidates]
                        if identity in confirmed_choices:
                            selected = confirmed_choices[identity] if any(row.get("id") == confirmed_choices[identity] for row in candidates) else None
                            reason = "用户已明确选择此高德POI" if selected else "用户已选的地点不在本次候选中，需要重新核对"
                            selection_results[identity] = (selected,reason)
                            return selected,reason
                        payload = {"stop_id":identity,"city":key[0],"name":key[1],"evidence":[stop.get("evidence",{})],
                                   "preferences":preferences,"decisions":state.get("decision_history",[]),
                                   "source_references":stop.get("source_references",[]),"alias_names":stop.get("alias_names",[]),
                                   "pois":candidate_sets[identity]}
                        config_store = getattr(self.models,"config_store",None)
                        config = config_store.snapshot().public() if config_store else {}
                        selection_key = ("place_selection",state["trip_id"],state.get("run_id"),digest([payload,config]))
                        if selection_key in self.amap.cache:
                            selection_results[identity] = self.amap.cache[selection_key]
                            return selection_results[identity]
                        choice = await self.models.ask(ResourceChoice,
                            "将攻略地点匹配到高德实际POI，只能选pois中的id。可识别明确简称、城市前缀、同一景区别名；"
                            "餐厅/酒店分店必须符合攻略提到的地址或总店等限定。多个同名地点没有足够依据时selected_id=null，不随意选第一个。"
                            "用户已明确选择场馆展区时按该选择匹配高德POI；飞猪坐标不参与定位，也不能据其与高德坐标不同拒绝匹配。"
                            "preferences是已确认的用户需求，优先使用其中明确选择的展区或地址；原攻略未写展区不代表用户后来选择无效。"
                            "综合完整攻略引用、用户最新需求与已回答选择，区分目的地本体、分店、入口、售票处和停车场。"
                            "多条候选本身不是查不到：证据足以唯一对应时选中实际POI，并引用具体匹配依据；仍无法区分才返回null，说明欠缺的区分信息。"
                            "不得生成坐标或新ID；必须返回selected_id；reason说明匹配依据或歧义。"
                            '输出形如 {"selected_id":null,"reason":"存在同名歧义"}；确认匹配时将null替换为pois中的ID，不返回POI原对象。',
                            payload)
                        if choice.selected_id is not None and choice.selected_id not in {row.get("id") for row in candidates}:
                            raise ToolError("place_choice","地点匹配引用了未知POI，未采用")
                        reason = choice.reason.strip() or ("模型匹配了候选POI，未返回匹配说明" if choice.selected_id else "模型未匹配地点，未返回原因说明")
                        # Keep unresolved decisions too; new evidence, candidates or runs get a new key.
                        self.amap.cache[selection_key] = (choice.selected_id,reason)
                        selection_results[identity] = (choice.selected_id,reason)
                        return choice.selected_id,reason
                    async with lookup_locks[key]:
                        place = await self.amap.place(state["trip_id"],*key,selector=select)
                    if identity in confirmed_choices and place and place.get("id") != confirmed_choices[identity]:
                        failures[identity] = "高德本次结果不是你已选的POI，未擅自替换"
                        place = None
                    if not place:
                        failures.setdefault(identity,str(self.amap.cache.get(("place_issue",state["trip_id"],*key)) or "高德尚未返回唯一匹配的地点与可用坐标"))
                    return {k:place.get(k) for k in ("id","name","location","address","cityname","adname","business_area","type","evidence_id","opentime2","open_time","match_basis")} if place and coordinates(place) else None
                except ToolError as exc:
                    failures[identity] = str(exc)
                    self.amap.cache[("place_issue",state["trip_id"],*key)] = str(exc)
                    return None
        results = await asyncio.gather(*(locate(key) for key in keys))
        places = dict(zip(keys,results))
        result = {identity:{**stop,"map_place":preserved.get(identity) or places.get(identity)} for identity,stop in stops.items()}
        for identity,stop in result.items():
            if identity in candidate_sets:
                stop["map_candidates"] = candidate_sets[identity]
                stop["map_selected_id"] = selection_results.get(identity,(None,""))[0]
            if coordinates(stop.get("map_place")):
                stop.pop("map_issue",None)
                stop["query_status"] = "resolved"
            else:
                stop["map_issue"] = failures.get(identity,"高德尚未返回唯一匹配的地点与可用坐标")
                stop["query_status"] = "unresolved"
        return canonicalize_stops(result)

    async def weather_context(self,state):
        result = {}
        for city in state["requirements"]["cities"]:
            try:
                result[city] = await self.amap.weather(state["trip_id"],city)
            except ToolError as exc:
                result[city] = {"forecasts":{},"note":str(exc)}
        return result

    @staticmethod
    def daily_weather(weather,city,day_date):
        city_weather = weather.get(city,{})
        forecast = city_weather.get("forecasts",{}).get(day_date)
        if not forecast:
            return {"date":day_date,"available":False,"note":city_weather.get("note") or "当前预报未覆盖此日期，临近出行需再查询"}
        weather_text = str(forecast.get("dayweather","")) + str(forecast.get("nightweather",""))
        return {**forecast,"available":True,"provider":"amap","queried_at":city_weather.get("queried_at"),
                "reporttime":city_weather.get("reporttime"),"evidence_id":city_weather.get("evidence_id"),
                "note":"有雨雪或恶劣天气，优先室内活动；户外安排需临近出行确认" if any(word in weather_text for word in ("雨","雪","雷","暴","沙尘")) else "预报可能变化，出行前请再确认"}

    async def window(self,state,day):
        start, end = 9*60,20*60
        if state["requirements"].get("earliest_departure"):
            hour,minute = map(int,state["requirements"]["earliest_departure"].split(":"))
            start = hour*60+minute
        scope = daily_route_scope(state["itinerary"],day)
        if scope["hotel_location_confirmed"] and state["requirements"].get("latest_return"):
            hour,minute = map(int,state["requirements"]["latest_return"].split(":"))
            end = hour*60+minute
        gaps, transfers = [],[]
        hotel = chosen_hotel(state["itinerary"],day["city"],day["date"])
        if not scope["hotel_location_confirmed"]:
            hotel = None
        tail = {"name":(hotel or {}).get("name"),"label":"返回住宿","start_name":(hotel or {}).get("name"),
                "start_place":(hotel or {}).get("map_place"),"end_place":(hotel or {}).get("map_place")}
        for segment in state["itinerary"].get("transport",[]):
            ctx, selected = segment["context"],segment.get("selected")
            if ctx["date"] != day["date"]:
                continue
            if not selected:
                gaps.append(segment["label"]+"尚未确认，当天安排为暂定")
                continue
            if ctx["destination"] == day["city"]:
                if hotel:
                    move, _ = await self.movement(state,day["city"],selected.get("arrival_place"),hotel["name"],destination_place=hotel.get("map_place"))
                    transfers.append({"label":"到达后前往住宿放行李","move":move})
                    transfer_minutes = move["minutes"] if move["minutes"] is not None else 60
                else:
                    tail["start_name"] = selected.get("arrival_place")
                    tail["start_place"] = None
                    transfer_minutes = 0
                arrival = timestamp(selected.get("arrival"),ctx["date"])
                if arrival:
                    if arrival.date().isoformat() != day["date"]:
                        gaps.append("交通到达跨日，需要调整城市停留日期")
                        start = end
                    else:
                        start = max(start,arrival.hour*60+arrival.minute+60+transfer_minutes)
                else:
                    gaps.append("到达时刻待确认，当天活动暂定")
            if ctx["origin"] == day["city"]:
                tail.update(name=selected.get("departure_place"),label="前往出发车站或机场",end_place=None)
                departure = timestamp(selected.get("departure"),ctx["date"])
                if departure:
                    end = min(end,departure.hour*60+departure.minute-segment["buffer_minutes"])
                else:
                    gaps.append("出发时刻待确认，当天活动暂定")
        if not tail["name"]:
            tail["label"] = "结束当天安排"
        if scope["hotel_location_confirmed"] and not tail["start_name"] and day["city"] == state["requirements"]["origin"]:
            tail["start_name"] = state["requirements"].get("origin_address") or None
        if not tail["start_name"]:
            gaps.append("住宿或出发地址尚未确定，首个景点从游览开始时刻计时，不计算前往首站的耗时")
        return start,end,gaps,transfers,tail

    async def daily_order(self,state,route_day,ids,locked_ids,payload):
        elsewhere = {identity for day in state["selected_route"]["days"] if day["date"] != route_day["date"] for identity in day["stop_ids"]}
        elsewhere |= set(payload.get("scheduled_stop_ids",[]))
        elsewhere |= {reference["stop_id"] for identity in list(elsewhere)
                      for reference in state["stops"].get(identity,{}).get("source_references",[])}
        excluded = set(state.get("stop_constraints",{}).get("excluded",[]))
        available = {identity:stop for identity,stop in state["stops"].items()
                     if stop["city"] == route_day["city"] and (identity not in elsewhere or repeat_allowed(stop,route_day,"visit",state["requirements"])) and not represented_ids([stop]) & excluded
                     and not is_dining(stop)
                     and (not stop.get("assigned_date") or stop["assigned_date"] == route_day["date"])}
        elsewhere_keys = {canonical_key(state["stops"][identity]) for identity in elsewhere if identity in state["stops"]}
        available = {identity:stop for identity,stop in available.items() if canonical_key(stop) not in elsewhere_keys or
                     repeat_allowed(stop,route_day,"visit",state["requirements"])}
        if state.get("repair_only"):
            available = {identity:stop for identity,stop in available.items() if identity in set(ids) | set(locked_ids)}
        available,place_groups = planning_stop_pool(available,ids,locked_ids)
        instruction = (
            "只安排当前一天。stop_ids、durations和start_times的键只能使用available_stops中的完整ID，不能用名称、POI ID或序号，不能重复。"
            "可从当天原方案及尚未安排的已选攻略活动中补充或替换，required和locked_ids必须保留。"
            "工具也只能查询当前available_stops中的地点，其他日期的条件留给对应日期处理；已有坐标或route_evidence的真实耗时无需重复查询。"
            "preferences中带明确日期或地点的公交条件仅对相应日期/地点生效，不能扩大到其他天或全部景点间。"
            "先查实际步行路线；步行耗时不超过15分钟（含15分钟）的路段直接采用步行，不再查询或要求提供公交地铁。超过15分钟或步行耗时未取得时再按交通偏好查询，步行仍累计进每日限制。"
            "不得默认所有景点之间都有公共交通；公共交通无结果时保留真实缺口，不编造线路或耗时，也不因短途未查询公交而要求删点。"
            "公交transits为空仅表示没返回公交换乘方案；route_evidence或walking_alternative已有真实步行分钟数时，不得称该路段没有耗时、无法通行或步行未核实。"
            "place只核实POI位置，不能核实开放时间、预约或门票，不能把位置查询成功当成营业查证成功。"
            "考虑实际可用时段、开放与预约说法、游玩时长、天气和步行上限。"
            "中午默认预留60分钟午餐，通常在11:30至14:00之间；durations只填实际游玩分钟数，系统另计用餐时间。"
            "全天园区的午餐在该活动内部预留，不新增餐厅或重复园区；不得用午餐替代游玩时长或省略用餐以塞入更多景点。"
            "time_window给出真实可用起止时刻及结束地点，不只是时长总数。对完整游览日安排上午和下午，不能照抄稀疏候选就结束。"
            "没有攻略duration_minutes的所选活动必须逐项填写durations，按活动内容分配合理参考分钟数；大型园区若解释为全天游玩，durations必须相符，不能漏填或机械估算90分钟。"
            "有攻略时长且用户未要求改变时优先保留；只为用户明确预约或指定开始时刻填start_times，格式HH:MM。"
            "required只要求保留景点，不固定旧开始时刻或旧游玩时长；不能把旧计划的start复制成预约。"
            "时间冲突应在用户授权内调整当天顺序和前序活动，只有明确预约、用户锁定和preserved_activities需要保留原时刻。"
            "opening_constraints是已取得的参考营业窗口，必须在窗口内安排；某处11:00开放时可先去其他景点或顺延到11:00，不需要用户批准，也不把开放时刻当成预约。"
            "有充足剩余时间时优先补充available_stops中同区域、尚未安排的已选攻略景点；不得重复其他天地点或虚增短景点时长、等待和餐饮凑时间。"
            "确实需要较早结束时填写short_day_reason，引用本次用户指令或明确的开放、返程、体力、攻略池限制；不能只写沿用当前顺序或自由活动。"
            "change.fewer_activities=true时按减少活动安排，不因完整日默认目标重新加点；仅调整选中项时不能扩大修改范围。"
            "schedule_feedback是实际排程反馈：原计划可能因开放窗口、交通或返程被删点，应据此重新选点和排序，不能只修改解释。"
            "user_request是当前用户对这些日期的最新调整要求，优先落实其顺序、节奏、游玩时长与指定时刻；调整有冲突时提出问题。"
            "preserved_activities是未选中的既有安排，必须保留其地点、相对顺序和原时段；不能通过移动它们来完成对选中项的调整。"
            "可删减可选项并解释；必去与硬约束冲突时返回一道问题。")
        feedback = [finding for finding in state.get("review_feedback",[])
                    if route_day["date"] in review_repair_dates([finding],state.get("itinerary",{}))]
        history = [item for item in payload.get("decisions",[]) if not item.get("questions") or any(
            not question.get("affected_dates") or route_day["date"] in question["affected_dates"] for question in item["questions"])]
        constraints = payload.get("constraints",state.get("stop_constraints",{}))
        local_constraints = {**constraints,**{field:[identity for identity in constraints.get(field,[]) if identity in available]
                                               for field in ("required","excluded")}}
        source_requirements = payload.get("requirements") or state.get("requirements",{})
        local_requirements = {**source_requirements,
                              **{field:[identity for identity in source_requirements.get(field,[]) if identity in available]
                                 for field in ("required_stop_ids","excluded_stop_ids")}}
        payload = {**payload,"available_stops":available,"current_stop_ids":ids,"locked_ids":sorted(locked_ids),
                   "allowed_stop_ids":sorted(available),
                   "canonical_place_groups":place_groups,"review_feedback":feedback,"decisions":history,
                   "constraints":local_constraints,"requirements":local_requirements,
                   "opening_constraints":{identity:hours for identity,stop in available.items() if (hours := reference_opening(stop,route_day["date"]))}}
        for attempt in range(2):
            turn = await self.route_agent.plan(state,instruction,payload,
                lambda request:self.agent_tool({**state,"stops":available,
                    "requirements":{**state.get("requirements",{}),"cities":[route_day["city"]]}},request),daily=True)
            updated = {identity:{**state["stops"][identity],"map_place":stop["map_place"]} for identity,stop in available.items()
                       if identity in state["stops"] and coordinates(stop.get("map_place"))}
            if updated:
                # The tool's daily pool is a copy; merge actual coordinates back before scheduling its first stop.
                state["stops"].update(updated)
                state["stops"].update(canonicalize_stops(state["stops"]))
            if turn.status == "needs_decision":
                return turn
            order = turn.order
            order.stop_ids = [resolve_stop_id(identity,available,route_day["city"]) or identity for identity in order.stop_ids]
            problems = []
            for field in ("durations","start_times"):
                normalized = {}
                for identity,value in getattr(order,field).items():
                    key = resolve_stop_id(identity,available,route_day["city"]) or identity
                    if key in normalized and normalized[key] != value:
                        problems.append(field+"对同一景点给出了互相冲突的值："+key)
                    normalized[key] = value
                setattr(order,field,normalized)
            invalid = (set(order.stop_ids) | set(order.durations) | set(order.start_times)) - set(available)
            if invalid:
                problems.append("引用了不在当天可选攻略池中的ID："+"、".join(sorted(invalid)))
            if len(order.stop_ids) != len(set(order.stop_ids)):
                problems.append("活动ID重复")
            preserved = [activity["id"] for activity in payload.get("preserved_activities",[])]
            if preserved and [identity for identity in order.stop_ids if identity in preserved] != preserved:
                problems.append("未选中的行程项不能删除或改变相对顺序")
            places = [canonical_key(available[identity],route_day["city"]) for identity in order.stop_ids if identity in available]
            if len(places) != len(set(places)):
                problems.append("同一实际地点使用多个攻略ID重复安排")
            if locked_ids - set(order.stop_ids):
                problems.append("遗漏必去或锁定活动："+"、".join(sorted(locked_ids-set(order.stop_ids))))
            if payload.get("time_window"):
                preserved_ids = {activity["id"] for activity in payload.get("preserved_activities",[])}
                missing = [identity for identity in order.stop_ids if identity in available and identity not in preserved_ids
                           and not available[identity].get("duration_minutes") and identity not in order.durations]
                if missing:
                    problems.append("以下活动缺少游玩时长，必须填写durations，不能默认90分钟："+"、".join(missing))
            if not problems:
                return turn
            if attempt:
                raise ToolError("activity_choice","路线 Agent 修正后仍不满足当天活动约束："+"；".join(problems))
            self.emit(state,"activities","正在修正路线 Agent 返回的活动范围")
            payload = {**payload,"previous_order":order.model_dump(mode="json"),"validation_error":"；".join(problems),
                       "reference_correction":{"invalid_ids":sorted(invalid),"allowed_stop_ids":sorted(available)}}

    async def movement(self,state,city,origin_name,destination_name,origin_place=None,destination_place=None):
        mode = Requirements.model_validate(state["requirements"]).local_transport
        modes = ["walking","transit"] if mode == "public" else [mode]
        if state["requirements"].get("allow_taxi") and "driving" not in modes:
            modes.append("driving")
        unknown = {"minutes":None,"estimate_minutes":45,"allowed_modes":modes,"origin":origin_name,"destination":destination_name,"note":"起终点待确认"}
        if not origin_name or not destination_name:
            return unknown,None
        try:
            origin = origin_place or await self.amap.place(state["trip_id"],city,origin_name)
            destination = destination_place or await self.amap.place(state["trip_id"],city,destination_name)
            if not coordinates(origin) or not coordinates(destination):
                details = [str(self.amap.cache.get(("place_issue",state["trip_id"],city,name)) or "地点未唯一匹配") for name,place in ((origin_name,origin),(destination_name,destination)) if not coordinates(place)]
                return {**unknown,"note":"；".join(dict.fromkeys(details))+"，路线待确认"},destination
            origin = {**origin,"name":origin.get("name") or origin_name}
            destination = {**destination,"name":destination.get("name") or destination_name}
            if state.get("repair_only"):
                for (old_date,old_city,first,last),old in route_moves(state.get("old_itinerary",{})).items():
                    if old_city != city or old_date not in state.get("change",{}).get("dates",[]):
                        continue
                    points = {item["name"]:item.get("map_place") for day in state["old_itinerary"].get("days",[]) if day["date"] == old_date
                              for item in day.get("activities",[]) if item.get("kind") == "visit"}
                    old_origin = coordinates(old.get("origin_place") or points.get(first))
                    old_destination = coordinates(old.get("destination_place") or points.get(last))
                    same_origin = old_origin is not None and old_origin == coordinates(origin)
                    same_destination = old_destination is not None and old_destination == coordinates(destination)
                    needs_refresh = any(item.get("action") == "evidence" and old.get("evidence_id") in item.get("evidence_ids",[])
                                        for item in state.get("review_feedback",[]))
                    if (same_origin and same_destination and not needs_refresh and old.get("minutes") is not None
                            and old.get("mode") in modes and str(old.get("evidence_id","")).startswith("query:")
                            and not (mode == "public" and old.get("mode") == "walking" and
                                     (old["minutes"] > 25 or (old.get("walking_meters") or 0) > 1500 or distance(coordinates(origin),coordinates(destination)) > 1500))):
                        return copy.deepcopy(old),destination
            if origin["location"] == destination["location"]:
                return {"minutes":0,"mode":"walking","walking_meters":0,"provider":"amap","origin":origin_name,"destination":destination_name,
                        "origin_place":origin,"destination_place":destination,"navigation_url":navigation_link(origin,destination)},destination
            fewer_walks = any(word in state["requirements"].get("preferences","") for word in ("少走路","少步行","不想走太多"))
            long_leg = distance(coordinates(origin),coordinates(destination)) > 1500
            order = ["walking","transit"] if mode == "public" else [mode]
            if "driving" in modes and "driving" not in order:
                order.append("driving")
            routes, errors, failure_codes = [],[],[]
            for allowed in order:
                try:
                    result = await self.amap.route(state["trip_id"],city,origin,destination,allowed)
                    if result.get("minutes") is not None:
                        routes.append(result)
                        if mode == "public" and allowed == "transit":
                            break
                        if mode == "public" and allowed == "walking" and 0 <= result["minutes"] <= 15:
                            return {**result,"origin":origin_name,"destination":destination_name,"transit_queried":False},destination
                except ToolError as exc:
                    errors.append(str(exc)); failure_codes.append(exc.code)
                    if exc.code in {"amap_limit","amap_rate_limit","amap_quota_limit","amap_usage_limit","amap_auth","amap_network","amap_failed"}:
                        break
            if mode == "public":
                transit = [route for route in routes if route.get("mode") == "transit"]
                if transit:
                    return {**transit[0],"origin":origin_name,"destination":destination_name},destination
                walking = next((route for route in routes if route.get("mode") == "walking"),None)
                if walking and (long_leg or walking["minutes"] > 25 or (walking.get("walking_meters") or 0) > 1500):
                    routes = [route for route in routes if route.get("mode") != "walking"]
                    unknown.update(walking_alternative=walking,note="长路段公交/地铁依据未取得，未把长距离步行当作推荐；"+("；".join(dict.fromkeys(errors)) or "公共交通查询未返回可用路线"))
            if not routes:
                return {**unknown,"failure_codes":failure_codes,"note":unknown["note"] if unknown.get("walking_alternative") else "；".join(dict.fromkeys(errors)) or "未取得允许交通方式的路线，未改用打车"},destination
            route = min(routes,key=lambda item:item["minutes"] + ((item.get("walking_meters") or 0)/80 if fewer_walks else 0))
            return {**route,"origin":origin_name,"destination":destination_name},destination
        except ToolError as exc:
            return {**unknown,"failure_codes":[exc.code],"note":str(exc)},None

    async def map_order(self,state,city,ids,origin_name,destination_name):
        if len(ids) < 2:
            return ids,"逐段查询允许交通方式的地图路线"
        places = {identity:state["stops"][identity].get("map_place") for identity in ids}
        async def anchor(name):
            if not name:
                return None
            try:
                return await self.amap.place(state["trip_id"],city,name)
            except ToolError:
                return None
        origin, destination = await asyncio.gather(anchor(origin_name),anchor(destination_name))
        proposed = geographic_order(ids,places,origin,destination)
        if proposed == ids:
            note = "按已定位地点的地图位置安排，逐段查询实际路线" if all(coordinates(place) for place in places.values()) else "部分地点未定位，保留活动顺序；无法确认是否绕路"
            return ids,note
        async def duration(order):
            total, unresolved = 0,0
            previous, previous_place = origin_name,origin
            for identity in order:
                stop = state["stops"][identity]
                if previous:
                    move, previous_place = await self.movement(state,city,previous,stop["name"],previous_place,places[identity])
                    unresolved += move["minutes"] is None
                    total += move["minutes"] or 0
                else:
                    previous_place = places[identity]
                previous = stop["name"]
            if previous and destination_name:
                move, _ = await self.movement(state,city,previous,destination_name,previous_place,destination)
                unresolved += move["minutes"] is None
                total += move["minutes"] or 0
            return unresolved,total
        # Compare two orders rather than querying all map pairs; this is not a global shortest-path guarantee.
        original_cost = await duration(ids)
        proposed_cost = await duration(proposed)
        if proposed_cost[0] == 0 and (original_cost[0] or proposed_cost[1] < original_cost[1]):
            return proposed,"结合地图位置重排，并按允许交通方式的实际路线耗时选择较省时的顺序"
        if original_cost[0] == 0:
            return ids,"比较重排前后的地图路线耗时，保留较省时的顺序"
        return proposed,"按地图坐标减少区域往返；部分路线耗时未取得，无法确认最省时顺序"

    @staticmethod
    def clock(minutes):
        return f"{min(minutes//60,23):02d}:{minutes%60:02d}"

    async def schedule_activity_day(self,state,route_day,ordering,locked_ids,window,day_weather,preserved_activities=None):
        city, day_date = route_day["city"],route_day["date"]
        req, stops, change = state["requirements"],state["stops"],state.get("change",{})
        start, end, base_gaps, transfers, tail = window
        gaps, ids = list(base_gaps),list(ordering.stop_ids)
        locked_ids = set(locked_ids)
        preserved_activities = preserved_activities or {}
        if locked_ids or ordering.start_times or preserved_activities or any(reference_opening(stops[identity],day_date) for identity in ids) or any(word in req["preferences"] for word in ("预约","日落","日出","顺序","先去","再去")):
            route_basis = "保留锁定或用户指定的时间顺序，逐段查询地图路线；可能存在必要折返"
        else:
            ids,route_basis = await self.map_order(state,city,ids,tail.get("start_name"),tail["name"])
        current, origin, origin_place = start,tail.get("start_name"),tail.get("start_place")
        activities = []
        preserve_timing = bool(change.get("refresh_resources") or state.get("revision_selected_ids"))
        lunch_enabled = start < 14*60 and end >= 12*60+30 and not preserve_timing
        lunch_done = False
        def meal(beginning,within=False):
            return {"id":f"lunch:{day_date}","kind":"rest","meal":"lunch","name":"午餐与休息",
                    "start":self.clock(beginning),"end":self.clock(beginning+60),"duration_minutes":60,
                    "adjustment":"在该活动期间自行用餐，请确认场内用餐条件；未指定餐厅" if within else "预留60分钟用餐，地点自行选择；攻略餐饮推荐仍为可选"}
        for stop_id in ids:
            stop = stops[stop_id]
            if origin:
                move, place = await self.movement(state,city,origin,stop["name"],origin_place=origin_place,destination_place=stop.get("map_place"))
            else:
                move, place = {},stop.get("map_place")
            travel = move.get("minutes") if move.get("minutes") is not None else move.get("estimate_minutes",0)
            duration = ordering.durations.get(stop_id) or stop.get("duration_minutes") or 90
            requested_duration = duration
            opening = reference_opening({**stop,"map_place":place or stop.get("map_place")},day_date)
            if opening and opening.get("closed") and stop_id not in locked_ids and stop_id not in change.get("user_selected_ids",[]):
                gaps.append(stop["name"]+"在该日期的参考开放信息为闭馆，未安排；特殊开放须另行核实")
                continue
            windows = opening["windows"] if opening else []
            preserved = preserved_activities.get(stop_id)
            appointment_time = preserved.get("appointment_time") if preserved else ordering.start_times.get(stop_id)
            appointment = timestamp(appointment_time,day_date)
            appointment_minutes = appointment.hour*60+appointment.minute if appointment else None
            fixed_start = timestamp(preserved.get("start"),day_date) if preserved else None
            not_before = max(appointment_minutes or 0,fixed_start.hour*60+fixed_start.minute if fixed_start else 0)
            minimum = max(current+travel,not_before)
            hours = next((window for window in windows if max(minimum,window[0])+duration <= window[1]),windows[0] if windows else None)
            earliest = max(hours[0] if hours else 0,not_before)
            visit_start = max(current+travel,earliest)
            lunch, lunch_position = None,None
            if lunch_enabled and not lunch_done:
                lunch_start = max(current,11*60+30,min(12*60,visit_start-travel-60))
                if (lunch_start+60 <= visit_start-travel or
                        visit_start >= 11*60+30 and visit_start+duration > 13*60):
                    proposed = max(lunch_start+60+travel,earliest)
                    if lunch_start+60 <= 14*60 and (appointment_minutes is None or proposed <= appointment_minutes):
                        lunch, lunch_position = meal(lunch_start),"before"
                        visit_start = proposed
                if lunch is None and duration >= 180 and visit_start < 13*60 and visit_start+duration > 13*60+30:
                    lunch, lunch_position = meal(max(12*60,visit_start),within=True),"within"
                    duration += 60
                if lunch is None and 11*60+30 <= visit_start+duration <= 13*60:
                    lunch, lunch_position = meal(visit_start+duration),"after"
            tail_move = (await self.movement(state,city,stop["name"],tail["name"],origin_place=place,destination_place=tail.get("end_place")))[0] if tail["name"] else {}
            tail_minutes = tail_move.get("minutes") if tail_move.get("minutes") is not None else tail_move.get("estimate_minutes",0)
            duration_adjustment = ""
            user_time_text = req.get("preferences","")+" "+state.get("revision_request","")
            explicit_duration = bool(re.search(r"[0-9一二三四五六七八九十百半两]+(?:\.\d+)?\s*(?:小时|分钟)",user_time_text))
            if (hours and stop_id in ordering.durations and not preserve_timing and stop_id not in state.get("locks",[]) and
                    stop_id not in change.get("user_selected_ids",[]) and not stop.get("product") and not explicit_duration):
                deadline = min(hours[1],end-(20+tail_minutes if tail["name"] else 0)-(60 if lunch_position == "after" else 0))
                available = deadline-visit_start
                meal_end = timestamp(lunch["end"],day_date) if lunch_position == "within" else None
                minimum = max(120,meal_end.hour*60+meal_end.minute-visit_start) if meal_end else 30
                if minimum <= available < duration:
                    duration = available
                    duration_adjustment = "按参考开放与当日结束期限压缩建议游玩时长；原建议"+str(requested_duration)+"分钟，调整后实际游玩"+str(duration-(60 if lunch_position == "within" else 0))+"分钟"
            required_end = visit_start+duration+(60 if lunch_position == "after" else 0)+(20+tail_minutes if tail["name"] else 0)
            last_entry = opening.get("last_entry_minutes") if opening else None
            if (required_end > end or hours and visit_start+duration > hours[1] or
                    last_entry is not None and visit_start > last_entry or
                    appointment_minutes is not None and visit_start > appointment_minutes) and stop_id not in locked_ids and stop_id not in change.get("user_selected_ids",[]):
                gaps.append(stop["name"]+"因当天可用时间（含午餐与返程预留）、指定开始时刻或攻略参考开放窗口不足未安排")
                continue
            if lunch_position == "before":
                activities.append(lunch)
                current = lunch_start+60
                lunch_done = True
            # A rejected afternoon appointment must not consume the time of subsequent feasible stops.
            waiting_end = visit_start-travel
            if waiting_end > current:
                activities.append({"id":f"wait:{day_date}:{stop_id}","kind":"rest","name":"预留休息与等待参考时段",
                                   "start":self.clock(current),"end":self.clock(waiting_end)})
            current = visit_start
            activities.append({"id":stop_id,"kind":"visit","name":stop["name"],"start":self.clock(current),
                "end":self.clock(current+duration),"duration_minutes":duration,
                "duration_basis":("路线 Agent 根据偏好分配的参考游玩时长，需复核" if stop_id in ordering.durations else "攻略参考耗时（模型提取，需核对）" if stop.get("duration_minutes") else "系统估算 90 分钟")+("；"+duration_adjustment if duration_adjustment else ""),
                "requested_visit_minutes":requested_duration if duration_adjustment else None,"duration_adjustment":duration_adjustment,
                "opening_window":list(hours) if hours else None,
                "opening_reference":opening,
                "appointment_time":appointment_time,
                "source_id":stop["source_id"],"source_title":stop["source_title"],"source_url":stop.get("source_url"),"source_kind":stop.get("source_kind","guide"),
                "evidence":stop["evidence"],"move_before":move,"opening_claim":stop.get("opening_claim"),
                "source_references":stop.get("source_references",[]),
                "canonical_place_id":canonical_key(stop),"alias_names":stop.get("alias_names",[]),"possible_duplicate_ids":stop.get("possible_duplicate_ids",[]),
                "ticket_claim":stop.get("ticket_claim"),"category":stop.get("category","sight"),"map_url":map_link(stop["name"],city,place.get("location") if place else None),
                "map_place":place,"navigation_url":navigation_link(None,place) if place else None,"product":stop.get("product"),
                "adjustment":ordering.explanation or route_day.get("adjustment","")})
            current += duration + 20
            if lunch_position == "within":
                activities[-1]["meal_break"] = lunch
                activities[-1]["visit_minutes"] = duration-60
                lunch_done = True
            elif lunch_position == "after":
                if required_end <= end:
                    activities.append(lunch)
                    current += 60
                    lunch_done = True
                else:
                    gaps.append("午餐60分钟与已锁定活动或返程时间冲突，请调整当天安排")
            origin = stop["name"]
            origin_place = place or stop.get("map_place")
        if preserve_timing:
            old_day = next((day for day in state.get("old_itinerary",{}).get("days",[]) if day["date"] == day_date),{})
            for old in old_day.get("activities",[]):
                if old.get("meal") == "lunch" and not any(item["start"] < old["end"] and item["end"] > old["start"] for item in activities):
                    activities.append(copy.deepcopy(old)); lunch_done = True
                if old.get("meal_break"):
                    kept = next((item for item in activities if item["id"] == old["id"] and item["start"] == old["start"] and item["end"] == old["end"]),None)
                    if kept:
                        kept["meal_break"] = copy.deepcopy(old["meal_break"])
                        kept["visit_minutes"] = old.get("visit_minutes",kept["duration_minutes"]-60)
                        lunch_done = True
            activities.sort(key=lambda item:item["start"])
        visits = [item for item in activities if item["kind"] == "visit"]
        if visits and not lunch_done and visits[0]["start"] < "13:00" and visits[-1]["end"] > "13:30":
            gaps.append("当前固定时段或可用时间未留足60分钟午餐，需要调整这一天的活动安排")
        return_move = (await self.movement(state,city,origin,tail["name"],origin_place=origin_place,destination_place=tail.get("end_place")))[0] if origin and tail["name"] else {}
        return {"date":day_date,"city":city,"activities":activities,"gaps":gaps,
                "transfers":transfers,"weather":day_weather,"route_basis":route_basis,
                "start_place":tail.get("start_name"),"end_place":tail["name"],"end_label":tail["label"],
                "available_start":start,"tail_deadline":end,
                "return_move":return_move,"adjustment":ordering.explanation,
                "meal_estimate":req["people"]*100,"meal_basis":"餐饮暂按每人每天 100 元估算"}

    async def activities(self,state):
        self.emit(state,"activities","正在结合天气、地图位置与允许的交通方式细化每日安排")
        repair_dates = set(state.get("change",{}).get("dates",[])) if state.get("repair_only") else None
        if repair_dates is not None:
            state = {**state,"stops":copy.deepcopy(state["stops"]),"resource_retry_done":True}
            for day in state.get("old_itinerary",{}).get("days",[]):
                for activity in day.get("activities",[]):
                    if activity["id"] in state["stops"] and coordinates(activity.get("map_place")):
                        state["stops"][activity["id"]]["map_place"] = copy.deepcopy(activity["map_place"])
        refreshed_cities = set()
        missing_hotels = [entry["city"] for entry in state["itinerary"].get("hotels",[]) if entry.get("selected_by_user") and not entry.get("selected") and entry["id"] not in state.get("locks",[])]
        missing_transport = [entry for entry in state["itinerary"].get("transport",[]) if not entry.get("selected") and entry["id"] not in state.get("locks",[])]
        if (missing_hotels or missing_transport) and not state.get("change",{}).get("resource_ids") and not state.get("resource_retry_done") and self.flyai.availability()["ready"] and any(
            any("配置" in error or "未启用" in error or "未找到" in error for error in entry.get("errors",[])) for group in ("transport","hotels") for entry in state["itinerary"].get(group,[]) if not entry.get("selected")):
            # Resuming at activities must not keep a pre-configuration failure forever.
            retry_state = {**state,"old_itinerary":copy.deepcopy(state["itinerary"]),"mode":"modify",
                "change":{"scopes":["hotel","transport"],"cities":[],
                          "resource_ids":["hotel:"+city for city in missing_hotels]+[entry["id"] for entry in missing_transport],
                          "preserve_cities":state.get("change",{}).get("preserve_cities",[])}}
            if missing_transport:
                retry_state.update(await self.transport(retry_state))
            if missing_hotels:
                retry_state.update(await self.hotels(retry_state))
            refreshed_cities.update(missing_hotels)
            refreshed_cities.update(city for entry in missing_transport for city in (entry["context"]["origin"],entry["context"]["destination"]))
            state = {**state,"itinerary":retry_state["itinerary"],"warnings":retry_state.get("warnings",state.get("warnings",[])),"resource_retry_done":True}
            self.emit(state,"activities","正在使用重新查询的住宿与交通结果细化地图路线")
        # Hotel names can have branches too; supply the queried address for POI matching.
        active_ids = {identity for day in state["selected_route"]["days"] if repair_dates is None or day["date"] in repair_dates for identity in day["stop_ids"]}
        if repair_dates is None:
            active_ids |= set(state.get("stop_constraints",{}).get("required",[]))
        active_ids |= set(state.get("change",{}).get("add_stop_ids",[]))
        route_cities = {day["city"] for day in state["selected_route"]["days"]}
        locatable = {identity:stop for identity,stop in state["stops"].items() if identity in active_ids and not is_dining(stop)}
        locatable.update({identity:stop for identity,stop in state["stops"].items() if is_dining(stop) and stop["city"] in route_cities
                          and identity not in state.get("stop_constraints",{}).get("excluded",[]) and repair_dates is None})
        if repair_dates is not None:
            locatable = {identity:stop for identity,stop in locatable.items() if not coordinates(stop.get("map_place"))}
        for entry in state["itinerary"].get("hotels",[]):
            hotel = entry.get("selected") if entry.get("selected_by_user") else None
            if hotel and repair_dates is None:
                if coordinates(hotel.get("map_place")):
                    self.amap.cache[(state["trip_id"],entry["city"],hotel["name"])] = hotel["map_place"]
                else:
                    locatable["hotel:"+entry["city"]] = {"name":hotel["name"],"city":entry["city"],"evidence":{"address":hotel.get("address")}}
        located = await self.locate_stops(state,locatable) if locatable else {}
        stops = canonicalize_stops({identity:located.get(identity,stop) for identity,stop in state["stops"].items()})
        weather = state.get("weather",{}) if repair_dates is not None else await self.weather_context(state)
        state = {**state,"stops":stops,"weather":weather}
        if clarification := self.place_clarification(stops,active_ids,state["selected_route"]["days"]):
            return {"itinerary":copy.deepcopy(state["itinerary"]),"stops":stops,"weather":weather,"decision_resume":"assess",**clarification}
        req, itinerary = state["requirements"],copy.deepcopy(state["itinerary"])
        for entry in itinerary.get("transport",[]):
            if entry.get("selected") and repair_dates is None and (state["mode"] != "modify" or self.affected(state,"transport",entry["context"]["origin"]) or self.affected(state,"transport",entry["context"]["destination"]) or self.affected(state,"hotel",entry["context"]["origin"]) or self.affected(state,"hotel",entry["context"]["destination"]) or any(hotel.get("selected") and not hotel.get("selected_by_user") and hotel["city"] in (entry["context"]["origin"],entry["context"]["destination"]) for hotel in itinerary.get("hotels",[]))):
                enriched = await self.enrich_transport({**state,"itinerary":itinerary},entry["context"],entry.get("options",[]) or [entry["selected"]])
                by_id = {option["id"]:option for option in enriched}
                entry["selected"] = by_id.get(entry["selected"]["id"],entry["selected"])
                entry["comparison"] = [comparison(option,req) for option in entry.get("options",[])]
        state = {**state,"itinerary":itinerary}
        async def locate_selected(ordering):
            missing = {identity:stops[identity] for identity in ordering.stop_ids if not coordinates(stops[identity].get("map_place"))}
            if missing:
                stops.update(await self.locate_stops(state,missing))
                stops.update(canonicalize_stops(stops))
        old_days = {d["date"]:d for d in state.get("old_itinerary",{}).get("days",[])}
        days, rebuilt_dates = [],set()
        for route_day in state["selected_route"]["days"]:
            city, day_date = route_day["city"],route_day["date"]
            affected = self.affected(state,"activity",city,day_date) or self.affected(state,"hotel",city) or self.affected(state,"transport",city)
            affected |= city in refreshed_cities
            if repair_dates is not None:
                affected = day_date in repair_dates
            # Outbound/inbound changes also affect neighbouring cities' usable time.
            if state["mode"] == "modify" and "transport" in state.get("change",{}).get("scopes",[]):
                targets = state["change"].get("cities",[])
                affected |= any((not targets or any(c in targets for c in (s["context"]["origin"],s["context"]["destination"]))) and s["context"]["date"] == day_date for s in itinerary.get("transport",[]))
            if city in state.get("change",{}).get("preserve_cities",[]) and day_date in old_days:
                affected = False
            if day_date in old_days and not affected:
                days.append(copy.deepcopy(old_days[day_date])); continue
            rebuilt_dates.add(day_date)
            ids = [identity for identity in route_day["stop_ids"] if not is_dining(stops[identity])]
            change = state.get("change",{})
            if change.get("refresh_resources") and day_date in old_days:
                existing = [activity["id"] for activity in old_days[day_date]["activities"] if activity["kind"] == "visit" and activity["id"] in stops]
                if existing or not state.get("assessment_refresh"):
                    ids = existing
            if self.affected(state,"activity",city,day_date):
                ids = [i for i in ids if i not in change.get("remove_stop_ids",[]) and not is_dining(stops[i])]
                ids.extend(i for i in change.get("add_stop_ids",[]) if i in state["stops"] and state["stops"][i]["city"] == city and i not in ids and not is_dining(stops[i]))
                if change.get("fewer_activities"):
                    ids = ids[:2]
            day_weather = old_days.get(day_date,{}).get("weather",{}) if repair_dates is not None else self.daily_weather(weather,city,day_date)
            window = await self.window(state,route_day)
            start, end, gaps, transfers, tail = window
            hotel = chosen_hotel(itinerary,city,day_date)
            locked_ids = {a["id"] for a in old_days.get(day_date,{}).get("activities",[]) if a["id"] in state.get("locks",[]) and not is_dining(a)}
            time_locked_ids = set(locked_ids)
            ids.extend(identity for identity in locked_ids if identity in stops and identity not in ids)
            choices = [stops[i] for i in ids if i in stops]
            required = set(state.get("stop_constraints",{}).get("required",[]))
            required_keys = {canonical_key(stops[identity]) for identity in required if identity in stops}
            locked_ids |= {identity for identity in ids if canonical_key(stops[identity]) in required_keys}
            ids = [identity for identity in ids if identity not in state.get("stop_constraints",{}).get("excluded",[])]
            locked_ids |= set(ids) & set(change.get("add_stop_ids",[]))
            choices = [stops[i] for i in ids if i in stops]
            day_payload = None
            preserved_activities = {}
            if not change.get("refresh_resources") and any(stop["city"] == city and not is_dining(stop) for stop in stops.values()):
                edge_evidence = []
                for first,second in zip(ids,ids[1:]):
                    move,_ = await self.movement(state,city,stops[first]["name"],stops[second]["name"],stops[first].get("map_place"),stops[second].get("map_place"))
                    edge_evidence.append({"origin_id":first,"destination_id":second,"movement":move})
                day_payload = {"city":city,"date":day_date,"day_adjustment":route_day.get("adjustment",""),"requirements":req,"change":change,"user_request":state.get("revision_request","") if not state.get("assessment_dates") or day_date in state["assessment_dates"] else "",
                     "current_activities":old_days.get(day_date,{}).get("activities",[]),
                     "effective_max_walk_per_day":walking_limit(req,day_date),
                     "scheduled_stop_ids":[a["id"] for day in days for a in day["activities"] if a["kind"] == "visit"],
                     "time_window":{"start":self.clock(start),"end":self.clock(end),"end_place":tail["name"],"end_label":tail["label"]},
                     "available_minutes":max(0,end-start),"hotel":hotel,"weather":day_weather,"locked_ids":sorted(locked_ids),
                     "constraints":state.get("stop_constraints",{}),"decisions":state.get("decision_history",[]),"route_evidence":edge_evidence,
                     "preserved_activities":[activity for activity in old_days.get(day_date,{}).get("activities",[]) if (state.get("revision_selected_ids") or state.get("repair_only")) and activity["kind"] == "visit" and activity["id"] in time_locked_ids and activity["id"] not in state.get("revision_selected_ids",[])]}
                turn = await self.daily_order(state,route_day,ids,locked_ids,day_payload)
                if turn.status == "needs_decision":
                    turn.question.affected_dates = [date.fromisoformat(day_date)]
                    state = {**state,"decision":state.get("decision") or turn.question.model_dump(mode="json")}
                    ordering = OrderChoice(stop_ids=ids,explanation="重要取舍等待你回答，当前安排为草稿")
                else:
                    ordering = turn.order
                if state.get("revision_selected_ids") or state.get("repair_only"):
                    for activity in old_days.get(day_date,{}).get("activities",[]):
                        if activity["id"] in time_locked_ids and activity["id"] in ordering.stop_ids and activity["id"] not in state.get("revision_selected_ids",[]):
                            preserved_activities[activity["id"]] = activity
                            beginning,ending = timestamp(activity["start"],day_date),timestamp(activity["end"],day_date)
                            if beginning and ending and ending > beginning:
                                ordering.durations[activity["id"]] = round((ending-beginning).total_seconds()/60)
                ids = ordering.stop_ids
                ids.extend(identity for identity in change.get("add_stop_ids",[]) if identity in stops and stops[identity]["city"] == city and (not stops[identity].get("assigned_date") or stops[identity]["assigned_date"] == day_date) and identity not in ids)
            elif choices:
                ordering = OrderChoice(stop_ids=ids,explanation="保留当前活动，更新报价与移动路线")
                for activity in old_days.get(day_date,{}).get("activities",[]):
                    if activity["kind"] == "visit" and activity["id"] in ids:
                        preserved_activities[activity["id"]] = activity
                        beginning,ending = timestamp(activity["start"],day_date),timestamp(activity["end"],day_date)
                        if beginning and ending and ending > beginning:
                            ordering.durations[activity["id"]] = round((ending-beginning).total_seconds()/60)
            else:
                ordering = OrderChoice(stop_ids=[],explanation="没有可追溯的活动，保留时间缺口")
            await locate_selected(ordering)
            day = await self.schedule_activity_day(state,route_day,ordering,locked_ids,window,day_weather,preserved_activities)
            feedback = early_finish(day)
            if (feedback and day_payload and not ordering.short_day_reason.strip() and not state.get("decision")
                    and not state.get("keep_current_plan") and not state.get("revision_selected_ids") and not state.get("repair_only") and not change.get("fewer_activities")):
                self.emit(state,"activities",day_date+"较早结束，正在结合剩余时间补充下午安排")
                turn = await self.daily_order(state,route_day,ids,locked_ids,
                    {**day_payload,"previous_order":ordering.model_dump(mode="json"),"schedule_feedback":feedback})
                if turn.status == "needs_decision":
                    turn.question.affected_dates = [date.fromisoformat(day_date)]
                    state = {**state,"decision":turn.question.model_dump(mode="json")}
                else:
                    ordering = turn.order
                    await locate_selected(ordering)
                    day = await self.schedule_activity_day(state,route_day,ordering,locked_ids,window,day_weather,preserved_activities)
                    feedback = early_finish(day)
            if feedback:
                reason = ordering.short_day_reason.strip() or (
                    "按本次减少活动的要求保留留白" if change.get("fewer_activities") else
                    "保留当前行程或选中项以外的安排" if state.get("keep_current_plan") or state.get("revision_selected_ids") or state.get("repair_only") or change.get("refresh_resources") else
                    "存在待回答的取舍，暂保留草稿" if state.get("decision") else
                    "本轮排程仍未补齐下午活动，请补充攻略或调整当天安排")
                day["gaps"].append("当天游览于"+feedback["last_visit_end"]+"结束，扣除返程参考预留后仍余约"+str(feedback["remaining_minutes"])+"分钟："+reason)
            if repair_dates is not None:
                quotes = {item["id"]:item["ticket_quote"] for item in old_days.get(day_date,{}).get("activities",[]) if item.get("ticket_quote")}
                for activity in day["activities"]:
                    if activity["id"] in quotes:
                        activity["ticket_quote"] = copy.deepcopy(quotes[activity["id"]])
            days.append(day)
        itinerary["days"] = days
        if state.get("old_itinerary"):
            itinerary = enforce_locks(state["old_itinerary"],itinerary,state["locks"])
        for day in itinerary["days"]:
            if repair_dates is not None and day["date"] not in repair_dates:
                continue
            day["activities"] = [activity for activity in day["activities"] if not is_dining(activity)]
            self.update_day_places(state,day,refresh_dining=False)
        mobility_changed = req.get("local_transport","public") != state.get("old_requirements",req).get("local_transport","public")
        await self.refresh_day_routes(state,itinerary,None if mobility_changed else rebuilt_dates,
                                      refresh_routes=mobility_changed or bool(state.get("assessment_refresh")) or bool(state.get("old_itinerary") and state.get("locks")))
        if repair_dates is None:
            await self.query_ticket_prices(state,itinerary,rebuilt_dates,missing_only=True)
        return {"itinerary":itinerary,"stops":stops,"weather":weather,"warnings":state.get("warnings",[]),"resource_retry_done":state.get("resource_retry_done",False),"decision":state.get("decision",{})}

    async def tickets(self,state):
        self.emit(state,"tickets","正在整理攻略票种，仅为涉及门票的活动查询参考价")
        itinerary = copy.deepcopy(state["itinerary"])
        await self.query_ticket_prices(state,itinerary,{day["date"] for day in itinerary["days"]},missing_only=True)
        return {"itinerary":itinerary}

    async def query_ticket_prices(self,state,itinerary,rebuilt_dates,missing_only=False):
        stops = state.get("stops",{})
        eligible = set()
        targeted = state.get("change",{}).get("resource_ids") or []
        for day in itinerary["days"]:
            if day["date"] not in rebuilt_dates:
                continue
            for activity in day["activities"]:
                if activity["kind"] != "visit":
                    continue
                policy = ticket_policy(activity)
                activity["ticket_query_required"] = ticket_query_required(activity)
                if "ticket:"+activity["id"] in targeted or activity["ticket_query_required"] or activity.get("product"):
                    eligible.add((day["date"],activity["id"]))
                elif policy in {"free","not_applicable"}:
                    activity.pop("ticket_quote",None)
                elif (activity.get("ticket_quote") or {}).get("price") is None and not (activity.get("ticket_quote") or {}).get("options"):
                    activity.pop("ticket_quote",None)
        if not eligible:
            return
        skip_quotes = set()
        if missing_only:
            current_time = timestamp(now(),now()[:10])
            for day in itinerary["days"]:
                for activity in day["activities"]:
                    quote = activity.get("ticket_quote") or {}
                    if quote and not activity.get("product"):
                        quote = activity["ticket_quote"] = default_ticket_price(quote)
                    queried = timestamp(quote.get("queried_at"),day["date"])
                    has_quote = quote.get("price") is not None and not quote.get("assumed_free") or bool(quote.get("options"))
                    retry_missing = state.get("change",{}).get("refresh_resources") and not has_quote
                    if not retry_missing and "ticket:"+activity["id"] not in targeted and quote.get("visit_date") == day["date"] and quote.get("status") != "pending_confirmation" and queried and 0 <= (current_time-queried).total_seconds() < 600:
                        skip_quotes.add((day["date"],activity["id"]))
        if self.flyai.availability()["ready"]:
            ticket_cache, ticket_error = {},None
            for day in itinerary["days"]:
                if day["date"] not in rebuilt_dates:
                    continue
                for activity in day["activities"]:
                    if (day["date"],activity["id"]) not in eligible:
                        continue
                    if (day["date"],activity["id"]) in skip_quotes:
                        continue
                    targeted = state.get("change",{}).get("resource_ids") or []
                    if targeted and "ticket:"+activity["id"] not in targeted:
                        previous = next((item for old_day in state.get("old_itinerary",{}).get("days",[]) for item in old_day["activities"] if item["id"] == activity["id"]),{})
                        if previous.get("ticket_quote"):
                            activity["ticket_quote"] = copy.deepcopy(previous["ticket_quote"])
                        continue
                    if activity.get("product"):
                        activity["ticket_quote"] = {"price":None,"note":"采用的体验商品费用单独待核对，避免与酒店、交通或门票重复计费","booking_url":activity["product"].get("booking_url")}
                        continue
                    if ticket_error:
                        activity["ticket_quote"] = {"price":None,"status":"paused","note":ticket_error,"queried_at":now()}
                        self.store.query(state["trip_id"],"flyai",{"kind":"poi","city":day["city"],"name":activity["name"]},
                                         {"status":"paused","message":ticket_error,"queried_at":now()})
                        continue
                    self.emit(state,"tickets","正在查询景点门票参考价："+activity["name"])
                    place_name = (activity.get("map_place") or stops.get(activity["id"],{}).get("map_place") or {}).get("name") or activity["name"]
                    key = (day["city"],place_name,day["date"])
                    try:
                        if key not in ticket_cache:
                            ticket_cache[key] = await self.resource_agent.ticket({**state,"itinerary":itinerary},day["city"],place_name,day["date"],fresh=bool(state.get("change",{}).get("refresh_resources") or "ticket:"+activity["id"] in targeted))
                        activity["ticket_quote"] = copy.deepcopy(ticket_cache[key])
                    except ToolError as exc:
                        activity["ticket_quote"] = {"price":None,"status":"error","code":exc.code,"note":str(exc),"queried_at":now()}
                        if exc.code in {"flyai_config","flyai_failed","flyai_timeout","flyai_auth","flyai_network","flyai_rate_limit","flyai_parameters","flyai_runtime"}:
                            ticket_error = str(exc)+"；本轮剩余门票查询已暂停，可更新配置后重查"
        else:
            unavailable = self.flyai.availability().get("message") or "飞猪查询组件尚未就绪"
            for day in itinerary["days"]:
                for activity in day["activities"]:
                    if (day["date"],activity["id"]) in eligible:
                        if (day["date"],activity["id"]) in skip_quotes:
                            continue
                        targeted = state.get("change",{}).get("resource_ids") or []
                        previous = next((item for old_day in state.get("old_itinerary",{}).get("days",[]) for item in old_day["activities"] if item["id"] == activity["id"]),{})
                        if targeted and "ticket:"+activity["id"] not in targeted and previous.get("ticket_quote"):
                            activity["ticket_quote"] = copy.deepcopy(previous["ticket_quote"])
                            continue
                        if activity.get("product"):
                            activity["ticket_quote"] = {"price":None,"note":"采用的体验商品费用单独待核对","booking_url":activity["product"].get("booking_url")}
                            continue
                        activity["ticket_quote"] = {"price":None,"status":"not_configured","note":"门票未查询："+unavailable,"queried_at":now()}
                        self.store.query(state["trip_id"],"flyai",{"kind":"poi","city":day["city"],"name":activity["name"]},
                                         {"status":"not_configured","message":unavailable,"queried_at":now()})
        for day in itinerary["days"]:
            if day["date"] in rebuilt_dates:
                for activity in day["activities"]:
                    if activity.get("ticket_quote"):
                        if not activity.get("product"):
                            activity["ticket_quote"] = default_ticket_price(activity["ticket_quote"])
                        activity["ticket_quote"]["visit_date"] = day["date"]

    def update_day_places(self,state,day,refresh_dining=True):
        stops = state.get("stops",{})
        for activity in day["activities"]:
            place = stops.get(activity["id"],{}).get("map_place")
            if coordinates(place):
                activity["map_place"] = place
                activity["canonical_place_id"] = canonical_key(stops[activity["id"]])
                activity["source_references"] = stops[activity["id"]].get("source_references",[])
                activity["possible_duplicate_ids"] = stops[activity["id"]].get("possible_duplicate_ids",[])
            place = activity.get("map_place")
            if coordinates(place):
                activity["map_url"] = map_link(activity["name"],day["city"],place["location"])
                activity["navigation_url"] = navigation_link(None,place)
        if refresh_dining:
            day["meal_recommendations"] = meal_recommendations(day,stops,state.get("stop_constraints",{}).get("excluded",[]),requirements=state.get("requirements",{}))
            self.meal_links(day)

    @staticmethod
    def meal_links(day):
        for recommendation in day.get("meal_recommendations",[]):
            recommendation["map_url"] = map_link(recommendation["name"],day["city"],recommendation["map_place"]["location"])

    async def refresh_day_routes(self,state,itinerary,dates=None,refresh_routes=True,missing_only=False):
        # Locks and repairs can change predecessors. Re-query those legs without changing locked timestamps.
        meal_dates = set(dates) if dates is not None else None
        mobility = Requirements.model_validate(state["requirements"]).local_transport
        allowed_modes = {"walking","transit"} if mobility == "public" else {mobility}
        if state["requirements"].get("allow_taxi"):
            allowed_modes.add("driving")
        async def refresh_move(old, city, origin, destination, origin_place=None, destination_place=None):
            matches = []
            for field,point in (("origin_place",origin_place),("destination_place",destination_place)):
                stored = coordinates((old or {}).get(field))
                current = coordinates(point)
                label, name = ("origin",origin) if field == "origin_place" else ("destination",destination)
                matches.append(stored == current if stored and current else (old or {}).get(label,name) == name)
            same_leg = all(matches)
            if same_leg:
                origin_place = origin_place or (old or {}).get("origin_place")
                destination_place = destination_place or (old or {}).get("destination_place")
            usable = same_leg and (old or {}).get("minutes") is not None and (old or {}).get("mode") in allowed_modes
            first = coordinates(origin_place or (old or {}).get("origin_place"))
            last = coordinates(destination_place or (old or {}).get("destination_place"))
            long_walk = mobility == "public" and (old or {}).get("mode") == "walking" and (
                (old.get("walking_meters") or 0) > 1500 or (old.get("minutes") or 0) > 25 or first and last and distance(first,last) > 1500)
            if missing_only and usable and not long_walk:
                return old,destination_place
            move, place = await self.movement(state,city,origin,destination,origin_place=origin_place,destination_place=destination_place)
            if (usable and not long_walk and str(old.get("evidence_id","")).startswith("query:")
                    and move.get("minutes") is None):
                return {**old,"refresh_failed":True,"failure_codes":move.get("failure_codes",[]),
                        "refresh_note":"本次补查未成功，沿用同一起终点和允许交通方式的上轮查询，耗时并非本次更新："+move.get("note","")},destination_place
            return move,place
        for day in itinerary["days"]:
            if dates is not None and day["date"] not in dates:
                continue
            original_count = len(day["activities"])
            day["activities"] = [activity for activity in day["activities"] if not is_dining(activity)
                                 and not is_dining(state.get("stops",{}).get(activity["id"],{}))]
            removed_dining = len(day["activities"]) != original_count
            self.update_day_places(state,day,refresh_dining=False)
            if removed_dining and meal_dates is not None:
                meal_dates.add(day["date"])
            if dates is not None and day["date"] not in dates:
                if not removed_dining:
                    continue
            day_refresh = refresh_routes or removed_dining
            hotel = chosen_hotel(itinerary,day["city"],day["date"])
            scope = daily_route_scope(itinerary,day)
            if not scope["hotel_location_confirmed"]:
                hotel = None
            origin = (hotel or {}).get("name")
            destination = origin
            if scope["hotel_location_confirmed"] and not origin and day["city"] == state["requirements"]["origin"]:
                origin = state["requirements"].get("origin_address") or None
            for entry in itinerary.get("transport",[]):
                context, selected = entry["context"],entry.get("selected") or {}
                if context["date"] != day["date"]:
                    continue
                if context["destination"] == day["city"] and not hotel:
                    origin = selected.get("arrival_place") or origin
                if context["origin"] == day["city"]:
                    destination = selected.get("departure_place") or destination
            day["start_place"] = origin
            day["end_place"] = destination
            if not hotel:
                day["transfers"] = [transfer for transfer in day.get("transfers",[]) if transfer["label"] != "到达后前往住宿放行李"]
            total, estimated = 0,False
            previous_visit = None
            for activity in day["activities"]:
                if activity["kind"] != "visit":
                    continue
                if not origin:
                    activity["move_before"] = {}
                elif day_refresh:
                    activity["move_before"], place = await refresh_move(activity.get("move_before"),day["city"],origin,activity["name"],
                        origin_place=previous_visit.get("map_place") if previous_visit else (hotel or {}).get("map_place"),destination_place=activity.get("map_place"))
                    if coordinates(place):
                        activity["map_place"] = place
                move = activity.get("move_before") or {}
                if previous_visit:
                    total += move["minutes"] if move.get("minutes") is not None else move.get("estimate_minutes",45)
                    estimated |= move.get("minutes") is None
                origin = activity["name"]
                previous_visit = activity
            if not origin or not destination:
                day["return_move"] = {}
            elif day_refresh:
                day["return_move"], _ = await refresh_move(day.get("return_move"),day["city"],origin,destination,
                    origin_place=previous_visit.get("map_place") if previous_visit else None)
            tail = day["return_move"]
            tail_minutes = tail["minutes"] if tail.get("minutes") is not None else tail.get("estimate_minutes",0)
            for transfer in day.get("transfers",[]):
                move = transfer["move"]
                if day_refresh and move.get("origin") and move.get("destination"):
                    transfer["move"], _ = await refresh_move(move,day["city"],move["origin"],move["destination"],
                        origin_place=move.get("origin_place"),destination_place=move.get("destination_place"))
                    move = transfer["move"]
            day["travel_minutes"],day["travel_estimated"] = total,estimated
            day["transport_transfers"] = transport_access_transfers(itinerary,day)
            movements = daily_movements(itinerary,day)
            day["walking_meters"] = sum(move.get("walking_meters") or 0 for move in movements)
            last = timestamp(day["activities"][-1]["end"],day["date"]) if day["activities"] else None
            tail_start = last.hour*60+last.minute+(20 if destination else 0) if last else day.get("available_start",9*60)
            day["return_start"],day["return_end"] = self.clock(tail_start),self.clock(tail_start+tail_minutes)
            day["tail_arrival"] = tail_start+tail_minutes
            self.update_day_places(state,day,refresh_dining=False)
        if not missing_only:
            refresh_meals(itinerary,state.get("stops",{}),state.get("stop_constraints",{}).get("excluded",[]),
                          dates=meal_dates,requirements=state.get("requirements",{}))
        for day in itinerary["days"]:
            if dates is None or day["date"] in dates:
                self.meal_links(day)

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
        if itinerary.get("personal_map") and plan_fingerprint(itinerary) != plan_fingerprint(state.get("old_itinerary") or state["itinerary"]):
            itinerary.pop("personal_map",None)
        return {"itinerary":itinerary}

    def after_check(self,state):
        # One shared repair budget is consumed after the independent review, including candidate assessment.
        return "save"

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
                        entry["buffer_minutes"] = 120 if option["kind"] == "flight" else 60
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
            if issue["code"] in {"arrival_conflict","departure_conflict","overlap","move_gap"}:
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
