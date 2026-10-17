"""交通、酒店、门票等外部资源查询、补全与过期刷新。"""
import copy
from datetime import date, datetime, timedelta, timezone

from ..adapters import ToolError, default_ticket_price
from ..rules import resource_cost, ticket_policy, ticket_query_required, timestamp, walking_limit
from ..store import now
from ..routing import coordinates, distance
from ..resource_planning import chosen_hotel, comparison, same_resource, transport_issues
from ..reviewing import quote_expiry


class ResourceNodes:
    async def refresh_expired_resources(self,state):
        itinerary = copy.deepcopy(state["itinerary"])
        current = datetime.now(timezone.utc)
        expired = []
        for group in ("transport","hotels"):
            for entry in itinerary.get(group,[]):
                selected = entry.get("selected")
                if not selected or group == "hotels" and not entry.get("selected_by_user"):
                    continue
                expires = quote_expiry(selected)
                if expires is None or expires <= current:
                    expired.append(entry["id"])
        if not expired:
            return {}
        self.emit(state,"assessing","正在重查已过期的原班次与已采用住宿，保留景点和游览时段")
        probe = {**state,"itinerary":itinerary,"old_itinerary":copy.deepcopy(itinerary),"mode":"modify",
                 "locks":[identity for identity in state.get("locks",[]) if identity not in expired],
                 "change":{"scopes":["transport","hotel"],"resource_ids":expired,"refresh_resources":True,
                           "refresh_selected":True,"cities":[],"dates":[]}}
        if any(entry["id"] in expired for entry in itinerary.get("transport",[])):
            probe.update(await self.transport(probe))
        if any(entry["id"] in expired for entry in itinerary.get("hotels",[])):
            probe.update(await self.hotels(probe))
        # Quote refresh cannot replace a resource or silently move the original schedule.
        for group in ("transport","hotels"):
            previous = {entry["id"]:entry.get("selected") or {} for entry in itinerary.get(group,[])}
            for entry in probe["itinerary"].get(group,[]):
                before, selected = previous.get(entry["id"],{}),entry.get("selected")
                if selected and same_resource(before,selected):
                    entry["selected"] = {**copy.deepcopy(before),**selected}
        return {"itinerary":probe["itinerary"],"warnings":probe.get("warnings",state.get("warnings",[]))}

    async def enrich_transport(self,state,ctx,options):
        req = state["requirements"]
        for option in options:
            option["constraint_issues"] = transport_issues(option,ctx,req)
        allowed = [option for option in options if not option["constraint_issues"]]
        # Price and duration shortlists retain distinct useful alternatives without mapping every hit.
        priced = sorted(allowed,key=lambda item:(item.get("price") is None,item.get("price") or 0))[:3]
        timed = sorted(allowed,key=lambda item:str(item.get("arrival") or "99"))[:3]
        shortlist = list({item["id"]:item for item in [*priced,*timed]}.values())
        for option in shortlist:
            departure = timestamp(option.get("departure"),ctx["date"])
            arrival = timestamp(option.get("arrival"),ctx["date"])
            origin_stops = self.route_stops(state,ctx["origin"],ctx["date"])
            arrival_stops = self.route_stops(state,ctx["destination"],ctx["date"])
            origin_hotel = chosen_hotel(state["itinerary"],ctx["origin"],ctx["date"])
            arrival_hotel = chosen_hotel(state["itinerary"],ctx["destination"],ctx["date"])
            source_name = req.get("origin_address") if ctx["origin"] == req["origin"] else origin_stops[-1]["name"] if origin_stops else (origin_hotel or {}).get("name")
            target_name = req.get("origin_address") if ctx["destination"] == req["origin"] else (arrival_hotel or {}).get("name") or (arrival_stops[0]["name"] if arrival_stops else None)
            source_place = None if ctx["origin"] == req["origin"] else (origin_hotel or {}).get("map_place") if not origin_stops else origin_stops[-1].get("map_place")
            target_place = None if ctx["destination"] == req["origin"] else (arrival_hotel or {}).get("map_place") or (arrival_stops[0].get("map_place") if arrival_stops else None)
            before,_ = await self.movement(state,ctx["origin"],source_name,option.get("departure_place"),origin_place=source_place)
            after,_ = await self.movement(state,ctx["destination"],option.get("arrival_place"),target_name,destination_place=target_place)
            known_time = departure is not None and arrival is not None and arrival > departure
            duration = int((arrival-departure).total_seconds()/60) if known_time else None
            buffer = 120 if option["kind"] == "flight" else 60
            unknown = []
            if not source_name:
                unknown.append("出发城市具体起终点未提供，完整门到门耗时待确认")
            if before.get("minutes") is None or after.get("minutes") is None or duration is None:
                unknown.append("部分班次或接驳耗时待确认")
            option.update(departure_transfer=before,arrival_transfer=after,
                door_minutes=duration+buffer+60+before["minutes"]+after["minutes"] if duration is not None and before.get("minutes") is not None and after.get("minutes") is not None else None,
                walking_meters=(before.get("walking_meters") or 0)+(after.get("walking_meters") or 0),comparison_unknown=unknown)
            if len(option.get("legs",[])) > 1:
                option["comparison_unknown"].append("中转最短衔接、行李与税费需要核对供应商规则")
            if departure and before.get("minutes") is not None and req.get("earliest_departure"):
                earliest = timestamp(req["earliest_departure"],ctx["date"])
                if departure-timedelta(minutes=buffer+before["minutes"]) < earliest:
                    option["constraint_issues"].append("计入接驳与进站后，需要早于允许时间出门")
        return shortlist + [item for item in options if item["constraint_issues"]][:3]

    def hotel_areas(self,state,city):
        specified = state.get("requirements",{}).get("hotel_areas",{}).get(city)
        if specified:
            return [specified]
        stops = self.route_stops(state,city)
        located = [stop for stop in stops if coordinates(stop.get("map_place"))]
        if not located:
            return list(dict.fromkeys(stop["name"] for stop in stops))[:3] or [None]
        central = min(located,key=lambda stop:sum(distance(coordinates(stop["map_place"]),coordinates(other["map_place"])) for other in located))
        selected = [central]
        for stop in sorted(located,key=lambda stop:distance(coordinates(central["map_place"]),coordinates(stop["map_place"])),reverse=True):
            if len(selected) >= 3:
                break
            if all(distance(coordinates(stop["map_place"]),coordinates(other["map_place"])) > 1500 for other in selected):
                selected.append(stop)
        return [stop["map_place"]["name"] for stop in selected]

    async def enrich_hotels(self,state,city,options):
        unique = list({str(item.get("supplier_id") or item["name"]):item for item in options}.values())
        shortlist = sorted(unique,key=lambda item:(item.get("price") is None,item.get("price") or 0))[:6]
        for option in shortlist:
            unknown, total, walking = [],0,0
            exceeds_limit = False
            try:
                place = await self.amap.match_resource(state["trip_id"],city,option)
            except ToolError as exc:
                place = None; unknown.append(str(exc))
            option["map_place"] = place
            if place:
                for day in state["selected_route"]["days"]:
                    if day["city"] != city:
                        continue
                    stops = self.route_stops(state,city,day["date"])
                    anchors, departures = [],[]
                    for entry in state["itinerary"].get("transport",[]):
                        if entry["context"]["date"] != day["date"]:
                            continue
                        resource = entry.get("selected") or {}
                        if entry["context"]["destination"] == city and resource.get("arrival_place"):
                            anchors.append(({"name":resource["arrival_place"]},False))
                        if entry["context"]["origin"] == city and resource.get("departure_place"):
                            departures.append({"name":resource["departure_place"]})
                    if stops:
                        anchors.append((stops[0],True))
                        if not departures:
                            anchors.append((stops[-1],False))
                    else:
                        anchors.extend((station,True) for station in departures)
                    daily_walk = 0
                    for anchor,from_hotel in anchors:
                        if from_hotel:
                            move,_ = await self.movement(state,city,option["name"],anchor["name"],place,anchor.get("map_place"))
                        else:
                            move,_ = await self.movement(state,city,anchor["name"],option["name"],anchor.get("map_place"),place)
                        if move.get("minutes") is None:
                            unknown.append(day["date"]+"部分住宿接驳待确认")
                        else:
                            total += move["minutes"]
                        daily_walk += move.get("walking_meters") or 0
                    walking = max(walking,daily_walk)
                    limit = walking_limit(state["requirements"],day["date"])
                    exceeds_limit |= limit is not None and daily_walk > limit
            else:
                unknown.append("酒店未匹配到唯一地点，通勤无法比较")
            option.update(commute_minutes=total if place and not unknown else None,walking_meters=walking,
                comparison_unknown=list(dict.fromkeys(unknown)),constraint_issues=["住宿接驳步行已超过每日上限"] if exceeds_limit else [])
        return shortlist

    def segments(self,state):
        days = state["selected_route"]["days"]
        req = state["requirements"]
        city_starts = [day for index,day in enumerate(days) if index == 0 or day["city"] != days[index-1]["city"]]
        segments = []
        origin = req["origin"]
        for day in city_starts:
            if origin != day["city"] and not (origin == req["origin"] and req.get("self_arranged_round_trip")):
                ctx = {"origin":origin,"destination":day["city"],"date":day["date"]}
                segments.append({"id":f"transport:{origin}:{day['city']}","label":origin+" → "+day["city"],"context":ctx})
            origin = day["city"]
        if req["return_to_origin"] and origin != req["origin"] and not req.get("self_arranged_round_trip"):
            ctx = {"origin":origin,"destination":req["origin"],"date":req["end_date"]}
            segments.append({"id":f"transport:{origin}:{req['origin']}","label":origin+" → "+req["origin"],"context":ctx})
        return segments

    async def transport(self,state):
        self.emit(state,"transport","正在查询跨城市交通")
        if state.get("change",{}).get("resource_choice"):
            return {"itinerary":copy.deepcopy(state["itinerary"])}
        old = {s["id"]:s for s in state.get("old_itinerary",{}).get("transport",[])}
        result, warnings = [],list(state.get("warnings",[]))
        req = state["requirements"]
        for segment in self.segments(state):
            ctx = segment["context"]
            refresh = self.affected(state,"transport",ctx["destination"]) or self.affected(state,"transport",ctx["origin"])
            if state.get("change",{}).get("resource_ids"):
                refresh &= segment["id"] in state["change"]["resource_ids"]
            if segment["id"] in old and (segment["id"] in state["locks"] or not refresh):
                result.append(copy.deepcopy(old[segment["id"]])); continue
            options, failures, query_ids = [],[],[]
            for kind in (["train","flight"] if req["transport"] == "any" else [req["transport"]]):
                deadline = req.get("latest_return") if ctx["destination"] == req["origin"] else req.get("latest_arrival")
                for sort in (2,3,4):
                    flags = {"origin":ctx["origin"],"destination":ctx["destination"],"dep-date":ctx["date"],"sort-type":sort,
                        "journey-type":1 if req.get("direct_only",True) else None,
                        "dep-hour-start":req["earliest_departure"][:2] if req.get("earliest_departure") else None,
                        "arr-hour-end":deadline[:2] if deadline else None,"total-duration-hour":req.get("max_transport_hours"),
                        "seat-class-name":req.get("flight_cabin" if kind == "flight" else "train_seat") or None}
                    try:
                        response = await self.resource_agent.search(state,kind,ctx,flags,fresh=bool(state.get("change",{}).get("refresh_resources")))
                        if response.get("evidence_id"):
                            query_ids.append(response["evidence_id"])
                        previous_ids = {option["id"] for option in options}
                        options.extend(response["options"])
                        if response.get("message"):
                            failures.append(response["message"])
                        if not response["options"] or not ({option["id"] for option in response["options"]}-previous_ids):
                            break
                    except ToolError as exc:
                        failures.append(str(exc))
                        if getattr(exc,"evidence_id",None):
                            query_ids.append(exc.evidence_id)
                        break
            options = list({option["id"]:option for option in options}.values())
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(segment["id"],{}).get("selected") or {}
                options = [option for option in options if same_resource(chosen,option)]
                for option in options:
                    option["constraint_issues"] = transport_issues(option,ctx,req)
                selected = next((option for option in options if not option.get("constraint_issues")),None)
                reason = "保留原班次、站点与时刻，仅更新本次报价依据"
                if selected is None:
                    failures.append("原班次未在本次查询中得到核实，旧报价不会延长有效期，请调整或重新查询")
            else:
                options = await self.enrich_transport(state,ctx,options)
                selected, reason = await self.resource_agent.choose(state,options,segment["label"] + "，" + state.get("change",{}).get("explanation",""))
            if selected is None and segment["id"] in old and old[segment["id"]].get("selected") and old[segment["id"]].get("context") == ctx:
                kept = copy.deepcopy(old[segment["id"]]); kept["errors"] = failures+["本次未获得替代方案，保留原交通；原报价未重新确认"]
                kept["evidence_ids"] = list(dict.fromkeys([*kept.get("evidence_ids",[]),*query_ids]))
                result.append(kept)
            else:
                result.append({**segment,"options":options,"selected":selected,"reason":reason,"evidence_ids":query_ids,
                               "buffer_minutes":120 if selected and selected["kind"] == "flight" else 60,"errors":list(dict.fromkeys(failures)),
                               "comparison":[comparison(option,req) for option in options]})
            warnings.extend(segment["label"]+"："+failure for failure in failures)
        itinerary = copy.deepcopy(state.get("itinerary") or {}); itinerary["transport"] = result
        return {"itinerary":itinerary,"warnings":list(dict.fromkeys(warnings))}

    async def hotels(self,state):
        self.emit(state,"hotels","正在匹配住宿日期与活动区域")
        if state.get("change",{}).get("resource_choice"):
            return {"itinerary":copy.deepcopy(state["itinerary"])}
        days, req = state["selected_route"]["days"],state["requirements"]
        old = {h["id"]:h for h in state.get("old_itinerary",{}).get("hotels",[])}
        result, warnings = [],list(state.get("warnings",[]))
        for city in req["cities"]:
            dates = [d["date"] for d in days if d["city"] == city]
            if not dates:
                continue
            check_in, check_out = dates[0],(date.fromisoformat(dates[-1])+timedelta(days=1)).isoformat()
            if check_out > req["end_date"]:
                check_out = req["end_date"]
            if check_in >= check_out:
                continue
            entry_id = "hotel:" + city
            refresh = self.affected(state,"hotel",city) or self.affected(state,"transport",city)
            if state.get("change",{}).get("resource_ids"):
                refresh &= entry_id in state["change"]["resource_ids"]
            if entry_id in old and (entry_id in state["locks"] or not refresh):
                result.append(copy.deepcopy(old[entry_id])); continue
            ctx = {"city":city,"check_in":check_in,"check_out":check_out}
            nights = (date.fromisoformat(check_out)-date.fromisoformat(check_in)).days
            total_nights = max(1,(date.fromisoformat(req["end_date"])-date.fromisoformat(req["start_date"])).days)
            known_transport = sum(float(cost) for entry in state["itinerary"].get("transport",[]) if (cost:=resource_cost(entry.get("selected"),req)) is not None)
            remaining = req["budget"]-known_transport-(total_nights+1)*req["people"]*100
            cap = max(1,remaining/total_nights/req["rooms"])
            if state.get("change",{}).get("cheaper_hotel") and entry_id in old:
                prior = old[entry_id].get("selected") or {}
                if prior.get("price_basis") == "room_night" and prior.get("price"):
                    cap = min(cap,prior["price"]*0.85)
            areas = self.hotel_areas(state,city)
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(entry_id,{}).get("selected") or {}
                areas = [chosen.get("search_area") or (areas[0] if areas else None)]
            errors,options,statuses,query_ids = [],[],[],[]
            for area in areas:
                flags = {"dest-name":city,"check-in-date":check_in,"check-out-date":check_out,"hotel-bed-types":req["bed_type"],
                         "poi-name":area,"max-price":round(cap),"hotel-types":req.get("hotel_type") or None,"hotel-stars":req.get("hotel_stars") or None,
                         "sort":"price_asc" if state.get("change",{}).get("cheaper_hotel") else "distance_asc"}
                if state.get("change",{}).get("refresh_selected"):
                    flags.update({"key-words":chosen.get("name"),"max-price":None})
                try:
                    response = await self.resource_agent.search(state,"hotel",ctx,flags,fresh=bool(state.get("change",{}).get("refresh_resources")))
                    if response.get("evidence_id"):
                        query_ids.append(response["evidence_id"])
                    statuses.append(response.get("status","ok"))
                    returned = response["options"] if state.get("change",{}).get("refresh_selected") else response["options"][:3]
                    options.extend({**option,"search_area":area} for option in returned)
                    if response.get("message"):
                        errors.append(response["message"])
                except ToolError as exc:
                    if getattr(exc,"evidence_id",None):
                        query_ids.append(exc.evidence_id)
                    statuses.append("error"); errors.append(str(exc)); break
            if state.get("change",{}).get("refresh_selected"):
                chosen = old.get(entry_id,{}).get("selected") or {}
                options = [option for option in options if same_resource(chosen,option)]
            if old.get(entry_id,{}).get("selected_by_user") and not state.get("change",{}).get("refresh_selected"):
                chosen = old[entry_id].get("selected") or {}
                adopted = [option for option in options if same_resource(chosen,option)]
                enriched = {option["id"]:option for option in await self.enrich_hotels(state,city,adopted)}
                options = [enriched.get(option["id"],option) for option in options]
            prior = old.get(entry_id,{})
            preserve_adopted = bool(prior.get("selected_by_user") and prior.get("selected") and
                                    not state.get("change",{}).get("cheaper_hotel"))
            if state.get("change",{}).get("refresh_selected"):
                selected = next((option for option in options if not option.get("constraint_issues")),None)
                reason = "保留你已采用的酒店、房型与日期，仅更新本次报价依据"
                if selected is None:
                    errors.append("原酒店及房型未在本次查询中得到核实，旧报价不会延长有效期，请调整或重新查询")
            elif preserve_adopted:
                selected = next((option for option in options if same_resource(prior["selected"],option) and not option.get("constraint_issues")),None)
                reason = "保留你已采用的住宿及房型，更新本次查询信息"
                if selected is None:
                    errors.append("原住宿未取得身份、房型和日期一致且满足约束的新结果，保留旧安排供处理，不能视为本次已核实")
            else:
                selected, reason = await self.resource_agent.choose(state,options,f"{city}住宿{nights}晚，{req['rooms']}间，整段费用口径必须明确。"+state.get("change",{}).get("explanation",""))
            if selected is None and entry_id in old and old[entry_id].get("selected") and old[entry_id].get("context") == ctx:
                kept = copy.deepcopy(old[entry_id]); kept["errors"] = errors+["本次未获得替代方案，保留原住宿；原报价未重新确认"]
                rejected = next((option for option in options if same_resource(prior["selected"],option) and option.get("constraint_issues")),None)
                if rejected:
                    kept["selected"]["constraint_issues"] = list(rejected["constraint_issues"])
                old_req = state.get("old_requirements") or req
                if any(old_req.get(field) != req.get(field) for field in ("bed_type","hotel_type","hotel_stars")):
                    kept["selected"]["constraint_issues"] = list(dict.fromkeys([
                        *kept["selected"].get("constraint_issues",[]),"原住宿尚未核实满足本次修改后的床型或酒店要求，请重新采用符合条件的房型"]))
                kept["evidence_ids"] = list(dict.fromkeys([*kept.get("evidence_ids",[]),*query_ids]))
                result.append(kept)
            else:
                confirmed = bool(prior.get("selected_by_user") and selected and prior.get("selected") and same_resource(prior["selected"],selected))
                result.append({"id":entry_id,"city":city,"label":city+"住宿","context":ctx,"options":options,
                               "evidence_ids":query_ids,
                               "selected":selected,"selected_by_user":confirmed,"reason":reason,"errors":list(dict.fromkeys(errors)),"search_areas":[area for area in areas if area],
                               "query_status":"ok" if options else next((status for status in statuses if status not in {"ok","empty"}),"empty"),
                               "comparison":[comparison(option,req) for option in options],"budget_basis":"扣除已知城际费用与餐饮估算后分配；未知门票及儿童价尚未扣除"})
            warnings.extend(city+"："+error for error in errors)
        itinerary = copy.deepcopy(state["itinerary"]); itinerary["hotels"] = result
        return {"itinerary":itinerary,"warnings":list(dict.fromkeys(warnings))}

    async def tickets(self,state):
        self.emit(state,"tickets","正在整理攻略票种，仅为涉及门票的活动查询参考价")
        itinerary = copy.deepcopy(state["itinerary"])
        await self.query_ticket_prices(state,itinerary,{day["date"] for day in itinerary["days"]},missing_only=True)
        return {"itinerary":itinerary}

    async def query_ticket_prices(self,state,itinerary,rebuilt_dates,missing_only=False):
        stops = state.get("stops",{})
        eligible = set()
        targeted = state.get("change",{}).get("resource_ids") or []
        for day in itinerary["days"]:
            if day["date"] not in rebuilt_dates:
                continue
            for activity in day["activities"]:
                if activity["kind"] != "visit":
                    continue
                policy = ticket_policy(activity)
                activity["ticket_query_required"] = ticket_query_required(activity)
                if "ticket:"+activity["id"] in targeted or activity["ticket_query_required"] or activity.get("product"):
                    eligible.add((day["date"],activity["id"]))
                elif policy in {"free","not_applicable"}:
                    activity.pop("ticket_quote",None)
                elif (activity.get("ticket_quote") or {}).get("price") is None and not (activity.get("ticket_quote") or {}).get("options"):
                    activity.pop("ticket_quote",None)
        if not eligible:
            return
        skip_quotes = set()
        if missing_only:
            current_time = timestamp(now(),now()[:10])
            for day in itinerary["days"]:
                for activity in day["activities"]:
                    quote = activity.get("ticket_quote") or {}
                    if quote and not activity.get("product"):
                        quote = activity["ticket_quote"] = default_ticket_price(quote)
                    queried = timestamp(quote.get("queried_at"),day["date"])
                    has_quote = quote.get("price") is not None and not quote.get("assumed_free") or bool(quote.get("options"))
                    retry_missing = state.get("change",{}).get("refresh_resources") and not has_quote
                    if not retry_missing and "ticket:"+activity["id"] not in targeted and quote.get("visit_date") == day["date"] and quote.get("status") != "pending_confirmation" and queried and 0 <= (current_time-queried).total_seconds() < 600:
                        skip_quotes.add((day["date"],activity["id"]))
        if self.flyai.availability()["ready"]:
            ticket_cache, ticket_error = {},None
            for day in itinerary["days"]:
                if day["date"] not in rebuilt_dates:
                    continue
                for activity in day["activities"]:
                    if (day["date"],activity["id"]) not in eligible:
                        continue
                    if (day["date"],activity["id"]) in skip_quotes:
                        continue
                    targeted = state.get("change",{}).get("resource_ids") or []
                    if targeted and "ticket:"+activity["id"] not in targeted:
                        previous = next((item for old_day in state.get("old_itinerary",{}).get("days",[]) for item in old_day["activities"] if item["id"] == activity["id"]),{})
                        if previous.get("ticket_quote"):
                            activity["ticket_quote"] = copy.deepcopy(previous["ticket_quote"])
                        continue
                    if activity.get("product"):
                        activity["ticket_quote"] = {"price":None,"note":"采用的体验商品费用单独待核对，避免与酒店、交通或门票重复计费","booking_url":activity["product"].get("booking_url")}
                        continue
                    if ticket_error:
                        activity["ticket_quote"] = {"price":None,"status":"paused","note":ticket_error,"queried_at":now()}
                        self.store.query(state["trip_id"],"flyai",{"kind":"poi","city":day["city"],"name":activity["name"]},
                                         {"status":"paused","message":ticket_error,"queried_at":now()})
                        continue
                    self.emit(state,"tickets","正在查询景点门票参考价："+activity["name"])
                    place_name = (activity.get("map_place") or stops.get(activity["id"],{}).get("map_place") or {}).get("name") or activity["name"]
                    key = (day["city"],place_name,day["date"])
                    try:
                        if key not in ticket_cache:
                            ticket_cache[key] = await self.resource_agent.ticket({**state,"itinerary":itinerary},day["city"],place_name,day["date"],fresh=bool(state.get("change",{}).get("refresh_resources") or "ticket:"+activity["id"] in targeted))
                        activity["ticket_quote"] = copy.deepcopy(ticket_cache[key])
                    except ToolError as exc:
                        activity["ticket_quote"] = {"price":None,"status":"error","code":exc.code,"note":str(exc),"queried_at":now()}
                        if exc.code in {"flyai_config","flyai_failed","flyai_timeout","flyai_auth","flyai_network","flyai_rate_limit","flyai_parameters","flyai_runtime"}:
                            ticket_error = str(exc)+"；本轮剩余门票查询已暂停，可更新配置后重查"
        else:
            unavailable = self.flyai.availability().get("message") or "飞猪查询组件尚未就绪"
            for day in itinerary["days"]:
                for activity in day["activities"]:
                    if (day["date"],activity["id"]) in eligible:
                        if (day["date"],activity["id"]) in skip_quotes:
                            continue
                        targeted = state.get("change",{}).get("resource_ids") or []
                        previous = next((item for old_day in state.get("old_itinerary",{}).get("days",[]) for item in old_day["activities"] if item["id"] == activity["id"]),{})
                        if targeted and "ticket:"+activity["id"] not in targeted and previous.get("ticket_quote"):
                            activity["ticket_quote"] = copy.deepcopy(previous["ticket_quote"])
                            continue
                        if activity.get("product"):
                            activity["ticket_quote"] = {"price":None,"note":"采用的体验商品费用单独待核对","booking_url":activity["product"].get("booking_url")}
                            continue
                        activity["ticket_quote"] = {"price":None,"status":"not_configured","note":"门票未查询："+unavailable,"queried_at":now()}
                        self.store.query(state["trip_id"],"flyai",{"kind":"poi","city":day["city"],"name":activity["name"]},
                                         {"status":"not_configured","message":unavailable,"queried_at":now()})
        for day in itinerary["days"]:
            if day["date"] in rebuilt_dates:
                for activity in day["activities"]:
                    if activity.get("ticket_quote"):
                        if not activity.get("product"):
                            activity["ticket_quote"] = default_ticket_price(activity["ticket_quote"])
                        activity["ticket_quote"]["visit_date"] = day["date"]
