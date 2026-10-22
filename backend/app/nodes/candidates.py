"""候选路线生成、评估与按用户意见修订。"""
import copy
from datetime import timedelta
from uuid import uuid4

from .. import ablation
from ..adapters import ToolError
from ..models import Candidate, CandidateRevision, Requirements
from ..rules import merge_candidate_duplicates, validate_candidate
from ..store import now
from ..routing import coordinates
from ..agents.route import resolve_stop_id
from ..dining import is_dining
from ..places import canonical_key, canonicalize_stops, planning_stop_pool, represented_ids
from ..conflicts import code_decision, trip_pool_question
from ..day_clustering import ORDINARY_LOCATION_LIMIT, build_spatial_plan, cluster_deviation, spatial_errors


class CandidateNodes:
    async def discover(self,state):
        enabled = not ablation.disabled("clustering")
        chosen = {s["id"]:s for s in self.selected_sources(state["trip_id"],state["selected_source_ids"])}
        stale_geography = any(source.get("extraction_version") in {1,2,3} for source in chosen.values())
        prepared = await self.prepare_guides(state) if "stops" not in state or stale_geography else {}
        stops = prepared.get("stops",state.get("stops",{}))
        if enabled:
            stops = copy.deepcopy(stops)
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
        clarification_ids = set(priority)
        if enabled:
            required = set(state.get("stop_constraints",{}).get("required",[])) | set(req.required_stop_ids) | set(state.get("locks",[]))
            priority = {i for i,s in eligible.items() if represented_ids([s]) & required or s.get("assigned_date")}
            city_anchors = [next((i for i in sorted(eligible) if eligible[i]["city"] == city),None) for city in req.cities]
            grouped = sorted({i for group in state.get("guide_brief",{}).get("groups",[])
                              for i in sorted(group.get("stop_ids",[]))[:2] if i in eligible})
            anchors = [i for i in dict.fromkeys([*city_anchors,*grouped,*sorted(eligible)])
                       if i and i not in priority and not coordinates(eligible[i].get("map_place"))]
            pool = [*sorted(priority),*anchors[:ORDINARY_LOCATION_LIMIT]]
        else:
            # Locate required places and a few anchors first; actual chosen days locate all their visits later.
            city_anchors = [next((identity for identity,stop in eligible.items() if stop["city"] == city),None) for city in req.cities]
            grouped = [identity for group in state.get("guide_brief",{}).get("groups",[]) for identity in group.get("stop_ids",[])[:2]
                       if identity in eligible]
            anchors = [identity for identity in dict.fromkeys([*city_anchors,*grouped,*eligible]) if identity and identity not in priority]
            pool = list(dict.fromkeys([*sorted(priority),*list(dict.fromkeys(anchors))[:24]]))
        located = await self.locate_stops(state,{identity:stops[identity] for identity in pool if not coordinates(stops[identity].get("map_place"))})
        stops = canonicalize_stops({identity:located.get(identity,stop) for identity,stop in stops.items()})
        if enabled:
            resolved_keys = {canonical_key(stops[i]) for i in eligible if coordinates(stops[i].get("map_place"))}
            clarification_ids = {i for i in clarification_ids if canonical_key(stops[i]) not in resolved_keys}
        if clarification := self.place_clarification(stops,clarification_ids):
            return {"stops":stops,"selected_route":{},"decision_resume":"discover",**clarification}
        weather = await self.weather_context(state)
        parsed_guides = [{"id":s["id"],"title":s["title"],"likes":s.get("likes",0),"favorites":s.get("favorites",0),
                         "published_at":s.get("published_at"),"guide":{key:value for key,value in s["extraction"].items() if key != "stops"}}
                        for s in chosen.values() if s.get("extraction")]
        dates = [(req.start_date + timedelta(days=i)).isoformat() for i in range((req.end_date-req.start_date).days+1)]
        available,place_groups = planning_stop_pool({identity:stop for identity,stop in stops.items()
                                                   if not is_dining(stop) and not represented_ids([stop]) & excluded},
                                                  priority,state.get("locks",[]))
        plan = build_spatial_plan({**state,"stops":stops},available) if enabled else None
        if enabled and not ablation.disabled("code_checks"):
            if plan["status"] == "conflict":
                raise ToolError("route_constraints","空间分日固定日期或城市约束冲突："+"；".join(plan["issues"]))
            if plan["status"] == "partial":
                warnings.append("空间分日初始分配未完整通过校验，已保留建议并交路线模型补齐："+"；".join(plan["issues"]))
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
        if enabled:
            payload["spatial_plan"] = plan
            instruction += ("spatial_plan 是代码按候选游玩时长、固定日期、已解析闭馆信息和坐标形成的初始分日。"
                + ("status=ready 时城市日期段已由代码确定，不得改变。" if plan["status"] == "ready" else
                   "status=partial 时可调整各城天数以补齐必去项，但须保持城市顺序与日期全覆盖。")
                + "status=ready 时以 days 为基准，只做有依据的微调：跨天移动加删除的游玩时长最多占初始已定位时长 25%，新增已定位可选项时长也最多占 25%；初始已定位项不超过 4 个时可例外单移一个或互换两个，但不能同时删点或加点。保留必去、锁定和 assigned_date 项，遵守 day_budgets，不为降低偏离而虚增时长。unlocated_stop_ids 没有空间依据，可在合法日期选择安排，不得虚构其远近。每个跨天移动必须在目标日 adjustment 写：移日：地点名|原日期|新日期|具体原因；每次移动单独一行。删除初始可选项须说明原因，新增备选须仍满足每日容量。status=partial 时补齐 issues，不得把未完成的初始建议冒充可执行方案。时间预算为估计，实际到离与酒店仍以随后查询为准。")
        def checked(proposal):
            normalized = merge_candidate_duplicates(proposal,req,stops,state.get("locks",[]))
            if any(before.stop_ids != after.stop_ids for before,after in zip(proposal.days,normalized.days)):
                self.emit(state,"candidates","已合并候选中的重复地点引用，保留全部攻略来源并继续校验")
            if enabled and plan["status"] == "ready":
                for day in normalized.days:
                    initial = next((d for d in plan["days"] if d["date"] == day.date.isoformat()),None)
                    if initial and day.stop_ids == initial["stop_ids"] and not day.adjustment:
                        day.adjustment = initial["adjustment"]
            candidate = validate_candidate(normalized,req,stops)
            if not ablation.disabled("code_checks"):
                used = represented_ids([stops[i] for day in candidate.days for i in day.stop_ids])
                if set(state.get("stop_constraints",{}).get("required",[])) - used:
                    raise ValueError("路线遗漏用户明确必去的景点，应提出取舍问题而不是删除")
                if used & set(state.get("stop_constraints",{}).get("excluded",[])):
                    raise ValueError("路线包含用户明确排除的景点")
                if enabled and (errors := spatial_errors(plan,candidate.model_dump(mode="json"),stops,state["requirements"])):
                    raise ValueError("；".join(errors))
            return candidate
        candidates = []
        for attempt in range(2):
            turn = await self.route_agent.plan(state,instruction,payload,lambda request:self.agent_tool({**state,"stops":stops},request))
            if turn.status == "needs_decision":
                question,options = turn.question.model_dump(mode="json"),{}
                # Too few stops for the user's daily minimum is asked with code-built options before any route exists.
                coded = trip_pool_question({**state,"stops":stops},lambda:self.store.sources(state["trip_id"]))
                if coded:
                    question,options = coded[0],{"question_id":coded[0]["id"],"options":coded[1]}
                return {"stops":stops,"weather":weather,"decision":question,"decision_options":options,"decision_resume":"discover"}
            try:
                for day in turn.candidate.days:
                    day.stop_ids = [resolve_stop_id(identity,available,day.city) or identity for identity in day.stop_ids]
                candidate = checked(turn.candidate)
                if enabled and plan["status"] == "ready" and not ablation.disabled("code_checks"):
                    deviation = cluster_deviation(plan,candidate.model_dump(mode="json"),stops,state["requirements"])
                    if deviation["exceeded"] or deviation["missing_reasons"]:
                        if not attempt:
                            payload = {**payload,"cluster_deviation":deviation}
                            raise ValueError("路线微调违反空间分日约定："+str(deviation))
                        candidate = checked(Candidate(title=f"{len(dates)}天空间分日方案",
                            reason="路线模型微调超出约定，采用代码初始分配",days=copy.deepcopy(plan["days"]),gaps=copy.deepcopy(plan["issues"])))
                        warnings.append("路线模型两次微调仍超出空间分日约定，已采用代码初始分配；后续仍将结合实际交通与开放信息检查。")
                if enabled:
                    for day in candidate.days:
                        initial = next((d for d in plan["days"] if d["date"] == day.date.isoformat()),None)
                        if initial:
                            initial_note = initial["adjustment"]
                            if set(day.stop_ids) != set(initial["stop_ids"]):
                                for sentence in initial_note.split("。")[1:]:
                                    if sentence:
                                        day.adjustment = "\n".join(line if line.strip().startswith("移日：") else line.replace(sentence+"。","")
                                                                   for line in day.adjustment.splitlines())
                                initial_note = initial_note.split("。",1)[0]+"。"
                            day.adjustment = "；".join(dict.fromkeys(part for note in (initial_note,day.adjustment) for part in note.split("；") if part)).replace("；移日：","\n移日：")
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
                        question,options = probe["decision"],{}
                        # A scheduling question about a day that cannot fit or has run out of stops gets code-built options;
                        # place and hub choices keep their own selection flow.
                        if not probe.get("place_selection"):
                            dates = question.get("affected_dates") or [day["date"] for day in candidate["days"]]
                            coded = code_decision({**probe,"selected_route":copy.deepcopy(candidate)},probe["itinerary"],
                                                  [str(day) for day in dates],lambda:self.store.sources(state["trip_id"]))
                            if coded:
                                question,options = coded[0],{"question_id":coded[0]["id"],"options":coded[1]}
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
                                "place_selection":probe.get("place_selection",{}),"decision_options":options,
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
