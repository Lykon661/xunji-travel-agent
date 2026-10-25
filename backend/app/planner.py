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

from .models import Candidate, Requirements
from .rules import validate_candidate
from .store import Conflict, now
from .agents import GuideAgent, ResourceAgent, RouteAgent, ReviewerAgent
from .decisions import confirmation_blockers
from .places import plan_fingerprint
from .nodes.candidates import CandidateNodes
from .nodes.coordinating import CoordinatingNodes, pending_batch, persisted
from .agents.coordinator import CoordinatorAgent
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


class Planner(SubmitWorkflow, GuideNodes, CandidateNodes, ResourceNodes, LocationNodes, SchedulingNodes, ReviewLoopNodes, CoordinatingNodes):
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
        self.coordinator_agent = CoordinatorAgent(self.models,store)
        self.tasks: dict[str,asyncio.Task] = {}
        self.gates = WeakValueDictionary()
        self._checkpointer = checkpointer

    @cached_property
    def graph(self):
        started = perf_counter()
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(TripState)
        names = ("coordinate","requirements_gate","guides_gate","prepare_guides","guide_analysis","confirm_route",
                 "review_result","review_gate","planning_failed","discover","assess","selection_gate","revise_candidate",
                 "transport","hotels","activities","tickets","check","save","coordinator","coordinator_gate",
                 "interpret_answer","decision_confirmation_gate","blocked_plan","apply_assumption_change")
        for name in names:
            graph.add_node(name,getattr(self,name))
        graph.add_edge(START,"coordinate")
        graph.add_conditional_edges("coordinate",self.after_coordinate,{"requirements":"requirements_gate","guides":"guides_gate",
            "resources":"transport","assumption":"apply_assumption_change"})
        graph.add_edge("apply_assumption_change","coordinator")
        graph.add_edge("requirements_gate","coordinate")
        graph.add_edge("guides_gate","prepare_guides")
        graph.add_conditional_edges("prepare_guides",lambda s:"guide_analysis" if s.get("stops") else "selection_gate")
        for name in ("guide_analysis","discover","assess","review_result","planning_failed"):
            graph.add_edge(name,"coordinator")
        graph.add_conditional_edges("coordinator",self.after_coordinator,
            {name:name for name in ("coordinator_gate","decision_confirmation_gate","blocked_plan","discover","assess","review_gate")})
        graph.add_edge("coordinator_gate","interpret_answer")
        graph.add_edge("decision_confirmation_gate","interpret_answer")
        graph.add_conditional_edges("interpret_answer",lambda s:"decision_confirmation_gate" if s.get("coordinator_confirmation")
            else "coordinator_gate" if s.get("coordinator_batch") else "coordinator")
        graph.add_edge("blocked_plan",END)
        graph.add_conditional_edges("review_gate",self.after_review_gate)
        graph.add_conditional_edges("selection_gate",lambda s:"revise" if s.get("route_edit") else "guides" if not s.get("selected_route") else "confirm",
            {"revise":"revise_candidate","guides":"guides_gate","confirm":"confirm_route"})
        graph.add_edge("confirm_route","save")
        graph.add_edge("revise_candidate","assess")
        for first,second in zip(("transport","hotels","activities","tickets","check"),("hotels","activities","tickets","check","review_result")):
            graph.add_edge(first,second)
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



    async def coordinator_gate(self,state):
        answer = interrupt(pending_batch(state))
        return {"coordinator_answer":answer}

    async def decision_confirmation_gate(self,state):
        answer = interrupt(pending_batch(state,confirming=True))
        return {"coordinator_answer":answer}

    async def confirm_route(self,state):
        itinerary = copy.deepcopy(state["selected_route"]["draft"])
        blockers = confirmation_blockers(itinerary,state.get("stop_constraints"),state["requirements"],state.get("stops"))
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
                doc.update(persisted(result))
                doc.update(pending=pending,stage="waiting",requirements=result.get("requirements",doc["requirements"]),
                           selected_source_ids=result.get("selected_source_ids",[]),candidates=result.get("candidates",[]),warnings=result.get("warnings",[]),interview=result.get("interview") or None,
                           draft_revision=result.get("draft_revision",doc.get("draft_revision",0)),
                           system_assumptions=copy.deepcopy(result.get("system_assumptions",doc.get("system_assumptions",[]))),
                           stop_constraints=result.get("stop_constraints",{}),decision_history=result.get("decision_history",[]),rollback=result.get("rollback",{}))
                doc["workflow_step"] = {"requirements":"requirements","guides":"guides","sources":"guides","interview":"interview","interview_confirmation":"interview","decision":"interview","decision_confirmation":"interview","selection":"routes"}.get(pending.get("kind"),doc.get("workflow_step","interview"))
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
                trip = self.store.trip(run["trip_id"])
                doc = trip["document"]
                doc.update(persisted(result))
                self.store.update(run["trip_id"],doc)
                self.store.finish_run(run_id,"completed",{"version":self.store.trip(run["trip_id"])["version"]})
                self.store.event(run_id,"completed","仍有阻断，草稿已保留" if result.get("coordinator_blocked") else "本轮行程处理完成")
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
                    doc.update(persisted(snapshot.values))
                    doc.update(requirements=snapshot.values.get("requirements",doc["requirements"]),
                               system_assumptions=copy.deepcopy(snapshot.values.get("system_assumptions",doc.get("system_assumptions",[]))),
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
        doc.update(persisted(state))
        self.assert_review(state,(state.get("selected_route") or {}).get("review"),state["itinerary"])
        if doc.get("itinerary_generation") == state["generation"] or self.store.generation_version(state["trip_id"],state["generation"]) is not None:
            doc["system_assumptions"] = copy.deepcopy(state.get("system_assumptions",doc.get("system_assumptions",[])))
            itinerary = doc.get("itinerary") or state["itinerary"]
            doc["stage"] = "completed" if doc.get("itinerary_saved_version") else "conflicts" if any(i["blocking"] for i in itinerary.get("issues",[])) else "incomplete" if itinerary.get("budget",{}).get("unknown") or itinerary.get("information_unknown") or any(d.get("gaps") for d in itinerary.get("days",[])) else "ready"
            self.store.update(state["trip_id"],doc)
            return {}
        self.emit(state,"saving","正在整理行程草稿和来源记录")
        trip = self.store.trip(state["trip_id"]); doc = trip["document"]
        doc.update(persisted(state))
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
                   system_assumptions=copy.deepcopy(state.get("system_assumptions",doc.get("system_assumptions",[]))),
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
