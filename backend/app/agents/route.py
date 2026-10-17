import copy
from typing import Literal

from pydantic import Field

from ..adapters import ToolError
from ..models import PlanningReview, ReviewRepairResolution, RouteToolRequest, RouteTurn
from ..routing import coordinates
from .runtime import AgentRuntime


def resolve_stop_id(value, stops, city=None):
    if not isinstance(value,str):
        return None
    eligible = {identity:stop for identity,stop in stops.items() if city is None or stop.get("city") == city}
    if value in eligible:
        return value
    value = value.strip()
    if value in eligible:
        return value
    matches = [identity for identity,stop in eligible.items() if value and stop.get("name") == value]
    return matches[0] if len(matches) == 1 else None


class RouteCompletion(RouteTurn):
    status: Literal["ready", "needs_decision"]
    tools: list[RouteToolRequest] = Field(default_factory=list, max_length=0)
    order: None = None


class DailyRouteCompletion(RouteTurn):
    status: Literal["ready", "needs_decision"]
    tools: list[RouteToolRequest] = Field(default_factory=list, max_length=0)
    candidate: None = None


class RouteAgent(AgentRuntime):
    def __init__(self, models, store):
        super().__init__("行程 Agent",models,store)

    def model_references(self, state, payload):
        stops = payload.get("available_stops",{})
        aliases = {}
        occupied = set(stops) | {stop.get("name") for stop in stops.values() if isinstance(stop,dict)}
        for identity in sorted(stops):
            if len(identity) <= 24:
                continue
            alias = f"stop_{len(aliases)+1}"
            while alias in occupied:
                alias = "_"+alias
            occupied.add(alias)
            aliases[alias] = identity
        return aliases

    def result_cacheable(self, state, payload, result):
        stops = payload.get("available_stops",{})
        order = getattr(result,"order",None)
        candidate = getattr(result,"candidate",None)
        groups = [(payload.get("city"),order.stop_ids,list(order.durations)+list(order.start_times))] if order else [
            (day.city,day.stop_ids,[]) for day in candidate.days] if candidate else []
        for city,identities,keys in groups:
            resolved = [resolve_stop_id(identity,stops,city) for identity in identities]
            if None in resolved or len(resolved) != len(set(resolved)) or any(resolve_stop_id(key,stops,city) is None for key in keys):
                return False
        return True

    def prepare_tool_request(self, state, payload, request):
        stops = payload.get("available_stops",state.get("stops"))
        cities = state.get("requirements",{}).get("cities",[])
        changes = {}
        if not request.city:
            if payload.get("city") in cities:
                changes["city"] = payload["city"]
            elif len(cities) == 1:
                changes["city"] = cities[0]
        city = changes.get("city",request.city)
        if isinstance(stops,dict):
            fields = ("origin_id","destination_id") if request.tool == "route" else ("destination_id",) if request.tool == "place" else ()
            for field in fields:
                identity = resolve_stop_id(getattr(request,field),stops,city)
                if identity is not None:
                    changes[field] = identity
        return request.model_copy(update=changes) if changes else request

    def apply_tool_observation(self, state, payload, observation):
        request, value = observation.get("request",{}),observation.get("result",{})
        if request.get("tool") != "place" or observation.get("status") not in {"ok","unresolved"} or not isinstance(value,dict):
            return
        place = value.get("map_place")
        stops = payload.get("available_stops",state.get("stops",{}))
        stop = stops.get(request.get("destination_id")) if isinstance(stops,dict) else None
        if stop and not coordinates(stop.get("map_place")):
            # Replaying evidence must not restore an old stop's name, source text or duration.
            for field in ("map_place","map_candidates","map_selected_id","map_issue","query_status"):
                if field in value:
                    stop[field] = copy.deepcopy(value[field])
        if stop and coordinates(stop.get("map_place")):
            stop["query_status"] = "resolved"
            stop.pop("map_issue",None)

    async def plan(self, state, instruction, payload, tools, daily=False):
        pool = payload.get("available_stops")
        if isinstance(pool,dict):
            # Annotate a copy for the model; the caller's pool only receives actual tool observations.
            payload = {**payload,"available_stops":{identity:{**stop,"query_status":
                ("resolved" if coordinates(stop.get("map_place")) else "unresolved" if stop.get("map_issue") else "not_queried")}
                for identity,stop in pool.items()}}
        try:
            return await self.plan_turn(state,instruction,payload,tools,daily)
        finally:
            if isinstance(pool,dict):
                for identity,stop in payload["available_stops"].items():
                    original = pool.get(identity)
                    if not isinstance(original,dict):
                        continue
                    for field in ("map_place","map_candidates","map_selected_id","map_issue"):
                        if field in stop:
                            if original.get(field) != stop[field]:
                                original[field] = stop[field]
                        else:
                            original.pop(field,None)

    async def plan_turn(self, state, instruction, payload, tools, daily):
        result = await self.ask(state,RouteTurn,
            "你与攻略 Agent、资源 Agent 协作，只形成一条完整行程。交通、住宿和门票使用资源 Agent 交接的真实结果。"
            "住宿仅在selected_by_user=true时参与行程；未选定酒店不得暂用，不安排酒店起终点、放行李或接驳，也不要求用户先选酒店。"
            "全程同一canonical_place_id默认只游览一次，多个source_references只是同一地点的多个来源；用户明确repeat_exceptions除外。"
            "可通过tools请求place、route、weather或guide_feedback，"
            "place用destination_id指定需要核实位置的现有景点，未定位的备选先查位置；place只匹配位置，不查询营业时间、预约或门票。"
            "origin_id和destination_id只能用available_stops的完整键，不能填景点名、POI ID或canonical_place_id。"
            "route只查询已有景点间实际交通依据；必须用mode表达查询方式，auto按用户允许方式综合选择，"
            "walking=步行、transit=公交地铁、driving=驾车打车、bicycling=骑行；公共交通偏好下先查询步行，实际步行不超过15分钟（含15分钟）就采用步行，不再查询公交。"
            "更长路段需要公共交通时用mode=transit，不能把要求藏在request备注；即使请求transit，工具也会跳过已核实15分钟以内路段的公交查询并返回实际walking方式。"
            "mode仍须符合用户允许的方式，未授权打车或自驾不能用driving；request是备注，不能让工具执行未提供的能力；"
            "必须根据工具实际返回的mode与缺口取舍，不能把备注要求当成已执行结果。weather城市须在目的地中。"
            "相同place、同起终点同mode的route或weather无需换备注重复查询；失败后先依据错误中的可用ID和方式纠正参数，"
            "正确参数仍未取得的资料应保留缺口，不能不断重查直至耗尽轮次。remaining_tool_rounds=0时必须交接结果，不能再请求工具。"
            "工具完成不代表请求的事实已取得：map_place没有坐标时位置未核实；query_status=unresolved或路线minutes=null时，对应交通方式仍无可用耗时，不能宣称该方式已有路线。"
            "地点query_status=not_queried表示尚未查询，不能称为查不到；有坐标即已定位，不能沿用旧的定位失败名单。"
            "未定位时根据map_issue区分候选歧义、网络请求失败和无匹配；有map_selected_id但无坐标是详情未取得，不重复询问选哪个候选。"
            "攻略gaps中的超出目的地范围属于城市范围问题，不能称为高德无法定位，也不能擅自把外地景点加入目的地城市。"
            "普通可选且未纳入路线的地点缺口不阻断整趟规划；确需用户选择时只问当前影响安排的具体地点与待决事项，不批量询问是否接受所有未定位景点。"
            "需区分交通方式：transit为空但walking_alternative或route_evidence有实际步行结果时，只是公交未取得，步行已经核实，不得一并说成未知。"
            "不得默认所有景点间都有公共交通，未查询公交不等于公交查询失败；短途已核实步行无需补公交证据或据此删点，仍计入每日步行上限。不把某日期/某景点的条件推广到整趟。"
            "先利用高德查询证据再确定顺序，不虚构交通耗时、位置或天气。"
            "地点坐标与路段起终点只用高德map_place，飞猪仅查询票务；不比较两平台坐标，不因票务坐标差异更换或清空高德定位，也不据此要求用户确认场馆。"
            "需要工具时status=tools；有重要未决取舍时status=needs_decision并返回一道question；"
            "普通可选项目可自行取舍并解释，必去/锁定项不能删除。"
            "餐厅、咖啡店、小吃和甜品店由系统按当天区域另列午餐或晚餐推荐，不是景点，不加入candidate.days.stop_ids或order.stop_ids。"
            "门票不是每个地点的必备条件；保留攻略中的免费说明和多个票种，不因未提供票价反复追问。"
            "用户最新明确指令优先于默认规则与旧偏好。walking_limit_exempt_dates中的日期已获用户授权豁免步行上限，"
            "effective_max_walk_per_day为null时只提示真实距离，不能因旧上限删点、换住宿或重复追问。"
            "工具失败是资料缺口，不冒充偏好问题。没有未决决策才status=ready，"
            +("返回order，candidate为null。" if daily else "返回单个candidate，order为null。")+instruction,
            payload,tools,completion_schema=DailyRouteCompletion if daily else RouteCompletion)
        if result.status == "ready" and ((daily and result.order is None) or (not daily and result.candidate is None)):
            raise ToolError("route_format","路线 Agent 未交接当前步骤需要的结构，未采用")
        return result

    async def review(self, state, issues):
        return await self.ask(state,PlanningReview,
            "当前路线经过局部自动修复后，仍有冲突或资料不足导致无法确定调整方式。按用户要求转到需求追问，不能以规划失败结束。"
            "围绕最先需要明确的一项问题，引用当前活动和具体卡点问一道可回答的问题，不把不确定性编造成已证实冲突。"
            "可询问必须保留的活动/时刻、可删减或可调整的范围，不能询问是否批准自动修复、查询或重审。"
            "只修改问题涉及的活动及必要的相邻衔接，其他日期与活动保持原样。"
            "给选项、推荐理由和影响日期；不要要求用户代查事实。不得把推荐当回答，也不得自行提高预算、"
            "删除必去项或修改交通限制。用户最新明确指令可以覆盖默认规则和历史偏好；用户已经接受的步行超限只做说明，"
            "不得以未解除旧上限为由再次要求选择。已经回答的问题不要重复，提出新的具体取舍。"
            "requirements.self_arranged_round_trip=true表示出发地与目的地之间的去程、返程由用户自行安排：不检查、不查询、不追问去程返程班次、到离时刻或车站机场接驳，只规划目的地内的行程及多个目的地之间的交通。",
            {"requirements":state["requirements"],"issues":issues,"route":state.get("selected_route") or state.get("candidates",[]),
             "available_stops":state.get("stops",{}),"constraints":state.get("stop_constraints",{}),"history":state.get("decision_history",[])})

    async def resolve_review(self, state, findings):
        return await self.ask(state,ReviewRepairResolution,
            "审查后的自动修复仍有阻断，请判断下一步，不能一律推给用户点击修复。"
            "若可在现有授权内改顺序、缩减可选项、预留午餐或调整时段，返回repair，explanation给出不同于前次失败的具体修复指令。"
            "只有必须修改用户明确保留的日期/时刻、删除必去或锁定项、放宽预算/交通限制等真实取舍才返回needs_decision及一道question。"
            "问题必须列出无法同时满足的用户条件与具体可行选项，不得询问是否允许继续自动修复、重审或查询。"
            "接口失败、事实查不到、模型或系统未修好属于blocked，不能包装成用户偏好问题。"
            "地点及路线坐标以高德为准，飞猪仅查票；两平台坐标差异不是用户取舍，不因此要求选择场馆或清空高德定位。"
            "只改变受影响日期，不虚构地点、耗时、价格和授权。已回答的取舍不重复追问。"
            "requirements.self_arranged_round_trip=true表示出发地与目的地之间的去程、返程由用户自行安排：不检查、不查询、不追问去程返程班次、到离时刻或车站机场接驳，只规划目的地内的行程及多个目的地之间的交通。",
            {"requirements":state["requirements"],"findings":findings,"itinerary":state["itinerary"],
             "constraints":state.get("stop_constraints",{}),"locks":state.get("locks",[]),
             "preserved_dates":state.get("assessment_dates",[]) if state.get("keep_current_plan") else [],
             "repair_attempts":state.get("repair_round",0),"history":state.get("decision_history",[])},cache=False)
