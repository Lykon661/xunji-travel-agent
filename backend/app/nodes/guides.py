"""需求协调、攻略解析、攻略 Agent 分析与访谈节点。"""
import re
from pathlib import Path
from uuid import uuid4

from ..adapters import ToolError, image_data, remote_image
from ..config import settings
from ..models import Candidate, ChangeRequest, InterviewRound, Requirements, RequirementUpdate
from ..rules import validate_candidate
from ..store import Conflict, dump
from ..routing import coordinates
from ..agents.runtime import digest
from ..decisions import apply_walking_decision, walking_question
from ..dining import is_dining
from ..workflow.state import TripState


class GuideNodes:
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
