"""地点定位、逐日排序、移动方式、时间窗与每日路线刷新。"""
import copy
import logging
import re

from .. import assumptions
from ..authority import USER_SOURCES
from ..opening import prepare as prepare_opening
from ..scheduler import (MAX_DAY_STOPS, MAX_ROUTE_PASSES, MAX_LONG_LEG_TRIALS, MAX_LONG_LEG_QUERIES,
                         long_leg_candidates, plan_day, plan_trip, route_metrics)
from ..adapters import ToolError, map_link, navigation_link
from ..models import UnmetItem, Requirements
from ..rules import enforce_locks, timestamp
from ..constraints import active, public_only
from ..routing import coordinates, daily_route_scope, daily_movements, transport_access_transfers, distance, reference_opening
from ..resource_planning import chosen_hotel, comparison
from ..agents.runtime import digest
from ..dining import is_dining, refresh_meals, plan_meal_wishes
from ..places import canonicalize_stops, canonical_key, represented_stop_ids
from ..planning_recovery import route_moves
from ..assumptions import visible_text
from ..actions import _route_days, execute
from ..coordinator import _record_defaults
from ..planning_repairs import protected_stops


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
        if scope["hotel_location_confirmed"] and not tail["start_name"]:
            gaps.append("住宿或出发地址尚未确定，首个景点从游览开始时刻计时，不计算前往首站的耗时")
        elif scope["first_leg_required"] and not tail["start_name"]:
            gaps.append("已选班次的到达站点尚未确定，车站接驳待核实")
        return start,end,gaps,transfers,tail

    @staticmethod
    def hub_rows(city,name,rows):
        """AMap airport/railway POIs for one provider hub name: the main building, terminals, halls and gates."""
        prefix = city.removesuffix("市")
        base = name.removeprefix(prefix).removeprefix("市")
        return [row for row in rows if row.get("id") and str(row.get("typecode","")).startswith(("1501","1502"))
                and str(row.get("name","")).removeprefix(prefix).removeprefix("市").startswith(base)]

    async def resolve_hubs(self,state,itinerary):
        """Pin station and airport POIs; unresolved identities become structured unmet items.

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
                hubs = found["hubs"][:4]
                state.setdefault("hub_candidates",[]).extend({**row,"city":city,"hub_name":name} for row in hubs)
                return {"hub_candidates":copy.deepcopy(state["hub_candidates"]),
                        "unmet":[UnmetItem(kind="hub_identity",refs=[{"type":"resource","id":segment["id"]}],
                            dates=[ctx["date"]],detail="班次站点存在多个接驳位置。").model_dump(mode="json")]}
        return None

    async def movement(self,state,city,origin_name,destination_name,origin_place=None,destination_place=None):
        mode = Requirements.model_validate(state["requirements"]).local_transport
        fewer_walks = bool(active(state["requirements"],"fewer_walking"))
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
                    if (same_origin and same_destination and old.get("minutes") is not None
                            and mode != "public"
                            and old.get("mode") in modes and str(old.get("evidence_id","")).startswith("query:")
                            and not (mode == "public" and old.get("mode") == "walking" and
                                     (old["minutes"] > 25 or (old.get("walking_meters") or 0) > 1500 or distance(coordinates(origin),coordinates(destination)) > 1500))):
                        return copy.deepcopy(old),destination
            if origin["location"] == destination["location"]:
                return {"minutes":0,"mode":"walking","walking_meters":0,"provider":"amap","origin":origin_name,"destination":destination_name,
                        "origin_place":origin,"destination_place":destination,"navigation_url":navigation_link(origin,destination)},destination
            meters = distance(coordinates(origin),coordinates(destination))
            long_leg = meters > 1500
            order = (["transit"] if meters > 2500 else ["walking","transit"]) if mode == "public" else [mode]
            if "driving" in modes and "driving" not in order:
                order.append("driving")
            routes, errors, failure_codes = [],[],[]
            for allowed in order:
                try:
                    result = await self.amap.route(state["trip_id"],city,origin,destination,allowed)
                    if result.get("minutes") is not None:
                        routes.append(result)
                        if mode == "public" and meters < 700 and allowed == "walking" and (not fewer_walks or result["minutes"] <= 15):
                            break
                        if mode == "public" and allowed == "transit":
                            break
                except ToolError as exc:
                    errors.append(str(exc)); failure_codes.append(exc.code)
                    if exc.code in {"amap_limit","amap_rate_limit","amap_quota_limit","amap_usage_limit","amap_auth","amap_network","amap_failed"}:
                        break
            if mode == "public":
                transit = [route for route in routes if route.get("mode") == "transit"]
                walking = next((route for route in routes if route.get("mode") == "walking"),None)
                if (walking and (fewer_walks and 0 <= walking["minutes"] <= 15 or
                        not fewer_walks and 0 <= walking["minutes"] <= 25 and
                        (not transit or walking["minutes"] <= transit[0]["minutes"]+5))):
                    return {**walking,"origin":origin_name,"destination":destination_name,
                            "transit_queried":any(route.get("mode") == "transit" for route in routes),
                            "transit_alternative":transit[0] if transit else None},destination
                if transit:
                    return {**transit[0],"origin":origin_name,"destination":destination_name},destination
                if walking and (long_leg or walking["minutes"] > 25 or (walking.get("walking_meters") or 0) > 1500):
                    routes = [route for route in routes if route.get("mode") != "walking"]
                    unknown.update(walking_alternative=walking,note="长路段公交/地铁依据未取得，未把长距离步行当作推荐；"+("；".join(dict.fromkeys(errors)) or "公共交通查询未返回可用路线"))
            if not routes:
                return {**unknown,"failure_codes":failure_codes,"note":unknown["note"] if unknown.get("walking_alternative") else "；".join(dict.fromkeys(errors)) or "未取得允许交通方式的路线，未改用打车"},destination
            route = min(routes,key=lambda item:item["minutes"] + ((item.get("walking_meters") or 0)/80 if fewer_walks else 0))
            return {**route,"origin":origin_name,"destination":destination_name},destination
        except ToolError as exc:
            return {**unknown,"failure_codes":[exc.code],"note":str(exc)},None

    @staticmethod
    def clock(minutes):
        return f"{min(minutes//60,23):02d}:{minutes%60:02d}"

    def schedule_activity_day(self,state,route_day,planned,window,day_weather):
        city, day_date = route_day["city"],route_day["date"]
        start,end,base_gaps,transfers,tail = window
        gaps = [*base_gaps,*[state["stops"][item["id"]]["name"]+"："+item["reason"]+
                "；需要用户取舍" for item in planned["unplaced"] if item["id"] in protected_stops(state)]]
        activities = []
        lunch = planned.get("lunch")
        def meal():
            return {"id":"lunch:"+day_date,"kind":"rest","meal":"lunch","name":"午餐与休息",
                    "start":self.clock(lunch["start"]),"end":self.clock(lunch["end"]),"duration_minutes":60,
                    "adjustment":"活动内部预留60分钟用餐，未指定餐厅" if lunch["within"] else "预留60分钟用餐，地点自行选择"}
        if lunch and not lunch["within"]:
            activities.append(meal())
        for row in planned["visits"]:
            stop = state["stops"][row["id"]]
            place,opening = stop.get("map_place"),row["opening_reference"]
            activity = {key:copy.deepcopy(stop.get(key)) for key in ("id","name","source_id","source_title","source_url","source_kind","evidence",
                "opening_claim","opening_schedule","source_references","alias_names","possible_duplicate_ids","ticket_claim","category","product",
                "map_selected_id","map_selected_by") if key in stop}
            activity.update(kind="visit",start=self.clock(row["start_minutes"]),end=self.clock(row["end_minutes"]),
                duration_minutes=row["end_minutes"]-row["start_minutes"],visit_minutes=row["duration_minutes"],
                duration_basis="用户指定时长优先，其次攻略时长；全天园区至少采用默认全天时长，其他缺失时长由代码估算",
                opening_window=row["opening_window"],opening_reference=opening,
                opening_note="开放时间未核实" if opening.get("unknown") else opening.get("note",""),
                appointment_time=row.get("appointment_time"),appointment_time_source=row.get("appointment_time_source"),
                move_before=row["move_before"],canonical_place_id=canonical_key(stop),map_place=place,
                map_url=map_link(stop["name"],city,place.get("location") if place else None),
                navigation_url=navigation_link(None,place) if place else None,
                adjustment=("开放时间未核实；" if opening.get("unknown") else "")+"代码按开放窗口、用户约束与实际交通排程")
            if lunch and lunch["within"] == row["id"]:
                activity["meal_break"] = meal()
            activities.append(activity)
        activities.sort(key=lambda item:item["start"])
        return visible_text({"date":day_date,"city":city,"activities":activities,"gaps":gaps,
            "transfers":transfers,"weather":day_weather,"route_basis":"代码按优先级选择可行顺序，并用选定相邻路段实际耗时重算",
            "start_place":tail.get("start_name"),"end_place":tail.get("name"),"end_label":tail["label"],
            "available_start":start,"tail_deadline":end,"return_move":planned["return_move"],
            "walking_meters":planned["walking_meters"],"tail_arrival":planned["finish"],
            "adjustment":route_day.get("adjustment",""),"meal_estimate":state["requirements"]["people"]*100,
            "meal_basis":"餐饮暂按每人每天 100 元估算"},state)

    async def code_schedule(self,state,routes,windows,preserved,exact_orders=None):
        moves = {}
        async def hydrate(result,budget=None):
            for day_date,day in result["days"].items():
                window = windows[day_date]
                tail = window["tail"]
                identities = day["stop_ids"]
                legs = list(zip(identities,identities[1:]))
                if identities and tail.get("start_name"):
                    legs.insert(0,("@start",identities[0]))
                if identities and tail.get("name"):
                    legs.append((identities[-1],"@end"))
                def endpoint(identity):
                    if identity == "@start":
                        return tail["start_name"],tail.get("start_place")
                    if identity == "@end":
                        return tail["name"],tail.get("end_place")
                    stop = state["stops"][identity]
                    return stop["name"],stop.get("map_place")
                for first,last in legs:
                    key = (day_date,first,last)
                    if key in moves:
                        continue
                    known = next((move for (_,a,b),move in moves.items() if a == first and b == last and move.get("minutes") is not None),None)
                    if known is not None:
                        moves[key] = copy.deepcopy(known)
                        continue
                    if budget is not None:
                        if budget[0] <= 0:
                            return False
                        budget[0] -= 1
                    origin,origin_place = endpoint(first)
                    destination,destination_place = endpoint(last)
                    moves[key],_ = await self.movement(state,window["city"],origin,destination,origin_place,destination_place)
            return True

        async def settle(result,max_leg_minutes=None,preserve_all=False,budget=None):
            current = copy.deepcopy(result)
            required = {date:list(day["stop_ids"]) for date,day in current["days"].items()}
            verified = None
            for _ in range(MAX_DAY_STOPS+1):
                if not await hydrate(current,budget):
                    return None
                exact = {date:plan_day(state,windows[date],day["stop_ids"],moves,preserved.get(date),
                                      order=day["stop_ids"],max_leg_minutes=max_leg_minutes)
                         for date,day in current["days"].items()}
                complete = all(set(day["stop_ids"]) == set(required[date]) for date,day in exact.items())
                if complete or not preserve_all:
                    verified = {**current,"days":exact}
                following = {}
                for date,day in exact.items():
                    ids = list(required[date]) if preserve_all else day["stop_ids"]
                    free = plan_day(state,windows[date],ids,moves,preserved.get(date),max_leg_minutes=max_leg_minutes)
                    if preserve_all and set(free["stop_ids"]) != set(required[date]):
                        return None
                    following[date] = free if set(free["stop_ids"]) == set(ids) else day
                stable = all(following[date]["stop_ids"] == current["days"][date]["stop_ids"] for date in following)
                if stable and (complete or not preserve_all):
                    return verified
                current = {**current,"days":following}
            return verified

        result = plan_trip(state,routes,windows,moves,preserved,exact_orders=exact_orders)
        for _ in range(MAX_ROUTE_PASSES):
            self.emit(state,"activities","正在查询选定顺序的相邻路段，并按实际交通重算时刻")
            await hydrate(result)
            updated = plan_trip(state,routes,windows,moves,preserved,exact_orders=exact_orders)
            if all(updated["days"][day]["stop_ids"] == result["days"][day]["stop_ids"] for day in windows):
                result = updated
                break
            result = updated
        actual = await settle(result)
        attempted,query_budget = set(),[MAX_LONG_LEG_QUERIES]
        for _ in range(MAX_LONG_LEG_TRIALS if exact_orders is None else 0):
            candidates = long_leg_candidates(state,windows,actual["days"],moves,preserved,attempted)
            if not candidates:
                break
            proposal = candidates[0]
            attempted.add(proposal["operation"])
            before = route_metrics({date:actual["days"][date] for date in proposal["days"]})
            confirmed = await settle({"days":proposal["days"]},max_leg_minutes=before[0]-1,preserve_all=True,budget=query_budget)
            if confirmed is None:
                if query_budget[0] <= 0:
                    break
                continue
            known = all(row["move_before"].get("minutes") is not None for day in confirmed["days"].values() for row in day["visits"][1:])
            after = route_metrics(confirmed["days"])
            if known and after[0] < before[0] and after[1] <= before[1]:
                actual["days"].update(confirmed["days"])
        used = {canonical_key(state["stops"][identity]) for row in actual["days"].values() for identity in row["stop_ids"]}
        for day in routes:
            if day["date"] in windows:
                intended = list(dict.fromkeys([*day["stop_ids"],*[identity for identity,origin in result["origins"].items() if origin == day["date"]]]))
                actual["days"][day["date"]]["unplaced"] = [{"id":identity,"reason":"真实交通重算后，开放时段、午餐、步行或当天容量仍不足"}
                    for identity in intended if identity in state["stops"] and canonical_key(state["stops"][identity]) not in used]
        final_dates = {identity:date for date,day in actual["days"].items() for identity in day["stop_ids"]}
        actual["added"] = [{**item,"date":final_dates[item["id"]]} for item in result["added"] if item["id"] in final_dates]
        actual["relocated"] = [{"id":identity,"from":origin,"to":final_dates[identity]} for identity,origin in result["origins"].items()
                               if identity in final_dates and origin != final_dates[identity]]
        actual["origins"] = copy.deepcopy(result["origins"])
        return actual

    async def apply_schedule(self,state,routes,windows,preserved,scheduled):
        failures = {}
        def apply(action):
            try:
                updates,_,_ = execute(state,[action],"system")
                state.update(updates)
                return True
            except ValueError as error:
                warning = "系统排程动作被拒绝："+str(action)+"；原因："+str(error)
                if warning not in state.setdefault("warnings",[]):
                    state["warnings"].append(warning)
                logging.getLogger(__name__).warning("Schedule action rejected: %s; reason: %s",action,error)
                failures[action["stop_id"]] = str(error)
                return False
        def route_contains(identity,date=None):
            return any(identity in row.get("stop_ids",[]) and (date is None or row["date"] == date) for row in _route_days(state))
        def omit_unplaced():
            used = {identity for day in scheduled["days"].values() for identity in day["stop_ids"]}
            used.update(identity for row in _route_days(state) if row["date"] not in windows for identity in row.get("stop_ids",[]))
            for row in list(_route_days(state)):
                if row["date"] not in windows:
                    continue
                planned = scheduled["days"][row["date"]]
                for identity in list(row.get("stop_ids",[])):
                    if identity not in state["stops"] or identity in used:
                        continue
                    entry = next((item for item in planned["unplaced"] if item["id"] == identity),None)
                    if entry is None:
                        entry = {"id":identity,"reason":"代码排程未能满足开放、交通或当天容量要求"}
                        planned["unplaced"].append(entry)
                    if identity in protected_stops(state):
                        continue
                    action = {"type":"skip","stop_id":identity,"date":row["date"]}
                    if apply(action):
                        self.opening_omission(state,identity,row["date"],entry["reason"]+"；其他同城开放日期也未能容纳，本次未排入。")
        async def drop_rejected(action):
            identity = action["stop_id"]
            for day_date,planned in list(scheduled["days"].items()):
                if identity not in planned["stop_ids"]:
                    continue
                ids = [item for item in planned["stop_ids"] if item != identity]
                replay = [{**row,"stop_ids":ids if row["date"] == day_date else list(row["stop_ids"])} for row in routes]
                recalculated = await self.code_schedule(state,replay,{day_date:windows[day_date]},preserved,
                                                       exact_orders={day_date:ids})
                updated = recalculated["days"][day_date]
                reasons = {item["id"]:item for item in [*planned["unplaced"],*updated["unplaced"]]}
                for other in planned["stop_ids"]:
                    if other not in updated["stop_ids"] and other != identity:
                        reasons.setdefault(other,{"id":other,"reason":"移除被拒绝动作后，剩余景点交通与时刻重算仍无法容纳"})
                if identity in protected_stops(state):
                    reasons[identity] = {"id":identity,"reason":"系统动作被拒绝，需要用户取舍："+failures[identity]}
                updated["unplaced"] = list(reasons.values())
                scheduled["days"][day_date] = updated
        omit_unplaced()
        for identity,origin in scheduled.get("origins",{}).items():
            targets = [day for day,row in scheduled["days"].items() if identity in row["stop_ids"]]
            if not targets or route_contains(identity):
                continue
            action = {"type":"add_optional_stop","stop_id":identity,"date":targets[0]}
            if not apply(action):
                await drop_rejected(action)
        for item in scheduled["relocated"]:
            if item["id"] in failures or item["id"] not in scheduled["days"][item["to"]]["stop_ids"] or route_contains(item["id"],item["to"]):
                continue
            action = {"type":"move","stop_id":item["id"],"date":item["from"],"to":item["to"]}
            if apply(action):
                self.emit(state,"activities",state["stops"][item["id"]]["name"]+"移至可容纳的同城开放日"+item["to"])
            else:
                await drop_rejected(action)
        for item in scheduled["added"]:
            if item["id"] in failures or item["id"] not in scheduled["days"][item["date"]]["stop_ids"] or route_contains(item["id"],item["date"]):
                continue
            before = copy.deepcopy(state)
            action = {"type":"add_optional_stop","stop_id":item["id"],"date":item["date"]}
            if apply(action):
                _record_defaults(state,before,[action],[[{"type":"skip","stop_id":item["id"],"date":item["date"]}]])
            else:
                await drop_rejected(action)
        omit_unplaced()
        for route in [state.get("selected_route") or {},*(state.get("candidates") or [])[:1]]:
            for row in route.get("days",[]):
                if row["date"] in scheduled["days"]:
                    ids = scheduled["days"][row["date"]]["stop_ids"]
                    retained = [identity for identity in row.get("stop_ids",[]) if identity not in ids]
                    row["stop_ids"] = [*ids,*retained]
        return scheduled

    def opening_omission(self,state,identity,day_date,reason):
        if identity in protected_stops(state):
            return
        stop = state["stops"][identity]
        alternatives = [{"id":"open-"+day["date"],"label":"加回到"+day["date"],
                         "actions":[{"type":"add_optional_stop","stop_id":identity,"date":day["date"]}]}
                        for day in state["selected_route"]["days"] if day["city"] == stop["city"]
                        and (not stop.get("assigned_date") or stop["assigned_date"] == day["date"])
                        and reference_opening(stop,day["date"])["status"] == "open"][:4]
        state.update(assumptions.record(state,{"id":"opening-"+digest([identity,day_date])[:24],
            "title":"本次未排入"+stop["name"],"dates":[day_date],"stop_ids":[identity],
            "current_action":{"type":"skip","stop_id":identity,"date":day_date},
            "reason":reason,"alternatives":alternatives}))

    async def activities(self,state):
        self.emit(state,"activities","正在结合天气、地图位置与允许的交通方式细化每日安排")
        repair_dates = set(state.get("change",{}).get("dates",[])) if state.get("repair_only") else None
        if repair_dates == set():
            return {"itinerary":copy.deepcopy(state["itinerary"])}
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
        spatial_pending = state["selected_route"].get("feasibility",{}).get("spatial_unplaced",[])
        for route in [state["selected_route"],*(state.get("candidates") or [])[:1]]:
            for item in spatial_pending:
                if repair_dates is not None and item["date"] not in repair_dates:
                    continue
                row = next((day for day in route.get("days",[]) if day["date"] == item["date"]),None)
                if row is not None and item["id"] not in row["stop_ids"]:
                    row["stop_ids"].append(item["id"])
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
        if clarification := self.place_clarification(stops,{identity for identity in active_ids if not is_dining(stops.get(identity,{}))},state["selected_route"]["days"]):
            protected = protected_stops(state)
            for item in clarification["unmet"]:
                identity = item["refs"][0]["id"]
                if identity in protected:
                    state["unmet"] = [*state.get("unmet",[]),item]
                    continue
                for day in list(state["selected_route"]["days"]):
                    if identity not in day["stop_ids"]:
                        continue
                    before = copy.deepcopy(state)
                    action = {"type":"skip","stop_id":identity,"date":day["date"]}
                    updates,_,_ = execute(state,[action],"system")
                    state.update(updates)
                    alternatives = [[{"type":"select_poi","stop_id":identity,"poi_id":poi["id"]},
                                     {"type":"add_optional_stop","stop_id":identity,"date":day["date"]}]
                                    for poi in stops[identity].get("map_candidates",[])[:2]]
                    _record_defaults(state,before,[action],alternatives)
        candidate_pool = {identity:stop for identity,stop in stops.items() if stop.get("city") in route_cities
                          and identity not in state.get("stop_constraints",{}).get("excluded",[])}
        await prepare_opening(self.models,self.store,state,candidate_pool,[day["date"] for day in state["selected_route"]["days"]])
        if clarification := await self.resolve_hubs(state,state["itinerary"]):
            state["unmet"] = [*state.get("unmet",[]),*clarification["unmet"]]
        req, itinerary = state["requirements"],copy.deepcopy(state["itinerary"])
        for entry in itinerary.get("transport",[]):
            if entry.get("selected") and repair_dates is None and (state["mode"] != "modify" or self.affected(state,"transport",entry["context"]["origin"]) or self.affected(state,"transport",entry["context"]["destination"]) or self.affected(state,"hotel",entry["context"]["origin"]) or self.affected(state,"hotel",entry["context"]["destination"]) or any(hotel.get("selected") and not hotel.get("selected_by_user") and hotel["city"] in (entry["context"]["origin"],entry["context"]["destination"]) for hotel in itinerary.get("hotels",[]))):
                enriched = await self.enrich_transport({**state,"itinerary":itinerary},entry["context"],entry.get("options",[]) or [entry["selected"]])
                by_id = {option["id"]:option for option in enriched}
                entry["selected"] = by_id.get(entry["selected"]["id"],entry["selected"])
                entry["comparison"] = [comparison(option,req) for option in entry.get("options",[])]
        state = {**state,"itinerary":itinerary}
        old_days = {day["date"]:day for day in state.get("old_itinerary",{}).get("days",[])}
        windows,raw_windows,preserved = {},{},{}
        routes = copy.deepcopy(state["selected_route"]["days"])
        for route_day in routes:
            city,day_date = route_day["city"],route_day["date"]
            affected = self.affected(state,"activity",city,day_date) or self.affected(state,"hotel",city) or self.affected(state,"transport",city) or city in refreshed_cities
            if repair_dates is not None:
                affected = day_date in repair_dates
            if city in state.get("change",{}).get("preserve_cities",[]) and day_date in old_days:
                affected = False
            if day_date in old_days and not affected:
                continue
            change = state.get("change",{})
            if change.get("refresh_resources") and day_date in old_days:
                route_day["stop_ids"] = [item["id"] for item in old_days[day_date].get("activities",[]) if item.get("kind") == "visit"]
            route_day["stop_ids"] = [identity for identity in route_day["stop_ids"] if identity in stops and not is_dining(stops[identity])
                                    and identity not in change.get("remove_stop_ids",[])]
            route_day["stop_ids"].extend(identity for identity in change.get("add_stop_ids",[]) if identity in stops and stops[identity]["city"] == city
                                        and stops[identity].get("assigned_date") in {None,day_date} and identity not in route_day["stop_ids"])
            preserved[day_date] = {item["id"]:item for item in old_days.get(day_date,{}).get("activities",[]) if item.get("kind") == "visit"
                and item["id"] not in represented_stop_ids(state) and (
                state.get("duplicate_repair_dates") or item["id"] in state.get("locks",[]) or change.get("refresh_resources") or
                item.get("appointment_time") and (item.get("appointment_time_source") or "legacy_unknown") in USER_SOURCES)}
            route_day["stop_ids"].extend(identity for identity in preserved[day_date] if identity in stops and identity not in route_day["stop_ids"])
            if change.get("fewer_activities"):
                kept = set(protected_stops(state)) | set(preserved[day_date])
                route_day["stop_ids"] = [identity for index,identity in enumerate(route_day["stop_ids"]) if index < 2 or identity in kept]
            raw = await self.window(state,route_day)
            raw_windows[day_date] = raw
            windows[day_date] = {"date":day_date,"city":city,"start":raw[0],"end":raw[1],"tail":raw[4],"transfers":raw[3],
                                 "durations":state["selected_route"].get("feasibility",{}).get("durations",{})}
        rebuilt_dates = set(windows)
        for route_day in routes:
            if route_day["date"] in windows:
                for route in [state["selected_route"],*(state.get("candidates") or [])[:1]]:
                    for row in route.get("days",[]):
                        if row["date"] == route_day["date"]:
                            row["stop_ids"] = list(route_day["stop_ids"])
        scheduled = await self.code_schedule(state,routes,windows,preserved) if windows else {"days":{},"relocated":[],"added":[]}
        scheduled = await self.apply_schedule(state,routes,windows,preserved,scheduled)
        for route in [state["selected_route"],*(state.get("candidates") or [])[:1]]:
            feasibility = route.get("feasibility",{})
            pending = [item for item in feasibility.get("spatial_unplaced",[]) if item["date"] not in windows]
            if pending:
                feasibility["spatial_unplaced"] = pending
            else:
                feasibility.pop("spatial_unplaced",None)
        scheduled_keys = {canonical_key(stops[identity]) for day in scheduled["days"].values() for identity in day["stop_ids"]}
        for item in state.get("system_assumptions",[]):
            action = item.get("current_action") or {}
            if (item.get("status") == "active" and action.get("type") == "skip" and action.get("stop_id") in stops
                    and canonical_key(stops[action["stop_id"]]) in scheduled_keys):
                item["status"] = "withdrawn"
        days = []
        for route_day in routes:
            day_date,city = route_day["date"],route_day["city"]
            if day_date not in windows:
                days.append(copy.deepcopy(old_days[day_date]))
                continue
            planned = scheduled["days"][day_date]
            weather_day = old_days.get(day_date,{}).get("weather",{}) if repair_dates is not None else self.daily_weather(weather,city,day_date)
            day = self.schedule_activity_day(state,route_day,planned,raw_windows[day_date],weather_day)
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
                                      refresh_routes=False)
        if repair_dates is None:
            await self.query_ticket_prices(state,itinerary,rebuilt_dates,missing_only=True)
        return {"itinerary":visible_text(itinerary,state),"stops":stops,"weather":weather,"warnings":state.get("warnings",[]),"resource_retry_done":state.get("resource_retry_done",False),"unmet":state.get("unmet",[]),
                "system_assumptions":copy.deepcopy(state.get("system_assumptions",[])),
                "selected_route":copy.deepcopy(state.get("selected_route",{})),"candidates":copy.deepcopy(state.get("candidates",[]))}

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
            itinerary = state.get("itinerary") or {}
            days = [day if item["date"] == day["date"] else item for item in itinerary.get("days",[])]
            if not any(item["date"] == day["date"] for item in days):
                days.append(day)
            refresh_meals({"days":days},stops,state.get("stop_constraints",{}).get("excluded",[]),
                          dates={day["date"]},requirements=state.get("requirements",{}),
                          meal_plan=state.get("selected_route",{}).get("feasibility",{}).get("meal_wishes"))
            self.meal_links(day)

    @staticmethod
    def meal_links(day):
        for recommendation in day.get("meal_recommendations",[]):
            place = recommendation.get("map_place") or {}
            recommendation["map_url"] = map_link(recommendation["name"],day["city"],place["location"]) if coordinates(place) else None

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
                          dates=meal_dates,requirements=state.get("requirements",{}),
                          meal_plan=state.get("selected_route",{}).get("feasibility",{}).get("meal_wishes"))
        for day in itinerary["days"]:
            if dates is None or day["date"] in dates:
                self.meal_links(day)
