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
from ..dining import is_dining, dining_wishes, visit_required_ids, plan_meal_wishes
from ..stop_types import with_visit_defaults
from ..places import canonical_key, canonicalize_stops, planning_stop_pool, represented_ids, represented_stop_ids
from ..models import UnmetItem
from ..day_clustering import ORDINARY_LOCATION_LIMIT, build_spatial_plan, recluster_candidate
from ..scheduler import clear_system_defaults
from ..assumptions import agent_payload
from ..planning_repairs import change_dependencies, protected_stops


class CandidateNodes:
    async def discover(self,state):
        defaults = clear_system_defaults(state)
        state = {**state,**defaults}
        result = {**defaults,**await self._discover(state)}
        result.setdefault("unmet",[])
        return {**result,"agent_unmet":[*state.get("coordinator_deferred_unmet",[]),*result["unmet"]],
                "coordinator_deferred_unmet":[],"coordinator_resume":"assess"}

    async def _discover(self,state):
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
                        ("map_place","map_candidates","map_selected_id","map_selected_by","map_issue","query_status",
                         "represented_by","representation_reason","represented_canonical_id","source_references") if key in previous})
        warnings = list(prepared.get("warnings",state.get("warnings",[])))
        req = Requirements.model_validate(state["requirements"])
        if prepared:
            chosen = {s["id"]:s for s in self.selected_sources(state["trip_id"],state["selected_source_ids"])}
        if not stops:
            return {"candidates":[],"stops":stops,"selected_route":{},"warnings":warnings}
        self.emit(state,"candidates","正在比较攻略路线，整理候选方案")
        sights = {identity:stop for identity,stop in stops.items() if not is_dining(stop)}
        if not sights and not dining_wishes(stops,state["requirements"]):
            return {"candidates":[],"stops":stops,"selected_route":{},"warnings":list(dict.fromkeys([
                *warnings,"所选攻略只有餐饮推荐，尚无可安排的游览景点；请补充包含景点的攻略"]))}
        priority = set(state.get("stop_constraints",{}).get("required",[])) & set(sights)
        excluded = set(state.get("stop_constraints",{}).get("excluded",[]))
        suppressed = represented_stop_ids({**state,"stops":stops})
        eligible = {identity:stop for identity,stop in sights.items() if not represented_ids([stop]) & excluded and identity not in suppressed}
        if not eligible and not dining_wishes(stops,state["requirements"]):
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
        pool = list(dict.fromkeys([*pool,*(identity for identity,stop in stops.items() if stop.get("source_kind") == "user")]))
        located = await self.locate_stops(state,{identity:stops[identity] for identity in pool if not coordinates(stops[identity].get("map_place"))})
        stops = canonicalize_stops({identity:with_visit_defaults(located.get(identity,stop)) for identity,stop in stops.items()})
        if enabled:
            resolved_keys = {canonical_key(stops[i]) for i in eligible if coordinates(stops[i].get("map_place"))}
            clarification_ids = {i for i in clarification_ids if not is_dining(stops[i]) and canonical_key(stops[i]) not in resolved_keys}
        clarification_ids = {i for i in clarification_ids if not is_dining(stops[i])}
        if clarification := self.place_clarification(stops,clarification_ids):
            return {"stops":stops,"selected_route":{},**clarification}
        weather = await self.weather_context(state)
        parsed_guides = [{"id":s["id"],"title":s["title"],"likes":s.get("likes",0),"favorites":s.get("favorites",0),
                         "published_at":s.get("published_at"),"guide":{key:value for key,value in s["extraction"].items() if key != "stops"}}
                        for s in chosen.values() if s.get("extraction")]
        dates = [(req.start_date + timedelta(days=i)).isoformat() for i in range((req.end_date-req.start_date).days+1)]
        suppressed = represented_stop_ids({**state,"stops":stops})
        available,place_groups = planning_stop_pool({identity:stop for identity,stop in stops.items()
                                                   if not is_dining(stop) and not represented_ids([stop]) & excluded and identity not in suppressed},
                                                  priority,state.get("locks",[]))
        plan = build_spatial_plan({**state,"stops":stops},available) if enabled else None
        if not available and dining_wishes(stops,state["requirements"]):
            plan = plan or build_spatial_plan({**state,"stops":stops},available)
            candidate = Candidate(id=uuid4().hex,title=f"{len(dates)}天用餐行程",reason="按城市日期段安排指定餐饮地点，游览时段留白",days=plan["days"],feasibility={"meal_wishes":plan.get("meal_wishes",{})})
            return {"candidates":[candidate.model_dump(mode="json")],"stops":stops,"weather":weather,"selected_route":{},"warnings":warnings}
        if enabled and not ablation.disabled("code_checks"):
            if plan["status"] == "conflict":
                raise ToolError("route_constraints","空间分日固定日期或城市约束冲突："+"；".join(plan["issues"]))
            if plan["status"] == "partial":
                warnings.append("空间分日初始分配未完整通过校验，后续由代码结合选点与排程处理："+"；".join(plan["issues"]))
        instruction = (
            "根据用户确认的访谈需求和勾选的攻略选择整趟景点集合，估计缺失时长与优先级；分天由代码按位置完成。不得使用未选择攻略或自行补景点。"
            "guides提供攻略整体说明、季节、提醒与缺口；景点明细及各攻略证据统一在available_stops和source_references中，不重复附攻略stops。"
            "每条路线的days必须与required_dates逐项完全一致，每个日期恰好一天，不得添加、缺少、重复或改变日期。"
            "trip_days是用户旅行天数，参考攻略原有天数不是旅行天数。例如用户3天、攻略5天时，必须删减或合并为3天，绝不能生成5天。"
            "路线标题和说明也必须与用户天数一致，城市按用户顺序连续停留。"
            "活动只引用available_stops中的景点ID，一项不可重复。普通攻略餐饮不加入stop_ids；source_kind=user的咖啡甜品饮品是必去游览，默认45分钟。正餐wish不占游览时长，代码结合meal_wishes安排日期与附近景点。用户确认的flyai来源体验只可放在assigned_date。"
            "canonical_place_groups中多个ID是同一实际地点的攻略引用，代表项的source_references保留全部来源，不必逐个安排。"
            "除用户明确再访授权外，全程每个实际地点只选择一次，不得虚构地点。"
            "按用户节奏选择全程游览量；每日容量、约60分钟午餐、全天园区独占一天与空间聚类均由代码处理。"
            "days仅承载所选ID；除用户固定日期外，不承担区域或主题分组职责。"
            "用户要求半天、留白或交通限制影响选点数量；不得虚增游玩时长、餐厅或未选攻略景点凑时长。"
            "在candidate.reason中说明选点取舍，days.adjustment由代码填写空间分日说明。地点未定位时不可虚构地理关系。"
            "weather只用于预报覆盖的对应日期；雨雪或恶劣天气优先已有攻略中的室内活动，保留必去项并说明风险；无预报不可编造天气。"
            "根据季节、交通方式和体力偏好取舍；所选攻略未覆盖某城市时保留空活动并写gap。"
            "尚未查询交通和酒店，不写实时报价、确定的到离时间。仅有一套有依据方案时只返回一套。")
        payload = {"requirements":state["requirements"],"required_dates":dates,"trip_days":len(dates),
                   "selected_source_ids":state["selected_source_ids"],"guides":parsed_guides,"available_stops":available,
                   "canonical_place_groups":place_groups,
                   "interview":state.get("interview",{}),"weather":weather,
                   "guide_brief":state.get("guide_brief",{}),"constraints":state.get("stop_constraints",{}),"decision_history":state.get("decision_history",[])}
        payload = agent_payload(state,payload)
        meal_plan = plan.get("meal_wishes",{}) if plan else {}
        payload["meal_wishes"] = meal_plan
        if enabled:
            payload["spatial_plan"] = plan
            instruction += ("spatial_plan提供代码计算的城市日期段；status=ready时保持城市日期段，status=partial时保持用户城市顺序并补齐日期。"
                "你只选择景点与估计时长/优先级，分天由代码按位置完成，不决定同日分组、每天顺序、起止时刻或实际交通。代码负责闭馆移日、容量校验、每日排序与补位。"
                "如有缺失游玩时长的估计，可在candidate.feasibility.durations中按地点ID给出分钟数；优先级建议放在feasibility.priorities中，使用recommended/optional/avoid。"
                "保留用户必去、想去、锁定和assigned_date，不能把你的建议当成新的用户硬要求。")
        draft_unmet = []
        location_attempts = set()
        async def locate_selected(proposal):
            nonlocal stops
            selected = {identity for day in proposal.days for identity in day.stop_ids}
            protected = protected_stops({**state,"stops":stops})
            selected.update(identity for identity,stop in available.items() if identity in protected or stop.get("assigned_date"))
            missing = {identity:stops[identity] for identity in selected if identity in stops and identity not in location_attempts
                       and not is_dining(stops[identity]) and not coordinates(stops[identity].get("map_place"))}
            if not missing:
                return
            location_attempts.update(missing)
            located = await self.locate_stops({**state,"stops":stops},missing)
            stops = canonicalize_stops({identity:with_visit_defaults(located.get(identity,stop)) for identity,stop in stops.items()})
            available.update({identity:stops[identity] for identity in available})

        def checked(proposal):
            normalized = merge_candidate_duplicates(proposal,req,stops,state.get("locks",[]))
            if any(before.stop_ids != after.stop_ids for before,after in zip(proposal.days,normalized.days)):
                self.emit(state,"candidates","已合并候选中的重复地点引用，保留全部攻略来源并继续校验")
            if enabled:
                for day in normalized.days:
                    day.adjustment = "空间分日：选点后由代码按位置、开放日期和每日容量重新分配。"
            candidate = validate_candidate(normalized,req,stops)
            if enabled:
                candidate = recluster_candidate({**state,"stops":stops},candidate,available)
            if not ablation.disabled("code_checks"):
                used = represented_ids([stops[i] for day in candidate.days for i in day.stop_ids])
                for identity in visit_required_ids(state.get("stop_constraints",{}).get("required",[]),stops,state["requirements"]) - used:
                    draft_unmet.append(UnmetItem(kind="stop_unplaced",refs=[{"type":"stop","id":identity}],detail="用户必去尚未纳入草稿。"))
                if used & set(state.get("stop_constraints",{}).get("excluded",[])):
                    raise ValueError("路线包含用户明确排除的景点")
            return candidate
        candidates = []
        for attempt in range(2):
            turn = await self.route_agent.plan(state,instruction,payload,lambda request:self.agent_tool({**state,"stops":stops},request))
            try:
                for day in turn.candidate.days:
                    day.stop_ids = [resolve_stop_id(identity,available,day.city) or identity for identity in day.stop_ids]
                await locate_selected(turn.candidate)
                candidate = checked(turn.candidate)
                candidate.id = uuid4().hex
                meal_plan = plan_meal_wishes([day.model_dump(mode="json") for day in candidate.days],stops,state["requirements"])
                estimates = candidate.feasibility.get("durations",{})
                priorities = candidate.feasibility.get("priorities",{})
                candidate.feasibility = {"meal_wishes":meal_plan,"spatial_unplaced":candidate.feasibility.get("spatial_unplaced",[]),
                    "durations":{identity:value for identity,value in estimates.items() if identity in available and isinstance(value,int) and not isinstance(value,bool) and 10 <= value <= 720} if isinstance(estimates,dict) else {},
                    "priorities":{identity:value for identity,value in priorities.items() if identity in available and isinstance(value,str) and value in {"recommended","optional","avoid"}} if isinstance(priorities,dict) else {}}
                candidates.append(candidate.model_dump(mode="json"))
                break
            except ValueError as exc:
                if attempt:
                    try:
                        await locate_selected(turn.candidate)
                        normalized = merge_candidate_duplicates(turn.candidate,req,stops,state.get("locks",[]))
                        if enabled:
                            for day in normalized.days:
                                day.adjustment = "空间分日：选点后由代码按位置、开放日期和每日容量重新分配。"
                        candidate = validate_candidate(normalized,req,stops)
                        if enabled:
                            candidate = recluster_candidate({**state,"stops":stops},candidate,available)
                    except ValueError as invalid:
                        raise ToolError("route_constraints","草稿未通过来源、日期或结构校验："+str(invalid)) from invalid
                    candidate.id = uuid4().hex
                    draft_unmet.append(UnmetItem(kind="other",refs=[{"type":"requirement","id":"constraints"}],detail=str(exc)[:2000]))
                    candidates.append(candidate.model_dump(mode="json"))
                    break
                payload = {**payload,"previous_candidate":turn.candidate.model_dump(mode="json"),"validation_error":str(exc)}
        if not candidates:
            warnings.append("未形成符合日期和来源约束的候选路线，请补充攻略后重试")
        return {"candidates":candidates,"stops":stops,"weather":weather,"selected_route":{},"warnings":list(dict.fromkeys(warnings)),"assessment_dates":[],"assessment_base":"","revision_rejected":False,"unmet":[item.model_dump(mode="json") for item in [*getattr(turn,"unmet",[]),*draft_unmet]]}

    async def assess(self,state):
        result = await self._assess(state)
        return {**result,"coordinator_resume":"review_gate"}

    async def _assess(self,state):
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
                     "old_itinerary":copy.deepcopy(state.get("old_itinerary",{})),"mode":"new","change":{},"locks":state.get("locks",[]),"repair_round":state.get("repair_round",0),
                     "resource_retry_done":True,"assessment":True}
            previous = state.get("previews",{}).get(state.get("assessment_base"))
            if not previous and state.get("review_previous") and state.get("assessment_dates"):
                previous = {"itinerary":state["review_previous"]["plan"]}
            if not previous and (state.get("revision_selected_ids") or state.get("assessment_refresh")) and candidate.get("draft",{}).get("days"):
                previous = {"itinerary":candidate["draft"]}
            if previous and state.get("assessment_dates"):
                probe.update(itinerary=copy.deepcopy(previous["itinerary"]),old_itinerary=copy.deepcopy(previous["itinerary"]),mode="modify",
                             repair_only=True,
                             change={"scopes":["activity"],"dates":state["assessment_dates"],"cities":[],"refresh_resources":bool(state.get("assessment_refresh"))})
            preserved = (previous or {}).get("itinerary") or candidate.get("draft")
            if state.get("revision_selected_ids") and preserved:
                fixed_ids = {activity["id"] for day in preserved.get("days",[]) if day["date"] in state.get("assessment_dates",[])
                             for activity in day["activities"] if activity["kind"] == "visit" and activity["id"] not in state["revision_selected_ids"]}
                probe["locks"] = sorted(set(probe["locks"]) | fixed_ids)
                probe["temporary_locks"] = sorted(set(state.get("temporary_locks",[])) | (fixed_ids-set(state.get("locks",[]))))
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
            candidate["feasibility"] = {**{key:value for key,value in candidate.get("feasibility",{}).items() if key in {"durations","priorities"}},
                **({"meal_wishes":itinerary["meal_wish_plan"]} if itinerary.get("meal_wish_plan") else {}),"queried_at":now(),"budget":itinerary["budget"],
                "issues":itinerary["issues"],"information_unknown":itinerary.get("information_unknown",[]),
                "transport":[{"label":entry["label"],"selected":{k:v for k,v in (entry.get("selected") or {}).items() if k != "raw"},"reason":entry["reason"]} for entry in itinerary["transport"]],
                 "hotels":[{"city":entry["city"],"selected_by_user":bool(entry.get("selected_by_user")),"name":(entry.get("selected") or {}).get("name"),"area":entry.get("search_areas",[]),"commute_minutes":(entry.get("selected") or {}).get("commute_minutes"),
                            "selected":{k:v for k,v in (entry.get("selected") or {}).items() if k != "raw"},"reason":entry["reason"],"errors":entry.get("errors",[]),"query_status":entry.get("query_status")} for entry in itinerary["hotels"]],
                "days":[{"date":day["date"],"city":day["city"],"available_start":day["available_start"],"available_end":day["tail_deadline"],
                         "travel_minutes":day.get("travel_minutes"),"gaps":day["gaps"]} for day in itinerary["days"]]}
            candidate["gaps"] = list(dict.fromkeys([*candidate.get("gaps",[]),*[gap for day in itinerary["days"] for gap in day["gaps"]]]))
            result = await self.assess_result(probe,candidate,itinerary)
            result["warnings"] = list(dict.fromkeys(warnings))
            result.setdefault("stops",probe["stops"])
            result.setdefault("weather",probe.get("weather",{}))
            result.setdefault("system_assumptions",copy.deepcopy(probe.get("system_assumptions",[])))
            result.update(assessment_dates=[],assessment_base="",assessment_refresh=False,keep_current_plan=False,repair_only=False,
                          review_previous=None,review_change=None)
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
        prior = copy.deepcopy(state)
        prior["itinerary"] = self.coordinator_plan(state,original.get("draft"))
        snapshot = self.review_snapshot(prior,prior["itinerary"],original.get("review"))
        for day in original["days"]:
            day["stop_ids"] = [identity for identity in day["stop_ids"] if not is_dining(state["stops"][identity])]
        required = [identity for identity in state.get("stop_constraints",{}).get("required",[]) if not is_dining(state["stops"][identity])]
        requirements = {**state["requirements"],"required_stop_ids":required}
        constraints = {**state.get("stop_constraints",{}),"required":required}
        state = {**state,"requirements":requirements,"stop_constraints":constraints}
        dependency = change_dependencies(prior,state,dates=edit["dates"])
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
                    "repair_round":state.get("repair_round",0),"keep_current_plan":False,
                    "review_previous":snapshot,"review_change":dependency}
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
                        "review_previous":None,"review_change":None,
                        "revision_selected_ids":[],"revision_reply":"这次调整没有满足日期、选择范围或攻略来源约束，已保留原路线："+str(exc),"warnings":list(dict.fromkeys([
                        *state.get("warnings",[]),"局部修改不符合日期或来源约束，原候选路线已保留："+str(exc)]))}
                payload = {**payload,"previous_patch":patch.model_dump(mode="json"),"validation_error":str(exc)}
        revised["id"] = uuid4().hex
        return {"candidates":[revised],"route_edit":{},"selected_route":{},"assessment_dates":edit["dates"],"assessment_base":original["id"],"assessment_refresh":False,"revision_rejected":False,
                "review_previous":snapshot,"review_change":dependency,
                "requirements":requirements,"stop_constraints":constraints,"revision_request":edit["content"],"revision_reply":patch.explanation,
                "revision_selected_ids":edit.get("selected_stop_ids",[]),"repair_round":state.get("repair_round",0),"keep_current_plan":False}

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
        meal_plan = plan_meal_wishes(revised["days"],stops,requirements)
        revised["feasibility"] = {**{key:value for key,value in original.get("feasibility",{}).items() if key in {"durations","priorities"}},"meal_wishes":meal_plan}
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
