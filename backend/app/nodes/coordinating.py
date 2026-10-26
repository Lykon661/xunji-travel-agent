import copy

from .. import assumptions, authority, coordinator
from ..actions import execute
from ..constraints import trip_dates
from ..models import DecisionBatch, UnmetItem
from ..places import plan_fingerprint
from ..planning_repairs import change_dependencies, identity_stop_ids, protected_stops
from ..reviewing import evidence_ids, review_context, review_context_state
from ..store import Conflict
from ..unmet import collect, fingerprint


DOCUMENT_FIELDS = ("coordinator_messages","system_assumptions","coordinator_confirmation",
                   "coordinator_batch","unmet","draft_revision",
                   "system_durations","system_free_time","stop_constraint_sources","stops","repair_round","decision_history",
                   "decision_snapshots","duplicate_review_generation")


def persisted(state):
    return {key:copy.deepcopy(state[key]) for key in DOCUMENT_FIELDS if key in state}


def public_document(document):
    result = copy.deepcopy(document)
    for key in ("coordinator_batch","coordinator_confirmation","decision_snapshots"):
        result.pop(key,None)
    def clean(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {"options","alternatives"} and isinstance(item,list):
                    for option in item:
                        if isinstance(option,dict):
                            option.pop("actions",None)
                clean(item)
        elif isinstance(value,list):
            for item in value:
                clean(item)
    clean(result)
    for item in result.get("system_assumptions",[]):
        item["stop_names"] = [document.get("stops",{}).get(identity,{}).get("name",identity) for identity in item.get("stop_ids",[])]
    return result


def pending_batch(state, confirming=False):
    batch = DecisionBatch.model_validate(state["coordinator_batch"]).model_dump(mode="json",exclude_none=True)
    for question in batch["questions"]:
        question.pop("unmet_refs",None)
        for option in question["options"]:
            option.pop("actions",None)
            option["recommended"] = option["id"] == question["recommended_option_id"]
    result = {**batch,"round_id":batch["batch_id"],"kind":"decision_confirmation" if confirming else "decision",
              "message":"请确认主理人对回答的理解" if confirming else batch["summary"]}
    if confirming:
        confirmation = state["coordinator_confirmation"]
        interpretation = confirmation["interpretation"]
        result["interpretation"] = {key:copy.deepcopy(interpretation[key]) for key in
            ("interpretation_id","summary","changes","relaxed")}
        result["interpretation"].update(original_answers=confirmation["original_answers"],original_note=confirmation["original_note"])
    return result


def identity_unmet(state):
    items = []
    relevant = identity_stop_ids(state)
    for identity,stop in state.get("stops",{}).items():
        if identity not in relevant:
            continue
        if not stop.get("map_selected_id") and not stop.get("map_place") and len(stop.get("map_candidates",[])) >= 2:
            items.append(UnmetItem(kind="place_identity",refs=[{"type":"stop","id":identity}],
                                   detail="现有地点候选未能唯一对应。"))
    return items


class CoordinatingNodes:
    async def apply_assumption_change(self,state):
        before = copy.deepcopy(state)
        request = state["assumption_change"]
        proposed,ledger = assumptions.replace(state,request["assumption_id"],request["alternative_id"])
        updates,_,_ = execute(state,proposed,"user")
        work = {**copy.deepcopy(state),**updates,**ledger}
        plan = self.coordinator_plan(before)
        report = before.get("review") or (before.get("candidates") or [{}])[0].get("review")
        work["review_previous"] = self.review_snapshot(before,plan,report)
        work["review_change"] = change_dependencies(before,work,proposed)
        work["coordinator_previous_plan"] = copy.deepcopy(plan)
        protected = authority.user_state(work)
        updated,items = await self.reassess(work,work["review_change"]["replan_dates"])
        if authority.user_changes(protected,authority.user_state(work)):
            restored = copy.deepcopy(before)
            coordinator._message(restored,"系统重排会改动你的选择，已撤回，请重新选择")
            return {"assumption_change":None,"coordinator_messages":restored["coordinator_messages"],
                    "coordinator_resume":"review_gate"}
        coordinator._message(work,"已切换系统假设："+"；".join(coordinator.actions._summary(before,action) for action in proposed))
        work.update(itinerary=updated,unmet=[item.model_dump(mode="json") for item in items],assumption_change=None,
                    coordinator_previous_plan=None,coordinator_resume="review_gate",coordinator_batch=None,coordinator_confirmation=None)
        work.update(self.pack_candidate(work,work["candidates"][0],updated,work["review"]))
        return {key:value for key,value in work.items() if state.get(key) != value}

    def coordinator_plan(self,state,proposed=None):
        plans = [proposed,state.get("itinerary"),
                 (state.get("candidates") or [{}])[0].get("draft"),
                 (state.get("selected_route") or {}).get("draft"),state.get("old_itinerary"),
                 (state.get("review_previous") or {}).get("plan")]
        return copy.deepcopy(next((plan for plan in plans if plan and plan.get("days")),
                                  next((plan for plan in plans if plan),{})))

    def review_snapshot(self,state,plan,report):
        if not report:
            return None
        sources = self.selected_sources(state["trip_id"],state.get("selected_source_ids",[]))
        return {"plan":copy.deepcopy(plan),"report":copy.deepcopy(report),"context":review_context(state,sources),
                "context_state":review_context_state(state),"sources":copy.deepcopy(sources),
                "queries":copy.deepcopy(self.store.review_queries(state["trip_id"],list(evidence_ids(plan))))}

    async def reassess(self,state,dates):
        if not self.coordinator_plan(state).get("days"):
            current = identity_unmet(state)
            state["unmet"] = [item.model_dump(mode="json") for item in current]
            return self.coordinator_plan(state),current
        old = self.coordinator_plan(state,state.get("coordinator_previous_plan"))
        report = state.get("review") or (state.get("candidates") or [{}])[0].get("review")
        previous = state.get("review_previous") or self.review_snapshot(state,old,report)
        probe = copy.deepcopy(state)
        route = copy.deepcopy((probe.get("candidates") or [probe.get("selected_route")])[0])
        dependency = state.get("review_change") or {}
        resource_scopes = dependency.get("resource_scopes",[])
        probe.update(selected_route=route,itinerary=self.coordinator_plan(state),old_itinerary=old,mode="modify",repair_only=True,assessment_dates=list(dates),agent_unmet=[],unmet=[],
            change={**state.get("change",{}),"scopes":[*resource_scopes,"activity"],"dates":list(dates),
                    "cities":dependency.get("resource_cities",[])},review_previous=previous,resource_retry_done=True)
        for day in probe["old_itinerary"].get("days",[]):
            for item in day.get("activities",[]):
                if item.get("id") in probe.get("locks",[]):
                    continue
                anchors = [entry["at"] for kind in ("time_anchor","booking_prerequisite")
                           for entry in coordinator.actions.active_constraints(probe["requirements"],kind,date=day["date"],stop_id=item["id"]) if entry.get("at")]
                current = next((activity for row in probe["itinerary"].get("days",[]) if row["date"] == day["date"]
                                for activity in row.get("activities",[]) if activity.get("id") == item["id"]),{})
                if not current.get("appointment_time") or anchors and set(anchors) != {item.get("start")}:
                    item.pop("appointment_time",None)
                    item.pop("appointment_time_source",None)
        for scope,operation in (("transport",self.transport),("hotel",self.hotels)):
            if scope in resource_scopes:
                probe.update(await operation(probe))
        if dates:
            probe.update(await self.activities(probe))
            probe["agent_unmet"] = copy.deepcopy(probe.get("unmet",[]))
            probe.update(await self.tickets(probe))
        probe.update(await self.check(probe))
        plan = probe["itinerary"]
        if not plan.get("days") and old.get("days"):
            items = collect(state,old,report)
            items.append(UnmetItem(kind="other",dates=list(dates),detail="本轮排程未返回逐日草稿，已保留上一版路线。"))
            state.update(itinerary=copy.deepcopy(old),unmet=[item.model_dump(mode="json") for item in items])
            return old,items
        untouched = {day["date"]:day for day in old.get("days",[]) if day["date"] not in dates}
        plan["days"] = [copy.deepcopy(untouched.get(day["date"],day)) for day in plan.get("days",[])]
        revision = state.get("draft_revision",0) + (plan_fingerprint(old) != plan_fingerprint(plan))
        probe["draft_revision"] = revision
        report = await self.audit(probe,plan)
        probe["review"] = report
        items = collect(probe,plan,report)
        state.update({key:copy.deepcopy(probe[key]) for key in ("stops","weather","itinerary","warnings","candidates","selected_route") if key in probe})
        state["system_assumptions"] = copy.deepcopy(probe.get("system_assumptions",[]))
        state.update(draft_revision=revision,review=report,review_previous=None,review_change=None,agent_unmet=probe.get("agent_unmet",[]),
                     coordinator_previous_plan=copy.deepcopy(plan),
                     unmet=[item.model_dump(mode="json") for item in items])
        for candidate in state.get("candidates",[]):
            candidate.update(draft=copy.deepcopy(plan),review=copy.deepcopy(report),draft_revision=revision)
        return plan,items

    async def coordinator(self,state):
        work = copy.deepcopy(state)
        plan = self.coordinator_plan(work)
        all_items = [UnmetItem.model_validate(item) for item in work.get("unmet",[])]
        relevant = identity_stop_ids(work)
        all_items = [item for item in all_items if item.kind != "place_identity" or
                     any(ref.type == "stop" and ref.id in relevant for ref in item.refs)]
        early = not plan.get("days") and (work.get("coordinator_resume") in {"discover","assess"}
            or any(item.kind in {"place_identity","hub_identity"} for item in all_items))
        if early:
            protected = protected_stops(work)
            known = {fingerprint(item):item for item in all_items + identity_unmet(work)}
            current = [item for item in known.values() if item.kind in {"place_identity","hub_identity"} and (
                item.kind == "hub_identity" or any(ref.id in protected and
                    len(work.get("stops",{}).get(ref.id,{}).get("map_candidates",[])) >= 2 for ref in item.refs))]
            work["coordinator_deferred_unmet"] = [item.model_dump(mode="json") for item in all_items if item not in current]
        else:
            all_items = collect(work,plan,work.get("review"))
            current = all_items
        work["coordinator_previous_plan"] = copy.deepcopy(plan)
        outcome = await coordinator.run(work,plan,current,self.coordinator_agent,self.reassess)
        work.update(outcome.state_updates)
        work.update(itinerary=outcome.itinerary,coordinator_batch=outcome.batch.model_dump(mode="json") if outcome.batch else None,
                    coordinator_confirmation=outcome.confirmation.model_dump(mode="json") if outcome.confirmation else None,
                    coordinator_blocked=bool(outcome.remaining_blockers) and outcome.batch is None,
                    coordinator_early=early,coordinator_previous_plan=None)
        if not early and work.get("candidates") and outcome.itinerary.get("days"):
            work.update(self.pack_candidate(work,work["candidates"][0],outcome.itinerary,work.get("review") or work["candidates"][0].get("review")))
        return {key:value for key,value in work.items() if state.get(key) != value}

    @staticmethod
    def after_coordinator(state):
        if state.get("coordinator_confirmation"):
            return "decision_confirmation_gate"
        if state.get("coordinator_batch"):
            return "coordinator_gate"
        if state.get("coordinator_blocked"):
            return "blocked_plan"
        return state.get("coordinator_resume") or "review_gate"

    async def interpret_answer(self,state):
        before = copy.deepcopy(state)
        answer = copy.deepcopy(state["coordinator_answer"])
        batch = copy.deepcopy(state["coordinator_batch"])
        if answer.get("draft_revision") is None:
            answer["draft_revision"] = batch["draft_revision"]
        try:
            outcome = await coordinator.apply_answer(state,batch,answer,self.coordinator_agent)
        except ValueError as error:
            raise Conflict(str(error)) from error
        work = {**copy.deepcopy(state),**outcome.state_updates}
        if outcome.confirmation:
            return {**outcome.state_updates,"coordinator_answer":{},"coordinator_confirmation":outcome.confirmation.model_dump(mode="json")}
        history = work["decision_history"]
        entry = history[-1]
        entry.update(questions=copy.deepcopy(batch["questions"]))
        proposed = entry.get("actions",[])
        if answer.get("confirm"):
            proposed = state["coordinator_confirmation"]["interpretation"]["actions"]
        elif answer.get("selected_options") and not answer.get("note") and not answer.get("answers"):
            proposed = [action for question in batch["questions"] for option in question["options"]
                        if answer["selected_options"].get(question["id"]) == option["id"] for action in option["actions"]]
        dependency = change_dependencies(before,work,proposed)
        dates = dependency["replan_dates"]
        if not proposed and not dates:
            stops = {ref["id"] for ref in entry["targets"] if ref["type"] == "stop"}
            dates = sorted({day["date"] for day in before.get("itinerary",{}).get("days",[]) if any(item.get("id") in stops for item in day.get("activities",[]))}) or trip_dates(work["requirements"])
        old_plan = self.coordinator_plan(before)
        old_report = before.get("review") or (before.get("candidates") or [{}])[0].get("review")
        work["coordinator_previous_plan"] = old_plan
        work["review_previous"] = self.review_snapshot(before,old_plan,old_report)
        work["review_change"] = dependency
        protected = authority.user_state(work)
        plan,items = await self.reassess(work,dates)
        if authority.user_changes(protected,authority.user_state(work)):
            restored = copy.deepcopy(before)
            coordinator._message(restored,"系统重排会改动你的选择，已撤回，请重新选择")
            return {"coordinator_messages":restored["coordinator_messages"],"coordinator_answer":{},
                    "coordinator_confirmation":None,"coordinator_batch":batch}
        work.update(itinerary=plan,unmet=[item.model_dump(mode="json") for item in items],coordinator_answer={},
                    coordinator_batch=None,coordinator_confirmation=None,coordinator_previous_plan=None)
        from ..coordinator import _relevant_hash
        entry["situation_hash"] = _relevant_hash(work,entry["targets"])
        if work.get("candidates"):
            work.update(self.pack_candidate(work,work["candidates"][0],plan,work.get("review")))
        return {key:value for key,value in work.items() if before.get(key) != value}

    async def blocked_plan(self,state):
        doc = self.store.trip(state["trip_id"])["document"]
        work = copy.deepcopy(state)
        plan = self.coordinator_plan(work)
        if not plan.get("days"):
            plan = self.coordinator_plan(doc)
        if plan.get("days"):
            work["itinerary"] = plan
            if work.get("candidates"):
                candidate = work["candidates"][0]
                candidate["draft"] = copy.deepcopy(plan)
                candidate["confirmation_blockers"] = list(dict.fromkeys([
                    *candidate.get("confirmation_blockers",[]),
                    *[item.get("detail") or "仍有待解决的行程阻断" for item in work.get("unmet",[]) if item.get("blocking",True)]]))
        doc.update(persisted(state))
        doc.update(candidates=copy.deepcopy(work.get("candidates") or doc.get("candidates",[])),decision_history=copy.deepcopy(state.get("decision_history",[])),
                   requirements=state["requirements"],stop_constraints=state.get("stop_constraints",{}),pending=None,
                   stage="conflicts",progress_stage="reviewing",workflow_step="routes",selected_route={})
        if plan.get("days"):
            doc.update(itinerary=copy.deepcopy(plan),review=copy.deepcopy(state.get("review") or doc.get("review")),
                       itinerary_outdated=False)
        self.store.update(state["trip_id"],doc)
        return {"coordinator_blocked":True,"itinerary":plan,"candidates":work.get("candidates") or doc.get("candidates",[])}
