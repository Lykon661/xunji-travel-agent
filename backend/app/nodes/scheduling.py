"""地点定位、逐日排序、移动方式、时间窗与每日路线刷新。"""
import asyncio
import copy
import re
from datetime import date

from ..adapters import ToolError, map_link, navigation_link
from ..models import OrderChoice, PlanningDecision, Requirements
from ..rules import enforce_locks, timestamp, walking_limit
from ..constraints import active, public_only, walking_target
from ..routing import coordinates, daily_route_scope, daily_movements, transport_access_transfers, distance, early_finish, geographic_order, reference_opening
from ..resource_planning import chosen_hotel, comparison
from ..agents.runtime import digest
from ..agents.route import resolve_stop_id
from ..dining import is_dining, meal_recommendations, refresh_meals
from ..places import canonicalize_stops, canonical_key, planning_stop_pool, repeat_allowed, represented_ids
from ..reviewing import review_repair_dates
from ..planning_recovery import route_moves


class SchedulingNodes:
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
            "requirements.constraints中的条件只在其scope内生效（scope为空表示全程），superseded_by非空的已失效；transit_rule等带日期或地点的条件不能扩大到其他天或全部景点间。"
            "fixed_order按before、after的先后安排，time_anchor按日出、日落或预约时刻安排，visit_duration的minutes写入durations；"
            "daily_min_stops要求当天至少count个景点，用available_stops中未安排的地点补足，地点确实不够时如实说明，不虚构地点；"
            "strength=soft表示用户已接受地点不够时少于count个，此时只在gap说明，不再为数量追问。"
            "先查实际步行路线；步行耗时不超过15分钟（含15分钟）的路段直接采用步行，不再查询或要求提供公交地铁。超过15分钟或步行耗时未取得时再按交通偏好查询，步行仍累计进每日限制。"
            "不得默认所有景点之间都有公共交通；公共交通无结果时保留真实缺口，不编造线路或耗时，也不因短途未查询公交而要求删点。"
            "公交transits为空仅表示没返回公交换乘方案；route_evidence或walking_alternative已有真实步行分钟数时，不得称该路段没有耗时、无法通行或步行未核实。"
            "place只核实POI位置，不能核实开放时间、预约或门票，不能把位置查询成功当成营业查证成功。"
            "考虑实际可用时段、开放与预约说法、游玩时长、天气和步行上限；walking_target_per_day非空时是用户少走路的每日目标，尽量不超过。"
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

    @staticmethod
    def hub_rows(city,name,rows):
        """AMap airport/railway POIs for one provider hub name: the main building, terminals, halls and gates."""
        prefix = city.removesuffix("市")
        base = name.removeprefix(prefix).removeprefix("市")
        return [row for row in rows if row.get("id") and str(row.get("typecode","")).startswith(("1501","1502"))
                and str(row.get("name","")).removeprefix(prefix).removeprefix("市").startswith(base)]

    async def resolve_hubs(self,state,itinerary):
        """Pin each routed station or airport to one AMap POI, or return a question instead of guessing a terminal.

        Exact unique names and the flight's own terminal resolve automatically; the user's earlier choice is reused.
        Pinned places stay in the AMap place cache, so every later lookup by the same name uses them.
        """
        req = state["requirements"]
        chosen = {(entry["hub_selection"]["city"],entry["hub_selection"]["name"]):entry["hub_selection"]["poi_id"]
                  for entry in state.get("decision_history",[]) if entry.get("hub_selection")}
        for segment in itinerary.get("transport",[]):
            selected, ctx = segment.get("selected") or {},segment["context"]
            for role,city,name,terminal in (("到达",ctx["destination"],selected.get("arrival_place"),selected.get("arrival_terminal")),
                                            ("出发",ctx["origin"],selected.get("departure_place"),selected.get("departure_terminal"))):
                # The origin city's hub is only routed when the user gave a home address there.
                if not name or city == req["origin"] and not req.get("origin_address"):
                    continue
                if (city,name) in chosen:
                    async def keep(candidates,poi_id=chosen[(city,name)]):
                        return ((poi_id,"你在需求追问中选择的到达或出发位置") if any(row.get("id") == poi_id for row in candidates)
                                else (None,"已选位置不在当前高德候选中"))
                    await self.amap.place(state["trip_id"],city,name,selector=keep)
                    continue
                if coordinates(await self.amap.place(state["trip_id"],city,name)):
                    continue
                found = {}
                async def pick(candidates,city=city,name=name,terminal=terminal,found=found):
                    hubs = found["hubs"] = self.hub_rows(city,name,candidates)
                    if terminal:
                        matched = [row for row in hubs if str(terminal).upper() in str(row["name"]).upper()]
                        plain = [row for row in matched if not re.search(r"[(（]",row["name"])]
                        for group in (plain,matched):
                            if len(group) == 1:
                                return group[0]["id"],"按班次航站楼"+str(terminal)+"唯一匹配"
                    if len(hubs) == 1:
                        return hubs[0]["id"],"车站或机场唯一匹配"
                    return None,"车站或机场有多个航站楼或进出站口，需要你确认"
                if coordinates(await self.amap.place(state["trip_id"],city,name,selector=pick)) or len(found.get("hubs",[])) < 2:
                    continue
                hubs = sorted(found["hubs"],key=lambda row:(bool(terminal) and str(terminal).upper() not in row["name"].upper(),len(row["name"])))[:10]
                options = {f"{index+1}. {row['name']}（{row.get('address') or '地址未返回'}）":row["id"] for index,row in enumerate(hubs)}
                question = PlanningDecision(id="hub-"+digest([city,name,role,list(options.values())])[:24],title="请选择"+role+"位置",
                    question=(segment["label"]+"的"+role+"地点“"+name+"”"+("（"+str(terminal)+"）" if terminal else "")+
                              "在高德有多个航站楼或进出站口，不能唯一对应。你会从哪个位置"+role+"？")[:1500],
                    options=list(options),recommendation="按机票或车票上的航站楼、进出站口选择。",
                    why_needed="接驳路线需要明确的起终点才能查询真实耗时，系统不会替你猜测航站楼或入口。",
                    affected_dates=[ctx["date"]]).model_dump(mode="json")
                return {"decision":question,"place_selection":{"kind":"hub","question_id":question["id"],"city":city,"name":name,"options":options}}
        return None

    async def movement(self,state,city,origin_name,destination_name,origin_place=None,destination_place=None):
        mode = Requirements.model_validate(state["requirements"]).local_transport
        modes = ["walking","transit"] if mode == "public" else [mode]
        if state["requirements"].get("allow_taxi") and "driving" not in modes and not public_only(state["requirements"]):
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
            fewer_walks = bool(active(state["requirements"],"fewer_walking"))
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
        for anchor in active(req,"time_anchor",date=day_date):
            if anchor["anchor"] == "appointment" and anchor["at"] and anchor["scope"]["stop_ids"][0] in ids:
                ordering.start_times.setdefault(anchor["scope"]["stop_ids"][0],anchor["at"])
        user_sequence = any(active(req,kind,date=day_date,stop_id=identity) for kind in ("fixed_order","time_anchor") for identity in ids)
        if locked_ids or ordering.start_times or preserved_activities or any(reference_opening(stops[identity],day_date) for identity in ids) or user_sequence:
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
            fixed_duration = next((item["minutes"] for item in active(req,"visit_duration",date=day_date,stop_id=stop_id)),None)
            duration = fixed_duration or ordering.durations.get(stop_id) or stop.get("duration_minutes") or 90
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
                # A shorter visit straddling 13:00 fits neither inside nor after lunch; eat first and start it later.
                if lunch is None and visit_start < 13*60 < visit_start+duration:
                    lunch_start = max(current,11*60+30)
                    proposed = max(lunch_start+60+travel,earliest)
                    if lunch_start+60 <= 14*60 and (appointment_minutes is None or proposed <= appointment_minutes):
                        lunch, lunch_position = meal(lunch_start),"before"
                        visit_start = proposed
            tail_move = (await self.movement(state,city,stop["name"],tail["name"],origin_place=place,destination_place=tail.get("end_place")))[0] if tail["name"] else {}
            tail_minutes = tail_move.get("minutes") if tail_move.get("minutes") is not None else tail_move.get("estimate_minutes",0)
            duration_adjustment = ""
            explicit_duration = fixed_duration is not None or bool(re.search(r"[0-9一二三四五六七八九十百半两]+(?:\.\d+)?\s*(?:小时|分钟)",state.get("revision_request","")))
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
        if clarification := await self.resolve_hubs(state,state["itinerary"]):
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
                     "effective_max_walk_per_day":walking_limit(req,day_date),"walking_target_per_day":walking_target(req,day_date),
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
        await self.resolve_hubs(state,itinerary)
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
