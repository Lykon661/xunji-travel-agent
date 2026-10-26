import copy
import hashlib
import json
import re
import uuid

from . import actions, assumptions, authority
from .decisions import route_structure
from .models import CoordinatorAnswer, CoordinatorConfirmation, CoordinatorOutcome, DecisionBatch, DecisionInterpretation, UnmetItem
from .planning_repairs import change_dependencies
from .store import now
from .views import stop_summary, activity_summary, coordinator_view


ANSWER_SNAPSHOT_DEFAULTS = {
    "candidates":[], "selected_route":{}, "itinerary":{}, "requirements":{},
    "stop_constraints":{}, "stop_constraint_sources":{}, "selected_source_ids":[], "locks":[],
    "system_durations":{}, "system_free_time":{}, "system_assumptions":[], "keep_current_plan":False,
    "draft_revision":0, "warnings":[], "unmet":[], "review":None, "repair_round":0,
    "duplicate_review_generation":"", "coordinator_resume":"review_gate",
}


def _json(value):
    return value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else copy.deepcopy(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _items(items):
    return [UnmetItem.model_validate(item) for item in items]


def _updates(before, after):
    return {key:copy.deepcopy(value) for key, value in after.items() if key not in before or value != before[key]}


def situation(state, itinerary, unmet):
    context = assumptions.agent_context(state)
    identities = {ref.id for item in _items(unmet) for ref in item.refs if ref.type == "stop"}
    return coordinator_view(state,{**context,
        "days":[{"date":day["date"],"city":day.get("city"),
                 "activities":[activity_summary(state,item,day["date"]) for item in day.get("activities",[])]}
                for day in itinerary.get("days",[])],
        "budget":itinerary.get("budget",{}),"unmet":[_json(item) for item in _items(unmet)],
        "relevant_stops":[stop_summary(state,state["stops"][identity]) for identity in sorted(identities) if identity in state.get("stops",{})]})


def _signature(state):
    return _hash({"user":authority.user_state(state), "route":route_structure(actions._plan(state)),
                  "days":actions._route_days(state), "durations":state.get("system_durations",{}),
                  "free_time":state.get("system_free_time",{}),
                  "pois":{key:value.get("map_selected_id") for key,value in state.get("stops",{}).items()}})


def _target_refs(state, proposed):
    targets = set()
    for action in proposed:
        targets.update(actions._targets(state,actions._actions([action])))
    return [{"type":kind,"id":identity} for kind,identity in sorted(targets)]


def _target_pairs(refs):
    return {(item["type"],item.get("id") or item.get("field")) for item in refs}


def _relevant_hash(state, refs):
    pairs = _target_pairs(refs)
    req = authority.user_state(state)["requirements"]
    dates = {identity for kind,identity in pairs if kind == "day"}
    stop_ids = {identity for kind,identity in pairs if kind == "stop"}
    fields = {identity for kind,identity in pairs if kind == "requirement"}
    plan = actions._plan(state)
    return _hash({"requirements":{key:value for key,value in req.items() if key in fields or key in {
                      "cities","start_date","end_date","constraints","required_stop_ids","excluded_stop_ids","wish_places"}},
                  "stops":{key:value for key,value in state.get("stops",{}).items() if key in stop_ids},
                  "days":[day for day in actions._route_days(state) if day["date"] in dates],
                  "schedule":[day for day in plan.get("days",[]) if day["date"] in dates],
                  "resources":{key:plan.get(key,[]) for key in ("transport","hotels")},
                  "budget":plan.get("budget") if "budget" in fields else None,
                  "durations":{date:value for date,value in state.get("system_durations",{}).items() if date in dates},
                  "sources":state.get("selected_source_ids",[]), "hubs":authority.user_state(state).get("hubs",{})})


def _effects(proposed):
    effects = {}
    for action in actions._actions(proposed) if proposed else []:
        kind, stop = action["type"], action.get("stop_id")
        if kind == "set_budget":
            effects[("budget",)] = action["amount"]
        elif kind == "set_time_window":
            effects[("time",action.get("field") or stop or action.get("resource_id"),tuple(action.get("dates",[])))] = action.get("value",action.get("at"))
        elif kind in {"shorten","default_duration"}:
            effects[("duration",stop,tuple(actions._scope(action)))] = action["minutes"]
        elif kind in {"set_walk_limit","exempt_walk_limit","restore_walk_limit"}:
            for date in action["dates"]:
                effects[("walk",date)] = (kind,action.get("meters"))
        elif kind in {"select_poi","select_hub"}:
            effects[("place",stop or (action["city"],action["name"]))] = action["poi_id"]
        elif kind in {"skip","exclude","move","add_optional_stop","replace_optional_stop"}:
            effects[("placement",stop)] = (kind,action.get("date"),action.get("to"),action.get("replacement_stop_id"))
        elif kind == "reorder_day":
            effects[("order",action["date"])] = action["stop_ids"]
        elif kind == "set_hotel_area":
            effects[("hotel",action["city"])] = action["area"]
    return effects


def _compatible(existing, proposed):
    incoming = _effects(proposed)
    if any(key in existing and existing[key] != value for key,value in incoming.items()):
        raise ValueError("Recommended actions conflict on the same target")
    return {**existing,**incoming}


def _message(state, text, decision_index=None):
    item = {"id":uuid.uuid4().hex,"text":text,"time":now(),"decision_index":decision_index}
    state.setdefault("coordinator_messages",[]).append(item)
    return item


def _alternatives(state, before, action, supplied=None):
    if action["type"] == "omit_unselected_hotel_legs":
        return []
    proposed = list(supplied or [])
    kind, stop, date = action["type"],action.get("stop_id"),action.get("date")
    if kind == "default_duration":
        minutes = before.get("system_durations",{}).get(date,{}).get(stop) or before["stops"][stop].get("duration_minutes") or 90
        proposed += [[{"type":"default_duration","stop_id":stop,"date":date,"minutes":minutes}]]
    elif kind in {"skip","replace_optional_stop"}:
        proposed += [[{"type":"add_optional_stop","stop_id":stop,"date":date}]]
        proposed += [[{"type":"exclude","stop_id":stop,"date":date}]]
    elif kind == "add_optional_stop":
        proposed += [[{"type":"skip","stop_id":stop,"date":date}]]
    elif kind == "retain_free_time":
        proposed += [[{"type":"retain_free_time","date":date,"free_minutes":0 if action.get("free_minutes",60) else 60}]]
    elif kind == "reorder_day":
        original = next((day["stop_ids"] for day in actions._route_days(before) if day["date"] == date),[])
        proposed += [[{"type":"reorder_day","date":date,"stop_ids":original}]]
    elif kind == "select_poi":
        proposed += [[{"type":"select_poi","stop_id":stop,"poi_id":item["id"]}]
                     for item in state["stops"][stop].get("map_candidates",[]) if item["id"] != action["poi_id"]]
    result, seen = [], set()
    for alternative in proposed:
        key = _hash(alternative)
        option = {"id":"alt-"+key[:16],"label":"改用此安排","actions":alternative}
        ok, _ = actions.validate_option({**state,"unmet":[]},option)
        if ok and key not in seen:
            updates,_,summary = actions.execute(state,alternative,"user")
            if _signature({**state,**updates}) != _signature(state):
                result.append({**option,"label":summary[:200],"description":summary[:2000]})
                seen.add(key)
        if len(result) == 2:
            break
    return result


def _record_defaults(state, before, proposed, supplied=None):
    for action in actions._actions(proposed):
        alternatives = _alternatives(state,before,action,supplied)
        if not alternatives:
            continue
        title = actions._summary(before,action)
        state.update(assumptions.record(state,{"id":"default-"+_hash(action)[:24],"title":title[:200],
            "dates":actions._scope(action),"stop_ids":[action["stop_id"]] if action.get("stop_id") else [],
            "current_action":action,"reason":"系统根据当前草稿采用的默认安排，可选择其他方案。",
            "alternatives":alternatives}))


async def run(state, itinerary, unmet, agent, reassess):
    from .tradeoffs import prepare
    work = copy.deepcopy(state)
    work["itinerary"] = copy.deepcopy(itinerary)
    start_messages = len(work.get("coordinator_messages",[]))
    prepared = prepare(work,work["itinerary"],unmet)
    if prepared.automatic_actions:
        before = copy.deepcopy(work)
        updates,_,summary = actions.execute(work,prepared.automatic_actions,"system")
        work.update(updates)
        work["review_change"] = change_dependencies(before,work,prepared.automatic_actions)
        plan,items = await reassess(work,prepared.affected_dates)
        authority.assert_system_action(before,work)
        _record_defaults(work,before,prepared.automatic_actions)
        _message(work,summary)
        prepared = prepare(work,plan,items,automatic=False)
    else:
        plan = copy.deepcopy(itinerary)
    batch = prepared.batch
    if batch is not None:
        batch = await agent.word(work,batch)
    work.update(unmet=[_json(item) for item in prepared.remaining])
    blockers = [item for item in prepared.remaining if item.blocking]
    if blockers and batch is None:
        _message(work,"当前草稿仍有需要处理的硬性要求，已保留路线与具体原因。")
    return CoordinatorOutcome(state_updates=_updates(state,work),itinerary=plan,batch=batch,
        messages=work.get("coordinator_messages",[])[start_messages:],remaining_blockers=blockers)


def _confirmation_hash(state):
    return _hash({key:value for key,value in state.items() if key not in {
        "coordinator_confirmation","coordinator_messages","active_run_id","run_id","base_version","generation",
        "coordinator_answer","user_inputs","round_backup","rollback","status","pending","events"}})


def _current_quotes(state, proposed, quotes, texts):
    shadow = copy.deepcopy(state)
    indexed = {item["action_index"]:item["quote"] for item in quotes}
    clauses = [[re.sub(r"\s+","",part) for part in re.split(r"[。，；！？,.;!?、\r\n]+",text)
                if re.sub(r"\s+","",part)] for text in texts]
    def complete_quote(quote):
        for parts in clauses:
            for start in range(len(parts)):
                candidate = ""
                for part in parts[start:]:
                    candidate += part
                    if len(candidate) >= len(quote):
                        if candidate == quote:
                            return True
                        break
        return False
    for index,action in enumerate(proposed):
        update,_,_ = actions.execute(shadow,[action],"user")
        candidate = {**shadow,**update}
        if authority.user_changes(authority.user_state(shadow),authority.user_state(candidate)):
            quote = re.sub(r"[\s。，；！？,.;!?、]+","",indexed.get(index,""))
            if not quote or not complete_quote(quote):
                return False
        shadow.update(update)
    return True


async def apply_answer(state, batch, response, agent):
    batch = DecisionBatch.model_validate(batch)
    answer = CoordinatorAnswer.model_validate(response)
    if (answer.batch_id or answer.round_id) != batch.batch_id or answer.draft_revision != batch.draft_revision or state.get("draft_revision",batch.draft_revision) != batch.draft_revision:
        raise ValueError("Stale decision batch or draft revision")
    raw_batch = batch.model_dump(mode="json")
    questions = {item["id"]:item for item in raw_batch["questions"]}
    if (set(answer.selected_options) | set(answer.answers)) - questions.keys():
        raise ValueError("Unknown question ID")
    work = copy.deepcopy(state)
    start_messages = len(work.get("coordinator_messages",[]))
    selected, effects = [], {}
    for qid,oid in answer.selected_options.items():
        option = next((item for item in questions[qid]["options"] if item["id"] == oid),None)
        if option is None:
            raise ValueError("Unknown option ID")
        effects = _compatible(effects,option["actions"])
        selected += option["actions"]
    original_answers, original_note = dict(answer.answers),answer.note
    quotes = None
    if answer.confirm:
        pending = CoordinatorConfirmation.model_validate(state.get("coordinator_confirmation") or {})
        if (pending.batch_id != batch.batch_id or pending.interpretation.interpretation_id != answer.interpretation_id
                or pending.state_hash != _confirmation_hash(state) or pending.batch_hash != _hash(raw_batch)):
            raise ValueError("Stale interpretation confirmation")
        proposed = pending.interpretation.model_dump(mode="json")["actions"]
        original_answers,original_note = pending.original_answers,pending.original_note
    elif answer.answers or answer.note:
        interpretation = DecisionInterpretation.model_validate(await agent.interpret(copy.deepcopy(work),raw_batch,
            dict(answer.answers),answer.note,situation(work,actions._plan(work),work.get("unmet",[]))))
        interpreted = interpretation.model_dump(mode="json")
        option_ids = {option["id"] for question in questions.values() for option in question["options"]}
        if set(interpreted["matched_option_ids"]) - option_ids:
            raise ValueError("Interpretation references unknown options")
        _compatible(effects,interpreted["actions"])
        proposed = selected+interpreted["actions"]
        if proposed:
            actions._actions(proposed)
        else:
            interpreted["ambiguities"].append("尚未得到可执行的具体安排，请补充回答。")
        if not answer.note.strip() and set(answer.selected_options) | set(answer.answers) != set(questions):
            interpreted["ambiguities"].append("本批次仍有问题尚未回答。")
        preview = copy.deepcopy(work)
        if selected:
            update,_,_ = actions.execute(preview,selected,"user")
            preview.update(update)
        valid_quotes = _current_quotes(preview,interpreted["actions"],interpreted["quotes"],[*answer.answers.values(),answer.note])
        if interpreted["ambiguities"] or not valid_quotes:
            interpreted["actions"] = proposed
            interpreted["quotes"] = [{**item,"action_index":item["action_index"]+len(selected)} for item in interpreted["quotes"]]
            pending = CoordinatorConfirmation(batch_id=batch.batch_id,draft_revision=batch.draft_revision,
                interpretation=interpreted,original_answers=original_answers,original_note=original_note,
                state_hash=_confirmation_hash(state),batch_hash=_hash(raw_batch))
            return CoordinatorOutcome(state_updates={"coordinator_confirmation":pending.model_dump(mode="json")},
                itinerary=copy.deepcopy(actions._plan(state)),batch=batch,confirmation=pending,
                remaining_blockers=[item for item in _items(state.get("unmet",[])) if item.blocking])
        quotes = {"text":"\n".join([*answer.answers.values(),answer.note]),"quotes":interpreted["quotes"]}
    else:
        if set(answer.selected_options) != set(questions):
            raise ValueError("Every question requires an answer")
        proposed = selected
    actions._actions(proposed)
    targets = _target_refs(state,proposed)
    if quotes is not None and selected:
        update,_,first_summary = actions.execute(work,selected,"user")
        work.update(update)
        update,_,summary = actions.execute(work,proposed[len(selected):],"user",quotes=quotes)
        summary = first_summary+"；"+summary
    else:
        update,_,summary = actions.execute(work,proposed,"user",quotes=quotes)
    work.update(update)
    snapshot = {key:copy.deepcopy(state.get(key,default)) for key,default in ANSWER_SNAPSHOT_DEFAULTS.items()}
    before_stops,after_stops = state.get("stops",{}),work.get("stops",{})
    snapshot["stops"] = {identity:copy.deepcopy(before_stops.get(identity)) for identity in before_stops.keys() | after_stops.keys()
                         if before_stops.get(identity) != after_stops.get(identity)}
    work.setdefault("decision_snapshots",{})[batch.batch_id] = {
        "batch":copy.deepcopy(raw_batch),"snapshot":snapshot,"history_start":len(state.get("decision_history",[]))}
    history = work.setdefault("decision_history",[])
    entry = {"source":"decision","batch_id":batch.batch_id,"targets":targets,"summary":summary,"actions":copy.deepcopy(proposed),
             "answers":original_answers,"note":original_note,"selected_options":dict(answer.selected_options),
             "situation_hash":_relevant_hash(work,targets),"reopenable":True,"snapshot_id":batch.batch_id}
    for native in history[len(state.get("decision_history",[])):]:
        native.update(source="decision",targets=targets,summary=summary,situation_hash=entry["situation_hash"])
    history.append(entry)
    _message(work,summary,len(history)-1)["reopenable"] = True
    work["coordinator_confirmation"] = None
    return CoordinatorOutcome(state_updates=_updates(state,work),itinerary=copy.deepcopy(actions._plan(work)),
        messages=work["coordinator_messages"][start_messages:],
        remaining_blockers=[item for item in _items(work.get("unmet",[])) if item.blocking])
