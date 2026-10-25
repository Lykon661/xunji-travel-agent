"""用户入口：提交、回退步骤、重开决策、删除行程及其校验。"""
import asyncio
import copy
import logging
from uuid import uuid4

from ..models import Candidate, Requirements, RequirementUpdate
from ..rules import timestamp, validate_candidate
from ..store import Conflict, now, dump
from ..decisions import input_hash
from ..reviewing import review_repair_dates
from ..planning_recovery import previous_round
from ..authority import user_texts
from .state import NODE_STAGES
from ..nodes.coordinating import persisted


class SubmitWorkflow:
    async def change_assumption(self,trip_id,assumption_id,alternative_id,assumption_revision,draft_revision,base_version):
        from .. import actions, assumptions
        async with self.gate(trip_id):
            trip = self.active(trip_id)
            doc = trip["document"]
            item = next((item for item in doc.get("system_assumptions",[]) if item["id"] == assumption_id),None)
            if (trip["version"] != base_version or doc.get("draft_revision",0) != draft_revision or not item
                    or item.get("status","active") != "active" or item["revision"] != assumption_revision):
                raise Conflict("系统假设或行程版本已变化，请刷新后重新选择。")
            if (doc.get("pending") or {}).get("kind") not in {None,"selection"}:
                raise Conflict("请先完成当前主理人批次，再修改系统假设。")
            candidate = copy.deepcopy(doc.get("selected_route") or (doc.get("candidates") or [{}])[0])
            plan = copy.deepcopy(candidate.get("draft") or doc.get("itinerary") or {})
            if not plan.get("days") or not candidate.get("days"):
                raise Conflict("当前没有可修改的路线草稿，请先生成路线。")
            thread = f"{trip_id}:{uuid4().hex}"
            state = {**persisted(doc),"trip_id":trip_id,"generation":thread,"base_version":base_version,
                "requirements":copy.deepcopy(doc["requirements"]),"old_requirements":copy.deepcopy(doc["requirements"]),
                "selected_source_ids":doc.get("selected_source_ids",[]),"mode":"modify","change":{},
                "stops":{**self.source_stops(trip_id,doc.get("selected_source_ids",[])),**doc.get("stops",{})},
                "candidates":[candidate],"selected_route":{},"itinerary":plan,"old_itinerary":copy.deepcopy(plan),
                "review":copy.deepcopy(candidate.get("review") or doc.get("review")),"review_previous":None,"review_change":None,
                "decision_history":copy.deepcopy(doc.get("decision_history",[])),"stop_constraints":doc.get("stop_constraints",{}),
                "locks":doc.get("locks",[]),"warnings":doc.get("warnings",[]),"coordinator_batch":None,"coordinator_confirmation":None,
                "coordinator_resume":"review_gate","assumption_change":{"assumption_id":assumption_id,"alternative_id":alternative_id},
                "user_inputs":user_texts(doc),"preferences_original":doc.get("preferences_original",doc["requirements"].get("preferences",""))}
            try:
                proposed,_ = assumptions.replace(state,assumption_id,alternative_id)
                actions.execute(state,proposed,"user")
            except ValueError as error:
                raise Conflict("该系统假设的替代方案已不可用："+str(error)) from error
            run_id = self.store.start_run(trip_id,base_version,thread,{"request":"修改系统假设",**state["assumption_change"]})
            state["run_id"] = run_id
            try:
                await self.graph.aupdate_state({"configurable":{"thread_id":thread}},state,as_node="coordinate")
                doc.update(stage="queued",pending=None,workflow_step="routes")
                self.store.update(trip_id,doc)
                self.store.event(run_id,"progress","正在切换系统假设并复查相关日期",stage="queued")
            except Exception:
                self.store.finish_run(run_id,"failed",{"message":"假设修改提交未完成，原方案已保留"})
                raise
            task = asyncio.create_task(self.execute(run_id,None),name=run_id)
            self.tasks[run_id] = task
            task.add_done_callback(lambda _:self.tasks.pop(run_id,None))
            return run_id

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
            doc.setdefault("preferences_original",doc.get("requirements",{}).get("preferences",""))
            if doc.get("stage") == "draft" and doc.get("workflow_step") == "guides" and not trip.get("active_thread"):
                doc["preferences_original"] = doc.get("requirements",{}).get("preferences","")
            if requirements is not None and "preferences" in requirements:
                doc["preferences_original"] = requirements["preferences"]
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
            available_ids = set(self.source_stops(trip_id,source_ids,req))
            if not set(req.get("required_stop_ids",[])+req.get("excluded_stop_ids",[])) <= available_ids:
                raise Conflict("景点约束必须来自当前勾选的攻略，请先调整必去或排除项")
            config = {"configurable":{"thread_id":trip["active_thread"]},"recursion_limit":60}
            snapshot = await self.graph.aget_state(config) if trip["active_thread"] else None
            pending = doc.get("pending") or {}
            legacy_question = bool(snapshot and (set(snapshot.next) & {"decision_gate","interview_gate","interview"}
                or pending.get("kind") in {"decision","decision_confirmation"} and not pending.get("batch_id")))
            if interview_response is not None and legacy_question:
                raise Conflict("旧问题已切换到主理人批次，旧轮次不能提交；请继续任务，从已保存草稿重新诊断。")
            migrating = False
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
                validate_candidate(Candidate.model_validate(doc["selected_route"]),Requirements.model_validate(req),self.source_stops(trip_id,source_ids,req))
                valid_ids = {entry["id"] for group in ("transport","hotels") for entry in doc["itinerary"].get(group,[])}
                valid_ids.update("ticket:"+activity["id"] for day in doc["itinerary"].get("days",[]) for activity in day["activities"] if activity["kind"] == "visit")
                if resource_ids and (not set(resource_ids) <= valid_ids or {identity.removeprefix("ticket:") for identity in resource_ids} & set(doc.get("locks",[]))):
                    raise Conflict("要更新的资源已变化或已锁定，请先刷新或解锁")
            elif resource_ids:
                raise Conflict("指定资源只能用于更新报价")
            if legacy_question and (resume or retry) and not selection_changed and req == doc["requirements"]:
                candidate = copy.deepcopy((doc.get("candidates") or [doc.get("selected_route") or {}])[0])
                plan = copy.deepcopy(candidate.get("draft") or doc.get("itinerary") or {})
                if not plan.get("days"):
                    raise Conflict("旧问题没有可恢复的草稿，请重新生成路线。")
                if not candidate.get("days"):
                    candidate.update(id=uuid4().hex,title="恢复的草稿",reason="切换主理人重新诊断",days=[
                        {"date":day["date"],"city":day["city"],"stop_ids":[item["id"] for item in day.get("activities",[]) if item.get("kind") == "visit"]}
                        for day in plan["days"]])
                thread = f"{trip_id}:{uuid4().hex}"
                invocation = {**persisted(doc),"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":req,
                    "selected_source_ids":source_ids,"stops":{**self.source_stops(trip_id,source_ids,req),**doc.get("stops",{})},
                    "candidates":[candidate],"selected_route":candidate,"itinerary":plan,"old_itinerary":copy.deepcopy(plan),
                    "old_requirements":copy.deepcopy(req),"decision_history":doc.get("decision_history",[]),"locks":doc.get("locks",[]),
                    "stop_constraints":doc.get("stop_constraints",{}),"warnings":doc.get("warnings",[]),"mode":"modify","change":{},
                    "coordinator_batch":None,"coordinator_confirmation":None,"coordinator_resume":"review_gate"}
                migrating = True
            elif resume:
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
                if pending.get("kind") not in {"interview","interview_confirmation","decision","decision_confirmation"} or not snapshot or not snapshot.next:
                    raise Conflict("当前没有等待回答的追问，请刷新后重试")
                if selection_changed or req != Requirements.model_validate(doc["requirements"]).model_dump(mode="json") or interview_response["round_id"] != pending.get("round_id"):
                    raise Conflict("攻略、需求或追问轮次已变化，请重新开始或刷新")
                if pending.get("kind") in {"decision","decision_confirmation"}:
                    if not pending.get("batch_id") or interview_response["round_id"] != pending["batch_id"]:
                        raise Conflict("问题批次已变化，请刷新后回答当前主理人批次。")
                    if not interview_response.get("confirm") and interview_response.get("draft_revision") != pending.get("draft_revision"):
                        raise Conflict("路线草稿已变化，请刷新后回答当前批次。")
                    if interview_response.get("confirm") and pending["kind"] != "decision_confirmation":
                        raise Conflict("当前批次没有待确认的理解。")
                else:
                    raise Conflict("旧访谈问题已停用，请继续任务，从保存草稿重新诊断。")
                thread, invocation = trip["active_thread"], Command(resume=interview_response)
                message = "\n".join([*interview_response.get("answers",{}).values(),interview_response.get("note","")]).strip()
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
                if candidate.input_hash != input_hash(req,source_ids,self.source_stops(trip_id,source_ids,req),self.selected_sources(trip_id,source_ids)):
                    raise Conflict("需求或攻略内容已变化，请重新规划")
                self.assert_review({**doc,"trip_id":trip_id,"requirements":req},candidate.review,candidate.draft)
                try:
                    validate_candidate(candidate, Requirements.model_validate(req), self.source_stops(trip_id, source_ids, req))
                except ValueError as exc:
                    raise Conflict("候选路线与当前日期或所选攻略不一致，请重新生成：" + str(exc)) from exc
                thread, invocation = trip["active_thread"], Command(resume={"candidate_id":candidate_id,"draft_revision":candidate_revision,"plan_hash":candidate_hash})
            elif pending and snapshot and snapshot.next and not selection_changed and req == doc["requirements"] and not (retry and pending.get("kind") in {"selection","interview","interview_confirmation","decision","decision_confirmation"}):
                if pending.get("kind") in {"interview","interview_confirmation","decision","decision_confirmation"}:
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
                original_route = copy.deepcopy(doc.get("selected_route") or (doc.get("candidates") or [{}])[0])
                old = copy.deepcopy(doc.get("itinerary") or original_route.get("draft") or (snapshot.values.get("old_itinerary") if snapshot else {}) or {})
                source_stops = self.source_stops(trip_id,source_ids,req)
                invocation = {"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":req,"message":message,
                    "mode":"modify" if old and not retry else "new", "warnings":[], "candidates":[],
                    "selected_route":original_route, "selected_source_ids":source_ids,"old_itinerary":old,"itinerary":copy.deepcopy(old),
                    "stops":{**source_stops,**{key:value for key,value in doc.get("stops",{}).items() if key in source_stops}},
                    "change":{},"source_selection_changed":selection_changed,
                    "repair_round":doc.get("repair_round",0) if old and not retry and not selection_changed else 0,"locks":doc.get("locks",[]),
                    "interview":{},"route_edit":{},
                    "stop_constraints":{"required":req.get("required_stop_ids",[]),"excluded":req.get("excluded_stop_ids",[])},
                    "decision_history":doc.get("decision_history",[]) if not selection_changed else [],
                    "draft_revision":doc.get("draft_revision",0),"decision":{},"route_confirmed":False,
                    "previews":{},"preview_ready":False,
                    "old_requirements":copy.deepcopy(doc["requirements"])}
                if old and not retry and not selection_changed:
                    invocation.update(candidates=[copy.deepcopy(original_route)] if original_route.get("days") else [],
                        review_previous=self.review_snapshot({**doc,"trip_id":trip_id},old,doc.get("review") or original_route.get("review")),
                        review_change=None)
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
                    changed_entry["selected_by_user"] = True
                    changed_entry["reason"] = "你选择的备选；本次查询报价，实际库存需在预订页确认"
                    if group == "transport":
                        changed_entry["buffer_minutes"] = 120 if option["kind"] == "flight" else 60
                    invocation["change"] = {"scopes":[group],"cities":cities,"resource_choice":resource_choice,"user_selected_ids":[entry["id"]],"explanation":"采用你选择的备选，并重算相关日期"}
                    invocation["change"]["dates"] = [day["date"] for day in old["days"] if day["city"] == entry["city"]] if group == "hotel" else sorted({
                        value for value in [entry["context"]["date"],str(option.get("arrival") or "")[:10]] if value in {day["date"] for day in old["days"]}})
                if supplement_choice:
                    product = self.validate_supplement(doc,supplement_choice)
                    stop_id = "product:"+product["id"]
                    place = product["map_place"]
                    stop = {"id":stop_id,"city":supplement_choice["city"],"name":place["name"],"day":1,
                        "assigned_date":supplement_choice["date"],"duration_minutes":supplement_choice["duration_minutes"],"selected_by_user":True,
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
            if invocation is not None and candidate_id is None and route_edit is None and interview_response is None and not hotel_area_choice and (retry or selection_changed or req != doc["requirements"]) and not (isinstance(invocation,dict) and invocation.get("review_previous")):
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
                    for key,value in persisted(doc).items():
                        invocation.setdefault(key,value)
                    invocation["dialogue_version"] = 2
                    invocation["system_assumptions"] = copy.deepcopy(doc.get("system_assumptions",[]))
                    invocation.update(user_inputs=user_texts(doc),preferences_original=doc["preferences_original"])
                    invocation.update(round_backup=copy.deepcopy(backup or {}),rollback={})
                    if migrating:
                        await self.graph.aupdate_state({"configurable":{"thread_id":thread}},invocation,as_node="check")
                        invocation = None
                elif snapshot:
                    # IDs are runtime metadata, updated outside nodes before resuming their durable checkpoint.
                    metadata = {"run_id":run_id,"base_version":base,"requirements":req,"selected_source_ids":source_ids,"locks":doc.get("locks",[]),
                                "system_assumptions":copy.deepcopy(doc.get("system_assumptions",[])),
                                "user_inputs":user_texts(doc),"preferences_original":doc["preferences_original"],
                                "round_backup":copy.deepcopy(backup or {}),"rollback":{}}
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
            elif step == "interview" and source_ids and stops and not Requirements.model_validate(req).missing():
                resume_node = "prepare_guides"
            threads = self.store.trip_threads(trip_id)
            snapshot = await self.graph.aget_state({"configurable":{"thread_id":trip["active_thread"]}}) if trip["active_thread"] and resume_node else None
            thread = f"{trip_id}:{uuid4().hex}"
            state = {"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":req,"message":"",
                     "system_assumptions":copy.deepcopy(doc.get("system_assumptions",[])),
                     "mode":"new","change":{},"selected_source_ids":source_ids,"stops":stops,"locks":[],
                     "interview":interview,"candidates":candidates,"selected_route":{},"old_itinerary":{},
                     "itinerary":{},"old_requirements":copy.deepcopy(req),"warnings":[],
                     "previews":{c["id"]:{"itinerary":copy.deepcopy(c.get("draft",{})),"queried_at":now()} for c in candidates},
                     "preview_ready":False,"route_edit":{},"repair_round":0,"decision":{},"route_confirmed":False,
                     "draft_revision":candidates[0].get("draft_revision",0) if candidates else 0,
                     "stop_constraints":doc.get("stop_constraints",{}),"decision_history":doc.get("decision_history",[]),
                     "guide_brief":copy.deepcopy(snapshot.values.get("guide_brief",{})) if snapshot else {}}
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
            if index < 0 or index >= len(history) or not history[index].get("batch"):
                raise Conflict("该记录不是可重开的主理人批次，请从当前草稿重新诊断。")
            candidate = copy.deepcopy((doc.get("candidates") or [doc.get("selected_route") or {}])[0])
            if not candidate.get("draft"):
                raise Conflict("请先完成路线草稿，再修改已有回答")
            batch = copy.deepcopy(history[index]["batch"])
            batch.update(batch_id=uuid4().hex,draft_revision=doc.get("draft_revision",0))
            thread = f"{trip_id}:{uuid4().hex}"
            state = {**persisted(doc),"trip_id":trip_id,"generation":thread,"base_version":base,"requirements":doc["requirements"],
                "selected_source_ids":doc.get("selected_source_ids",[]),"stops":{**self.source_stops(trip_id,doc.get("selected_source_ids",[])),**doc.get("stops",{})},
                "mode":"modify","change":{},"locks":doc.get("locks",[]),"warnings":doc.get("warnings",[]),
                "candidates":[candidate],"selected_route":{},"old_itinerary":copy.deepcopy(candidate["draft"]),
                "old_requirements":copy.deepcopy(doc["requirements"]),"itinerary":copy.deepcopy(candidate["draft"]),
                "decision_history":copy.deepcopy(history),"stop_constraints":doc.get("stop_constraints",{}),
                "coordinator_batch":batch,"coordinator_confirmation":None,"coordinator_blocked":False,"coordinator_resume":"review_gate"}
            run_id = self.store.start_run(trip_id,base,thread,{"request":"重新回答已有选择","history_index":index})
            state["run_id"] = run_id
            try:
                await self.graph.aupdate_state({"configurable":{"thread_id":thread}},state,as_node="coordinator")
                doc.update(stage="queued",pending=None,workflow_step="interview")
                self.store.update(trip_id,doc)
            except Exception:
                self.store.finish_run(run_id,"failed",{"message":"批次恢复失败，原记录已保留"})
                raise
            task = asyncio.create_task(self.execute(run_id,None),name=run_id)
            self.tasks[run_id] = task
            task.add_done_callback(lambda _:self.tasks.pop(run_id,None))
            return run_id
